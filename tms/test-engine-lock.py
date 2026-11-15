#!/usr/bin/python3
"""Real concurrent engine calls; backups, locks, logs, and workers stay in /tmp."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent


class EngineLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tms-engine-lock-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "file").write_text("fixture")
        self.other = self.base / "other"
        self.other.mkdir()
        self.state = self.base / "state"
        self.volume = self.base / "destination"
        self.worker = self.base / "worker-pid"
        excludes = self.base / "excludes"
        excludes.write_text("")
        self.env = dict(os.environ, backup_volume=str(self.volume), TMS_BACKUP_VOLUME=str(self.volume),
                        TMS_STATE_DIR=str(self.state), TMS_LOG_DIR=str(self.base / "logs"),
                        TMS_CONFIG_FILE=str(self.base / "config.json"), TMS_EMAIL_ENABLED="0",
                        TMS_EXCLUDES_FILE=str(excludes), TMS_TEST_WORKER_PID=str(self.worker),
                        PATH="/opt/homebrew/bin:" + os.environ["PATH"])
        self.env.pop("TMS_ENGINE_LOCK_FD", None)
        self.holder = None
        self.output = None
        self.addCleanup(self.stop_holder)

    def invoke(self, *args, env=None, batch=False, pass_fds=()):
        function = "tms_batch" if batch else "tms"
        return subprocess.run(["/bin/bash", "-c", 'source "$1"; shift; ' + function + ' "$@"',
                               "tms-test", str(ROOT / "tms.sh"), *args],
                              env=env or self.env, pass_fds=pass_fds, text=True, capture_output=True, timeout=10)

    def start_holder(self, service=False):
        env = dict(self.env)
        env["BASH_FUNC_rsync%%"] = '() { printf "%s\\n" "$$" > "$TMS_TEST_WORKER_PID"; sleep 120; }'
        self.output = (self.base / "holder-output").open("w")
        if service:
            command = [sys.executable, "-B", str(ROOT / "tms-service.py"), "run", "--source", str(self.source)]
        else:
            command = ["/bin/bash", "-c", 'source "$1"; tms "$2"', "tms-test", str(ROOT / "tms.sh"), str(self.source)]
        self.holder = subprocess.Popen(command, env=env, stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.worker.exists():
                return
            if self.holder.poll() is not None:
                self.fail((self.base / "holder-output").read_text())
            time.sleep(.05)
        self.fail("Fixture did not reach the blocked copy stage")

    def stop_holder(self):
        if self.worker.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(self.worker.read_text()), signal.SIGKILL)
        if self.holder:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.holder.pid, signal.SIGKILL)
            self.holder.wait(timeout=5)
            self.holder = None
        if self.output:
            self.output.close()
            self.output = None

    def assert_blocked(self, *args, **kwargs):
        result = self.invoke(*args, **kwargs)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn("Another TMS backup is running", result.stderr)

    def test_terminal_blocks_every_mutating_entry_point_and_spoofed_flags(self):
        self.start_holder()
        backup = self.volume / "backup"
        before = set(backup.rglob("*"))
        alias = self.base / "alias"
        alias.symlink_to(self.source)
        config = self.base / "batch"
        config.write_text(str(self.source) + "\n")
        for args in ((str(self.source),), (str(self.source), "full"), (str(alias),),
                     ("--all",), ("--consolidate", str(self.base))):
            self.assert_blocked(*args)
        self.assert_blocked(str(config), batch=True)
        spoofed = dict(self.env, _tms_service_run="yes", TMS_ENGINE_LOCK_FD="999999")
        self.assert_blocked(str(self.source), env=spoofed)
        with (self.base / "unrelated-lock").open("a+") as unrelated:
            spoofed["TMS_ENGINE_LOCK_FD"] = str(unrelated.fileno())
            self.assert_blocked(str(self.source), env=spoofed, pass_fds=(unrelated.fileno(),))
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tms-service.py"), "run", "--all"],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("duplicate request skipped", result.stdout)
        self.assertFalse((self.state / "status.json").exists())
        self.assertEqual(set(backup.rglob("*")), before)
        self.assertEqual(self.invoke("--help").returncode, 0)
        self.assertEqual(self.invoke("--estimate", str(self.source), "--json").returncode, 0)
        self.stop_holder()
        result = self.invoke(str(self.other))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_service_blocks_plain_and_full_terminal_backups(self):
        self.start_holder(service=True)
        self.assert_blocked(str(self.source))
        self.assert_blocked(str(self.source), "full")
        self.assert_blocked("--all")

    def test_lock_survives_coordinator_death_until_worker_exits(self):
        self.start_holder()
        owner = json.loads((self.state / "engine-owner.json").read_text())
        os.kill(owner["pid"], signal.SIGKILL)
        self.holder.wait(timeout=5)
        self.assert_blocked(str(self.source))
        self.stop_holder()
        with (self.state / "run.lock").open("r") as lock:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)


if __name__ == "__main__":
    unittest.main()
