#!/usr/bin/python3
"""Shared live status, streamed rsync progress, and log discovery in /tmp."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tms_status as status
from tms_lock import engine_lock, FD_ENV

ROOT = Path(__file__).resolve().parent


class StatusTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tms-status-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, TMS_STATE_DIR=str(self.directory))
        environment.start()
        self.addCleanup(environment.stop)

    def test_read_only_before_first_run_and_stale_state(self):
        missing = self.directory / "missing"
        self.assertEqual(status.status_lines(status.snapshot(missing)), ["Backup: Not run yet"])
        self.assertFalse(missing.exists())
        with engine_lock() as fd:
            self.assertEqual(status.snapshot()["phase"], "START")
            status.update(lock_fd=fd, phase="COPY", source="/source", estimated_bytes=100)
            self.assertTrue(status.snapshot()["active"])
        self.assertEqual(status.snapshot()["status"], "INTERRUPTED")
        self.assertFalse(status.snapshot()["active"])

    def test_streams_cr_progress_verbatim_with_matching_estimate(self):
        stats = self.directory / "copy.stats"
        raw = (b"file name\n\r          512  50%  1.00MB/s  0:00:01 (xfr#1, to-chk=1/2)\r"
               b"        1,024 100%  1.00MB/s  0:00:00 (xfr#2, to-chk=0/2)\n"
               b"Number of regular files transferred: 2\n")
        with engine_lock() as fd:
            status.update(lock_fd=fd, phase="COPY", source="/source", estimated_bytes=1024, log="/current.log")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tms_status.py"), "copy", str(stats)],
                                    input=raw, capture_output=True, pass_fds=(fd,),
                                    env=dict(os.environ, **{FD_ENV: str(fd)}), check=True)
            state = status.snapshot()
            self.assertEqual(state["percent"], 100)
            self.assertEqual(state["processed_bytes"], 1024)
            self.assertEqual(state["estimated_bytes"], 1024)
            self.assertIn("Copy progress: 100%", status.status_lines(state))
            self.assertIn("Estimated amount: 1.00 KiB (current source)", status.status_lines(state))
            self.assertEqual(result.stdout, raw)
            self.assertEqual(stats.read_bytes(), raw)
            # A new source must not inherit the previous source's amount or percent.
            subprocess.run([sys.executable, "-B", str(ROOT / "tms_status.py"), "phase", "INIT", "/next"],
                           pass_fds=(fd,), env=dict(os.environ, **{FD_ENV: str(fd)}), check=True)
            self.assertIsNone(status.snapshot()["percent"])
            self.assertIsNone(status.snapshot()["estimated_bytes"])

    def test_copy_estimate_from_rsync_when_manifest_cannot_size_links(self):
        with engine_lock() as fd:
            status.update(lock_fd=fd, phase="COPY", estimated_bytes=None)
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tms_status.py"), "copy", str(self.directory / "stats")],
                                    input=b"  512 50% 1MB/s 0:00:01 (xfr#1, to-chk=1/2)\r",
                                    capture_output=True, pass_fds=(fd,),
                                    env=dict(os.environ, **{FD_ENV: str(fd)}), check=True)
            self.assertEqual(status.snapshot()["estimated_bytes"], 1024)
            self.assertEqual(status.snapshot()["percent"], 50)

    def test_unlocked_updates_and_stale_owner_are_ignored(self):
        with engine_lock() as fd:
            status.update(lock_fd=fd, phase="COPY")
        previous = (self.directory / "progress.json").read_bytes()
        status.update(phase="INIT")
        self.assertEqual((self.directory / "progress.json").read_bytes(), previous)
        owner = status.read_json(self.directory / "engine-owner.json")
        owner["pid"] += 1
        (self.directory / "engine-owner.json").write_text(json.dumps(owner))
        state = status.snapshot(running=True)
        self.assertEqual(state["phase"], "START")
        self.assertNotIn("percent", state)

    def test_existing_service_run_recovers_phase_and_log(self):
        log = self.directory / "active.log"
        log.write_text("Creating hard links...\nApplying exclusion rules and discovering files...\nCopying incremental backup...\n  4 50%\n")
        (self.directory / "engine-owner.json").write_text(json.dumps({"kind": "service", "pid": 7}))
        (self.directory / "status.json").write_text(json.dumps({"pid": 7, "status": "RUNNING", "log": str(log)}))
        state = status.snapshot(running=True)
        self.assertEqual(state["phase"], "COPY")
        self.assertEqual(state["log"], str(log))
        self.assertNotIn("percent", state)


if __name__ == "__main__":
    unittest.main()
