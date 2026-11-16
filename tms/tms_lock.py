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
from tms_status import begin, update, snapshot, status_lines

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
        begin(state_dir, owner)
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
            log_dir = Path(os.environ.get("TMS_LOG_DIR", Path.home() / "Library/Logs/tms"))
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / (dt.datetime.now().strftime("%Y-%m-%dT%H%M%S") + "-" + str(os.getpid()) + "-terminal.log")
            update(lock_fd=fd, log=str(log))
            print("Current log: " + str(log), flush=True)
            with log.open("wb") as stream:
                child = subprocess.Popen(["/bin/bash", "-c", command, "tms-engine", str(ROOT / "tms.sh"), *arguments],
                                         env=env, pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                try:
                    while True:
                        chunk = os.read(child.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        stream.write(chunk)
                        stream.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    code = child.wait()
                except KeyboardInterrupt:
                    update(lock_fd=fd, status="INTERRUPTED", phase="INTERRUPTED")
                    raise
                finally:
                    child.stdout.close()
            code = code if code >= 0 else 128 - code
            # A partial source is a successful command with warnings.
            with log.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                partial = any(len(parts) > 1 and parts[1].strip() == b"PARTIAL"
                              for parts in (line.split(b"|") for line in stream.read().splitlines()))
            status = "FAILED" if code else "PARTIAL" if partial else "OK"
            update(lock_fd=fd, status=status, phase="DONE" if not code else "FAILED")
            return code
    except EngineBusy as error:
        print("Error: " + str(error), file=sys.stderr)
        print("\n".join(status_lines(snapshot())), file=sys.stderr)
        return 75
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
