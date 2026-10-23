# Troubleshooting

## Health check reports a missing socket

Open Codex Desktop and keep the target thread visible. Confirm `desktop_ipc_socket_path` points to `~/.codex/ipc/ipc.sock` unless your installation uses another path.

## The message stays queued

The selected thread may still be running, owner discovery may have failed, or an earlier IPC dispatch may be ambiguous. Read the local log. Do not delete the queue item until you know whether the turn reached Codex.

## Text arrives but the photo does not

Check that the media directory is writable and has enough space. The adapter accepts JPEG, PNG, GIF, and WebP, with a 25 MB limit. Failed decryption or an unsupported signature leaves a text notice in the queued message.

## WeChat receives no answer

Confirm the configured sender ID and credentials, then inspect whether the answer was persisted in state. A saved answer will be retried without starting another Codex turn.

## Safe diagnostic bundle

Share software versions and redacted error classes. Remove tokens, base URLs, user IDs, thread IDs, local paths, message text, and images before posting an issue.
