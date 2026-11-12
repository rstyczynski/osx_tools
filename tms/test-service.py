#!/usr/bin/python3
"""Temporary-fixture tests; email and cron are never contacted."""
import contextlib
import base64
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("service", Path(__file__).with_name("tms-service.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tms-service-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        (self.source / "file.txt").write_text("test data\n")
        self.config = {"backup_volume": str(self.root / "destination"), "state_dir": self.root / "state", "log_dir": self.root / "logs"}
        self.config_patch = patch.object(service, "configuration", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        schedule_patch = patch.object(service, "scheduler_description", return_value="Scheduler: disabled; launchd unloaded; next none")
        schedule_patch.start()
        self.addCleanup(schedule_patch.stop)

    def invoke(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return service.run_backup(arguments)

    def test_backup_summary_inventory_menu_and_lock(self):
        with patch.object(service, "send_email") as email:
            self.assertEqual(self.invoke(["--source", str(self.source)]), 0)
            email.assert_called_once()
        summary = (self.config["state_dir"] / "last-summary.txt").read_text()
        self.assertIn("FINAL BACKUP REPORT", summary)
        self.assertIn("source with spaces", summary)
        self.assertIn("| OK", summary)
        self.assertIn("Scheduler: disabled; launchd unloaded; next none", summary)
        time.sleep(1)
        with patch.object(service, "send_email"):
            self.assertEqual(self.invoke(["--all"]), 0)
        self.assertEqual(json.loads((self.config["state_dir"] / "status.json").read_text())["status"], "OK")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.menu()
        self.assertIn("Back up all now", out.getvalue())
        self.assertIn("--Back up now", out.getvalue())
        # Click handlers use cached paths, without SwiftBar reading protected folders.
        with patch.object(service, "sources", side_effect=PermissionError("protected")):
            with patch.object(service.subprocess, "Popen") as spawn:
                service.start(base64.urlsafe_b64encode(self.source.name.encode()).decode())
                self.assertEqual(spawn.call_args[0][0][-2:], ["--source", str(self.source)])
        before = (self.config["state_dir"] / "status.json").read_text()
        with (self.config["state_dir"] / "run.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(service, "send_email") as email:
                self.assertEqual(self.invoke(["--all"]), 0)
                email.assert_not_called()
        self.assertEqual(before, (self.config["state_dir"] / "status.json").read_text())

    def test_email_failure_visible(self):
        with patch.object(service, "send_email", side_effect=RuntimeError("test mail error")):
            self.assertEqual(self.invoke(["--test-email"]), 1)
        state = json.loads((self.config["state_dir"] / "status.json").read_text())
        self.assertIn("FAILED: test mail error", state["email"])


if __name__ == "__main__":
    unittest.main()
