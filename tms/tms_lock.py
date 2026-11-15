#!/usr/bin/python3
"""Engine-owned backup mutex, shared by every TMS entry point."""
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
FD_ENV = "TMS_ENGINE_LOCK_FD"


class EngineBusy(RuntimeError):
    pass


def state_directory():
    return Path(os.environ.get("TMS_STATE_DIR", Path.home() / "Library/Application Support/tms"))


def inherited_lock(state_dir=None, fd=None):
    """A marker is insufficient: require a live descriptor for the actual mutex."""
    try:
        fd = int(os.environ.get(FD_ENV, "-1")) if fd is None else int(fd)
        if fd < 3:
            return None
        opened = os.fstat(fd)
        expected = ((state_dir or state_directory()) / "run.lock").stat()
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError):
        return None


@contextmanager
def engine_lock(state_dir=None, kind="terminal"):
    state_dir = state_dir or state_directory()
    inherited = inherited_lock(state_dir)
    if inherited is not None:
        yield inherited
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EngineBusy("Another TMS backup is running; no snapshot created or resumed. Retry after it finishes.")
        owner = {"pid": os.getpid(), "kind": kind,
                 "started": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        temporary = state_dir / ("engine-owner." + str(os.getpid()) + ".tmp")
        temporary.write_text(json.dumps(owner) + "\n")
        temporary.replace(state_dir / "engine-owner.json")
        # Close only. Explicit LOCK_UN would also unlock inherited worker copies.
        yield lock.fileno()


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "check":
        return 0 if inherited_lock() is not None else 1
    if len(sys.argv) < 3 or sys.argv[1] not in ("tms", "batch"):
        raise ValueError("Expected check, tms ARGS, or batch CONFIG")
    mode, arguments = sys.argv[1], sys.argv[2:]
    try:
        kind = "consolidation" if mode == "tms" and arguments[:1] == ["--consolidate"] else "terminal"
        with engine_lock(kind=kind) as fd:
            env = dict(os.environ, **{FD_ENV: str(fd)})
            function = "tms" if mode == "tms" else "tms_batch"
            command = 'source "$1"; shift; ' + function + ' "$@"'
            result = subprocess.run(["/bin/bash", "-c", command, "tms-engine", str(ROOT / "tms.sh"), *arguments],
                                    env=env, pass_fds=(fd,))
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except EngineBusy as error:
        print("Error: " + str(error), file=sys.stderr)
        return 75
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
