#!/bin/bash
# Shared fail-closed loader for repo-local secret environment files.

_SECURE_ENV_READER="$({ cd "$(dirname "${BASH_SOURCE[0]}")" && pwd; })/secure_env_reader.py"

load_private_env_file() {
  local secret_file="$1"
  local secret_payload source_status
  local restore_allexport=0

  if ! secret_payload="$(python3 "$_SECURE_ENV_READER" "$secret_file")"; then
    return 1
  fi

  case $- in
    *a*) ;;
    *) set -a; restore_allexport=1 ;;
  esac

  # Source only the bytes captured from the already-validated file descriptor.
  # shellcheck source=/dev/null
  if . /dev/stdin <<< "$secret_payload"; then
    source_status=0
  else
    source_status=$?
  fi

  if [ "$restore_allexport" -eq 1 ]; then
    set +a
  fi
  unset secret_payload
  return "$source_status"
}
