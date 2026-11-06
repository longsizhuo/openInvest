"""Credential archives stay private even when the caller uses umask 022.

Runs the real launcher and zip with a fake backend, never real user data/network.
Run through the repository's isolated pytest runner.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

RUN_SH = Path(__file__).resolve().parents[1] / "plugin/skills/invest-backup/scripts/run.sh"


@unittest.skipUnless(shutil.which("bash") and shutil.which("zip"), "requires bash and zip")
class BackupPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".env").write_text("FAKE_TEST_TOKEN=fixture-only\n")
        (self.home / "memory").mkdir()
        (self.home / "memory/portfolio.md").write_text("fixture portfolio\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "uv"
        fake.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys, zipfile
code = sys.argv[-1]
home = pathlib.Path(os.environ["INVEST_HOME"])
if "manual_backup" in code:
    if os.environ.get("FAKE_MODERN") != "1":
        sys.exit(42)
    dest = pathlib.Path(os.environ["INVEST_BACKUP_OUTPUT_DIR"]) / "modern.zip"
    with zipfile.ZipFile(dest, "w") as archive:
        archive.write(home / ".env", ".env")
    print(json.dumps({"status": "ok", "path": str(dest)}))
elif "checkpoint_dbs" in code:
    sys.exit(1)
elif "print(INVEST_ROOT)" in code:
    print(home)
else:
    raise SystemExit("unexpected backend invocation")
''')
        fake.chmod(0o755)
        date = self.bin / "date"
        date.write_text("#!/bin/sh\nprintf '20000101-000000\\n'\n")
        date.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "INVEST_HOME": str(self.home)}

    def run_backup(self, *args, modern=False):
        return subprocess.run(["bash", str(RUN_SH), "backup", *map(str, args)],
                              cwd=self.root, env={**self.env, "FAKE_MODERN": str(int(modern))},
                              capture_output=True, text=True, umask=0o022, timeout=20)

    def assert_private_archive(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(json.loads(result.stdout)["path"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read(".env"), b"FAKE_TEST_TOKEN=fixture-only\n")
        return path

    def test_legacy_backup_archive_and_new_directory_are_private(self):
        path = self.assert_private_archive(self.run_backup())
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertFalse(list(path.parent.glob(".openinvest-backup.*")))

    def test_modern_backend_inherits_private_creation_mask(self):
        path = self.assert_private_archive(self.run_backup(modern=True))
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_existing_backup_is_not_overwritten_or_updated(self):
        path = self.assert_private_archive(self.run_backup())
        before = path.read_bytes()
        (self.home / ".env").write_text("changed fake fixture")
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob(".openinvest-backup.*")))

    def test_relative_custom_directory_preserves_existing_permissions(self):
        output = self.root / "shared-parent"
        output.mkdir(mode=0o755)
        path = self.assert_private_archive(self.run_backup("shared-parent"))
        self.assertEqual(path.parent, output)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)

