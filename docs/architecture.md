# Architecture

```text
WeChat sender
    │ text / voice transcript / encrypted photo
    ▼
WeChat adapter
    │ allow-list + decrypt + normalize
    ▼
Persistent local queue
    │ stable client message id + deduplication
    ▼
Codex Desktop IPC owner
    │ start a turn in the selected visible thread
    ▼
Thread rollout
    │ final answer for that exact turn
    ▼
WeChat adapter → original sender
```

## Delivery rules

The first poll seeds the server cursor and drops historical messages. New messages are written to local state before Codex receives them. A generated answer is also persisted before the WeChat send call, so a transient network failure does not regenerate the answer.

Each injected turn carries a stable `clientUserMessageId`. If the local socket disconnects after dispatch, the bridge searches the rollout for that ID. When acceptance still cannot be proven, the queue item remains in an ambiguous state and is not resent automatically.

## Images

Inbound image bytes are downloaded, decrypted, type-checked, size-limited, written with mode `0600`, and passed to Codex as a native local image attachment.

The first public release does not upload local files back to WeChat. Local image paths in a Codex answer are removed before the remaining text is sent.

## Backends

`desktop-ipc` is the intended backend because it targets the visible Codex Desktop thread. `queue`, `app-server`, and isolated `exec` remain as experimental recovery paths and are not part of the basic quick start.
