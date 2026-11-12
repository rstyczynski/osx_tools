#!/usr/bin/python3
"""Explicit, independent controls for the native scheduler and SwiftBar."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
LABEL = "local.tms.backup"
HOURS = tuple(range(0, 24, 4))


def command(args, check=False):
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Command failed: " + repr(args))
    return result


def domain():
    return "gui/" + str(os.getuid())


def agent_path():
    return HOME / "Library/LaunchAgents" / (LABEL + ".plist")


def helper_path():
    return HOME / "Applications/TMS Backup.app/Contents/MacOS/TMSBackup"


def prepare():
    for path in (HOME / ".config/tms", HOME / "Library/Logs/tms", HOME / "Library/Application Support/tms"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    exclusions = HOME / ".config/tms/excludes"
    try:
        with exclusions.open("x") as stream:
            stream.write((ROOT / "excludes.default").read_text())
        exclusions.chmod(0o600)
    except FileExistsError:
        pass  # Preserve the user's central policy, including an intentionally empty file.
    config = HOME / ".config/tms/config.json"
    if not config.exists():
        data = {"backup_volume": os.environ.get("TMS_BACKUP_VOLUME") or os.environ.get("backup_volume") or "/Volumes/Backup"}
        if os.environ.get("TMS_REPORT_EMAIL"):
            data["mail_from"] = os.environ["TMS_REPORT_EMAIL"]
        config.write_text(json.dumps(data, indent=2) + "\n")
        config.chmod(0o600)


def build_helper():
    prepare()
    app = HOME / "Applications/TMS Backup.app"
    contents = app / "Contents"
    binary = helper_path()
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    binary.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((ROOT / "TMSBackup.swift").read_bytes()).hexdigest()
    stamp = resources / "launcher.sha256"
    info_path = contents / "Info.plist"
    old_info = plistlib.loads(info_path.read_bytes()) if info_path.exists() else {}
    signature = command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    if (signature.returncode == 0 and stamp.exists() and stamp.read_text() == digest
            and old_info.get("TMSRunnerPath") == str(ROOT / "tms-run.sh")):
        return binary  # Preserve the identity already granted Full Disk Access.
    command(["/usr/bin/swiftc", "-O", str(ROOT / "TMSBackup.swift"), "-o", str(binary)], check=True)
    info = {"CFBundleIdentifier": "local.tms.helper", "CFBundleName": "TMS Backup",
            "CFBundleDisplayName": "TMS Backup", "CFBundleExecutable": "TMSBackup",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1.7.0", "LSUIElement": True,
            "NSAppleEventsUsageDescription": "TMS sends your backup summary using your configured Mail account.",
            "TMSRunnerPath": str(ROOT / "tms-run.sh")}
    info_path.write_bytes(plistlib.dumps(info))
    stamp.write_text(digest)
    command(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", "local.tms.helper", str(app)], check=True)
    return binary


def scheduler_info():
    path = agent_path()
    target = domain() + "/" + LABEL
    runtime = command(["/bin/launchctl", "print", target])
    overrides = command(["/bin/launchctl", "print-disabled", domain()])
    disabled = bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*true', overrides.stdout))
    info = {"installed": path.exists(), "loaded": runtime.returncode == 0,
            "enabled": False, "state": "not installed", "hours": [], "next": "none",
            "timezone": dt.datetime.now().astimezone().tzname(), "plist": str(path)}
    if path.exists():
        job = plistlib.loads(path.read_bytes())
        info["hours"] = sorted(x["Hour"] for x in job.get("StartCalendarInterval", []) if "Hour" in x)
        # launchctl's explicit override takes precedence over the plist default.
        explicitly_enabled = bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*false', overrides.stdout))
        info["enabled"] = not disabled and (explicitly_enabled or not job.get("Disabled", False))
        info["state"] = "enabled" if info["enabled"] else "disabled"
        if info["enabled"] and not info["loaded"]:
            info["state"] = "enabled but not loaded"
        if info["enabled"] and info["loaded"] and info["hours"]:
            now = dt.datetime.now().astimezone()
            candidates = [now.replace(hour=h, minute=0, second=0, microsecond=0) + dt.timedelta(days=d)
                          for d in (0, 1) for h in info["hours"]]
            info["next"] = min(x for x in candidates if x > now).isoformat(timespec="minutes")
    if overrides.returncode:
        info["state"] += " (launchd override status unavailable)"
    match = re.search(r'^\s*state = (.+)$', runtime.stdout, re.MULTILINE)
    info["runtime"] = match.group(1) if match else "unloaded"
    return info


def scheduler_description(info=None):
    info = scheduler_info() if info is None else info
    hours = ", ".join("%02d:00" % h for h in info["hours"]) or "not configured"
    return ("Scheduler: " + info["state"] + "; launchd " + info["runtime"]
            + "; schedule " + hours + " " + str(info["timezone"])
            + "; next " + info["next"] + "; catches up once after sleep while logged in")


def scheduler(action):
    before = scheduler_info()
    if action == "status":
        print(scheduler_description(before))
        print("LaunchAgent:", before["plist"])
        return 0
    if before["runtime"] == "running":
        raise RuntimeError("A scheduled backup is running. Retry the scheduler change after it finishes; no backup was interrupted.")
    target = domain() + "/" + LABEL
    if action == "off":
        command(["/bin/launchctl", "disable", target], check=True)
        if before["loaded"]:
            command(["/bin/launchctl", "bootout", target], check=True)
        if agent_path().exists():
            job = plistlib.loads(agent_path().read_bytes())
            job["Disabled"] = True
            agent_path().write_bytes(plistlib.dumps(job))
        print("Scheduler disabled. Snapshots, logs, configuration, and SwiftBar are retained.")
    elif action == "on":
        binary = build_helper()
        path = agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        job = {"Label": LABEL, "ProgramArguments": [str(binary), "--all"],
               "StartCalendarInterval": [{"Hour": h, "Minute": 0} for h in HOURS],
               "RunAtLoad": False, "Disabled": False, "ProcessType": "Background", "Nice": 10,
               "LowPriorityIO": True, "Umask": 63, "EnvironmentVariables": {"HOME": str(HOME)},
               "StandardOutPath": str(HOME / "Library/Logs/tms/launchd.log"),
               "StandardErrorPath": str(HOME / "Library/Logs/tms/launchd-error.log")}
        if before["loaded"]:
            command(["/bin/launchctl", "bootout", target], check=True)
        path.write_bytes(plistlib.dumps(job))
        path.chmod(0o600)
        command(["/bin/launchctl", "enable", target], check=True)
        command(["/bin/launchctl", "bootstrap", domain(), str(path)], check=True)
    print(scheduler_description())
    return 0


def plugin_path():
    setting = command(["/usr/bin/defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"])
    directory = Path(setting.stdout.strip()) if setting.returncode == 0 and setting.stdout.strip() else HOME / "Library/Application Support/SwiftBar/Plugins"
    return directory / "tms.1m.sh"


def swiftbar(action):
    link = plugin_path()
    owned = link.is_symlink() and link.resolve() == ROOT / "tms.1m.sh"
    if action == "status":
        print("SwiftBar:", "enabled" if owned else "disabled", "(" + str(link) + ")")
        return 0
    if (link.exists() or link.is_symlink()) and not owned:
        raise RuntimeError("Refusing to replace/remove an unrelated SwiftBar plugin: " + str(link))
    if action == "on":
        build_helper()
        link.parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "tms.1m.sh").chmod(0o755)
        if not owned:
            link.symlink_to(ROOT / "tms.1m.sh")
        command(["/usr/bin/open", "-a", "SwiftBar"], check=True)
    elif owned:
        link.unlink()
    command(["/usr/bin/open", "-g", "swiftbar://refreshallplugins"])
    return swiftbar("status")


def access(action):
    if action == "setup":
        build_helper()
        print("macOS does not allow scripts to grant Full Disk Access.")
        print("1. In Full Disk Access, click + (authenticate if macOS asks).")
        print("2. Press Command-Shift-G and enter " + str(HOME / "Applications/TMS Backup.app"))
        print("3. Add TMS Backup.app and turn its switch ON. No Bash/Python/cron grant is needed.")
        print("4. Run: tms --access status")
        command(["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"], check=True)
        command(["/usr/bin/open", "-R", str(HOME / "Applications/TMS Backup.app")], check=True)
        return 0
    binary = helper_path()
    if not binary.exists():
        print("TMS app is not installed. Run tms --access setup first.")
        return 1
    prepare()
    state_dir = HOME / "Library/Application Support/tms"
    result_path = state_dir / ("access-" + str(os.getpid()) + ".json")
    plist = state_dir / ("access-" + str(os.getpid()) + ".plist")
    label = "local.tms.access-check." + str(os.getpid())
    job = {"Label": label, "ProgramArguments": [str(binary), "--check-access"], "RunAtLoad": True,
           "EnvironmentVariables": {"HOME": str(HOME), "TMS_ACCESS_RESULT": str(result_path)},
           "StandardErrorPath": str(HOME / "Library/Logs/tms/access-check-error.log")}
    plist.write_bytes(plistlib.dumps(job))
    try:
        command(["/bin/launchctl", "bootstrap", domain(), str(plist)], check=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not result_path.exists():
            time.sleep(0.2)
        if not result_path.exists():
            raise RuntimeError("Background access check timed out; see access-check-error.log")
        result = json.loads(result_path.read_text())
        for row in result["paths"]:
            print(row["status"] + ": " + row["path"] + (" — " + row["error"] if row.get("error") else ""))
        print("Access check: " + ("PASS" if result["ok"] else "FAILED; run tms --access setup"))
        return 0 if result["ok"] else 1
    finally:
        command(["/bin/launchctl", "bootout", domain() + "/" + label])
        plist.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feature", choices=("scheduler", "swiftbar", "access"))
    parser.add_argument("action", nargs="?", default="status")
    args = parser.parse_args(arguments)
    action = "status" if args.action == "check" else args.action
    allowed = ("status", "setup") if args.feature == "access" else ("on", "off", "status")
    if action not in allowed:
        parser.error(args.feature + " supports " + ", ".join(allowed))
    return {"scheduler": scheduler, "swiftbar": swiftbar, "access": access}[args.feature](action)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        sys.exit(main())
    except Exception as error:
        print("TMS setup error: " + str(error), file=sys.stderr)
        sys.exit(1)
