#!/usr/bin/python3
"""Exercise setup state transitions without modifying real launchd or SwiftBar."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import tms_manage as manage


class ManagementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tms-manage-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.repository = self.home / "repository"
        self.repository.mkdir()
        (self.repository / "tms.1m.sh").write_text("#!/bin/bash\n")
        (self.repository / "excludes.default").write_text((manage.ROOT / "excludes.default").read_text())
        self.loaded, self.disabled = False, False
        self.calls = []
        self.helper = self.home / "Applications/TMS Backup.app/Contents/MacOS/TMSBackup"
        self.helper.parent.mkdir(parents=True)
        self.helper.touch()
        for item in (patch.object(manage, "HOME", self.home),
                     patch.object(manage, "ROOT", self.repository),
                     patch.object(manage, "command", side_effect=self.command),
                     patch.object(manage, "build_helper", return_value=self.helper)):
            item.start()
            self.addCleanup(item.stop)

    def command(self, args, check=False):
        self.calls.append(args)
        code, out = 0, ""
        if args[0] == "/bin/launchctl":
            operation = args[1]
            if operation == "print":
                code = 0 if self.loaded else 113
                out = "state = waiting\n" if self.loaded else ""
            elif operation == "print-disabled":
                out = '"local.tms.backup" => ' + str(self.disabled).lower()
            elif operation == "enable":
                self.disabled = False
            elif operation == "disable":
                self.disabled = True
            elif operation == "bootstrap":
                job = plistlib.loads(Path(args[-1]).read_bytes())
                if job["Label"].startswith("local.tms.access-check."):
                    Path(job["EnvironmentVariables"]["TMS_ACCESS_RESULT"]).write_text(json.dumps({"ok": True, "paths": []}))
                else:
                    self.loaded = True
            elif operation == "bootout" and args[-1].endswith("/local.tms.backup"):
                self.loaded = False
        elif args[0] == "/usr/bin/defaults":
            out = str(self.home / "plugins")
        return subprocess.CompletedProcess(args, code, out, "")

    def quiet(self, callback, *arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            return callback(*arguments)

    def test_scheduler_on_off_on_and_status(self):
        self.quiet(manage.scheduler, "status")
        self.assertFalse(manage.agent_path().exists())
        self.assertTrue(all(call[1] in ("print", "print-disabled") for call in self.calls))
        for _ in range(2):
            self.quiet(manage.scheduler, "on")
            state = manage.scheduler_info()
            self.assertTrue(state["enabled"] and state["loaded"])
            self.assertEqual(state["hours"], [0, 4, 8, 12, 16, 20])
            self.assertNotEqual(state["next"], "none")
            self.assertFalse(manage.plugin_path().exists())
            self.quiet(manage.scheduler, "off")
            state = manage.scheduler_info()
            self.assertFalse(state["enabled"] or state["loaded"])
            self.assertEqual(state["next"], "none")
            self.assertTrue(plistlib.loads(manage.agent_path().read_bytes())["Disabled"])

    def write_config(self, data):
        path = self.home / ".config/tms/config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_scheduler_applies_configured_hours_and_reloads(self):
        path = self.write_config({"backup_volume": "/Volumes/custom", "schedule_hours": [22, 7, 13]})
        original = path.read_text()
        self.quiet(manage.scheduler, "on")
        job = plistlib.loads(manage.agent_path().read_bytes())
        self.assertEqual(job["StartCalendarInterval"], [
            {"Hour": h, "Minute": 0} for h in (7, 13, 22)])
        self.assertEqual(manage.scheduler_info()["hours"], [7, 13, 22])
        self.assertEqual(path.read_text(), original)
        self.write_config({"schedule_hours": [0, 23]})
        self.assertEqual(manage.scheduler_info()["hours"], [7, 13, 22])
        self.quiet(manage.scheduler, "on")
        self.assertEqual(manage.scheduler_info()["hours"], [0, 23])

    def test_existing_config_without_hours_uses_defaults(self):
        path = self.write_config({"backup_volume": "/Volumes/custom", "mail_from": "user@example.com"})
        original = path.read_text()
        self.quiet(manage.scheduler, "on")
        self.assertEqual(manage.scheduler_info()["hours"], [0, 4, 8, 12, 16, 20])
        self.assertEqual(path.read_text(), original)

    def test_invalid_hours_preserve_loaded_job(self):
        self.quiet(manage.scheduler, "on")
        original = manage.agent_path().read_bytes()
        invalid = ([], [24], [-1], [True], [3.5], ["4"], [4, 4], "4", None, {}, [[1]])
        for hours in invalid:
            with self.subTest(hours=hours):
                self.write_config({"schedule_hours": hours})
                self.calls.clear()
                with self.assertRaisesRegex(RuntimeError, "schedule_hours"):
                    self.quiet(manage.scheduler, "on")
                self.assertTrue(self.loaded)
                self.assertEqual(manage.agent_path().read_bytes(), original)
                self.assertTrue(all(call[1] in ("print", "print-disabled") for call in self.calls))
        path = self.write_config({})
        for content in ('{', '[]'):
            with self.subTest(content=content):
                path.write_text(content)
                self.calls.clear()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.quiet(manage.scheduler, "on")
                self.assertEqual(manage.agent_path().read_bytes(), original)
                self.assertTrue(all(call[1] in ("print", "print-disabled") for call in self.calls))
        # Broken settings must not prevent inspecting or disabling the scheduler.
        self.quiet(manage.scheduler, "status")
        self.quiet(manage.scheduler, "off")
        self.assertFalse(self.loaded)

    def test_swiftbar_independent_and_owned_link_only(self):
        self.quiet(manage.swiftbar, "on")
        self.assertEqual(manage.plugin_path().resolve(), manage.ROOT / "tms.1m.sh")
        self.quiet(manage.swiftbar, "off")
        self.assertFalse(manage.plugin_path().exists())
        self.assertFalse(any(call[0] == "/bin/launchctl" for call in self.calls))
        manage.plugin_path().write_text("someone else's plugin")
        with self.assertRaises(RuntimeError):
            manage.swiftbar("off")
        self.assertEqual(manage.plugin_path().read_text(), "someone else's plugin")

    def test_background_access_check_cleans_up(self):
        self.assertEqual(self.quiet(manage.access, "status"), 0)
        state = self.home / "Library/Application Support/tms"
        self.assertFalse(list(state.glob("access-*.plist")))
        self.assertFalse(list(state.glob("access-*.json")))
        self.assertTrue(any(call[1] == "bootout" for call in self.calls if call[0] == "/bin/launchctl"))

    def test_central_policy_created_once_and_user_edits_preserved(self):
        manage.prepare()
        config = json.loads((self.home / ".config/tms/config.json").read_text())
        self.assertEqual(config["schedule_hours"], [0, 4, 8, 12, 16, 20])
        policy = self.home / ".config/tms/excludes"
        self.assertIn(".models/", policy.read_text())
        policy.write_text("")
        manage.prepare()
        self.assertEqual(policy.read_text(), "")


if __name__ == "__main__":
    unittest.main()
