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
        self.home = Path(temporary.name)
        self.loaded, self.disabled = False, False
        self.calls = []
        self.helper = self.home / "Applications/TMS Backup.app/Contents/MacOS/TMSBackup"
        self.helper.parent.mkdir(parents=True)
        self.helper.touch()
        for item in (patch.object(manage, "HOME", self.home),
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


if __name__ == "__main__":
    unittest.main()
