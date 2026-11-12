#!/usr/bin/python3
"""macOS runner and SwiftBar adapter; stdlib only, no interactive shell startup."""
import base64
from collections import deque
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
from tms_manage import scheduler_description

ROOT = Path(__file__).resolve().parent
HOME = Path.home()


def configuration():
    config_path = Path(os.environ.get("TMS_CONFIG_FILE", HOME / ".config/tms/config.json"))
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    config["backup_volume"] = os.environ.get("TMS_BACKUP_VOLUME", config.get("backup_volume", "/Volumes/Backup"))
    config["state_dir"] = Path(os.environ.get("TMS_STATE_DIR", HOME / "Library/Application Support/tms"))
    config["log_dir"] = Path(os.environ.get("TMS_LOG_DIR", HOME / "Library/Logs/tms"))
    return config


def atomic_text(path, content):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)


def save_state(config, state):
    atomic_text(config["state_dir"] / "status.json", json.dumps(state, indent=2) + "\n")


def environment(config):
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
    env["backup_volume"] = config["backup_volume"]
    return env


def sources(config):
    """Read only path metadata; do not rescan large source trees every minute."""
    root = Path(config["backup_volume"]) / "backup"
    if not root.is_dir():
        return []
    result = []
    for label in sorted(root.iterdir()):
        if not label.is_dir() or label.is_symlink():
            continue
        proc = subprocess.run(
            ["/bin/bash", "-c", 'source "$1"; _tms_source_for_label "$2"', "tms", str(ROOT / "tms.sh"), str(label)],
            env=environment(config), capture_output=True, text=True, timeout=20)
        source = proc.stdout.rstrip("\n") if proc.returncode == 0 else None
        latest = label / "latest"
        result.append({"label": label.name, "source": source,
                       "last": latest.resolve().name if latest.is_dir() else "never"})
    return result


def send_email(config, summary_file, subject):
    recipient = os.environ.get("TMS_REPORT_EMAIL", "")
    if not recipient or any(c in recipient for c in "\r\n"):
        raise RuntimeError("TMS_REPORT_EMAIL is missing or invalid; export it in ~/.bash_profile")
    sender = config.get("mail_from", recipient)
    command = ["/usr/bin/osascript", str(ROOT / "tms-mail.applescript"), recipient, sender, subject, str(summary_file)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=75)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "Apple Mail could not submit the report")


def check_access(config):
    """Exercise access under the actual app/launchd identity; never grants it."""
    result = {"ok": True, "paths": []}
    paths = [HOME / "Documents", Path(config["backup_volume"])]
    try:
        paths += [Path(x["source"]) for x in sources(config) if x["source"]]
    except OSError as error:
        result["paths"].append({"path": str(Path(config["backup_volume"]) / "backup"), "status": "DENIED", "error": str(error)})
        result["ok"] = False
    for path in dict.fromkeys(paths):
        try:
            with os.scandir(path) as entries:
                next(entries, None)
            row = {"path": str(path), "status": "READABLE"}
        except OSError as error:
            row = {"path": str(path), "status": "UNAVAILABLE", "error": str(error)}
            result["ok"] = False
        result["paths"].append(row)
    destination = Path(os.environ.get("TMS_ACCESS_RESULT", config["state_dir"] / "access-status.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(destination, json.dumps(result, indent=2) + "\n")
    return 0 if result["ok"] else 1


def run_backup(arguments):
    config = configuration()
    if arguments == ["--check-access"]:
        return check_access(config)
    state_dir, log_dir = config["state_dir"], config["log_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # The kernel releases this lock after normal exit, termination, or a crash.
    with (state_dir / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("TMS is already running; duplicate request skipped.")
            return 0
        if not arguments:
            arguments = ["--all"]
        email_test = arguments == ["--test-email"]
        if not email_test and not (arguments == ["--all"] or (len(arguments) == 2 and arguments[0] == "--source")):
            raise ValueError("Usage: tms-run.sh [--all | --source PATH | --test-email]")
        started = dt.datetime.now().astimezone()
        log = log_dir / (started.strftime("%Y-%m-%dT%H%M%S") + "-" + str(os.getpid()) + ".log")
        state = {"status": "RUNNING", "pid": os.getpid(), "started": started.isoformat(timespec="seconds"),
                 "scope": arguments[-1], "log": str(log)}
        save_state(config, state)
        child = None

        def interrupted(signum, frame):
            if child and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
            raise RuntimeError("Backup interrupted by signal " + str(signum))

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        output, exit_code = deque(maxlen=4000), 1
        try:
            if email_test:
                output = ["FINAL BACKUP REPORT\n", "Setup test only — no backup was started.\n"]
                output += ["{source} | {last}\n".format(source=x["source"] or x["label"] + " (unknown)", last=x["last"]) for x in sources(config)]
                log.write_text("".join(output))
                exit_code = 0
            else:
                tms_args = ["--all"] if arguments == ["--all"] else [arguments[1]]
                command = ["/bin/bash", "-c", 'source "$1"; shift; tms "$@"', "tms", str(ROOT / "tms.sh")] + tms_args
                with log.open("w") as stream:
                    child = subprocess.Popen(command, env=environment(config), stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
                    for line in child.stdout:
                        stream.write(line)
                        stream.flush()
                        print(line, end="", flush=True)
                        output.append(line)
                    exit_code = child.wait()
        except Exception as error:
            if child and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=20)
            output.append("Runner error: " + str(error) + "\n")
            with log.open("a") as stream:
                stream.write(output[-1])
        finished = dt.datetime.now().astimezone()
        text = "".join(output)
        index = text.rfind("FINAL BACKUP REPORT")
        report = text[index:] if index >= 0 else "No final table was produced.\n" + text[-6000:]
        try:
            schedule = scheduler_description()
        except Exception as error:
            schedule = "Scheduler: status unavailable — " + str(error)
        summary = ("TMS " + ("setup email test" if email_test else "backup report") + " — " + socket.gethostname() + "\n"
                   + "Started: " + started.isoformat(timespec="seconds") + "\n"
                   + "Finished: " + finished.isoformat(timespec="seconds") + "\n"
                   + "Destination: " + config["backup_volume"] + "\n"
                   + schedule + "\n"
                   + "Backup exit status: " + str(exit_code) + "\n\n" + report + "\nLog: " + str(log) + "\n")
        summary_file = log.with_suffix(".summary.txt")
        summary_file.write_text(summary)
        atomic_text(state_dir / "last-summary.txt", summary)
        state.update(status=("TEST OK" if email_test else "OK") if exit_code == 0 else "FAILED", finished=finished.isoformat(timespec="seconds"),
                     exit_code=exit_code, summary=str(summary_file), email="pending")
        try:
            atomic_text(state_dir / "sources.json", json.dumps(sources(config), indent=2) + "\n")
        except (OSError, subprocess.SubprocessError) as error:
            state["inventory_error"] = str(error)
        save_state(config, state)
        try:
            send_email(config, summary_file, "[TMS] " + ("setup test" if email_test else state["status"]) + " — " + finished.strftime("%Y-%m-%d %H:%M"))
            state["email"] = "submitted to Apple Mail"
        except Exception as error:
            state["email"] = "FAILED: " + str(error)
            print(state["email"], file=sys.stderr)
        save_state(config, state)
        return 1 if exit_code or state["email"].startswith("FAILED") else 0


def display(text):
    return str(text).replace("|", "¦").replace("\n", " ").replace("\r", " ")


def menu():
    config = configuration()
    path = config["state_dir"] / "status.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    active = False
    lock_path = config["state_dir"] / "run.lock"
    if lock_path.exists():
        with lock_path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
    email_failed = state.get("email", "").startswith("FAILED")
    status = "RUNNING" if active else state.get("status", "Not run yet")
    if status == "RUNNING" and not active:
        status = "INTERRUPTED"
    icon = "↻" if active else ("!" if email_failed or status in ("FAILED", "INTERRUPTED") else "✓" if status == "OK" else "—")
    print("TMS " + icon)
    print("---")
    print(display(status + " · " + state.get("finished", state.get("started", ""))))
    print(display(scheduler_description()))
    print(display("Destination: " + config["backup_volume"]))
    if state.get("email"):
        print(display("Email: " + state["email"]))
    action = " | bash=/usr/bin/python3 param1=" + shlex.quote(str(ROOT / "tms-service.py")) + " param2=start param3="
    print("---")
    print("Back up all now" + action + "--all terminal=false refresh=true")
    try:
        cache = config["state_dir"] / "sources.json"
        inventory = json.loads(cache.read_text()) if cache.exists() else sources(config)
    except (OSError, subprocess.SubprocessError) as error:
        inventory = []
        print(display("Sources unavailable: " + str(error)))
    for source in inventory:
        token = base64.urlsafe_b64encode(source["label"].encode()).decode()
        print(display(source["label"] + " · " + source["last"]))
        if source["source"]:
            print("--Back up now" + action + shlex.quote(token) + " terminal=false refresh=true")
            print("--" + display(source["source"]))
        else:
            print("--Original source unknown; cannot back up")
    print("---")
    summary = config["state_dir"] / "last-summary.txt"
    if summary.exists():
        print("Open last summary | bash=/usr/bin/open param1=" + shlex.quote(str(summary)) + " terminal=false")
    print("Open logs | bash=/usr/bin/open param1=" + shlex.quote(str(config["log_dir"])) + " terminal=false")
    print("Refresh | refresh=true")


def start(token):
    arguments = ["--all"]
    if token != "--all":
        label = base64.urlsafe_b64decode(token.encode()).decode()
        config = configuration()
        cache = config["state_dir"] / "sources.json"
        inventory = json.loads(cache.read_text()) if cache.exists() else sources(config)
        matches = [x for x in inventory if x["label"] == label and x["source"]]
        if not matches:
            raise ValueError("Unknown source label")
        arguments = ["--source", matches[0]["source"]]
    # Detach from the short-lived SwiftBar click handler.
    helper = HOME / "Applications/TMS Backup.app/Contents/MacOS/TMSBackup"
    command = [str(helper)] if helper.exists() else ["/bin/bash", str(ROOT / "tms-run.sh")]
    subprocess.Popen(command + arguments,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "menu"
        if mode == "run":
            sys.exit(run_backup(sys.argv[2:]))
        elif mode == "menu":
            menu()
        elif mode == "start" and len(sys.argv) == 3:
            start(sys.argv[2])
        else:
            raise ValueError("Expected run, menu, or start LABEL")
    except Exception as error:
        print("TMS error: " + str(error), file=sys.stderr)
        sys.exit(1)
