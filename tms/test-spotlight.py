#!/usr/bin/python3
"""Spotlight storage preparation in tms.sh, using only temporary archives."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tms_lock import engine_lock, FD_ENV

ROOT = Path(__file__).resolve().parent


class SpotlightTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tms-spotlight-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.volume = self.base / "destination with spaces"
        self.state = self.base / "state"
        self.legacy = self.volume / "backup"
        self.target = self.volume / "backup.noindex"

    def prepare(self, fail_link=False):
        with engine_lock(self.state) as fd:
            command = 'source "$1"; '
            if fail_link:
                command += 'ln() { return 1; }; '
            command += '_tms_prepare_backup_root'
            return subprocess.run(["/bin/bash", "-c", command, "tms-test", str(ROOT / "tms.sh")],
                                  env=dict(os.environ, backup_volume=str(self.volume),
                                           TMS_STATE_DIR=str(self.state), **{FD_ENV: str(fd)}),
                                  pass_fds=(fd,), text=True, capture_output=True)

    def test_fresh_root_and_idempotent_reuse(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.legacy.is_symlink())
        self.assertEqual(self.legacy.resolve(), self.target)
        self.assertTrue((self.target / ".metadata_never_index").is_file())
        before = self.target.stat().st_ino
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.target.stat().st_ino, before)

    def test_migration_keeps_inodes_links_and_old_paths(self):
        label = self.legacy / "Documents"
        snapshot = label / "2026-11-15T120000.full"
        snapshot.mkdir(parents=True)
        file = snapshot / "file"
        file.write_text("retained backup")
        (label / "latest").symlink_to(snapshot)
        inode = file.stat().st_ino
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(file.read_text(), "retained backup")
        self.assertEqual(file.stat().st_ino, inode)
        self.assertEqual((label / "latest/file").read_text(), "retained backup")

    def test_conflicting_archives_are_not_merged(self):
        self.legacy.mkdir(parents=True)
        self.target.mkdir()
        (self.legacy / "old").write_text("old")
        (self.target / "other").write_text("other")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertFalse(self.legacy.is_symlink())
        self.assertEqual((self.legacy / "old").read_text(), "old")
        self.assertEqual((self.target / "other").read_text(), "other")

    def test_failed_compatibility_link_rolls_back_rename(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "old").write_text("preserve")
        self.assertNotEqual(self.prepare(fail_link=True).returncode, 0)
        self.assertFalse(self.legacy.is_symlink())
        self.assertEqual((self.legacy / "old").read_text(), "preserve")
        self.assertFalse(self.target.exists())

    def test_unexpected_symlinks_are_not_replaced(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.volume.mkdir()
        self.legacy.symlink_to(elsewhere)
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertEqual(self.legacy.resolve(), elsewhere)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
