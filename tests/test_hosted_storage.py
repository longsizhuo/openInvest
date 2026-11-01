"""Real filesystem failures must not lose encrypted archive objects or cross recovery boundaries."""
from dataclasses import replace
import asyncio
import os
import stat
import time

import pytest
from openinvest.hosted.storage import FileArchive

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Hosted filesystem deployment uses Linux")


def test_capacity_failure_keeps_previous_object_and_no_partial_upload(tmp_path):
    archive = FileArchive(tmp_path / "archive", max_bytes=6)
    archive.put("research/a/one.aesgcm", b"first")
    with pytest.raises(OSError):
        archive.put("research/a/one.aesgcm", b"too-large")
    with pytest.raises(OSError):
        archive.put("research/b/two.aesgcm", b"next")
    assert archive.get("research/a/one.aesgcm") == b"first"
    assert not list(archive.root.rglob(".upload-*"))
    assert stat.S_IMODE(archive.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((archive.root / "research/a/one.aesgcm").stat().st_mode) == 0o600
    archive.put("research/a/one.aesgcm", b"new")
    assert archive.status() == {"kind": "filesystem", "bytes": 3, "max_bytes": 6, "off_host": False}


@pytest.mark.parametrize("key", ["/backups/x", "backups/../outside", "backups//x", "backups/./x", "backups\\x", "elsewhere/x", "backups", None])
def test_keys_cannot_escape_private_archive(tmp_path, key):
    archive = FileArchive(tmp_path / "archive")
    with pytest.raises(ValueError):
        archive.put(key, b"content")


def test_symlink_cannot_read_overwrite_or_delete_another_file(tmp_path):
    archive = FileArchive(tmp_path / "archive")
    victim = tmp_path / "victim"
    victim.write_bytes(b"unrelated")
    (archive.root / "research").symlink_to(tmp_path, target_is_directory=True)
    for action in (lambda: archive.put("research/victim", b"changed"),
                   lambda: archive.get("research/victim"), lambda: archive.delete("research/victim")):
        with pytest.raises(ValueError):
            action()
    assert victim.read_bytes() == b"unrelated"


def test_retention_prunes_only_expired_backup_objects(tmp_path):
    archive = FileArchive(tmp_path / "archive")
    old, recent = str(int(time.time()) - 31 * 86400), str(int(time.time()))
    archive.put(f"backups/manifests/{old}.aesgcm", b"old")
    archive.put(f"backups/account/{old}.aesgcm", b"old")
    archive.put(f"backups/account/{recent}.aesgcm", b"recent")
    archive.put(f"research/participant/{old}.aesgcm", b"permanent")
    archive.prune_backups(30)
    with pytest.raises(FileNotFoundError):
        archive.get(f"backups/account/{old}.aesgcm")
    assert archive.get(f"backups/account/{recent}.aesgcm") == b"recent"
    assert archive.get(f"research/participant/{old}.aesgcm") == b"permanent"


def test_encrypted_database_backup_can_restore_without_live_credentials(tmp_path):
    pytest.importorskip("authlib")
    from openinvest.hosted.backup import backup_all, restore_database
    from openinvest.hosted.config import Settings
    from openinvest.hosted.store import Store
    from openinvest.hosted.research import Research

    settings = Settings(tmp_path / "live", "https://test.invalid", "https://identity.invalid/", "mcp",
                        "client", "secret", "s" * 40, b"k" * 32)
    store = Store(settings.root, settings.encryption_key)
    research = Research(settings, store)
    account = store.identity("synthetic-private-identity")
    store.preferences(account["id"], True, settings.policy_version)
    token, _ = store.credential(account["id"])
    class Pool:
        async def internal(self, *_):
            raise AssertionError("Uninitialized account has no worker to export")
    archive = FileArchive(tmp_path / "archive")
    result = asyncio.run(backup_all(settings, store, research, Pool(), archive))
    assert result["accounts"] == 0
    recovered = replace(settings, root=tmp_path / "recovered")
    for name in ("control", "research", "receipts"):
        blob = archive.get(f"backups/{name}/{result['backup']}.aesgcm")
        assert b"synthetic-private-identity" not in blob and token.encode() not in blob
        restore_database(recovered, archive, name, result["backup"])
    restored = Store(recovered.root, recovered.encryption_key)
    assert restored.authenticate(token, "pat") is None
    assert restored.account(account["id"])["research"] == 0
    assert restored.account(account["id"])["accepted"] is None


def test_s3_retention_paginates_and_preserves_research_and_recent_backups(tmp_path):
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    from openinvest.hosted.config import Settings
    from openinvest.hosted.storage import S3Archive
    client = boto3.client("s3", region_name="auto", aws_access_key_id="synthetic-access",
                          aws_secret_access_key="synthetic-secret", endpoint_url="https://example.r2.cloudflarestorage.com")
    settings = Settings(tmp_path, "https://test.invalid", "https://id.invalid/", "mcp", "client", "secret",
                        "s" * 40, b"k" * 32, s3_bucket="synthetic", s3_endpoint="https://example.r2.cloudflarestorage.com")
    archive = S3Archive(settings, client=client)
    old = str(int(time.time()) - 31 * 86400)
    recent = str(int(time.time()))
    with Stubber(client) as stub:
        stub.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "next",
            "Contents": [{"Key": f"backups/account/{old}.aesgcm"}, {"Key": f"backups/account/{recent}.aesgcm"}]},
            {"Bucket": "synthetic", "Prefix": "backups/"})
        stub.add_response("delete_object", {}, {"Bucket": "synthetic", "Key": f"backups/account/{old}.aesgcm"})
        stub.add_response("list_objects_v2", {"IsTruncated": False,
            "Contents": [{"Key": f"research/participant/{old}.aesgcm"}, {"Key": f"backups/account/{old}.txt"},
                         {"Key": f"backups/manifests/{old}.aesgcm"}]},
            {"Bucket": "synthetic", "Prefix": "backups/", "ContinuationToken": "next"})
        stub.add_response("delete_object", {}, {"Bucket": "synthetic", "Key": f"backups/manifests/{old}.aesgcm"})
        archive.prune_backups(30)
        stub.assert_no_pending_responses()
    assert archive.status() == {"kind": "r2", "off_host": True}
