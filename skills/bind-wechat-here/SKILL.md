---
name: bind-wechat-here
description: Rebind an installed Same Thread Bridge to the current Codex Desktop chat when the user asks WeChat to follow, move, or bind to this window.
---

# Bind WeChat Here

Run the installed helper from the current chat's workspace:

```bash
"$HOME/Library/Application Support/SameThreadBridge/bin/bind-wechat-here"
```

The helper obtains the current chat ID from Codex, finds its rollout, checks that the bridge queue is empty, updates the three binding fields, restarts the bridge, and rolls back on failure.

Treat the switch as successful only when the helper returns JSON with `"ok": true`. Tell the user which workspace is now bound and that the previous chat will no longer receive new WeChat messages. Do not expose raw chat IDs or private paths unless troubleshooting requires them.

If the helper reports queued messages, leave the existing binding untouched and explain that the queued message must finish before switching.
