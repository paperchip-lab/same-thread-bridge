#!/bin/zsh
set -euo pipefail

agent_path="${SAME_THREAD_BRIDGE_AGENT_PATH:-$HOME/Library/LaunchAgents/com.paperchip.same-thread-bridge.plist}"
if [[ ! -f "$agent_path" ]]; then
  echo "LaunchAgent is not installed: $agent_path" >&2
  exit 1
fi
launchctl bootstrap "gui/$UID" "$agent_path" 2>/dev/null || true
launchctl kickstart -k "gui/$UID/com.paperchip.same-thread-bridge"
echo "Same Thread Bridge started."
