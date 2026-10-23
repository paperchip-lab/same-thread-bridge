#!/bin/zsh
set -euo pipefail

install_dir="${SAME_THREAD_BRIDGE_HOME:-$HOME/Library/Application Support/SameThreadBridge}"
config_dir="${SAME_THREAD_BRIDGE_CONFIG_DIR:-$HOME/.config/same-thread-bridge}"
data_dir="${SAME_THREAD_BRIDGE_DATA_DIR:-$HOME/.local/share/same-thread-bridge}"
agent_path="${SAME_THREAD_BRIDGE_AGENT_PATH:-$HOME/Library/LaunchAgents/com.paperchip.same-thread-bridge.plist}"

launchctl bootout "gui/$UID/com.paperchip.same-thread-bridge" 2>/dev/null || true
rm -f "$agent_path"
rm -rf "$install_dir"

if [[ "${1:-}" == "--purge-data" ]]; then
  rm -rf "$config_dir" "$data_dir"
  echo "Removed the program, config, state, logs, and media."
else
  echo "Removed the program. Config and local data were preserved."
fi
