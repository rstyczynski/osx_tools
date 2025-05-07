#!/bin/bash

function tms() {
  local src_dir="$1"
  local full="${2:-no}"

  #
  # Check parameters
  #
  if [ -z "$src_dir" ]; then
    echo "Error: source path must be provided."
    echo
    usage
    return 1
  fi

  if [ -z "$backup_volume" ]; then
    cat <<EOF
Warning: backup destination path must be provided. Defaults to /Volumes/Backup. 
Use backup_volume=/my/path to set it. 

Note that destination file system must support hard links.
EOF
  fi

  #
  # pre-process parameters
  #
  src_dir=$(realpath "$1")

  #
  # Check required utilities
  #
  if ! command -v gcp >/dev/null 2>&1; then
    echo "Error: gcp (GNU cp from coreutils) is not installed. Install it with 'brew install coreutils'."
    return 1
  fi

  #
  # Wait for backup destination to be available (e.g., external disk)
  #
  local backup_dir="${backup_volume:-/Volumes/Backup}/backup/$(basename "$src_dir")"
  while [ ! -d "$backup_dir" ]; do
    mkdir -p "$backup_dir" >/dev/null 2>&1
    echo "Waiting for $backup_dir..."
    sleep 5
  done

  #
  # Mark full backup directory
  #
  local timestamp
  timestamp=$(date +%Y-%m-%dT%H%M%S)
  local backup_dst="$backup_dir/$timestamp"
  if [ "$full" = "full" ]; then
    rm -f "$backup_dir/latest"
    backup_dst="${backup_dst}.full"
  fi

  mkdir -p "$backup_dst"
  touch "$backup_dst.start"

  #
  # Process file discovery
  #
  if [ ! -d "$backup_dir/latest" ]; then
    find "$src_dir" > "$backup_dst.files"
  else
    local latest_backup
    latest_backup="$(readlink "$backup_dir/latest").start"

    echo "Creating hard links..."
    gcp -rl "$(readlink "$backup_dir/latest")"/* "$backup_dst"

    echo "Discovering changed files..."
    find "$src_dir" -cnewer "$latest_backup" -type f > "$backup_dst.files"
  fi

  #
  # Backup new and changed files
  #
  echo "Copying changed files..."
  time rsync -a --progress --files-from="$backup_dst.files" / "$backup_dst"

  rm -f "$backup_dir/latest"
  ln -s "$backup_dst" "$backup_dir/latest"
}

function usage() {
  cat <<EOF

TimeMachine Simplified 1.0

Usage: 
backup_volume=/Volumes/Backup
tms src_dir [full]
EOF
}

# Example usage (uncomment to run manually):

# backup_volume=/Volumes/Data
# cd; backup Documents full
# touch Documents/hello.world
# backup Documents