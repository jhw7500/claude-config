#!/bin/bash
# Shared fail-closed loader for repo-local secret environment files.

_SECURE_ENV_READER="$({ cd "$(dirname "${BASH_SOURCE[0]}")" && pwd; })/secure_env_reader.py"

load_private_env_file() {
  local secret_file="$1"
  local reader_fd reader_pid reader_status key value declaration attributes index
  local stream_status=0
  local -a env_names=() env_values=()

  exec {reader_fd}< <(python3 "$_SECURE_ENV_READER" "$secret_file")
  reader_pid=$!

  while IFS= read -r -d '' key <&"$reader_fd"; do
    if ! IFS= read -r -d '' value <&"$reader_fd"; then
      stream_status=1
      break
    fi
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      stream_status=1
      break
    fi
    env_names+=("$key")
    env_values+=("$value")
  done

  exec {reader_fd}<&-
  if wait "$reader_pid"; then
    reader_status=0
  else
    reader_status=$?
  fi

  if [ "$reader_status" -ne 0 ]; then
    return "$reader_status"
  fi
  if [ "$stream_status" -ne 0 ] || [ "${#env_names[@]}" -ne "${#env_values[@]}" ]; then
    echo "unsafe secret file content: invalid assignment transport" >&2
    return 1
  fi

  # Refuse shell variables whose attributes would reinterpret or reject scalar data.
  for key in "${env_names[@]}"; do
    if declaration="$(declare -p "$key" 2>/dev/null)"; then
      if [[ "$declaration" =~ ^declare\ -([^[:space:]]+) ]]; then
        attributes="${BASH_REMATCH[1]}"
        if [ "$attributes" != "-" ] && [ "$attributes" != "x" ]; then
          echo "unsafe secret file content: assignment conflicts with shell variable attributes" >&2
          return 1
        fi
      fi
    fi
  done

  # Preflight every assignment in a subshell so a failure cannot partially update the caller.
  if ! (
    for ((index = 0; index < ${#env_names[@]}; index++)); do
      printf -v "${env_names[index]}" '%s' "${env_values[index]}" 2>/dev/null || exit 1
      export "${env_names[index]}" 2>/dev/null || exit 1
    done
  ); then
    echo "unsafe secret file content: assignment cannot be exported" >&2
    return 1
  fi

  for ((index = 0; index < ${#env_names[@]}; index++)); do
    if ! printf -v "${env_names[index]}" '%s' "${env_values[index]}" \
      || ! export "${env_names[index]}"; then
      echo "unsafe secret file content: assignment cannot be exported" >&2
      return 1
    fi
  done
}
