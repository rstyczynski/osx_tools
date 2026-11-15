#!/usr/bin/python3
"""Consolidation integration checks; all backups and locks stay in temporary fixtures."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class ConsolidationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tms-consolidate-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.parent = self.base / "parent with spaces"
        self.child = self.parent / "child"
        self.child.mkdir(parents=True)
        (self.child / "keep.txt").write_text("important content")
        self.volume = self.base / "destination"
        self.central = self.base / "excludes"
        self.central.write_text("")
        self.env = dict(os.environ, backup_volume=str(self.volume),
                        TMS_EXCLUDES_FILE=str(self.central), TMS_STATE_DIR=str(self.base / "state"),
                        PATH="/opt/homebrew/bin:" + os.environ["PATH"])
        result = self.tms(str(self.child))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.child_label = self.volume / "backup" / self.child.name
        self.original_snapshot = (self.child_label / "latest").resolve()

    def tms(self, *args):
        return subprocess.run(["/bin/bash", "-c", 'source "$1"; shift; tms "$@"',
                               "tms", str(ROOT / "tms.sh"), *args], env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def consolidate(self, *args):
        return self.tms("--consolidate", str(self.parent), *args)

    def assert_active_history(self):
        self.assertFalse(os.path.lexists(self.child_label / ".retired"))
        self.assertEqual((self.child_label / "latest").resolve(), self.original_snapshot)
        self.assertTrue(self.original_snapshot.is_dir())

    def test_preview_then_success_and_restore_retired_history(self):
        before = set(self.volume.rglob("*"))
        result = self.consolidate("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Preview passed", result.stdout)
        self.assertEqual(set(self.volume.rglob("*")), before)
        self.assert_active_history()
        result = self.consolidate()
        self.assertEqual(result.returncode, 0, result.stdout)
        marker = json.loads((self.child_label / ".retired").read_text())
        self.assertEqual(marker["replacement"], str(self.parent))
        latest = (self.volume / "backup" / self.parent.name / "latest").resolve()
        self.assertEqual(marker["replacement_snapshot"], latest.name)
        self.assertTrue(latest.name.endswith(".full"))
        self.assertEqual((latest / str(self.child).lstrip("/") / "keep.txt").read_text(), "important content")
        self.assertEqual((self.child_label / "latest").resolve(), self.original_snapshot)
        self.assertNotEqual(self.tms(str(self.child)).returncode, 0)
        result = self.tms("--all")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Total: 2 | OK: 1 | FAILED: 0 | SKIPPED: 1", result.stdout)
        restore = self.base / "restored"
        result = subprocess.run(["/bin/bash", "-c", 'source "$1"; tms_restore "$2" "$3" "$4"',
                                 "tms", str(ROOT / "tms.sh"), self.child.name,
                                 self.original_snapshot.name, str(restore)], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((restore / self.original_snapshot.name / str(self.child).lstrip("/") / "keep.txt").read_text(), "important content")

    def test_exclusion_gap_blocks_preview_and_execution(self):
        (self.parent / ".tmsignore").write_text("child/\n")
        for args in (("--dry-run",), ()):
            result = self.consolidate(*args)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("Coverage gap", result.stdout)
            self.assert_active_history()
            self.assertFalse((self.volume / "backup" / self.parent.name).exists())

    def test_copy_failure_does_not_retire_children(self):
        commands = self.base / "commands"
        commands.mkdir()
        rsync = commands / "rsync"
        rsync.write_text("#!/bin/bash\nexit 11\n")
        rsync.chmod(0o700)
        self.env["PATH"] = str(commands) + ":" + self.env["PATH"]
        result = self.consolidate()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Parent backup failed", result.stdout)
        self.assert_active_history()
        self.env["PATH"] = self.env["PATH"].split(":", 1)[1]
        result = self.tms("--all")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Total: 2 | OK: 1 | FAILED: 0 | SKIPPED: 1", result.stdout)

    def test_scheduled_lock_blocks_execution(self):
        state = Path(self.env["TMS_STATE_DIR"])
        state.mkdir()
        with (state / "run.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.consolidate()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Another TMS run is active", result.stdout)
        self.assert_active_history()

    def test_successful_transfer_with_missing_entries_does_not_retire(self):
        commands = self.base / "commands"
        commands.mkdir()
        rsync = commands / "rsync"
        rsync.write_text("#!/bin/bash\nexit 0\n")
        rsync.chmod(0o700)
        self.env["PATH"] = str(commands) + ":" + self.env["PATH"]
        result = self.consolidate()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("absent from parent snapshot", result.stdout)
        self.assert_active_history()
        self.env["PATH"] = self.env["PATH"].split(":", 1)[1]
        result = self.tms("--all")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Total: 2 | OK: 1 | FAILED: 0 | SKIPPED: 1", result.stdout)

    def test_ancestor_and_label_conflict_rejected(self):
        nested = self.child / "nested"
        nested.mkdir()
        result = self.tms("--consolidate", str(nested), "--dry-run")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("already covered", result.stdout)
        label = self.volume / "backup" / self.parent.name
        label.mkdir()
        (label / ".source").write_text(str(self.base / "other" / self.parent.name) + "\n")
        result = self.consolidate()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("belongs to another source", result.stdout)
        self.assert_active_history()

    def test_retry_reuses_snapshot_and_applies_updated_exclusions(self):
        transient = self.parent / "exclude-later.tmp"
        transient.write_text("temporary content")
        actual_rsync = shutil.which("rsync", path=self.env["PATH"])
        commands = self.base / "commands"
        commands.mkdir()
        wrapper = commands / "rsync"
        wrapper.write_text('#!/bin/bash\n' + shlex.quote(actual_rsync) + ' "$@" || exit $?\nprintf "interrupted after copying\\n" >&2\nexit 11\n')
        wrapper.chmod(0o700)
        self.env["PATH"] = str(commands) + ":" + self.env["PATH"]
        result = self.consolidate()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        label = self.volume / "backup" / self.parent.name
        pending = json.loads((label / ".consolidating").read_text())
        snapshot = label / pending["snapshot"]
        copied = snapshot / str(self.child).lstrip("/") / "keep.txt"
        previous_inode = copied.stat().st_ino
        before = sorted(path.name for path in label.iterdir() if path.is_dir())
        self.central.write_text("exclude-later.tmp\n")
        (self.child / "new.txt").write_text("created after interruption")
        self.env["PATH"] = self.env["PATH"].split(":", 1)[1]
        result = self.tms(str(self.parent))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Resuming pending consolidation", result.stdout)
        self.assertIn("Resuming existing snapshot", result.stdout)
        self.assertNotIn("Copying full backup", result.stdout)
        self.assertEqual((label / "latest").resolve(), snapshot)
        self.assertEqual(copied.stat().st_ino, previous_inode)
        self.assertEqual(sorted(path.name for path in label.iterdir() if path.is_dir() and not path.is_symlink()), before)
        self.assertFalse((snapshot / str(transient).lstrip("/")).exists())
        self.assertTrue((snapshot / str(self.child).lstrip("/") / "new.txt").is_file())
        self.assertTrue((self.child_label / ".retired").is_file())

    def test_legacy_pending_selects_most_copied_attempt(self):
        label = self.volume / "backup" / self.parent.name
        label.mkdir()
        (label / ".consolidating").write_text(json.dumps({"source": str(self.parent)}))
        for name, size in (("2026-01-01T120000.full", 30000), ("2026-01-01T130000.full", 1000)):
            snapshot = label / name
            snapshot.mkdir()
            Path(str(snapshot) + ".start").touch()
            Path(str(snapshot) + ".rules.json").write_text(json.dumps({"source": str(self.parent)}))
            Path(str(snapshot) + ".stats").write_text("Total transferred file size: %s bytes\n" % size)
        result = self.consolidate("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Resume existing snapshot: " + str(label / "2026-01-01T120000.full"), result.stdout)

    def test_partial_copy_is_done_and_next_backup_is_incremental(self):
        commands = self.base / "commands"
        commands.mkdir()
        wrapper = commands / "rsync"
        wrapper.write_text('#!/bin/bash\nprintf "could not copy keep.txt\\n" >&2\nexit 23\n')
        wrapper.chmod(0o700)
        self.env["PATH"] = str(commands) + ":" + self.env["PATH"]
        result = self.consolidate()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        self.assertIn("WARNING: backup done", result.stdout)
        self.assertIn("could not copy keep.txt", result.stdout)
        self.assertEqual((self.child_label / "latest").resolve(), self.original_snapshot)
        self.assertTrue(self.original_snapshot.is_dir())
        label = self.volume / "backup" / self.parent.name
        self.assertFalse((label / ".consolidating").exists())
        retirement = json.loads((self.child_label / ".retired").read_text())
        self.assertEqual(retirement["replacement_status"], "PARTIAL")
        self.assertFalse(retirement["coverage_verified"])
        self.assertTrue(Path(str((label / "latest").resolve()) + ".partial").is_file())
        # A partial result is completed: the next plain backup is incremental.
        snapshot = (label / "latest").resolve()
        actual_rsync = shutil.which("rsync", path=self.env["PATH"].split(":", 1)[1])
        wrapper.write_text('#!/bin/bash\n' + shlex.quote(actual_rsync) + ' "$@" || exit $?\nprintf "socket still could not copy\\n" >&2\nexit 23\n')
        result = self.tms(str(self.parent))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Copying incremental backup", result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        self.assertIn("socket still could not copy", result.stdout)
        self.assertNotIn("Copying full backup", result.stdout)
        latest = (label / "latest").resolve()
        self.assertNotEqual(latest, snapshot)
        self.assertTrue((latest / str(self.child).lstrip("/") / "keep.txt").is_file())
        self.assertTrue((self.child_label / ".retired").is_file())
        self.assertFalse((label / ".consolidating").exists())
        self.assertEqual(len([p for p in label.iterdir() if p.is_dir() and not p.is_symlink()]), 2)

    def test_plain_backup_recovers_legacy_partial_pending_state(self):
        label = self.volume / "backup" / self.parent.name
        snapshot = label / "2026-01-01T120000.full"
        snapshot.mkdir(parents=True)
        (label / ".source").write_text(str(self.parent) + "\n")
        (label / ".consolidating").write_text(json.dumps({"source": str(self.parent), "snapshot": snapshot.name}))
        (label / "latest").symlink_to(snapshot)
        Path(str(snapshot) + ".start").touch()
        Path(str(snapshot) + ".partial").write_text("rsync_exit=23\n")
        commands = self.base / "commands"
        commands.mkdir()
        actual_rsync = shutil.which("rsync", path=self.env["PATH"])
        wrapper = commands / "rsync"
        wrapper.write_text('#!/bin/bash\n' + shlex.quote(actual_rsync) + ' "$@" || exit $?\nprintf "socket warning retained\\n" >&2\nexit 23\n')
        wrapper.chmod(0o700)
        self.env["PATH"] = str(commands) + ":" + self.env["PATH"]
        result = self.tms(str(self.parent))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Resuming pending consolidation", result.stdout)
        self.assertIn("socket warning retained", result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        self.assertNotIn("Copying full backup", result.stdout)
        self.assertEqual((label / "latest").resolve(), snapshot)
        self.assertFalse((label / ".consolidating").exists())
        self.assertTrue((self.child_label / ".retired").exists())


if __name__ == "__main__":
    unittest.main()
