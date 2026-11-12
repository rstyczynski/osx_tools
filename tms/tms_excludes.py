#!/usr/bin/python3
"""Ordered TMS rules, source manifests, and filtering of newly cloned snapshots."""
import argparse
from dataclasses import dataclass
import fnmatch
from functools import lru_cache
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parent


def absolute(path):
    # Resolve parents, not the leaf: rules describe the backed-up link's name.
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    return path.parent.resolve() / path.name


@lru_cache(maxsize=8192)
def match_parts(path, pattern):
    if not pattern:
        return not path
    if pattern[0] == "**":
        return match_parts(path, pattern[1:]) or bool(path and match_parts(path[1:], pattern))
    return bool(path and fnmatch.fnmatchcase(path[0], pattern[0]) and match_parts(path[1:], pattern[1:]))


@dataclass(frozen=True)
class Rule:
    base: Path
    origin: str
    line: int
    text: str
    pattern: str
    include: bool
    directory: bool
    anchored: bool

    def matches(self, path, is_directory):
        try:
            parts = path.relative_to(self.base).parts
        except ValueError:
            return False
        pattern = tuple(self.pattern.split("/"))
        for end in range(1, len(parts) + 1):
            if self.directory and not (end < len(parts) or is_directory):
                continue
            if len(pattern) == 1 and not self.anchored:
                if fnmatch.fnmatchcase(parts[end - 1], self.pattern):
                    return True
            elif match_parts(parts[:end], pattern):
                return True
        return False


class Policy:
    def __init__(self, source):
        self.source = Path(source).resolve()
        self.central = absolute(os.environ.get("TMS_EXCLUDES_FILE", Path.home() / ".config/tms/excludes"))
        self.records = {}
        self.cache = {}
        actual = self.central if self.central.exists() else ROOT / "excludes.default"
        self.central_rules = self.read(actual, self.source)

    def read(self, file, base):
        if not file.exists() and not file.is_symlink():
            return []
        text = file.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Exclusion configuration exceeds 1 MiB: " + str(file))
        self.records[str(file)] = {"path": str(file), "base": str(base), "text": text}
        rules = []
        for number, raw in enumerate(text.splitlines(), 1):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            if "\x00" in value:
                raise ValueError("NUL in exclusion rule: " + str(file))
            include = value.startswith("!")
            pattern = value[1:] if include else value
            if pattern.startswith(("\\#", "\\!")):
                pattern = pattern[1:]
            directory, anchored = pattern.endswith("/"), pattern.startswith("/")
            pattern = pattern.strip("/")
            if not pattern or ".." in pattern.split("/"):
                raise ValueError("Invalid rule at %s:%s: %s" % (file, number, raw))
            rules.append(Rule(base, str(file), number, raw, pattern, include, directory, anchored))
        return rules

    def local_rules(self, directory):
        if directory in self.cache:
            return self.cache[directory]
        parent = [] if directory == directory.parent else self.local_rules(directory.parent)
        result = parent + self.read(directory / ".tmsignore", directory)
        self.cache[directory] = result
        return result

    def decision(self, path, is_directory=False):
        if path == self.source or path.name == ".tmsignore" or path == self.central:
            return True, None, "TMS source/configuration is always retained"
        included, winner = True, None
        for rule in self.central_rules + self.local_rules(path.parent):
            if rule.matches(path, is_directory):
                included, winner = rule.include, rule
        return included, winner, "default include" if winner is None else "last matching rule"


def walk(root):
    def failed(error):
        raise error  # An unreadable directory must not silently publish an incomplete backup.
    for directory, dirs, files in os.walk(root, onerror=failed, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            path = Path(directory) / name
            yield path, path.lstat()


def raise_walk_error(error):
    raise error


def prepare(source, snapshot, incremental=False, marker=None):
    source, snapshot = Path(source).resolve(), Path(snapshot).resolve()
    if snapshot == source or source in snapshot.parents:
        raise ValueError("Backup destination must be outside the source directory")
    policy = Policy(source)
    threshold = Path(marker).stat().st_mtime_ns if incremental else None
    excluded, manifest = set(), {source}
    mirror = snapshot / str(source).lstrip("/")
    for path, info in walk(source):
        directory = stat.S_ISDIR(info.st_mode)
        included, _, _ = policy.decision(path, directory)
        if not included:
            if not directory:
                excluded.add(path)
            continue
        target = snapshot / str(path).lstrip("/")
        missing = not os.path.lexists(target)
        # Missing files include unchanged files newly re-included by an override.
        selected = (not incremental or missing or path.name == ".tmsignore"
                    or stat.S_ISLNK(info.st_mode) or (not directory and info.st_ctime_ns > threshold))
        if selected:
            manifest.add(path)
            manifest.update(parent for parent in path.parents if parent == source or source in parent.parents)
    removed = 0
    if incremental and mirror.is_dir():
        # Only unlink entries in the NEW snapshot; old snapshots and source data remain intact.
        for directory, dirs, files in os.walk(mirror, topdown=False, followlinks=False,
                                             onerror=raise_walk_error):
            for name in files + dirs:
                target = Path(directory) / name
                original = source / target.relative_to(mirror)
                info = target.lstat()
                is_directory = stat.S_ISDIR(info.st_mode)
                included, _, _ = policy.decision(original, is_directory)
                if included:
                    continue
                if is_directory:
                    if not any(target.iterdir()):
                        target.rmdir()
                else:
                    target.unlink()
                    excluded.add(original)
                    removed += 1
    manifest_file = Path(str(snapshot) + ".files0")
    with manifest_file.open("wb") as stream:
        for path in sorted(manifest):
            stream.write(os.fsencode(path) + b"\x00")
    record = {"source": str(source), "central_path": str(policy.central),
              "configurations": list(policy.records.values()), "excluded_files": len(excluded),
              "removed_from_new_snapshot": removed, "manifest_entries": len(manifest)}
    sidecar = Path(str(snapshot) + ".rules.json")
    sidecar.write_text(json.dumps(record, indent=2) + "\n")
    sidecar.chmod(0o600)
    return len(excluded)


def infer_source(path, volume):
    known = []
    backup = Path(volume) / "backup"
    if backup.is_dir():
        for entry in backup.iterdir():
            metadata = entry / ".source"
            if entry.is_dir() and metadata.is_file():
                value = metadata.read_text().rstrip("\n")
                if not value or not Path(value).is_absolute():
                    continue
                source = Path(value).resolve()
                if path == source or source in path.parents:
                    known.append(source)
    if known:
        return max(known, key=lambda item: len(item.parts))
    candidates = [path] + list(path.parents)
    for parent in candidates:
        if (parent / ".git").exists():
            return parent
    for parent in candidates:
        if (parent / ".tmsignore").is_file():
            return parent
    return path if path.is_dir() else path.parent


def estimate(source):
    """Metadata-only full-source estimate; create no manifests or snapshot state."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("SOURCE must be an existing directory: " + str(source))
    policy = Policy(source)
    policy.local_rules(source)
    categories = {name: {"entries": 0, "regular_files": 0, "symlinks": 0,
                         "other_entries": 0, "logical_bytes": 0}
                  for name in ("included", "excluded")}
    root_info = source.stat()
    seen = {(root_info.st_dev, root_info.st_ino)}
    allocated = root_info.st_blocks * 512
    hardlink_aliases = 0
    for path, info in walk(source):
        directory = stat.S_ISDIR(info.st_mode)
        if path.name == ".tmsignore" and not directory:
            policy.local_rules(path.parent)
        included, _, _ = policy.decision(path, directory)
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            allocated += info.st_blocks * 512
            seen.add(key)
        elif stat.S_ISREG(info.st_mode):
            hardlink_aliases += 1
        if directory:
            continue
        bucket = categories["included" if included else "excluded"]
        bucket["entries"] += 1
        if stat.S_ISREG(info.st_mode):
            bucket["regular_files"] += 1
            bucket["logical_bytes"] += info.st_size
        elif stat.S_ISLNK(info.st_mode):
            bucket["symlinks"] += 1
        else:
            bucket["other_entries"] += 1
    total = {key: categories["included"][key] + categories["excluded"][key]
             for key in categories["included"]}
    return {"source": str(source), "central_file": str(policy.central),
            "central_loaded_from": str(next(iter(policy.records))),
            "local_rule_files": [path for path in policy.records if Path(path).name == ".tmsignore"],
            "total": total, **categories, "source_allocated_bytes": allocated,
            "hardlink_aliases": hardlink_aliases, "symlink_targets_sized": False,
            "estimate_kind": "full-source logical regular-file bytes; not incremental destination usage"}


def human_bytes(value):
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return "%d B" % value if unit == "B" else "%.2f %s" % (size, unit)
        size /= 1024


def print_estimate(result):
    print("TMS SOURCE ESTIMATE")
    print("Source:", result["source"])
    print("Central rules:", result["central_file"])
    if result["central_loaded_from"] != result["central_file"]:
        print("Using bundled defaults:", result["central_loaded_from"])
    print("Local rule files:", len(result["local_rule_files"]))
    for path in result["local_rule_files"]:
        print("  " + path)
    print()
    print("%-10s | %12s | %16s | %s" % ("CATEGORY", "ENTRIES", "LOGICAL SIZE", "EXACT BYTES"))
    for name in ("total", "included", "excluded"):
        row = result[name]
        print("%-10s | %12s | %16s | %s" % (name.upper(), format(row["entries"], ","),
              human_bytes(row["logical_bytes"]), format(row["logical_bytes"], ",")))
    total_bytes = result["total"]["logical_bytes"]
    percentage = 100 * result["excluded"]["logical_bytes"] / total_bytes if total_bytes else 0
    print("Excluded share of regular-file bytes: %.1f%%" % percentage)
    print("Source allocated usage (hard links counted once):", human_bytes(result["source_allocated_bytes"]))
    print("Entries count non-directory paths; bytes count regular-file data per path.")
    print("Symlinks: %s included, %s excluded; targets are not traversed or sized." %
          (result["included"]["symlinks"], result["excluded"]["symlinks"]))
    print("Read-only full-source estimate; no backup or state files created.")
    print("Destination usage varies with links, sparse files, filesystem allocation, and snapshot history.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("prepare")
    scan.add_argument("source")
    scan.add_argument("snapshot")
    scan.add_argument("--marker")
    explain = commands.add_parser("explain")
    explain.add_argument("path")
    explain.add_argument("--source")
    size = commands.add_parser("estimate")
    size.add_argument("source")
    size.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.source, args.snapshot, bool(args.marker), args.marker))
    elif args.command == "estimate":
        result = estimate(args.source)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_estimate(result)
    else:
        path = absolute(args.path)
        source = Path(args.source).resolve() if args.source else infer_source(path, os.environ.get("backup_volume", "/Volumes/Backup"))
        if path != source and source not in path.parents:
            parser.error("PATH must be inside --source")
        policy = Policy(source)
        included, rule, reason = policy.decision(path, path.is_dir() or args.path.endswith("/"))
        print("Decision:", "INCLUDED" if included else "EXCLUDED")
        print("Path:", path)
        print("Source root:", source)
        print("Central rules:", policy.central)
        print("Reason:", reason)
        if rule:
            print("Rule: %s:%s: %s" % (rule.origin, rule.line, rule.text))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print("TMS exclusion error: " + str(error), file=sys.stderr)
        sys.exit(1)
