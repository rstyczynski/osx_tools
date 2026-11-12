#!/bin/bash
set -eu
source "$(dirname "$0")/tms.sh"
fixture=$(mktemp -d /tmp/tms-test.XXXXXX)
fixture=$(cd "$fixture" && pwd -P)
trap 'rm -rf "$fixture"' EXIT
export PATH="/opt/homebrew/bin:$PATH"
backup_volume="$fixture/destination"
mkdir -p "$fixture/source one" "$fixture/source two"
printf 'original\n' > "$fixture/source one/file"
printf 'second\n' > "$fixture/source two/file"
tms "$fixture/source one" > "$fixture/output"
tms "$fixture/source two" >> "$fixture/output"
grep -q "backup_volume=$backup_volume" "$fixture/output"
[[ $(cat "$backup_volume/backup/source one/.source") = "$fixture/source one" ]]
# Inventory is read-only and reports source state, size, file count, and latest.
before=$(readlink "$backup_volume/backup/source one/latest")
tms --sources > "$fixture/output"
awk -F ' *[|] *' -v src="$fixture/source one" '$1 == src && $2 == "READY" && $3 ~ /KiB/ && $4 == 1 && $5 ~ /[.]full$/ {found=1} END {exit !found}' "$fixture/output"
[[ $(readlink "$backup_volume/backup/source one/latest") = "$before" ]]
# Exercise recovery of legacy snapshots lacking metadata.
rm "$backup_volume/backup/source two/.source"
[[ $(_tms_source_for_label "$backup_volume/backup/source two") = "$fixture/source two" ]]
sleep 1
printf 'changed\n' > "$fixture/source one/file"
tms --all > "$fixture/output"
grep -q '2 succeeded, 0 failed' "$fixture/output"
[[ $(grep -c '^FINAL BACKUP REPORT$' "$fixture/output") = 1 ]]
awk -F ' *[|] *' -v src="$fixture/source one" '$1 == src && $2 == "OK" && $3 == 1 {found=1} END {exit !found}' "$fixture/output"
awk -F ' *[|] *' -v src="$fixture/source two" '$1 == src && $2 == "OK" && $3 == 0 {found=1} END {exit !found}' "$fixture/output"
cmp "$fixture/source one/file" "$backup_volume/backup/source one/latest$fixture/source one/file"
# Missing sources must not stop other known sources from running.
sleep 1
mv "$fixture/source one" "$fixture/moved source"
if tms --all > "$fixture/output" 2>&1; then exit 1; fi
grep -q '1 succeeded, 1 failed' "$fixture/output"
awk -F ' *[|] *' -v src="$fixture/source one" '$1 == src && $2 == "FAILED" && $3 == "-" {found=1} END {exit !found}' "$fixture/output"
tms --sources > "$fixture/inventory"
awk -F ' *[|] *' -v src="$fixture/source one" '$1 == src && $2 == "MISSING" && $3 == "-" && $4 == "-" {found=1} END {exit !found}' "$fixture/inventory"
# Failed copies must preserve the previous successful latest snapshot.
previous=$(readlink "$backup_volume/backup/source two/latest")
rsync() { return 23; }
sleep 1
if tms "$fixture/source two" > "$fixture/output" 2>&1; then exit 1; fi
[[ $(readlink "$backup_volume/backup/source two/latest") = "$previous" ]]
grep -q 'FAILED.*Copy failed' "$fixture/output"
unset -f rsync
# File-config batches share one final report, including failed sources.
sleep 1
printf '%s\n' "$fixture/source one" "$fixture/source two" > "$fixture/config"
if tms_batch "$fixture/config" > "$fixture/output" 2>&1; then exit 1; fi
[[ $(grep -c '^FINAL BACKUP REPORT$' "$fixture/output") = 1 ]]
grep -q 'Total: 2 | OK: 1 | FAILED: 1 | SKIPPED: 0' "$fixture/output"
# Unknown legacy labels must get one SKIPPED report row.
mkdir -p "$backup_volume/backup/unknown"
sleep 1
if tms --all > "$fixture/output" 2>&1; then exit 1; fi
awk -F ' *[|] *' '$1 == "unknown" && $2 == "SKIPPED" {found=1} END {exit !found}' "$fixture/output"
grep -q 'Total: 3 | OK: 1 | FAILED: 1 | SKIPPED: 1' "$fixture/output"
tms --sources > "$fixture/inventory"
awk -F ' *[|] *' '$1 == "unknown (unknown path)" && $2 == "UNKNOWN" && $5 == "-" {found=1} END {exit !found}' "$fixture/inventory"
backup_volume="$fixture/empty"
if tms --all > "$fixture/output" 2>&1; then exit 1; fi
if tms --sources > "$fixture/output" 2>&1; then exit 1; fi
mkdir -p "$backup_volume/backup"
tms --sources > "$fixture/output"
grep -q 'Sources: 0' "$fixture/output"
# Bare invocation and informational options must not start backups.
tms > "$fixture/output"
grep -q 'With no arguments' "$fixture/output"
tms --version | grep -q '1.5'
tms --help > "$fixture/output"
if tms --unknown > "$fixture/output" 2>&1; then exit 1; fi
if tms --all extra > "$fixture/output" 2>&1; then exit 1; fi
if tms --sources extra > "$fixture/output" 2>&1; then exit 1; fi
echo 'PASS: backups, discovery, options, spaces, missing sources, copy failures, final report, read-only source inventory'
