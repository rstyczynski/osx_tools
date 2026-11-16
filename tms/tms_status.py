#!/usr/bin/python3
"""Shared live backup phases and read-only status for CLI and SwiftBar."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time


def state_directory():
    return Path(os.environ.get("TMS_STATE_DIR", Path.home() / "Library/Application Support/tms"))


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(directory, state):
    temporary = directory / ("progress." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(directory / "progress.json")


def begin(directory, owner):
    write_state(directory, dict(owner, status="RUNNING", phase="START"))


def update(directory=None, lock_fd=None, **fields):
    # Writers must possess the engine lock; a stray invocation cannot corrupt a run.
    from tms_lock import inherited_lock
    directory = directory or state_directory()
    if inherited_lock(directory, fd=lock_fd) is None:
        return
    state = read_json(directory / "progress.json")
    owner = read_json(directory / "engine-owner.json")
    if not state or (state.get("pid"), state.get("started")) != (owner.get("pid"), owner.get("started")):
        return
    if fields.get("status") not in (None, "RUNNING"):
        fields.setdefault("finished", dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    state.update(fields, updated=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    write_state(directory, state)


def active(directory):
    try:
        with (directory / "run.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def snapshot(directory=None, running=None):
    directory = directory or state_directory()
    running = active(directory) if running is None else running
    owner = read_json(directory / "engine-owner.json")
    progress = read_json(directory / "progress.json")
    service = read_json(directory / "status.json")
    matches = bool(owner) and (progress.get("pid"), progress.get("started")) == (owner.get("pid"), owner.get("started"))
    service_matches = owner.get("kind", "service") == "service" and owner.get("pid", service.get("pid")) == service.get("pid")
    if running:
        state = dict(progress if matches else owner)
        if service_matches:
            state = dict(service, **state)
        state["status"] = "RUNNING"
        state.setdefault("phase", "START")
        # Older jobs already in flight do not emit phases. Recover only the phase,
        # never interpret old per-file rsync percentages as whole-copy progress.
        if not matches and state.get("log"):
            try:
                with Path(state["log"]).open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 65536))
                    text = stream.read().decode(errors="replace")
                events = re.findall(r"Creating hard links|Applying exclusion rules|Copying (?:full|incremental) backup|Resuming transfer", text)
                if events:
                    state["phase"] = {"Creating hard links": "CREATE HARDLINK", "Applying exclusion rules": "INIT"}.get(events[-1], "COPY")
                elif re.search(r"(?:to-chk|ir-chk)=", text):
                    state["phase"] = "COPY"
            except OSError:
                pass
    else:
        state = dict(progress if progress.get("started", "") >= service.get("started", "") else service)
        if service_matches and service.get("status") not in (None, "RUNNING") and service.get("started", "") >= progress.get("started", ""):
            state = dict(service)
        if state.get("status") == "RUNNING":
            state.update(status="INTERRUPTED", phase="INTERRUPTED")
    state["active"] = running
    return state


def amount(value):
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return ("%d B" % value) if unit == "B" else ("%.2f %s" % (size, unit))
        size /= 1024


def status_lines(state):
    lines = ["Backup: " + state.get("status", "Not run yet")]
    if state.get("active"):
        lines.append("Phase: " + state.get("phase", "START"))
        if state.get("source"):
            lines.append("Source: " + state["source"])
        if state.get("phase") == "COPY":
            percent = state.get("percent")
            lines.append("Copy progress: " + (str(percent) + "%" if percent is not None else "calculating"))
            estimated = state.get("estimated_bytes")
            lines.append("Estimated amount: " + (amount(estimated) if estimated is not None else "calculating")
                         + " (current source)")
            if state.get("processed_bytes") is not None:
                lines.append("Processed: " + amount(state["processed_bytes"]))
    if state.get("log"):
        lines.append(("Current log: " if state.get("active") else "Log: ") + state["log"])
    return lines


PROGRESS = re.compile(rb"^\s*([\d,]+)\s+(\d+)%\s+\S+\s+\S+.*(?:to-chk|ir-chk)=")


def copy_stream(stats):
    """Tee raw rsync output and publish progress2 records split by CR or LF."""
    pending = b""
    last_write = 0
    latest = None
    estimate = read_json(state_directory() / "progress.json").get("estimated_bytes")

    def publish(record):
        fields = dict(record)
        if estimate is None and record["percent"]:
            fields["estimated_bytes"] = round(record["processed_bytes"] * 100 / record["percent"])
        update(**fields)

    with Path(stats).open("wb") as output:
        while True:
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            output.write(chunk)
            output.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            records = re.split(rb"[\r\n]", pending + chunk)
            pending = records.pop()[-8192:]
            for record in records:
                match = PROGRESS.match(record)
                if match:
                    latest = {"processed_bytes": int(match[1].replace(b",", b"")), "percent": min(100, int(match[2]))}
            now = time.monotonic()
            if latest is not None and now - last_write >= 0.5:
                publish(latest)
                last_write = now
        match = PROGRESS.match(pending)
        if match:
            latest = {"processed_bytes": int(match[1].replace(b",", b"")), "percent": min(100, int(match[2]))}
        if latest is not None:
            publish(latest)


def main():
    args = sys.argv[1:]
    if not args or args == ["--json"]:
        state = snapshot()
        print(json.dumps(state, indent=2) if args else "\n".join(status_lines(state)))
    elif args[:1] == ["phase"]:
        fields = dict(phase=args[1], percent=None, processed_bytes=None, estimated_bytes=None)
        if len(args) > 2:
            fields["source"] = args[2]
        if len(args) > 3:
            fields["estimated_bytes"] = read_json(Path(args[3])).get("estimated_bytes")
        update(**fields)
    elif len(args) == 2 and args[0] == "copy":
        copy_stream(args[1])
    else:
        raise ValueError("Expected [--json], phase PHASE [SOURCE [RULES]], or copy STATS")


if __name__ == "__main__":
    os.umask(0o077)
    main()
