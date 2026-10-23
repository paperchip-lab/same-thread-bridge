#!/bin/zsh
set -euo pipefail

install_dir="${SAME_THREAD_BRIDGE_HOME:-$HOME/Library/Application Support/SameThreadBridge}"
config_dir="${SAME_THREAD_BRIDGE_CONFIG_DIR:-$HOME/.config/same-thread-bridge}"
executable="$install_dir/.venv/bin/same-thread-bridge"

if [[ ! -x "$executable" ]]; then
  echo "Bridge executable is missing. Run scripts/install.sh first." >&2
  exit 1
fi
if [[ ! -f "$config_dir/config.json" ]]; then
  echo "Config is missing: $config_dir/config.json" >&2
  exit 1
fi

"$executable" --config "$config_dir/config.json" --health
