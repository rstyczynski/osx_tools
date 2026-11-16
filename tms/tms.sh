#!/bin/bash

# Resolve companion scripts when sourced, independently of the caller's directory.
_TMS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

function tms() {
  case "${1:-}" in
    ''|-h|--help|--version|--sources|--status|--estimate|--explain|--scheduler|--swiftbar|--switfbar|--access)
      _tms_main "$@"; return $? ;;
    --consolidate)
      if [[ ${3:-} = --dry-run && $# = 3 ]]; then
        _tms_main "$@"; return $?
      fi
      ;;
  esac
  _tms_engine_run tms "$@"
}

function _tms_engine_run() {
  local mode="$1"
  shift
  if /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_lock.py" check; then
    if [[ "$mode" = batch ]]; then _tms_batch_main "$@"; else _tms_main "$@"; fi
    return $?
  fi
  (
    # Preserve supported shell overrides without modifying the caller's exports.
    local utility
    for utility in rsync gcp; do
      if declare -F "$utility" >/dev/null; then export -f "$utility"; fi
    done
    export backup_volume="${backup_volume:-/Volumes/Backup}"
    export _tms_service_run="${_tms_service_run:-no}"
    export _tms_skip_retired="${_tms_skip_retired:-no}"
    /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_lock.py" "$mode" "$@"
  )
}

function _tms_main() {
  local src_dir="${1:-}"
  local full="${2:-no}"

  if [[ "$src_dir" != --estimate && "$src_dir" != --status ]]; then
    printf 'backup_volume=%s\n' "${backup_volume:-/Volumes/Backup}"
  fi
  case "$src_dir" in
    ''|-h|--help) usage; return 0 ;;
    --version) printf 'TimeMachine Simplified 1.8.6\n'; return 0 ;;
    --scheduler|--swiftbar|--switfbar|--access)
      if [[ $# -gt 2 ]]; then
        printf 'Usage: tms %s [on|off|status] (access: setup|status)\n' "$src_dir" >&2
        return 2
      fi
      local feature="${src_dir#--}"
      [[ "$feature" = switfbar ]] && feature=swiftbar
      TMS_BACKUP_VOLUME="${backup_volume:-${TMS_BACKUP_VOLUME:-}}" /usr/bin/python3 \
        "$_TMS_SCRIPT_DIR/tms_manage.py" "$feature" "${2:-status}"
      return $?
      ;;
    --status)
      if [[ $# -gt 2 || ( $# -eq 2 && "${2:-}" != --json ) ]]; then
        printf 'Usage: tms --status [--json]\n' >&2
        return 2
      fi
      /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" "${@:2}"
      return $?
      ;;
    --estimate)
      if [[ $# -lt 2 || $# -gt 3 || ( $# -eq 3 && "${3:-}" != --json ) ]]; then
        printf 'Usage: tms --estimate SOURCE [--json]\n' >&2
        return 2
      fi
      shift
      /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_excludes.py" estimate "$@"
      return $?
      ;;
    --consolidate)
      if [[ $# -lt 2 || $# -gt 3 || ( $# -eq 3 && "${3:-}" != --dry-run ) ]]; then
        printf 'Usage: tms --consolidate PARENT [--dry-run]\n' >&2
        return 2
      fi
      shift
      backup_volume="${backup_volume:-/Volumes/Backup}" /usr/bin/python3 -B \
        "$_TMS_SCRIPT_DIR/tms_sources.py" "$@"
      return $?
      ;;
    --explain)
      if [[ $# -lt 2 ]]; then
        printf 'Usage: tms --explain PATH [--source ROOT]\n' >&2
        return 2
      fi
      shift
      backup_volume="${backup_volume:-/Volumes/Backup}" /usr/bin/python3 -B \
        "$_TMS_SCRIPT_DIR/tms_excludes.py" explain "$@"
      return $?
      ;;
    --sources)
      if [[ $# -ne 1 ]]; then
        printf 'Usage: tms --sources\n' >&2
        return 2
      fi
      _tms_list_sources
      return $?
      ;;
    --all)
      if [[ $# -ne 1 ]]; then
        printf 'Usage: tms --all\n' >&2
        return 2
      fi
      _tms_known_sources
      return $?
      ;;
    -*) printf 'Error: unknown option: %s\n' "$src_dir" >&2; usage >&2; return 2 ;;
  esac
  if [[ $# -gt 2 || ( $# -eq 2 && "$full" != full ) ]]; then
    printf 'Usage: tms src_dir [full]\n' >&2
    return 2
  fi

  # A manual backup of a pending parent continues its recorded consolidation.
  # Batch/service callers keep their existing skip/locking behavior.
  if [[ "$full" = no && ${_tms_skip_retired:-no} != yes && ${_tms_service_run:-no} != yes ]]; then
    local pending_source pending_label
    pending_source=$(/usr/bin/python3 -B -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$src_dir") || return 1
    pending_label="${backup_volume:-/Volumes/Backup}/backup/${pending_source##*/}"
    if _tms_is_consolidating "$pending_label" && [[ ${_tms_consolidation_parent:-} != "$pending_source" ]]; then
      printf 'Resuming pending consolidation; reusing the recorded snapshot.\n'
      backup_volume="${backup_volume:-/Volumes/Backup}" /usr/bin/python3 -B \
        "$_TMS_SCRIPT_DIR/tms_sources.py" "$pending_source"
      return $?
    fi
  fi

  # Batch callers own the arrays; standalone calls create a one-row report.
  if [[ ${_tms_collect_report:-no} != yes ]]; then
    local _tms_report_count=0
    local _tms_report_excluded=()
    local _tms_report_sources=() _tms_report_statuses=() _tms_report_changes=()
    local _tms_report_types=() _tms_report_seconds=() _tms_report_details=()
    local _tms_report_warning_logs=()
  fi
  local _tms_result_excluded=- _tms_result_changed=- _tms_result_type=- _tms_result_detail='Backup failed (see output)'
  local _tms_result_warning_log=
  local started=$SECONDS status=FAILED result=1
  if _tms_backup_source "$src_dir" "$full"; then
    status=OK
    result=0
  else
    result=$?
    if [[ "$result" = 4 ]]; then
      status=PARTIAL
      result=0
    elif [[ "$result" = 3 && ${_tms_skip_retired:-no} = yes ]]; then
      status=SKIPPED
      result=0
    fi
  fi
  _tms_report_add "$src_dir" "$status" "$_tms_result_changed" \
    "$_tms_result_type" "$((SECONDS - started))" "$_tms_result_detail" "$_tms_result_excluded" "$_tms_result_warning_log"
  if [[ ${_tms_collect_report:-no} != yes ]]; then
    _tms_report_print
  fi
  return "$result"
}

# Marker presence is authoritative, even when its contents are damaged.
function _tms_is_retired() {
  [[ -e "$1/.retired" || -L "$1/.retired" ]]
}

function _tms_is_consolidating() {
  [[ -e "$1/.consolidating" || -L "$1/.consolidating" ]]
}

function _tms_retired_detail() {
  /usr/bin/python3 -B - "$1/.retired" <<'PY'
import json
import sys
try:
    with open(sys.argv[1]) as stream:
        replacement = json.load(stream)["replacement"]
    if not isinstance(replacement, str) or not replacement.startswith("/"):
        raise ValueError("Invalid replacement")
    print("Source retired; replacement: " + replacement)
except (OSError, ValueError, KeyError, TypeError):
    print("Source retired; retirement metadata unreadable or invalid")
PY
}

# Compare canonical roots at directory boundaries, including legacy labels.
function _tms_check_overlap() {
  local source="$1" root="${backup_volume:-/Volumes/Backup}/backup"
  local label known conflict=no ancestor=
  for label in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [[ -d "$label" && ! -L "$label" ]] || continue
    _tms_is_retired "$label" && continue
    _tms_is_consolidating "$label" && continue
    known=$(_tms_source_for_label "$label") || continue
    known=$(/usr/bin/python3 -B -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$known") || return 1
    [[ "$source" = "$known" ]] && continue
    # Only the validated consolidation runner may temporarily cover descendants.
    if [[ ${_tms_consolidation_parent:-} = "$source" && "${known%/}/" = "${source%/}/"* ]]; then
      continue
    fi
    if [[ "${source%/}/" = "${known%/}/"* || "${known%/}/" = "${source%/}/"* ]]; then
      if [[ "$conflict" = no ]]; then
        printf 'Error: source overlaps existing active backup sources:\n' >&2
      fi
      printf '  %s (label: %s)\n' "$known" "${label##*/}" >&2
      conflict=yes
      [[ "${source%/}/" = "${known%/}/"* ]] && ancestor=$known
    fi
  done
  if [[ "$conflict" = yes ]]; then
    printf 'No snapshot created. Existing backups remain active and unchanged.\n' >&2
    if [[ -n "$ancestor" ]]; then
      printf '\nThis path is already covered. Use the existing parent source:\n  tms %q\n' "$ancestor" >&2
    else
      printf '\nTo replace child sources with this parent while keeping their history:\n' >&2
      printf '  Preview: tms --consolidate %q --dry-run\n' "$source" >&2
      printf '  Run:     tms --consolidate %q\n' "$source" >&2
      printf 'The command checks coverage, creates or resumes the parent snapshot, then retires child sources.\n' >&2
    fi
    return 1
  fi
}

function _tms_backup_source() {
  local src_dir="$1" full="$2"
  if ! /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_lock.py" check; then
    _tms_result_detail='Engine lock required; invoke backups through tms'
    printf '%s\n' "$_tms_result_detail" >&2
    return 1
  fi
  /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" phase INIT "$src_dir"
  _tms_source_header "$src_dir"
  # Resolve aliases even if the source is now missing: retirement still applies.
  src_dir=$(/usr/bin/python3 -B -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$src_dir") || return 1
  local backup_dir="${backup_volume:-/Volumes/Backup}/backup/$(basename "$src_dir")"
  local known_source
  if [[ -f "$backup_dir/.source" ]]; then
    IFS= read -r known_source < "$backup_dir/.source"
    if [[ "$known_source" != "$src_dir" ]]; then
      _tms_result_detail="Label belongs to $known_source"
      printf 'Error: backup label already belongs to %s\n' "$known_source" >&2
      return 1
    fi
  fi
  if _tms_is_retired "$backup_dir"; then
    _tms_result_detail=$(_tms_retired_detail "$backup_dir")
    printf '%s\n' "$_tms_result_detail" >&2
    printf 'No backup created. History remains available for restore.\nRetirement marker: %s/.retired\nTo reactivate deliberately, remove this marker after reviewing overlap with the replacement.\n' "$backup_dir" >&2
    return 3
  fi
  if _tms_is_consolidating "$backup_dir" && [[ ${_tms_consolidation_parent:-} != "$src_dir" ]]; then
    _tms_result_detail='Parent consolidation incomplete; child sources remain active'
    printf '%s\nResume with: tms --consolidate %q\n' "$_tms_result_detail" "$src_dir" >&2
    return 3
  fi
  if [[ ! -d "$src_dir" ]]; then
    _tms_result_detail='Source directory missing'
    printf 'Error: source directory does not exist: %s\n' "$src_dir" >&2
    return 1
  fi
  _tms_result_detail='Overlapping active backup sources (see output)'
  _tms_check_overlap "$src_dir" || return 1

  #
  # Check required utilities
  #
  if ! command -v gcp >/dev/null 2>&1; then
    _tms_result_detail='Required gcp utility missing'
    echo "Error: gcp (GNU cp from coreutils) is not installed. Install it with 'brew install coreutils'."
    return 1
  fi

  #
  # Wait for backup restore_destination to be available (e.g., external disk)
  #
  while [ ! -d "$backup_dir" ]; do
    mkdir -p "$backup_dir" >/dev/null 2>&1
    echo "Waiting for $backup_dir..."
    sleep 5
  done

  #
  # Mark full backup directory
  #
  local backup_type=incremental
  local timestamp
  timestamp=$(date +%Y-%m-%dT%H%M%S)
  local backup_dst="$backup_dir/$timestamp"
  # full backup when requested or when initial backup is taken
  if [ "$full" = "full" ] || [ ! -d "$backup_dir/latest" ]; then
    backup_dst="${backup_dst}.full"
    backup_type=full
  fi

  local resuming=no
  if [[ ${_tms_consolidation_parent:-} = "$src_dir" && -n ${_tms_resume_snapshot:-} ]]; then
    backup_dst=$_tms_resume_snapshot
    if [[ "${backup_dst%/*}" != "$backup_dir" || -L "$backup_dst" ]]; then
      _tms_result_detail='Invalid consolidation snapshot path'
      return 1
    fi
    backup_type=incremental
    [[ "$backup_dst" = *.full ]] && backup_type=full
    if [[ -d "$backup_dst" ]]; then
      [[ -f "$backup_dst.start" ]] || return 1
      resuming=yes
      printf 'Resuming existing snapshot: %s\n' "$backup_dst"
    fi
  fi

  _tms_result_type="$backup_type"
  [[ "$resuming" = yes ]] && _tms_result_type=resume
  _tms_result_detail="Cannot create snapshot $timestamp"

  # Never merge two snapshots created within the same second.
  if [[ "$resuming" != yes ]]; then
    mkdir "$backup_dst" || return 1
    touch "$backup_dst.start" || return 1
  fi

  _tms_result_detail="File discovery or snapshot linking failed"

  #
  # Process file discovery
  #
  local latest_marker=
  if [[ "$backup_type" != full && "$resuming" != yes ]]; then
    latest_marker="$(realpath "$backup_dir/latest").start"
    /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" phase "CREATE HARDLINK" "$src_dir"
    echo "Creating hard links..."
    gcp -al "$backup_dir/latest/." "$backup_dst/" || return 1
  fi

  /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" phase INIT "$src_dir"
  echo "Applying exclusion rules and discovering files..."
  local policy_args=(prepare "$src_dir" "$backup_dst")
  [[ -n "$latest_marker" ]] && policy_args+=(--marker "$latest_marker")
  [[ "$resuming" = yes ]] && policy_args+=(--resume)
  _tms_result_detail='Exclusion scan failed (see output)'
  _tms_result_excluded=$(/usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_excludes.py" "${policy_args[@]}") || return 1
  printf 'Excluded files: %s\n' "$_tms_result_excluded"

  #
  # Backup files
  #
  _tms_result_detail='Copy failed (see output)'
  local progress_option=--info=progress2
  /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" phase COPY "$src_dir" "$backup_dst.rules.json"
  if [[ "$resuming" = yes ]]; then
    echo 'Resuming transfer; files already present are checked and reused...'
  else
    echo "Copying $backup_type backup..."
  fi
  # Keep progress visible and retain the exact transfer statistics for reporting.
  _tms_result_warning_log="$backup_dst.warnings"
  : > "$backup_dst.stats" || return 1
  LC_ALL=C rsync -a --copy-links "$progress_option" --stats \
    --from0 --files-from="$backup_dst.files0" / "$backup_dst" 2>"$_tms_result_warning_log" | /usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_status.py" copy "$backup_dst.stats"
  local copy_status=("${PIPESTATUS[@]}")
  local transferred
  transferred=$(awk '/^Number of regular files transferred:/ {gsub(/,/, "", $NF); print $NF}' "$backup_dst.stats")
  [[ "$transferred" =~ ^[0-9]+$ ]] && _tms_result_changed="$transferred"
  local partial=no
  if [[ "${copy_status[1]}" != 0 ]]; then
    _tms_result_detail="Copy failed: cannot save transfer output (exit ${copy_status[1]})"
    return 1
  fi
  case "${copy_status[0]}" in
    0) rm -f "$backup_dst.partial" || return 1 ;;
    23|24)
      partial=yes
      printf 'rsync_exit=%s\n' "${copy_status[0]}" > "$backup_dst.partial" || return 1
      ;;
    *) _tms_result_detail="Copy failed (rsync exit ${copy_status[0]}; see warnings below)"; return 1 ;;
  esac
  _tms_result_detail='Failed to publish snapshot metadata'

  printf '%s\n' "$src_dir" > "$backup_dir/.source" || return 1
  rm -f "$backup_dir/latest" || return 1
  ln -s "$backup_dst" "$backup_dir/latest" || return 1
  _tms_result_detail="${backup_dst##*/}"
  if [[ "$partial" = yes ]]; then
    _tms_result_detail+="; done with warnings (rsync ${copy_status[0]})"
    return 4
  fi
  return 0
}

# Bash's dynamic scope lets batch and standalone callers own report arrays.
function _tms_report_add() {
  _tms_report_sources+=("$1")
  _tms_report_statuses+=("$2")
  _tms_report_changes+=("$3")
  _tms_report_types+=("$4")
  _tms_report_seconds+=("$5")
  _tms_report_details+=("$6")
  _tms_report_excluded+=("${7:--}")
  _tms_report_warning_logs+=("${8:-}")
  _tms_report_count=$((_tms_report_count + 1))
}

function _tms_report_print() {
  if [[ ${_tms_service_run:-no} = yes ]]; then
    _tms_report_render
    return $?
  fi
  local report_file
  report_file=$(mktemp "${TMPDIR:-/tmp}/tms-report.XXXXXX") || { _tms_report_render; return; }
  _tms_report_render | tee "$report_file"
  if ! TMS_BACKUP_VOLUME="${backup_volume:-/Volumes/Backup}" /usr/bin/python3 -B \
    "$_TMS_SCRIPT_DIR/tms-service.py" record-terminal < "$report_file"; then
    printf 'Warning: backup report could not be saved for SwiftBar.\n' >&2
  fi
  rm -f "$report_file"
}

function _tms_report_render() {
  local width=6 i source detail ok=0 failed=0 skipped=0 partial=0 warning_log
  for ((i=0; i<_tms_report_count; i++)); do
    source=${_tms_report_sources[i]}
    source=${source//$'\n'/\\n}
    source=${source//$'\r'/\\r}
    source=${source//$'\t'/\\t}
    (( ${#source} > width )) && width=${#source}
  done
  printf '\n================================================================\n'
  printf 'FINAL BACKUP REPORT\n'
  printf '%-*s | %-7s | %7s | %8s | %-11s | %7s | %s\n' \
    "$width" SOURCE STATUS CHANGED EXCLUDED TYPE SECONDS 'SNAPSHOT / NOTE'
  for ((i=0; i<_tms_report_count; i++)); do
    source=${_tms_report_sources[i]//$'\n'/\\n}
    source=${source//$'\r'/\\r}
    source=${source//$'\t'/\\t}
    detail=${_tms_report_details[i]//$'\n'/\\n}
    detail=${detail//$'\r'/\\r}
    detail=${detail//$'\t'/\\t}
    printf '%-*s | %-7s | %7s | %8s | %-11s | %7s | %s\n' \
      "$width" "$source" "${_tms_report_statuses[i]}" "${_tms_report_changes[i]}" "${_tms_report_excluded[i]}" \
      "${_tms_report_types[i]}" "${_tms_report_seconds[i]}" "$detail"
    case "${_tms_report_statuses[i]}" in
      OK) ok=$((ok + 1)) ;;
      FAILED) failed=$((failed + 1)) ;;
      SKIPPED) skipped=$((skipped + 1)) ;;
      PARTIAL) partial=$((partial + 1)) ;;
    esac
  done
  printf 'Total: %s | OK: %s | FAILED: %s | SKIPPED: %s | PARTIAL: %s\n' \
    "$_tms_report_count" "$ok" "$failed" "$skipped" "$partial"
  printf 'Changed = regular files transferred; excluded = unique excluded files in the source or carried snapshot; - = unavailable.\n'
  for ((i=0; i<_tms_report_count; i++)); do
    warning_log=${_tms_report_warning_logs[i]:-}
    if [[ -n "$warning_log" && -s "$warning_log" ]]; then
      printf '\nTRANSFER DIAGNOSTICS: %s\nSaved in: %s\n' "${_tms_report_sources[i]}" "$warning_log"
      cat "$warning_log"
    fi
  done
}

function _tms_source_header() {
  local divider='================================================================'
  printf '\n%s\nSource: %s\nDestination: %s\n%s\n' \
    "$divider" "$1" "${backup_volume:-/Volumes/Backup}" "$divider"
}

# Read explicit metadata, or recover the absolute source from a legacy snapshot.
function _tms_source_for_label() {
  local label_dir="$1" snapshot current child only_child count
  local label="${1##*/}"
  if [[ -f "$label_dir/.source" ]]; then
    IFS= read -r current < "$label_dir/.source" || [[ -n "$current" ]] || return 1
    [[ "$current" = /* && "${current##*/}" = "$label" ]] || return 1
    printf '%s\n' "$current"
    return 0
  fi

  local snapshots=() snapshot_count=0
  if [[ -d "$label_dir/latest" ]]; then
    snapshots=("$label_dir/latest")
    snapshot_count=1
  else
    for child in "$label_dir"/????-??-??T*; do
      if [[ -d "$child" ]]; then
        snapshots+=("$child")
        snapshot_count=$((snapshot_count + 1))
      fi
    done
  fi
  local i
  for ((i=snapshot_count-1; i>=0; i--)); do
    snapshot="${snapshots[i]}"
    current="$snapshot"
    # rsync stores the absolute path below the snapshot root.
    while :; do
      if [[ "$current" != "$snapshot" && "${current##*/}" = "$label" ]]; then
        printf '%s\n' "${current#"$snapshot"}"
        return 0
      fi
      count=0
      only_child=
      for child in "$current"/* "$current"/.[!.]* "$current"/..?*; do
        [[ -d "$child" && ! -L "$child" ]] || continue
        only_child="$child"
        count=$((count + 1))
      done
      [[ "$count" = 1 ]] || break
      current="$only_child"
    done
  done
  return 1
}

function _tms_list_sources() {
  local root="${backup_volume:-/Volumes/Backup}/backup"
  if [[ ! -d "$root" ]]; then
    printf 'Error: no backup directory at %s.\n' "$root" >&2
    return 1
  fi
  local labels=() sources=() known=()
  local label_dir source_dir count=0 width=6 i state size files last size_output
  for label_dir in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [[ -d "$label_dir" && ! -L "$label_dir" ]] || continue
    labels+=("$label_dir")
    if source_dir=$(_tms_source_for_label "$label_dir"); then
      known+=(yes)
    else
      source_dir="${label_dir##*/} (unknown path)"
      known+=(no)
    fi
    # Escape control characters so each entry stays on one output line.
    source_dir=${source_dir//$'\n'/\\n}
    source_dir=${source_dir//$'\r'/\\r}
    source_dir=${source_dir//$'\t'/\\t}
    (( ${#source_dir} > width )) && width=${#source_dir}
    sources+=("$source_dir")
    count=$((count + 1))
  done
  printf '\nKNOWN BACKUP SOURCES\n'
  printf '%-*s | %-11s | %10s | %8s | %s\n' \
    "$width" SOURCE STATE SIZE FILES 'LAST BACKUP'
  for ((i=0; i<count; i++)); do
    label_dir=${labels[i]}
    size=- files=- last=- state=UNKNOWN
    if [[ -d "$label_dir/latest" ]]; then
      last=$(readlink "$label_dir/latest")
      last=${last##*/}
    fi
    if _tms_is_retired "$label_dir"; then
      state=RETIRED
    elif _tms_is_consolidating "$label_dir"; then
      state=PENDING
    elif [[ ${known[i]} = yes ]]; then
      # Resolve again rather than using the escaped display path for filesystem access.
      source_dir=$(_tms_source_for_label "$label_dir") || source_dir=
      if [[ ! -d "$source_dir" ]]; then
        state=MISSING
      else
        state=READY
        [[ "$last" = - ]] && state=NO-BACKUP
        [[ "$last" != - && -f "$label_dir/$last.partial" ]] && state=PARTIAL
        if size_output=$(du -sk "$source_dir" 2>/dev/null); then
          size=$(awk 'NR==1 {n=$1; split("KiB MiB GiB TiB PiB", u, " "); i=1; while(n>=1024 && i<5){n/=1024;i++} printf "%.1f %s", n, u[i]}' <<< "$size_output")
        else
          state=UNREADABLE
        fi
        if files=$(set -o pipefail; find "$source_dir" -type f -print0 2>/dev/null | LC_ALL=C tr -cd '\000' | wc -c); then
          files=${files//[[:space:]]/}
        else
          files=-
          state=UNREADABLE
        fi
      fi
    fi
    last=${last//$'\n'/\\n}
    last=${last//$'\r'/\\r}
    last=${last//$'\t'/\\t}
    printf '%-*s | %-11s | %10s | %8s | %s\n' \
      "$width" "${sources[i]}" "$state" "$size" "$files" "$last"
  done
  printf 'Sources: %s. Size = allocated source disk usage; files = regular source files.\n' "$count"
  printf 'Read-only inventory; no backup is started. Scans do not follow symbolic links.\n'
}

function _tms_known_sources() {
  local _tms_collect_report=yes
  local _tms_report_count=0
  local _tms_report_excluded=()
  local _tms_report_sources=() _tms_report_statuses=() _tms_report_changes=()
  local _tms_report_types=() _tms_report_seconds=() _tms_report_details=()
  local _tms_report_warning_logs=()
  local root="${backup_volume:-/Volumes/Backup}/backup"
  local label_dir source_dir count=0 failures=0 retired=0 pending=0
  if [[ ! -d "$root" ]]; then
    printf 'Error: no known backups at %s. Run tms /source/directory first.\n' "$root" >&2
    _tms_report_print
    return 1
  fi
  for label_dir in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [[ -d "$label_dir" && ! -L "$label_dir" ]] || continue
    count=$((count + 1))
    if _tms_is_consolidating "$label_dir"; then
      source_dir=$(_tms_source_for_label "$label_dir") || source_dir=${label_dir##*/}
      _tms_report_add "$source_dir" SKIPPED - - 0 'Parent consolidation incomplete; resume with tms --consolidate PARENT'
      pending=$((pending + 1))
      continue
    fi
    if _tms_is_retired "$label_dir"; then
      source_dir=$(_tms_source_for_label "$label_dir") || source_dir=${label_dir##*/}
      _tms_report_add "$source_dir" SKIPPED - - 0 "$(_tms_retired_detail "$label_dir")"
      retired=$((retired + 1))
      continue
    fi
    if ! source_dir=$(_tms_source_for_label "$label_dir"); then
      _tms_source_header "${label_dir##*/} (original path unknown)"
      printf 'Skipping %s: cannot determine original source.\n' "${label_dir##*/}" >&2
      _tms_report_add "${label_dir##*/}" SKIPPED - - 0 'Original source unknown'
      failures=$((failures + 1))
      continue
    fi
    if ! tms "$source_dir"; then
      failures=$((failures + 1))
    fi
  done
  _tms_report_print
  printf 'Known-source backups: %s succeeded, %s failed or skipped; %s retired; %s pending.\n' "$((count - failures - retired - pending))" "$failures" "$retired" "$pending"
  [[ "$count" -gt 0 && "$failures" -eq 0 ]]
}

function tms_restore() {

  local backup_label=$1 #notebook
  local backup_src="$backup_volume/backup/$backup_label"

  local backup_time_label=$2 # 2025-05-06T205100

  local restore_dest=${3:-/Volumes/Data/Restore} #/Volumes/Data/Restore

  if [[ -z "$backup_time_label" ]]; then
    echo "Error: Backup timestamp label missing. Exiting."
    return 1
  fi

  # Exit if restore_destination exists
  if [[ ! -e "$backup_src/$backup_time_label" ]]; then
    echo "Error: restore_destination '$backup_src/$backup_time_label' does not exist. Nothing to do."
    return 1
  fi

  # Exit if restore_destination exists
  if [[ -e "$restore_dest" ]]; then
    echo "Error: restore_destination '$restore_dest' already exists. Exiting to avoid overwriting."
    return 1
  fi

  # Create restore_destination
  mkdir -p "$restore_dest" || {
    echo "Error: Failed to create restore_destination directory '$restore_dest'"
    return 1
  }


  echo "Restoring '$backup_src/$backup_time_label' -> '$restore_dest/'"
  rsync -a --info=progress2 "$backup_src/$backup_time_label" "$restore_dest/" || return 1
  if [[ -f "$backup_src/$backup_time_label.rules.json" ]]; then
    cp "$backup_src/$backup_time_label.rules.json" "$restore_dest/tms-exclusions.json" || return 1
  fi

}

function usage() {
  cat <<EOF

TimeMachine Simplified 1.8.6

Usage:
backup_volume=/Volumes/Backup
tms src_dir [full]
tms --all
tms --sources
tms --estimate SOURCE [--json]
tms --status [--json]
tms --consolidate PARENT [--dry-run]
tms --explain PATH [--source ROOT]
tms --scheduler on|off|status
tms --swiftbar on|off|status
tms --access setup|status
tms --help
tms --version

All backup entry points share the engine lock; concurrent writers are rejected.
Use --all to back up every source already known at the destination.
With no arguments, display this help without starting a backup.
Current backup_volume: ${backup_volume:-/Volumes/Backup}

tms_restore backup_label timestamp_label target_directory
EOF
}


function tms_batch() {
  _tms_engine_run batch "${1:-$HOME/etc/tms_config}"
}

function _tms_batch_main() {
  local _tms_skip_retired=yes
  local config_file=${1:-"$HOME/etc/tms_config"}
  if [[ ! -f "$config_file" ]]; then
    printf 'Error: configuration file not found: %s\n' "$config_file" >&2
    return 1
  fi
  local _tms_collect_report=yes
  local _tms_report_count=0
  local _tms_report_excluded=()
  local _tms_report_sources=() _tms_report_statuses=() _tms_report_changes=()
  local _tms_report_types=() _tms_report_seconds=() _tms_report_details=()
  local _tms_report_warning_logs=()
  local line failures=0
  printf 'Processing batch backup from: %s\n' "$config_file"
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Trim surrounding whitespace without interpreting quotes in source names.
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" = \#* ]] && continue
    if ! tms "$line"; then
      failures=$((failures + 1))
    fi
  done < "$config_file"
  _tms_report_print
  [[ "$failures" -eq 0 ]]
}



# Example usage (uncomment to run manually):

# backup_volume=/Volumes/Data
# cd; tms Documents full
# touch Documents/hello.world
# tms Documents
