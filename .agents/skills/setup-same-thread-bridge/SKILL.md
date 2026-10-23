---
name: setup-same-thread-bridge
description: Install, configure, validate, start, stop, or troubleshoot Same Thread Bridge on macOS. Use when a user wants WeChat text or photos to enter an existing visible Codex Desktop thread, or when that bridge is queued, disconnected, duplicated, or failing its text/photo loopback test.
---

# Set up Same Thread Bridge

Keep credentials and runtime data outside the repository. Never print tokens, sender IDs, thread IDs, private paths, message bodies, or downloaded photos into chat or logs.

## Install

1. Confirm macOS, Python 3.11+, and Codex Desktop are available.
2. Run `scripts/install.sh` from the repository.
3. Help the user edit `~/.config/same-thread-bridge/config.json` locally. Do not ask them to paste secrets into chat.
4. Check that the selected Codex thread is open and that its rollout and IPC socket paths exist.
5. Run `scripts/healthcheck.sh`.

## Validate

Use a harmless text message first. Confirm all three observations:

1. The message appears in the intended visible Codex thread with the configured source label.
2. The generated final answer returns to the same WeChat sender.
3. Sending the same update again does not create a duplicate turn.

Then send one non-sensitive photo. Confirm the image appears as a native attachment in the same Codex thread. The public bridge returns text only and does not upload local files back to WeChat.

## Diagnose

Read `docs/troubleshooting.md`. Prefer the persisted queue and redacted error class over repeating a message. If IPC acceptance is ambiguous, stop and inspect the rollout for the stable client message ID. Never switch to another delivery backend automatically after an ambiguous dispatch.

## Remove

Run `scripts/uninstall.sh` to remove the service while preserving local config and data. Use `scripts/uninstall.sh --purge-data` only after the user explicitly asks to delete config, state, logs, and downloaded media.
