#!/bin/bash
set -eu
source "$(dirname "$0")/tms.sh"
fixture=$(mktemp -d /tmp/tms-test.XXXXXX)
fixture=$(cd "$fixture" && pwd -P)
trap 'rm -rf "$fixture"' EXIT
export PATH="/opt/homebrew/bin:$PATH"
export TMS_EXCLUDES_FILE="$fixture/central-excludes"
: > "$TMS_EXCLUDES_FILE"
backup_volume="$fixture/destination"
mkdir -p "$fixture/source one" "$fixture/source two"
printf 'original\n' > "$fixture/source one/file"
printf 'second\n' > "$fixture/source two/file"
tms "$fixture/source one" > "$fixture/output"
tms "$fixture/source two" >> "$fixture/output"
grep -q "backup_volume=$backup_volume" "$fixture/output"
[[ $(cat "$backup_volume/backup/source one/.source") = "$fixture/source one" ]]
# Parent and child overlaps stop before creating any destination label/snapshot.
if tms "$fixture" > "$fixture/output" 2>&1; then exit 1; fi
grep -q 'source overlaps existing active backup sources' "$fixture/output"
grep -q "$fixture/source one" "$fixture/output"
grep -q "$fixture/source two" "$fixture/output"
[[ ! -e "$backup_volume/backup/${fixture##*/}" ]]
mkdir "$fixture/source one/child"
if tms "$fixture/source one/child" > "$fixture/output" 2>&1; then exit 1; fi
grep -q 'No snapshot created' "$fixture/output"
[[ ! -e "$backup_volume/backup/child" ]]
rmdir "$fixture/source one/child"
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
rsync() { printf 'simulated fatal I/O error\n' >&2; return 11; }
sleep 1
if tms "$fixture/source two" > "$fixture/output" 2>&1; then exit 1; fi
[[ $(readlink "$backup_volume/backup/source two/latest") = "$previous" ]]
grep -q 'FAILED.*Copy failed' "$fixture/output"
grep -q 'simulated fatal I/O error' "$fixture/output"
unset -f rsync
# Partial transfer is done with warnings; diagnostics appear after the report.
rsync() { printf 'file could not be copied\n' >&2; printf 'remaining files processed\n'; return 23; }
sleep 1
tms "$fixture/source two" > "$fixture/output" 2>&1
grep -q 'PARTIAL.*done with warnings' "$fixture/output"
awk '/FINAL BACKUP REPORT/ {report=NR} /file could not be copied/ {warning=NR} END {exit !(warning>report && report>0)}' "$fixture/output"
partial_snapshot=$(readlink "$backup_volume/backup/source two/latest")
test -f "$partial_snapshot.partial"
grep -q 'file could not be copied' "$partial_snapshot.warnings"
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
# Retirement blocks direct/full/alias backups without touching snapshot history.
mv "$fixture/moved source" "$fixture/source one"
label="$backup_volume/backup/source one"
previous=$(readlink "$label/latest")
printf '{"replacement":"%s"}\n' "$fixture/parent" > "$label/.retired"
ln -s "$fixture/source one" "$fixture/source alias"
for source in "$fixture/source one" "$fixture/source alias"; do
  if tms "$source" full > "$fixture/output" 2>&1; then exit 1; fi
  grep -q "Source retired; replacement: $fixture/parent" "$fixture/output"
  grep -q 'To reactivate deliberately' "$fixture/output"
  [[ $(readlink "$label/latest") = "$previous" ]]
done
tms --sources > "$fixture/inventory"
awk -F ' *[|] *' -v src="$fixture/source one" '$1 == src && $2 == "RETIRED" && $3 == "-" && $4 == "-" {found=1} END {exit !found}' "$fixture/inventory"
# Isolate retirement from the earlier unknown-label failure.
mv "$backup_volume/backup/unknown" "$fixture/unknown-label"
sleep 1
tms --all > "$fixture/output" 2>&1
grep -q '1 succeeded, 0 failed or skipped; 1 retired' "$fixture/output"
grep -q 'Total: 2 | OK: 1 | FAILED: 0 | SKIPPED: 1' "$fixture/output"
sleep 1
tms_batch "$fixture/config" > "$fixture/output" 2>&1
grep -q 'Total: 2 | OK: 1 | FAILED: 0 | SKIPPED: 1' "$fixture/output"
# Even a missing source, corrupt JSON or broken marker symlink stays retired.
mv "$fixture/source one" "$fixture/moved source"
printf 'invalid json\n' > "$label/.retired"
if tms "$fixture/source one" > "$fixture/output" 2>&1; then exit 1; fi
grep -q 'retirement metadata unreadable or invalid' "$fixture/output"
rm "$label/.retired"
ln -s "$fixture/nonexistent-marker" "$label/.retired"
if tms "$fixture/source one" > "$fixture/output" 2>&1; then exit 1; fi
grep -q 'Source retired' "$fixture/output"
[[ $(readlink "$label/latest") = "$previous" ]]
# Restoring old snapshots works while retired; reactivation only removes the marker.
tms_restore 'source one' "${previous##*/}" "$fixture/restore-retired" > "$fixture/output"
test -f "$fixture/restore-retired/${previous##*/}$fixture/source one/file"
rm "$label/.retired"
mv "$fixture/moved source" "$fixture/source one"
sleep 1
tms "$fixture/source one" > "$fixture/output"
grep -q 'Total: 1 | OK: 1' "$fixture/output"
backup_volume="$fixture/empty"
if tms --all > "$fixture/output" 2>&1; then exit 1; fi
if tms --sources > "$fixture/output" 2>&1; then exit 1; fi
mkdir -p "$backup_volume/backup"
tms --sources > "$fixture/output"
grep -q 'Sources: 0' "$fixture/output"
# Bare invocation and informational options must not start backups.
tms > "$fixture/output"
grep -q 'With no arguments' "$fixture/output"
tms --version | grep -q '1.8.1'
tms --help > "$fixture/output"
if tms --unknown > "$fixture/output" 2>&1; then exit 1; fi
if tms --all extra > "$fixture/output" 2>&1; then exit 1; fi
if tms --sources extra > "$fixture/output" 2>&1; then exit 1; fi
echo 'PASS: backups, discovery, options, spaces, missing sources, copy failures, final report, read-only source inventory'
