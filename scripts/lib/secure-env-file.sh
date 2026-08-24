#!/bin/bash
# Shared fail-closed loader for repo-local secret environment files.

_SECURE_ENV_READER="$({ cd "$(dirname "${BASH_SOURCE[0]}")" && pwd; })/secure_env_reader.py"

load_private_env_file() {
  local secret_file="$1"
  local reader_fd reader_pid reader_status source_status
  local restore_allexport=0

  exec {reader_fd}< <(python3 "$_SECURE_ENV_READER" "$secret_file")
  reader_pid=$!

  case $- in
    *a*) ;;
    *) set -a; restore_allexport=1 ;;
  esac

  # Source the exact byte stream emitted from the already-validated descriptor.
  # shellcheck source=/dev/null
  . "/dev/fd/$reader_fd"
  source_status=$?

  exec {reader_fd}<&-
  wait "$reader_pid"
  reader_status=$?

  if [ "$restore_allexport" -eq 1 ]; then
    set +a
  fi

  if [ "$source_status" -ne 0 ]; then
    return "$source_status"
  fi
  return "$reader_status"
}
