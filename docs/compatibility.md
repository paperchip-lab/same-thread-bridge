# Compatibility

## Codex Desktop

The `desktop-ipc` backend relies on a local, internal Codex Desktop protocol and a rollout JSONL file. Both are implementation details and can change without notice. Re-run the text and photo tests after every Codex Desktop update.

If owner discovery fails, keep the target thread open and visible, then rerun the health check. Do not enable automatic fallback after an ambiguous IPC dispatch; it can duplicate a message.

## WeChat

The adapter expects an authorized bot endpoint compatible with the configured update and send-message calls. Interface availability, account restrictions, and platform policy can change. Use a test account and comply with the platform's rules.

## Supported baseline

- macOS
- Python 3.11+
- One local Codex Desktop thread
- One allow-listed WeChat sender
