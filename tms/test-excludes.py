#!/usr/bin/python3
"""Rule semantics and real full/incremental/restore tests using temporary data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from tms_excludes import Policy, prepare

ROOT = Path(__file__).resolve().parent


class ExclusionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tms-exclusions-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.source = self.base / "source with spaces"
        self.source.mkdir()
        self.central = self.base / "central"
        self.central.write_text(".build/\n.models/\n.cache/\n*.tmp\n")
        env = patch.dict(os.environ, {"TMS_EXCLUDES_FILE": str(self.central)})
        env.start()
        self.addCleanup(env.stop)

    def file(self, name, content="contents"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def included(self, name, directory=False):
        return Policy(self.source).decision(self.source / name, directory)[0]

    def test_overrides_ancestor_rules_scope_and_globs(self):
        (self.base / ".tmsignore").write_text("source with spaces/ancestor.txt\n")
        self.file(".tmsignore", "!/.models/trained/\n!ancestor.txt\n**/scratch/*.log\n")
        self.file("project-a/.tmsignore", "extra/\n!extra/keep.txt\n")
        self.file(".models/trained/.tmsignore", "secret.tmp\n!public.tmp\n")
        self.assertFalse(self.included(".build/out/file"))
        self.assertFalse(self.included("nested/.cache/weights.bin"))
        self.assertTrue(self.included("something-cache/valuable.txt"))
        self.assertFalse(self.included(".models/other/weights.bin"))
        self.assertTrue(self.included(".models/trained/weights.bin"))
        self.assertTrue(self.included(".models/trained/public.tmp"))
        self.assertFalse(self.included(".models/trained/secret.tmp"))
        self.assertTrue(self.included("ancestor.txt"))
        self.assertFalse(self.included("project-a/extra/drop.txt"))
        self.assertTrue(self.included("project-a/extra/keep.txt"))
        self.assertTrue(self.included("project-b/extra/drop.txt"))
        self.assertFalse(self.included("scratch/out.log"))
        self.assertFalse(self.included("a/b/scratch/out.log"))
        self.assertTrue(self.included("a/b/scratch/nested/out.log"))
        self.assertTrue(self.included(".models/.tmsignore"))

    def test_directory_rules_do_not_exclude_same_named_files(self):
        self.assertTrue(self.included(".build"))
        self.assertFalse(self.included(".build", True))
        self.central.write_text("/.build/\n")
        self.assertFalse(self.included(".build/output"))
        self.assertTrue(self.included("nested/.build/output"))

    def test_invalid_rules_fail_and_escaped_comment_matches(self):
        self.central.write_text("\\#special\n\\!literal\n")
        self.assertFalse(self.included("#special"))
        self.assertFalse(self.included("!literal"))
        self.central.write_text("!\n")
        with self.assertRaises(ValueError):
            Policy(self.source)

    def test_full_scan_keeps_config_and_newline_filename(self):
        self.file(".build/discard")
        self.file(".models/other/discard")
        self.file(".models/trained/line\nbreak.bin")
        self.file(".tmsignore", "!/.models/trained/\n.tmsignore\n")
        snapshot = self.base / "full"
        snapshot.mkdir()
        self.assertEqual(prepare(self.source, snapshot), 2)
        entries = Path(str(snapshot) + ".files0").read_bytes().split(b"\0")
        self.assertIn(os.fsencode(self.source / ".models/trained/line\nbreak.bin"), entries)
        self.assertIn(os.fsencode(self.source / ".models"), entries)
        self.assertIn(os.fsencode(self.source / ".tmsignore"), entries)
        record = json.loads(Path(str(snapshot) + ".rules.json").read_text())
        self.assertTrue(any(item["path"] == str(self.central) for item in record["configurations"]))

    def test_full_incremental_reinclude_restore_and_explain(self):
        self.central.write_text("")
        self.file(".build/discard")
        self.file(".models/raw/discard")
        kept = self.file(".models/trained/keep")
        self.file("file with\nnewline.txt")
        self.file(".tmsignore", "")
        os.utime(kept, (1000000000, 1000000000))
        volume = self.base / "destination"
        label = volume / "backup" / self.source.name
        label.mkdir(parents=True)
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

        def run(*arguments, success=True):
            command = ["/bin/bash", "-c", 'source "$1"; backup_volume=$2; shift 2; "$@"',
                       "tms", str(ROOT / "tms.sh"), str(volume)] + list(arguments)
            completed = subprocess.run(command, env=env, capture_output=True, text=True)
            if success:
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            else:
                self.assertNotEqual(completed.returncode, 0)
            return completed

        run("tms", str(self.source))
        original = (label / "latest").resolve()
        stored = lambda snapshot, name: snapshot / str(self.source / name).lstrip("/")
        self.assertTrue(stored(original, ".build/discard").exists())
        self.central.write_text(".build/\n.models/\n")
        self.file(".tmsignore", "!/.models/trained/\n")
        time.sleep(1)
        completed = run("tms", str(self.source))
        second = (label / "latest").resolve()
        self.assertIn("EXCLUDED", completed.stdout)
        self.assertFalse(stored(second, ".build/discard").exists())
        self.assertFalse(stored(second, ".models/raw/discard").exists())
        self.assertEqual(stored(second, ".models/trained/keep").read_text(), "contents")
        self.assertTrue(stored(second, "file with\nnewline.txt").exists())
        self.assertTrue(stored(original, ".build/discard").exists())
        self.assertTrue((self.source / ".build/discard").exists())
        record = json.loads(Path(str(second) + ".rules.json").read_text())
        self.assertEqual(record["excluded_files"], 2)
        self.assertEqual(record["removed_from_new_snapshot"], 2)
        explained = run("tms", "--explain", str(self.source / ".models/trained/keep"), "--source", str(self.source))
        self.assertIn("INCLUDED", explained.stdout)
        self.assertIn(str(self.source / ".tmsignore") + ":1", explained.stdout)
        # A new include must recover files that have not changed since the previous backup.
        self.file(".tmsignore", "!/.models/trained/\n!/.build/\n")
        (self.source / "new-empty").mkdir()
        time.sleep(1)
        run("tms", str(self.source))
        third = (label / "latest").resolve()
        self.assertTrue(stored(third, ".build/discard").exists())
        self.assertFalse(stored(second, ".build/discard").exists())
        self.assertTrue(stored(third, "new-empty").is_dir())
        destination = self.base / "restored"
        run("tms_restore", self.source.name, third.name, str(destination))
        self.assertTrue((destination / "tms-exclusions.json").is_file())
        self.assertTrue(stored(destination / third.name, ".tmsignore").is_file())
        # Invalid configuration must not replace the last successful snapshot.
        self.central.write_text("!\n")
        time.sleep(1)
        run("tms", str(self.source), success=False)
        self.assertEqual((label / "latest").resolve(), third)


if __name__ == "__main__":
    unittest.main()
