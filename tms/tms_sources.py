#!/usr/bin/python3
"""Consolidate descendant backup sources without deleting their history."""
import argparse
import datetime as dt
import fcntl
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from tms_excludes import Policy, walk

ROOT = Path(__file__).resolve().parent


def retired(label):
    return os.path.lexists(label / ".retired")


def source_for(label):
    result = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; _tms_source_for_label "$2"',
         "tms", str(ROOT / "tms.sh"), str(label)],
        capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("Cannot identify active source at %s; resolve its metadata first" % label)
    return Path(result.stdout.rstrip("\n")).resolve()


def descendants(parent, backup):
    children = {}
    for label in sorted(backup.iterdir()):
        if not label.is_dir() or label.is_symlink() or retired(label):
            continue
        # A failed initial parent snapshot may not have published .source yet.
        if label.name == parent.name and not (label / ".source").exists() and not (label / "latest").exists():
            continue
        source = source_for(label)
        if source == parent:
            continue
        if source in parent.parents:
            raise ValueError("Source is already covered by %s; use that existing parent" % source)
        if parent in source.parents:
            if not source.is_dir():
                raise ValueError("Child source missing: %s; restore or resolve it before consolidation" % source)
            children[label] = source
    return children


def check_coverage(parent, children, snapshot=None):
    """Compare current inclusion policies; after copying, also require snapshot entries."""
    parent_policy = Policy(parent)
    for label, child in children.items():
        child_policy = Policy(child)
        entries = ((child, child.lstat()),)
        for path, info in itertools.chain(entries, walk(child)):
            directory = stat.S_ISDIR(info.st_mode)
            if not child_policy.decision(path, directory)[0]:
                continue
            included, rule, _ = parent_policy.decision(path, directory)
            if not included:
                reason = "%s:%s (%s)" % (rule.origin, rule.line, rule.text) if rule else "parent policy"
                raise ValueError("Coverage gap: %s is included by child %s but excluded by %s. "
                                 "Adjust exclusions and preview again." % (path, child, reason))
            if snapshot is not None and not os.path.lexists(snapshot / str(path).lstrip("/")):
                raise ValueError("Coverage gap: current child entry absent from parent snapshot: %s. "
                                 "Retry to pick up entries that appeared during copying." % path)


def atomic_marker(label, record):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=".retired-", dir=str(label), delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Never overwrite another actor's retirement decision.
        os.link(str(temporary), str(label / ".retired"))
    finally:
        if temporary is not None:
            temporary.unlink()


def snapshot_for(parent, label, record):
    """Reuse the pending attempt; migrate older source-only pending records."""
    name = record.get("snapshot")
    if name:
        if not isinstance(name, str) or Path(name).name != name or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}(?:\.full)?", name):
            raise ValueError("Invalid pending snapshot name")
        candidate = label / name
        if candidate.is_symlink():
            raise ValueError("Pending snapshot must not be a symlink")
        return candidate
    if record:
        candidates = []
        for candidate in label.glob("????-??-??T*.full"):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                policy = json.loads(Path(str(candidate) + ".rules.json").read_text())
                if policy.get("source") != str(parent) or not Path(str(candidate) + ".start").is_file():
                    continue
                stats = Path(str(candidate) + ".stats")
                with stats.open("rb") as stream:
                    stream.seek(max(0, stats.stat().st_size - 65536))
                    tail = stream.read().decode("utf-8", errors="replace")
                totals = re.findall(r"Total transferred file size: ([\d,]+)", tail)
                progress = re.findall(r"(?:^|[\r\n])\s*([\d,]+)\s+\d+%", tail)
                transferred = max([int(value.replace(",", "")) for value in totals + progress] or [0])
                candidates.append((transferred, candidate.name, candidate))
            except (OSError, ValueError, AttributeError):
                continue
        if candidates:
            return max(candidates)[2]
    name = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    if not (label / "latest").is_dir():
        name += ".full"
    candidate = label / name
    if candidate.exists():
        raise ValueError("Snapshot name is already in use; retry in a second")
    return candidate


def write_pending(path, parent, snapshot):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=".consolidating-", dir=str(path.parent), delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"source": str(parent), "snapshot": snapshot.name}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def consolidate(parent, volume, dry_run=False, lock_fd=None):
    parent = Path(parent).resolve()
    backup = Path(volume).resolve() / "backup"
    if not parent.is_dir() or not parent.name:
        raise ValueError("An existing non-root parent directory is required")
    if backup == parent or parent in backup.parents:
        raise ValueError("Backup destination must be outside the parent source")
    if not backup.is_dir():
        raise ValueError("No existing backups at %s" % backup)
    parent_label = backup / parent.name
    pending = parent_label / ".consolidating"
    record = {}
    if os.path.lexists(pending):
        record = json.loads(pending.read_text())
        if not isinstance(record, dict) or record.get("source") != str(parent):
            raise ValueError("Pending consolidation belongs to another source or has invalid metadata: %s" % pending)
    if retired(parent_label):
        raise ValueError("Parent source is itself retired; review its replacement first")
    if (parent_label / ".source").exists() and source_for(parent_label) != parent:
        raise ValueError("Parent backup label belongs to another source: %s" % parent_label)
    children = descendants(parent, backup)
    if not children:
        raise ValueError("No active child sources to consolidate; use tms %s for a normal backup" % parent)
    print("Consolidation parent: %s" % parent, flush=True)
    snapshot = snapshot_for(parent, parent_label, record)
    print(("Resume existing snapshot: " if snapshot.is_dir() else "New baseline snapshot: ") + str(snapshot), flush=True)
    print("Children to retire after verified parent coverage (history retained):", flush=True)
    for label, child in children.items():
        print("  %s (label: %s)" % (child, label.name), flush=True)
    print("Checking current child inclusion against parent exclusion rules...", flush=True)
    check_coverage(parent, children)
    if dry_run:
        print("Preview passed. No snapshots or retirement markers created.")
        print("Next: tms --consolidate %s" % shlex.quote(str(parent)))
        return
    parent_label.mkdir(exist_ok=True)
    # Record the chosen snapshot before copying so retries do not allocate another.
    write_pending(pending, parent, snapshot)
    result = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; _tms_consolidation_parent=$2; _tms_resume_snapshot=$3; tms "$2"',
         "tms", str(ROOT / "tms.sh"), str(parent), str(snapshot)],
        env=dict(os.environ, backup_volume=str(Path(volume).resolve())),
        pass_fds=() if lock_fd is None else (lock_fd,))
    if result.returncode:
        raise ValueError("Parent backup failed. No children retired; existing child backups remain active")
    if (parent_label / "latest").resolve(strict=True) != snapshot or not snapshot.is_dir():
        raise ValueError("Cannot verify completed parent snapshot; no children retired")
    if descendants(parent, backup) != children:
        raise ValueError("Active sources changed during copying; no children retired. Preview and retry")
    print("Checking coverage against the completed parent snapshot...", flush=True)
    coverage_verified = True
    try:
        check_coverage(parent, children, snapshot)
    except ValueError as error:
        if Path(str(snapshot) + ".partial").exists():
            coverage_verified = False
            print("WARNING: backup done (PARTIAL); some child entries were not copied: %s" % error)
            print("Old child histories are retained. The next normal backup retries missing or changed entries.")
        else:
            raise
    # Partial coverage gaps are warnings. Finish the handoff while holding the lock.
    pending.unlink()
    record = {"replacement": str(parent), "retired_at": dt.datetime.now().astimezone().isoformat(),
              "replacement_snapshot": snapshot.name,
              "replacement_status": "PARTIAL" if Path(str(snapshot) + ".partial").exists() else "OK",
              "coverage_verified": coverage_verified}
    for label, child in children.items():
        atomic_marker(label, record)
        print("RETIRED: %s; history retained at %s" % (child, label), flush=True)
    status = "PARTIAL — done with warnings" if Path(str(snapshot) + ".partial").exists() else "OK"
    print("Consolidation complete (%s). Future backups use %s; old child snapshots remain restorable." % (status, parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    volume = os.environ.get("backup_volume", "/Volumes/Backup")
    try:
        if args.dry_run:
            consolidate(args.parent, volume, True)
        else:
            # Share the scheduler's lock; independent manual commands must also be idle.
            state = Path(os.environ.get("TMS_STATE_DIR", Path.home() / "Library/Application Support/tms"))
            state.mkdir(parents=True, exist_ok=True)
            with (state / "run.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Another TMS run is active; wait for it to finish and retry")
                consolidate(args.parent, volume, lock_fd=lock.fileno())
    except (OSError, ValueError) as error:
        print("Error: %s" % error, file=sys.stderr)
        print("Existing snapshots have not been deleted. Any completed retirement is recorded in its child's .retired file.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
