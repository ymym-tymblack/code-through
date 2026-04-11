#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_file="$repo_root/.devcontainer/devcontainer.json"
workspace_dir="/workspaces/$(basename "$repo_root")"
shell_name="${1:-bash}"
if [[ $# -gt 0 ]]; then
  shift
fi

cid="$(docker ps -q --filter "label=devcontainer.config_file=$config_file" | head -n 1)"

if [[ -z "$cid" ]]; then
  echo "Dev container is not running for $config_file." >&2
  echo "Run 'make dc-up' first." >&2
  exit 1
fi

exec devcontainer exec \
  --workspace-folder "$repo_root" \
  --container-id "$cid" \
  bash -lc 'cd "$1" && exec "$2" "${@:3}"' \
  bash "$workspace_dir" "$shell_name" "$@"
