"""Private S3/R2 and filesystem archives; research and backup payloads are already encrypted."""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
from openinvest.utils.oslock import flock, LOCK_EX


class FileArchive:
    kind = "filesystem"

    def __init__(self, root, max_bytes=2 * 1024**3):
        root = Path(root)
        if root.is_symlink() or not root.is_absolute() or max_bytes < 1:
            raise ValueError("Use an absolute private archive directory and positive capacity")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        self.root.chmod(0o700)
        self.max_bytes = max_bytes

    def _path(self, key):
        if not isinstance(key, str):
            raise ValueError("Invalid archive key")
        parsed = PurePosixPath(key)
        if (not isinstance(key, str) or not key or parsed.is_absolute() or
                parsed.as_posix() != key or "\\" in key or
                any(part in {".", ".."} for part in parsed.parts) or
                len(parsed.parts) < 2 or parsed.parts[0] not in {"research", "backups"}):
            raise ValueError("Invalid archive key")
        target = self.root
        for part in parsed.parts:
            target /= part
            if target.is_symlink():
                raise ValueError("Archive symlinks are not allowed")
        return target

    @contextmanager
    def _lock(self):
        descriptor = os.open(self.root/".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            flock(descriptor, LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _objects(self):
        for parent, directories, files in os.walk(self.root, followlinks=False):
            for name in directories + files:
                path = Path(parent)/name
                if path.is_symlink():
                    raise ValueError("Archive symlinks are not allowed")
            for name in files:
                if not name.startswith("."):
                    yield Path(parent)/name

    def put(self, key, body):
        with self._lock():
            target = self._path(key)
            total = sum(path.stat().st_size for path in self._objects())
            replaced = target.stat().st_size if target.exists() else 0
            if total - replaced + len(body) > self.max_bytes:
                raise OSError("Archive capacity reached")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def get(self, key):
        with self._lock():
            descriptor = os.open(self._path(key), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                return source.read()

    def delete(self, key):
        with self._lock():
            self._path(key).unlink(missing_ok=True)

    def prune_backups(self, days):
        cutoff = time.time() - days * 86400
        with self._lock():
            for path in list(self._objects()):
                if path.relative_to(self.root).parts[0] != "backups":
                    continue
                stamp, suffix = path.name.rsplit(".", 1) if "." in path.name else ("", "")
                if suffix == "aesgcm" and stamp.isdigit() and int(stamp) < cutoff:
                    path.unlink()

    def status(self):
        with self._lock():
            return {"kind": self.kind, "bytes": sum(p.stat().st_size for p in self._objects()),
                    "max_bytes": self.max_bytes, "off_host": False}


class S3Archive:
    def __init__(self, settings, client=None):
        self.bucket = settings.s3_bucket
        self.kind = "r2" if settings.s3_endpoint.endswith(".r2.cloudflarestorage.com") else "s3"
        if client is None:
            import boto3
            from botocore.config import Config
            credentials = {} if settings.s3_profile else {
                "aws_access_key_id": os.environ["HOSTED_S3_ACCESS_KEY_ID"],
                "aws_secret_access_key": os.environ["HOSTED_S3_SECRET_ACCESS_KEY"]}
            session = boto3.Session(profile_name=settings.s3_profile) if settings.s3_profile else boto3.Session()
            client = session.client("s3", endpoint_url=settings.s3_endpoint,
                region_name=settings.s3_region,
                **credentials,
                config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts": 2},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required"))
        self.client = client

    def put(self, key, body):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/octet-stream")

    def get(self, key):
        stream = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return stream.read()
        finally:
            stream.close()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def prune_backups(self, days):
        cutoff = time.time() - days * 86400
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="backups/")
        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                stamp, suffix = key.rsplit("/", 1)[-1].rsplit(".", 1) if "." in key.rsplit("/", 1)[-1] else ("", "")
                if key.startswith("backups/") and suffix == "aesgcm" and stamp.isdigit() and int(stamp) < cutoff:
                    self.delete(key)

    def status(self):
        return {"kind": self.kind, "off_host": True}


def configured_archive(settings):
    if sum(bool(value) for value in (settings.archive_dir, settings.cos_bucket, settings.s3_bucket)) > 1:
        raise ValueError("Choose one archive backend")
    if settings.archive_dir:
        return FileArchive(settings.archive_dir, settings.archive_bytes)
    if settings.s3_bucket:
        return S3Archive(settings)
    if settings.cos_bucket:
        from .research import COSArchive
        return COSArchive(settings)
    return None
