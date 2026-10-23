# Configuration

Copy `config.example.json` to `~/.config/same-thread-bridge/config.json`.

Required fields:

- `thread_id`: the Codex thread that should receive mobile messages.
- `user_id`: the only WeChat sender accepted by the bridge.
- `credentials_path`: JSON containing the authorized endpoint `baseUrl` and `token`.
- `workspace`: absolute path to the Codex workspace used by the thread.
- `state_path`: persistent queue and deduplication state.
- `rollout_path`: local rollout JSONL for the target thread.

Recommended defaults:

- `codex_mode`: `desktop-ipc`.
- `source_label`: `WeChat`.
- `poll_seconds`: `2`.

Credential file shape:

```json
{
  "baseUrl": "https://your-authorized-endpoint.example",
  "token": "replace-locally"
}
```

Set both local files to owner-only permissions:

```bash
chmod 600 ~/.config/same-thread-bridge/config.json
chmod 600 ~/.config/same-thread-bridge/wechat-credentials.json
```

The repository deliberately does not automate credential acquisition.

## Current-window binding

When the installed `bind-wechat-here` skill is invoked inside a Codex chat, the helper reads `CODEX_THREAD_ID`, uses the current working directory as `workspace`, and locates the matching rollout under `~/.codex/sessions`.

It updates only:

- `thread_id`
- `workspace`
- `rollout_path`
- `initial_blocking_turn_id`, which is cleared

The switch stops if the persistent bridge queue contains a message. Before writing, it keeps one owner-only `config.json.before-bind` backup. A failed restart or verification restores that backup automatically.
