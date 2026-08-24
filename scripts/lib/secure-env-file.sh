#!/bin/bash
# Shared fail-closed metadata gate for repo-local secret environment files.

require_private_env_file() {
  local secret_file="$1"
  local metadata owner_uid mode link_count

  if [ -L "$secret_file" ] || [ ! -f "$secret_file" ]; then
    echo "unsafe secret file: expected a regular non-symlink file" >&2
    return 1
  fi

  if ! metadata="$(stat -c '%u %a %h' -- "$secret_file" 2>/dev/null)"; then
    echo "unsafe secret file: metadata inspection failed" >&2
    return 1
  fi
  read -r owner_uid mode link_count <<< "$metadata"

  if [ "$owner_uid" != "$(id -u)" ] || [ "$mode" != "600" ] || [ "$link_count" != "1" ]; then
    echo "unsafe secret file: require current owner, mode 0600, and one hard link" >&2
    return 1
  fi
}
