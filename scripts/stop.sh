#!/bin/zsh
set -euo pipefail

launchctl bootout "gui/$UID/com.paperchip.same-thread-bridge" 2>/dev/null || true
echo "Same Thread Bridge stopped."
