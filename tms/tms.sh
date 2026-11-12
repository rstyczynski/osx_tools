#!/bin/bash

# Resolve companion scripts when sourced, independently of the caller's directory.
_TMS_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

function tms() {
  local src_dir="${1:-}"
  local full="${2:-no}"

  printf 'backup_volume=%s\n' "${backup_volume:-/Volumes/Backup}"
  case "$src_dir" in
    ''|-h|--help) usage; return 0 ;;
    --version) printf 'TimeMachine Simplified 1.6.0\n'; return 0 ;;
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

  # Batch callers own the arrays; standalone calls create a one-row report.
  if [[ ${_tms_collect_report:-no} != yes ]]; then
    local _tms_report_count=0
    local _tms_report_excluded=()
    local _tms_report_sources=() _tms_report_statuses=() _tms_report_changes=()
    local _tms_report_types=() _tms_report_seconds=() _tms_report_details=()
  fi
  local _tms_result_excluded=- _tms_result_changed=- _tms_result_type=- _tms_result_detail='Backup failed (see output)'
  local started=$SECONDS status=FAILED result=1
  if _tms_backup_source "$src_dir" "$full"; then
    status=OK
    result=0
  fi
  _tms_report_add "$src_dir" "$status" "$_tms_result_changed" \
    "$_tms_result_type" "$((SECONDS - started))" "$_tms_result_detail" "$_tms_result_excluded"
  if [[ ${_tms_collect_report:-no} != yes ]]; then
    _tms_report_print
  fi
  return "$result"
}

function _tms_backup_source() {
  local src_dir="$1" full="$2"
  _tms_source_header "$src_dir"
  if [[ ! -d "$src_dir" ]]; then
    _tms_result_detail='Source directory missing'
    printf 'Error: source directory does not exist: %s\n' "$src_dir" >&2
    return 1
  fi
  src_dir=$(realpath "$src_dir") || return 1

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
  local backup_dir
  backup_dir="${backup_volume:-/Volumes/Backup}/backup/$(basename "$src_dir")"
  local known_source
  if [[ -f "$backup_dir/.source" ]]; then
    IFS= read -r known_source < "$backup_dir/.source"
    if [[ "$known_source" != "$src_dir" ]]; then
      _tms_result_detail="Label belongs to $known_source"
      printf 'Error: backup label already belongs to %s\n' "$known_source" >&2
      return 1
    fi
  fi
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

  _tms_result_type="$backup_type"
  _tms_result_detail="Cannot create snapshot $timestamp"

  # Never merge two snapshots created within the same second.
  mkdir "$backup_dst" || return 1
  touch "$backup_dst.start" || return 1

  _tms_result_detail="File discovery or snapshot linking failed"

  #
  # Process file discovery
  #
  local latest_marker=
  if [[ "$backup_type" != full ]]; then
    latest_marker="$(realpath "$backup_dir/latest").start"
    echo "Creating hard links..."
    gcp -al "$backup_dir/latest/." "$backup_dst/" || return 1
  fi

  echo "Applying exclusion rules and discovering files..."
  local policy_args=(prepare "$src_dir" "$backup_dst")
  [[ -n "$latest_marker" ]] && policy_args+=(--marker "$latest_marker")
  _tms_result_detail='Exclusion scan failed (see output)'
  _tms_result_excluded=$(/usr/bin/python3 -B "$_TMS_SCRIPT_DIR/tms_excludes.py" "${policy_args[@]}") || return 1
  printf 'Excluded files: %s\n' "$_tms_result_excluded"

  #
  # Backup files
  #
  _tms_result_detail='Copy failed (see output)'
  local progress_option=--progress
  [[ "$backup_type" = full ]] && progress_option=--info=progress2
  echo "Copying $backup_type backup..."
  # Keep progress visible and retain the exact transfer statistics for reporting.
  LC_ALL=C rsync -a --copy-links "$progress_option" --stats \
    --from0 --files-from="$backup_dst.files0" / "$backup_dst" | tee "$backup_dst.stats"
  local copy_status=("${PIPESTATUS[@]}")
  local transferred
  transferred=$(awk '/^Number of regular files transferred:/ {gsub(/,/, "", $NF); print $NF}' "$backup_dst.stats")
  [[ "$transferred" =~ ^[0-9]+$ ]] && _tms_result_changed="$transferred"
  [[ "${copy_status[0]}" = 0 && "${copy_status[1]}" = 0 ]] || return 1
  _tms_result_detail='Failed to publish snapshot metadata'

  printf '%s\n' "$src_dir" > "$backup_dir/.source" || return 1
  rm -f "$backup_dir/latest" || return 1
  ln -s "$backup_dst" "$backup_dir/latest" || return 1
  _tms_result_detail="${backup_dst##*/}"
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
  _tms_report_count=$((_tms_report_count + 1))
}

function _tms_report_print() {
  local width=6 i source detail ok=0 failed=0 skipped=0
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
    esac
  done
  printf 'Total: %s | OK: %s | FAILED: %s | SKIPPED: %s\n' \
    "$_tms_report_count" "$ok" "$failed" "$skipped"
  printf 'Changed = regular files transferred; excluded = unique excluded files in the source or carried snapshot; - = unavailable.\n'
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
    if [[ ${known[i]} = yes ]]; then
      # Resolve again rather than using the escaped display path for filesystem access.
      source_dir=$(_tms_source_for_label "$label_dir") || source_dir=
      if [[ ! -d "$source_dir" ]]; then
        state=MISSING
      else
        state=READY
        [[ "$last" = - ]] && state=NO-BACKUP
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
  local root="${backup_volume:-/Volumes/Backup}/backup"
  local label_dir source_dir count=0 failures=0
  if [[ ! -d "$root" ]]; then
    printf 'Error: no known backups at %s. Run tms /source/directory first.\n' "$root" >&2
    _tms_report_print
    return 1
  fi
  for label_dir in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [[ -d "$label_dir" && ! -L "$label_dir" ]] || continue
    count=$((count + 1))
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
  printf 'Known-source backups: %s succeeded, %s failed or skipped.\n' "$((count - failures))" "$failures"
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

TimeMachine Simplified 1.6.0

Usage:
backup_volume=/Volumes/Backup
tms src_dir [full]
tms --all
tms --sources
tms --explain PATH [--source ROOT]
tms --scheduler on|off|status
tms --swiftbar on|off|status
tms --access setup|status
tms --help
tms --version

Use --all to back up every source already known at the destination.
With no arguments, display this help without starting a backup.
Current backup_volume: ${backup_volume:-/Volumes/Backup}

tms_restore backup_label timestamp_label target_directory
EOF
}


function tms_batch() {
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
