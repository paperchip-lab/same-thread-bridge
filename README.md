# Same Thread Bridge

Send mobile messages and photos into the Codex Desktop thread already doing the work.

The useful part is continuity: a photo taken beside a device appears in the current Codex window, with the same project files, instructions, and conversation context. The final reply goes back to the same WeChat sender.

> Experimental · macOS · single-user · local-first

## What it does

- Accepts text, voice transcription, and photos from one allow-listed WeChat user.
- Adds each message to one existing, visible Codex Desktop thread.
- Sends only that turn's final answer back to WeChat.
- Keeps a persistent local queue and deduplicates retries.
- Downloads and decrypts inbound photos into a private local media directory.
- Stops on ambiguous desktop delivery instead of risking a duplicate turn.

The first public release deliberately supports photo in and text answer out. It does not upload local files from Codex back to WeChat.

## Requirements

- macOS with Codex Desktop open.
- Python 3.11 or newer.
- An existing Codex thread and its local rollout file.
- Credentials for a WeChat bot endpoint you are authorized to use.

This repository does not obtain, scrape, or bypass WeChat login credentials. Account access and API compatibility remain your responsibility.

## Quick start

```bash
git clone https://github.com/paperchip-lab/same-thread-bridge.git
cd same-thread-bridge
./scripts/install.sh
```

The installer reuses a compatible system `cryptography` package when available, so an existing setup can be installed offline. If the dependency is missing, pip needs network access once. It creates:

- `~/Library/Application Support/SameThreadBridge/`
- `~/.config/same-thread-bridge/config.json`
- `~/Library/LaunchAgents/com.paperchip.same-thread-bridge.plist`

Edit the generated config and credentials files, then run:

```bash
./scripts/healthcheck.sh
./scripts/start.sh
```

Send one harmless text message first. After it appears in the selected Codex window and the reply returns to WeChat, try a photo.

## Move WeChat to another Codex window

The installer also adds a small Codex skill. Open the destination chat and say:

> Bind WeChat to this window.

The helper detects that chat and its workspace automatically. It switches only when the local bridge queue is empty, restarts the background bridge, verifies the new binding, and restores the previous config if verification fails. Only one Codex chat receives new WeChat messages at a time.

To inspect the proposed switch without changing anything:

```bash
"$HOME/Library/Application Support/SameThreadBridge/bin/bind-wechat-here" --dry-run
```

See [configuration](docs/configuration.md) for every field and [troubleshooting](docs/troubleshooting.md) when the desktop thread is not found.

## Safety model

- One configured WeChat user ID is accepted; all other senders are ignored.
- The bridge targets one explicit Codex thread.
- Rebinding moves that single target; it does not broadcast messages to multiple chats.
- Historical messages are ignored when the poll cursor is seeded for the first time.
- Credentials, state, logs, and media are ignored by Git.
- Final answers return to WeChat. Analysis, tool traces, and approval UI remain on the computer.
- Local image paths in a Codex answer are removed before the text is sent to WeChat.

Do not bind this to a Codex workspace with broader permissions than you are willing to expose to messages from the configured sender.

## Status and compatibility

The current-window route uses a local Codex Desktop IPC protocol. That protocol is internal and may change between app versions. Read [compatibility](docs/compatibility.md) before upgrading either side.

The WeChat adapter is based on an authorized bot endpoint and may also change. A green API response is not treated as proof of end-user delivery in the tests.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Architecture and failure behavior are documented in [docs/architecture.md](docs/architecture.md).

## Uninstall

```bash
./scripts/uninstall.sh
```

The default uninstall stops the background service and removes the installed program while preserving config, state, and media. Use `./scripts/uninstall.sh --purge-data` only when you also want those local files removed.

## License

MIT. This project is independent and is not affiliated with OpenAI or Tencent.
