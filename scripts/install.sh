#!/bin/zsh
set -euo pipefail

repo_dir="${0:A:h:h}"
install_dir="${SAME_THREAD_BRIDGE_HOME:-$HOME/Library/Application Support/SameThreadBridge}"
config_dir="${SAME_THREAD_BRIDGE_CONFIG_DIR:-$HOME/.config/same-thread-bridge}"
data_dir="${SAME_THREAD_BRIDGE_DATA_DIR:-$HOME/.local/share/same-thread-bridge}"
agent_path="${SAME_THREAD_BRIDGE_AGENT_PATH:-$HOME/Library/LaunchAgents/com.paperchip.same-thread-bridge.plist}"
skip_agent=0

if [[ "${1:-}" == "--no-launch-agent" ]]; then
  skip_agent=1
fi

mkdir -p "$install_dir" "$config_dir" "$data_dir" "${agent_path:h}"
chmod 700 "$config_dir" "$data_dir"

python3 -m venv --system-site-packages "$install_dir/.venv"
if "$install_dir/.venv/bin/python" -c 'import cryptography' >/dev/null 2>&1; then
  "$install_dir/.venv/bin/python" -m pip install --quiet --no-build-isolation --no-deps "$repo_dir"
else
  "$install_dir/.venv/bin/python" -m pip install --quiet "$repo_dir"
fi

if [[ ! -f "$config_dir/config.json" ]]; then
  cp "$repo_dir/config.example.json" "$config_dir/config.json"
  chmod 600 "$config_dir/config.json"
fi

if [[ ! -f "$config_dir/wechat-credentials.json" ]]; then
  printf '%s\n' '{' '  "baseUrl": "https://replace-with-authorized-endpoint.example",' '  "token": "replace-locally"' '}' > "$config_dir/wechat-credentials.json"
  chmod 600 "$config_dir/wechat-credentials.json"
fi

if (( skip_agent )); then
  echo "Installed without a LaunchAgent."
  echo "Config: $config_dir/config.json"
  exit 0
fi

escaped_program=${install_dir//&/&amp;}
escaped_program=${escaped_program//</&lt;}
escaped_program=${escaped_program//>/&gt;}
escaped_config=${config_dir//&/&amp;}
escaped_config=${escaped_config//</&lt;}
escaped_config=${escaped_config//>/&gt;}
escaped_data=${data_dir//&/&amp;}
escaped_data=${escaped_data//</&lt;}
escaped_data=${escaped_data//>/&gt;}

cat > "$agent_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.paperchip.same-thread-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>${escaped_program}/.venv/bin/same-thread-bridge</string>
    <string>--config</string>
    <string>${escaped_config}/config.json</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${escaped_data}/bridge.log</string>
  <key>StandardErrorPath</key><string>${escaped_data}/bridge.log</string>
</dict>
</plist>
PLIST

chmod 600 "$agent_path"
launchctl bootout "gui/$UID/com.paperchip.same-thread-bridge" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$agent_path"

echo "Installed Same Thread Bridge."
echo "Edit $config_dir/config.json, then run scripts/healthcheck.sh."
