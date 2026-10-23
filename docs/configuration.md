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
