#!/usr/bin/env python3
"""Route WeChat text and images into one visible Codex Desktop thread."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path(
    os.environ.get(
        "SAME_THREAD_BRIDGE_CONFIG",
        Path.home() / ".config" / "same-thread-bridge" / "config.json",
    )
).expanduser()
WECHAT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
WECHAT_CHANNEL_VERSION = "2.4.8"
WECHAT_BOT_AGENT = "SameThreadBridge/0.2.0"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")


class BridgeError(RuntimeError):
    pass


class AmbiguousIpcDelivery(BridgeError):
    """The desktop may have accepted a turn, so the bridge must not resend it."""


class IpcRejected(BridgeError):
    """The desktop returned an explicit error before accepting the requested turn."""


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def message_key(sender: str, context_token: str, index: int, content: str) -> str:
    raw = f"{sender}\0{context_token}\0{index}\0{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def split_text(text: str, limit: int = 1800) -> list[str]:
    value = text.strip()
    if not value:
        return []
    parts: list[str] = []
    while len(value) > limit:
        cut = max(value.rfind("\n", 0, limit + 1), value.rfind("。", 0, limit + 1))
        if cut < limit // 2:
            cut = limit
        elif value[cut] == "。":
            cut += 1
        parts.append(value[:cut].strip())
        value = value[cut:].strip()
    if value:
        parts.append(value)
    return parts


def sanitize_reply_text(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = urllib.parse.unquote(match.group(1) or match.group(2) or "")
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in ("http", "https"):
            return match.group(0)
        if raw.startswith("file://"):
            raw = parsed.path
        if raw.startswith(("/", "~")):
            return "[local image omitted]"
        return match.group(0)

    cleaned = MARKDOWN_IMAGE_RE.sub(replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


@dataclass(frozen=True)
class Config:
    thread_id: str
    user_id: str
    credentials_path: Path
    inbox_path: Path
    state_path: Path
    workspace: Path
    rollout_path: Path | None
    initial_blocking_turn_id: str
    codex_path: str
    poll_seconds: float
    reply_limit: int
    codex_mode: str = "desktop-ipc"
    codex_model: str = ""
    desktop_ipc_socket_path: Path = Path.home() / ".codex" / "ipc" / "ipc.sock"
    media_dir: Path | None = None
    source_label: str = "WeChat"

    @classmethod
    def read(cls, path: Path) -> "Config":
        raw = load_json(path)
        if not isinstance(raw, dict):
            raise BridgeError(f"missing or invalid config: {path}")
        required = ("thread_id", "user_id", "credentials_path", "state_path", "workspace")
        missing = [name for name in required if not raw.get(name)]
        if missing:
            raise BridgeError(f"config missing: {', '.join(missing)}")
        return cls(
            thread_id=str(raw["thread_id"]),
            user_id=str(raw["user_id"]),
            credentials_path=Path(raw["credentials_path"]).expanduser(),
            inbox_path=Path(raw.get("inbox_path", ROOT / "runtime" / "messages.jsonl")).expanduser(),
            state_path=Path(raw["state_path"]).expanduser(),
            workspace=Path(raw["workspace"]).expanduser(),
            rollout_path=Path(raw["rollout_path"]).expanduser() if raw.get("rollout_path") else None,
            initial_blocking_turn_id=str(raw.get("initial_blocking_turn_id") or ""),
            codex_path=str(raw.get("codex_path", "codex")),
            poll_seconds=max(1.0, float(raw.get("poll_seconds", 2.0))),
            reply_limit=max(500, int(raw.get("reply_limit", 1800))),
            codex_mode=str(raw.get("codex_mode") or "desktop-ipc"),
            codex_model=str(raw.get("codex_model") or ""),
            desktop_ipc_socket_path=Path(
                raw.get("desktop_ipc_socket_path")
                or Path.home() / ".codex" / "ipc" / "ipc.sock"
            ).expanduser(),
            media_dir=Path(raw["media_dir"]).expanduser() if raw.get("media_dir") else None,
            source_label=str(raw.get("source_label") or "WeChat").strip() or "WeChat",
        )


class WeChatClient:
    def __init__(self, config: Config):
        self.config = config

    def _credentials(self) -> tuple[str, str]:
        raw = load_json(self.config.credentials_path)
        if not isinstance(raw, dict) or not raw.get("token") or not raw.get("baseUrl"):
            raise BridgeError("WeChat credentials are unavailable")
        return str(raw["baseUrl"]).rstrip("/"), str(raw["token"])

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": str(random.randint(100_000_000, 999_999_999)),
        }

    def _post(self, endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        base, token = self._credentials()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{base}{endpoint}",
            data=body,
            method="POST",
            headers=self._headers(token),
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8") or "{}")
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            raise BridgeError(f"WeChat request failed: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise BridgeError("WeChat returned an invalid response")
        return value

    @staticmethod
    def _base_info() -> dict[str, str]:
        return {
            "channel_version": WECHAT_CHANNEL_VERSION,
            "bot_agent": WECHAT_BOT_AGENT,
        }

    @staticmethod
    def _aes_key(image_item: dict[str, Any]) -> bytes | None:
        hex_key = str(image_item.get("aeskey") or "")
        if re.fullmatch(r"[0-9a-fA-F]{32}", hex_key):
            return bytes.fromhex(hex_key)
        encoded = str((image_item.get("media") or {}).get("aes_key") or "")
        if not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise BridgeError("WeChat image has an invalid AES key") from exc
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
            return bytes.fromhex(decoded.decode("ascii"))
        raise BridgeError("WeChat image has an invalid AES key length")

    @staticmethod
    def _decrypt_image(ciphertext: bytes, key: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        try:
            return unpadder.update(padded) + unpadder.finalize()
        except ValueError as exc:
            raise BridgeError("WeChat image decryption failed") from exc

    @staticmethod
    def _image_suffix(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        raise BridgeError("WeChat image format is unsupported")

    @staticmethod
    def _read_limited(response: Any, limit: int = MAX_IMAGE_BYTES) -> bytes:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise BridgeError("WeChat image is too large")
        return data

    def download_image(self, image_item: dict[str, Any]) -> Path:
        media = image_item.get("media") or {}
        encrypted_query = str(media.get("encrypt_query_param") or "")
        if not encrypted_query:
            raise BridgeError("WeChat image is missing its download parameter")
        url = (
            f"{WECHAT_CDN_BASE_URL}/download?encrypted_query_param="
            f"{urllib.parse.quote(encrypted_query, safe='')}"
        )
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                encrypted = self._read_limited(response)
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise BridgeError(f"WeChat image download failed: {type(exc).__name__}") from exc
        key = self._aes_key(image_item)
        plaintext = self._decrypt_image(encrypted, key) if key else encrypted
        suffix = self._image_suffix(plaintext)
        media_dir = self.config.media_dir or self.config.state_path.parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(plaintext).hexdigest()[:20]
        path = media_dir / f"wechat-{int(time.time() * 1000)}-{digest}{suffix}"
        path.write_bytes(plaintext)
        path.chmod(0o600)
        return path.resolve()

    def poll(self, poll_buf: str) -> tuple[str, list[dict[str, Any]]]:
        value = self._post(
            "/ilink/bot/getupdates",
            {"get_updates_buf": poll_buf, "base_info": {"channel_version": "0.1.0"}},
            timeout=70,
        )
        next_buf = str(value.get("get_updates_buf") or poll_buf)
        messages = value.get("msgs") or []
        return next_buf, messages if isinstance(messages, list) else []

    def send(self, text: str, context_token: str) -> None:
        for part in split_text(text, self.config.reply_limit):
            value = self._post(
                "/ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": self.config.user_id,
                        "client_id": f"same-thread-bridge-{int(time.time())}-{uuid.uuid4().hex[:8]}",
                        "message_type": 2,
                        "message_state": 3,
                        "item_list": [{"type": 1, "text_item": {"text": part}}],
                        "context_token": context_token,
                    }
                },
                timeout=25,
            )
            if not value.get("message_id"):
                reason = value.get("errmsg") or value.get("ret") or "missing message_id"
                raise BridgeError(f"WeChat send rejected: {reason}")

class CodexAppServer:
    def __init__(self, config: Config):
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self.notifications: list[dict[str, Any]] = []
        self.next_id = 1
        self.resumed = False

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self.stop()
        self.process = subprocess.Popen(
            [self.config.codex_path, "app-server"],
            cwd=self.config.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.resumed = False
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "same_thread_bridge",
                    "title": "Same Thread Bridge",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self.notify("initialized", {})

    def stop(self) -> None:
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self.resumed = False

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self.events.put(BridgeError("Codex app-server exited"))
        except BaseException as exc:  # reader failures must wake the main loop
            self.events.put(exc)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            text = line.strip()
            if text:
                log(f"codex: {text[:400]}")

    def _send(self, value: dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise BridgeError("Codex app-server is not running")
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        value: dict[str, Any] = {"method": method}
        if params is not None:
            value["params"] = params
        self._send(value)

    def _server_request(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        request_id = event.get("id")
        if method == "currentTime/read":
            result: dict[str, Any] = {"currentTimeAt": int(time.time())}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        else:
            result = {"decision": "decline"}
        self._send({"id": request_id, "result": result})

    def _next_event(self, timeout: float) -> dict[str, Any]:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty as exc:
            raise BridgeError("Codex app-server timed out") from exc
        if isinstance(event, BaseException):
            raise BridgeError(f"Codex reader failed: {type(event).__name__}") from event
        if event.get("method") and event.get("id") is not None:
            self._server_request(event)
            return self._next_event(timeout)
        return event

    def request(self, method: str, params: dict[str, Any], timeout: float = 60) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            event = self._next_event(max(0.1, deadline - time.monotonic()))
            if event.get("id") == request_id:
                if event.get("error"):
                    error = event["error"]
                    raise BridgeError(str(error.get("message") or error))
                return event.get("result")
            if event.get("method"):
                self.notifications.append(event)

    def ensure_resumed(self) -> None:
        self.start()
        if self.resumed:
            return
        result = self.request(
            "thread/resume",
            {"threadId": self.config.thread_id},
            timeout=60,
        )
        actual = str((result or {}).get("thread", {}).get("id") or "")
        if actual != self.config.thread_id:
            raise BridgeError("Codex resumed the wrong thread")
        self.resumed = True

    def reply(
        self, text: str, client_message_id: str, image_paths: list[Path] | None = None
    ) -> str:
        # A fresh app-server sees every turn the desktop may have added since the
        # previous WeChat message. Keeping a resumed thread loaded here would
        # leave it with stale history.
        self.stop()
        self.ensure_resumed()
        result = self.request(
            "turn/start",
            {
                "threadId": self.config.thread_id,
                "input": [
                    {"type": "text", "text": text},
                    *[
                        {"type": "localImage", "path": str(path)}
                        for path in (image_paths or [])
                    ],
                ],
                "clientUserMessageId": client_message_id,
            },
            timeout=60,
        )
        turn_id = str((result or {}).get("turn", {}).get("id") or "")
        if not turn_id:
            raise BridgeError("Codex did not start a turn")
        completed_messages: list[tuple[str, str]] = []
        deadline = time.monotonic() + 600
        while True:
            if self.notifications:
                event = self.notifications.pop(0)
            else:
                event = self._next_event(max(0.1, deadline - time.monotonic()))
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("threadId") not in (None, self.config.thread_id):
                continue
            if params.get("turnId") not in (None, turn_id):
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and str(item.get("text") or "").strip():
                    completed_messages.append(
                        (str(item.get("phase") or ""), str(item.get("text")).strip())
                    )
            elif method == "error" and params.get("willRetry") is False:
                detail = (params.get("error") or {}).get("message") or "Codex turn failed"
                raise BridgeError(str(detail))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "")
                if status not in ("completed", "Completed"):
                    raise BridgeError(f"Codex turn ended with status {status or 'unknown'}")
                finals = [body for phase, body in completed_messages if phase == "final_answer"]
                if finals:
                    return finals[-1]
                if completed_messages:
                    return completed_messages[-1][1]
                raise BridgeError("Codex completed without a reply")


class CodexExec:
    """Run one isolated Codex turn for an incoming WeChat message."""

    def __init__(self, config: Config):
        self.config = config

    def reply(
        self, text: str, client_message_id: str, image_paths: list[Path] | None = None
    ) -> str:
        del client_message_id
        image_note = ""
        if image_paths:
            image_note = "\n\n随消息附带的本地图片：\n" + "\n".join(
                str(path) for path in image_paths
            )
        prompt = (
            "Read the current project's instructions and relevant context, then answer the incoming "
            "WeChat message. Return only the final message that should be sent back. Do not include "
            "analysis, tool traces, or operational notes.\n\n"
            f"Incoming WeChat message:\n{text}{image_note}"
        )
        output_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="same-thread-bridge-", suffix=".txt", delete=False
            ) as handle:
                output_path = Path(handle.name)
            command = [
                self.config.codex_path,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--cd",
                str(self.config.workspace),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.config.codex_model:
                command[2:2] = ["--model", self.config.codex_model]
            child_env = os.environ.copy()
            codex_bin_dir = str(Path(self.config.codex_path).parent)
            child_env["PATH"] = f"{codex_bin_dir}:{child_env.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
                env=child_env,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "Codex exec failed").strip()
                raise BridgeError(detail[-600:])
            reply = output_path.read_text(encoding="utf-8").strip()
            if not reply:
                raise BridgeError("Codex exec completed without a reply")
            return reply
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("Codex exec timed out") from exc
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)


class CodexQueue:
    """Queue a WeChat message into the visible Codex thread and read its reply."""

    def __init__(self, config: Config, sleeper: Callable[[float], None] = time.sleep):
        self.config = config
        self.sleeper = sleeper

    def _rollout(self) -> Path:
        path = self.config.rollout_path
        if path is None or not path.is_file():
            raise BridgeError("the visible Codex thread rollout is unavailable")
        return path

    @staticmethod
    def _assistant_text(payload: dict[str, Any]) -> str:
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return ""
        parts = []
        for item in payload.get("content") or []:
            if item.get("type") == "output_text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()

    def reply(
        self, text: str, client_message_id: str, image_paths: list[Path] | None = None
    ) -> str:
        del client_message_id
        rollout = self._rollout()
        start_offset = rollout.stat().st_size
        visible_message = f"【{self.config.source_label}】\n{text}"
        if image_paths:
            visible_message += "\n" + "\n".join(
                f"[微信图片：{path}]" for path in image_paths
            )
        command = [
            self.config.codex_path,
            "queue",
            "--thread",
            self.config.thread_id,
            "--message",
            visible_message,
        ]
        child_env = os.environ.copy()
        codex_bin_dir = str(Path(self.config.codex_path).parent)
        child_env["PATH"] = f"{codex_bin_dir}:{child_env.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=600,
                check=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("Codex queue timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Codex queue failed").strip()
            raise BridgeError(detail[-600:])

        queue_turns: set[str] = set()
        turn_id = ""
        latest_answer = ""
        deadline = time.monotonic() + 600
        with rollout.open(encoding="utf-8") as handle:
            handle.seek(start_offset)
            while time.monotonic() < deadline:
                position = handle.tell()
                line = handle.readline()
                if not line:
                    handle.seek(position)
                    self.sleeper(0.25)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "event_msg" and payload.get("type") == "task_started":
                    attribution = payload.get("turn_attribution") or {}
                    if attribution.get("turn_trigger") == "queue":
                        candidate = str(payload.get("turn_id") or "")
                        if candidate:
                            queue_turns.add(candidate)
                    continue
                if (
                    event.get("type") == "event_msg"
                    and payload.get("type") == "item_completed"
                    and (payload.get("item") or {}).get("type") == "UserMessage"
                ):
                    candidate = str(payload.get("turn_id") or "")
                    user_text = "\n".join(
                        str(part.get("text") or "")
                        for part in (payload.get("item") or {}).get("content") or []
                        if part.get("type") == "text"
                    ).strip()
                    if candidate in queue_turns and user_text == visible_message.strip():
                        turn_id = candidate
                    continue
                if event.get("type") == "response_item" and turn_id:
                    answer = self._assistant_text(payload)
                    if answer and payload.get("phase") == "final_answer":
                        latest_answer = answer
                    continue
                if (
                    event.get("type") == "event_msg"
                    and payload.get("type") == "task_complete"
                    and turn_id
                    and str(payload.get("turn_id") or "") == turn_id
                ):
                    answer = str(payload.get("last_agent_message") or "").strip() or latest_answer
                    if not answer:
                        raise BridgeError("the visible Codex turn completed without a reply")
                    return answer
        raise BridgeError("the visible Codex turn timed out")


class CodexDesktopIpc:
    """Start a turn in the Codex Desktop window that owns the configured thread."""

    INITIAL_CLIENT_ID = "initializing-client"
    MAX_FRAME_BYTES = 256 * 1024 * 1024

    def __init__(self, config: Config, sleeper: Callable[[float], None] = time.sleep):
        self.config = config
        self.sleeper = sleeper

    def _rollout(self) -> Path:
        path = self.config.rollout_path
        if path is None or not path.is_file():
            raise BridgeError("the visible Codex thread rollout is unavailable")
        return path

    @staticmethod
    def _frame(value: dict[str, Any]) -> bytes:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not payload or len(payload) > CodexDesktopIpc.MAX_FRAME_BYTES:
            raise BridgeError("invalid Codex Desktop IPC frame size")
        return len(payload).to_bytes(4, "little") + payload

    @staticmethod
    def _read_exact(connection: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = connection.recv(remaining)
            if not chunk:
                raise ConnectionError("Codex Desktop IPC disconnected")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def _read_frame(cls, connection: socket.socket) -> dict[str, Any]:
        size = int.from_bytes(cls._read_exact(connection, 4), "little")
        if size <= 0 or size > cls.MAX_FRAME_BYTES:
            raise BridgeError("invalid Codex Desktop IPC frame size")
        try:
            value = json.loads(cls._read_exact(connection, size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid Codex Desktop IPC response") from exc
        if not isinstance(value, dict):
            raise BridgeError("invalid Codex Desktop IPC response")
        return value

    @classmethod
    def _request(
        cls,
        connection: socket.socket,
        client_id: str,
        method: str,
        params: dict[str, Any],
        version: int,
        timeout_ms: int,
        target_client_id: str = "",
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        request: dict[str, Any] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": client_id,
            "version": version,
            "method": method,
            "params": params,
            "timeoutMs": timeout_ms,
        }
        if target_client_id:
            request["targetClientId"] = target_client_id
        connection.settimeout(max(1.0, timeout_ms / 1000 + 5))
        connection.sendall(cls._frame(request))
        while True:
            message = cls._read_frame(connection)
            if message.get("type") == "client-discovery-request":
                connection.sendall(
                    cls._frame(
                        {
                            "type": "client-discovery-response",
                            "requestId": message.get("requestId"),
                            "response": {"canHandle": False},
                        }
                    )
                )
                continue
            if message.get("type") != "response" or message.get("requestId") != request_id:
                continue
            if message.get("resultType") != "success":
                raise IpcRejected(
                    f"Codex Desktop IPC rejected {method}: {message.get('error') or 'unknown'}"
                )
            return message

    @staticmethod
    def _start_params(
        thread_id: str,
        visible_message: str,
        client_message_id: str,
        image_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        text_item = {"type": "text", "text": visible_message, "text_elements": []}
        local_images = [
            {"type": "localImage", "path": str(path)} for path in (image_paths or [])
        ]
        restored_images = [
            {"src": path.as_uri(), "localPath": str(path)} for path in (image_paths or [])
        ]
        restore_message = {
            "id": client_message_id,
            "text": visible_message,
            "context": {
                "workspaceRoots": [],
                "imageAttachments": restored_images,
                "fileAttachments": [],
                "addedFiles": [],
                "commentAttachments": [],
                "responseTextAnnotations": [],
                "attachmentOrder": [],
                "prompt": visible_message,
            },
            "responsesapiClientMetadata": {
                "source": "codex",
                "client_type": "same_thread_bridge",
            },
        }
        return {
            "conversationId": thread_id,
            "turnStart": {
                "request": {
                    "threadId": thread_id,
                    "input": [text_item, *local_images],
                    "clientUserMessageId": client_message_id,
                },
                "context": {
                    "restoreMessage": restore_message,
                    "inheritThreadSettings": True,
                },
            },
        }

    @staticmethod
    def _turn_id(response: dict[str, Any]) -> str:
        value: Any = response.get("result")
        for _ in range(3):
            if isinstance(value, dict) and isinstance(value.get("turn"), dict):
                return str(value["turn"].get("id") or "")
            value = value.get("result") if isinstance(value, dict) else None
        return ""

    def _start_turn(
        self,
        visible_message: str,
        client_message_id: str,
        image_paths: list[Path] | None = None,
    ) -> str:
        path = self.config.desktop_ipc_socket_path
        if not path.exists():
            raise BridgeError("Codex Desktop IPC socket is unavailable")
        accepted_may_be_unknown = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(str(path))
                initialized = self._request(
                    connection,
                    self.INITIAL_CLIENT_ID,
                    "initialize",
                    {"clientType": "same-thread-bridge"},
                    version=0,
                    timeout_ms=5_000,
                )
                client_id = str((initialized.get("result") or {}).get("clientId") or "")
                if not client_id:
                    raise BridgeError("Codex Desktop IPC did not assign a client id")
                owner = self._request(
                    connection,
                    client_id,
                    "thread-owner-discovery",
                    {"hostId": "local", "conversationId": self.config.thread_id},
                    version=1,
                    timeout_ms=5_000,
                )
                owner_id = str(owner.get("handledByClientId") or "")
                if not owner_id:
                    raise BridgeError("the current Codex Desktop thread has no owner")
                accepted_may_be_unknown = True
                started = self._request(
                    connection,
                    client_id,
                    "thread-follower-start-turn",
                    self._start_params(
                        self.config.thread_id,
                        visible_message,
                        client_message_id,
                        image_paths,
                    ),
                    version=2,
                    timeout_ms=60_000,
                    target_client_id=owner_id,
                )
                accepted_may_be_unknown = False
        except IpcRejected:
            raise
        except BridgeError as exc:
            if accepted_may_be_unknown:
                raise AmbiguousIpcDelivery(
                    "Codex Desktop IPC returned an invalid response after dispatch; refusing to resend"
                ) from exc
            raise
        except (OSError, TimeoutError, ConnectionError) as exc:
            if accepted_may_be_unknown:
                raise AmbiguousIpcDelivery(
                    "Codex Desktop IPC disconnected after dispatch; refusing to resend"
                ) from exc
            raise BridgeError(f"Codex Desktop IPC unavailable: {type(exc).__name__}") from exc
        turn_id = self._turn_id(started)
        if not turn_id:
            raise AmbiguousIpcDelivery(
                "Codex Desktop accepted an unrecognized start response; refusing to resend"
            )
        return turn_id

    def recover_reply(self, client_message_id: str, wait_seconds: float = 0) -> str | None:
        rollout = self._rollout()
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            turn_id = ""
            latest_answer = ""
            try:
                with rollout.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload") or {}
                        if (
                            event.get("type") == "event_msg"
                            and payload.get("type") == "item_completed"
                            and (payload.get("item") or {}).get("type") == "UserMessage"
                            and str((payload.get("item") or {}).get("client_id") or "")
                            == client_message_id
                        ):
                            turn_id = str(payload.get("turn_id") or "")
                            continue
                        if event.get("type") == "response_item" and turn_id:
                            answer = CodexQueue._assistant_text(payload)
                            if answer and payload.get("phase") == "final_answer":
                                latest_answer = answer
                            continue
                        if (
                            event.get("type") == "event_msg"
                            and payload.get("type") == "task_complete"
                            and turn_id
                            and str(payload.get("turn_id") or "") == turn_id
                        ):
                            answer = str(payload.get("last_agent_message") or "").strip()
                            return answer or latest_answer or None
            except OSError as exc:
                raise BridgeError("cannot read the Codex thread rollout") from exc
            if time.monotonic() >= deadline:
                return None
            self.sleeper(0.25)

    def _wait_for_reply(self, turn_id: str, start_offset: int) -> str:
        rollout = self._rollout()
        latest_answer = ""
        deadline = time.monotonic() + 600
        with rollout.open(encoding="utf-8") as handle:
            handle.seek(start_offset)
            while time.monotonic() < deadline:
                position = handle.tell()
                line = handle.readline()
                if not line:
                    handle.seek(position)
                    self.sleeper(0.25)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "response_item":
                    answer = CodexQueue._assistant_text(payload)
                    if answer and payload.get("phase") == "final_answer":
                        latest_answer = answer
                    continue
                if (
                    event.get("type") == "event_msg"
                    and payload.get("type") == "task_complete"
                    and str(payload.get("turn_id") or "") == turn_id
                ):
                    answer = str(payload.get("last_agent_message") or "").strip() or latest_answer
                    if not answer:
                        raise BridgeError("the visible Codex turn completed without a reply")
                    return answer
        raise BridgeError("the visible Codex turn timed out")

    def reply(
        self, text: str, client_message_id: str, image_paths: list[Path] | None = None
    ) -> str:
        rollout = self._rollout()
        recovered = self.recover_reply(client_message_id)
        if recovered:
            return recovered
        start_offset = rollout.stat().st_size
        visible_message = f"【{self.config.source_label}】\n{text}"
        started_at = time.monotonic()
        try:
            turn_id = self._start_turn(visible_message, client_message_id, image_paths)
        except AmbiguousIpcDelivery:
            recovered = self.recover_reply(client_message_id, wait_seconds=30)
            if recovered:
                return recovered
            raise
        log(f"Codex Desktop accepted {client_message_id[:18]} in {time.monotonic() - started_at:.3f}s")
        return self._wait_for_reply(turn_id, start_offset)


class Bridge:
    def __init__(
        self,
        config: Config,
        wechat: WeChatClient | None = None,
        codex: CodexAppServer | CodexExec | CodexQueue | CodexDesktopIpc | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.wechat = wechat or WeChatClient(config)
        if codex is not None:
            self.codex = codex
        elif config.codex_mode == "exec":
            self.codex = CodexExec(config)
        elif config.codex_mode == "queue":
            self.codex = CodexQueue(config, sleeper)
        elif config.codex_mode == "desktop-ipc":
            self.codex = CodexDesktopIpc(config, sleeper)
        else:
            self.codex = CodexAppServer(config)
        self.sleeper = sleeper
        self._busy_notice = False
        self.state = load_json(
            config.state_path,
            {"version": 1, "poll_buf": "", "seeded": False, "queue": [], "seen": []},
        )

    def save(self) -> None:
        self.state["seen"] = list(self.state.get("seen") or [])[-200:]
        save_json(self.config.state_path, self.state)

    @staticmethod
    def _content(item: dict[str, Any]) -> str:
        item_type = item.get("type")
        if item_type == 1:
            return str((item.get("text_item") or {}).get("text") or "").strip()
        if item_type == 3:
            return str((item.get("voice_item") or {}).get("text") or "").strip()
        return ""

    @staticmethod
    def _item_signature(item: dict[str, Any]) -> str:
        content = Bridge._content(item)
        if content:
            return f"{item.get('type')}:{content}"
        if item.get("type") == 2:
            image = item.get("image_item") or {}
            media = image.get("media") or {}
            stable = {
                "encrypt_query_param": media.get("encrypt_query_param"),
                "aes_key": media.get("aes_key"),
                "aeskey": image.get("aeskey"),
                "hd_size": image.get("hd_size"),
                "mid_size": image.get("mid_size"),
            }
            return "2:" + json.dumps(stable, sort_keys=True, separators=(",", ":"))
        return ""

    def poll_once(self) -> int:
        old_buf = str(self.state.get("poll_buf") or "")
        next_buf, messages = self.wechat.poll(old_buf)
        self.state["poll_buf"] = next_buf
        if not self.state.get("seeded"):
            self.state["seeded"] = True
            self.save()
            log(f"seeded WeChat cursor; ignored {len(messages)} historical message(s)")
            return 0
        seen = set(self.state.get("seen") or [])
        queued = {str(item.get("key")) for item in self.state.get("queue") or []}
        added = 0
        for message in messages:
            sender = str(message.get("from_user_id") or "")
            if sender != self.config.user_id:
                continue
            received_at_ms = int(time.time() * 1000)
            try:
                created_at_ms = int(message.get("create_time_ms") or 0)
            except (TypeError, ValueError):
                created_at_ms = 0
            upstream_delay_ms = (
                max(0, received_at_ms - created_at_ms) if created_at_ms > 0 else None
            )
            context_token = str(message.get("context_token") or "")
            items = message.get("item_list") or []
            signatures = [self._item_signature(item) for item in items]
            signatures = [value for value in signatures if value]
            if not signatures:
                continue
            key = message_key(sender, context_token, 0, "\n".join(signatures))
            if key in seen or key in queued:
                continue
            texts = [self._content(item) for item in items]
            texts = [value for value in texts if value]
            image_paths: list[str] = []
            image_failures = 0
            for item in items:
                if item.get("type") != 2 or not isinstance(item.get("image_item"), dict):
                    continue
                try:
                    image_paths.append(str(self.wechat.download_image(item["image_item"])))
                except BridgeError as exc:
                    image_failures += 1
                    log(f"image download failed for {key[:10]}: {exc}")
            if image_paths:
                texts.append("The sender attached a photo from WeChat.")
            if image_failures:
                texts.append("A WeChat photo could not be downloaded. Ask the sender to resend it.")
            content = "\n".join(texts).strip()
            if not content:
                continue
            self.state.setdefault("queue", []).append(
                {
                    "key": key,
                    "content": content,
                    "image_paths": image_paths,
                    "context_token": context_token,
                    "reply": "",
                    "received_at": received_at_ms // 1000,
                    "created_at_ms": created_at_ms,
                    "upstream_delay_ms": upstream_delay_ms,
                }
            )
            self.config.inbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config.inbox_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": received_at_ms // 1000,
                            "sender": sender,
                            "content": content,
                            "image_paths": image_paths,
                            "key": key,
                            "created_at_ms": created_at_ms,
                            "upstream_delay_ms": upstream_delay_ms,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            queued.add(key)
            added += 1
            timing = (
                f" upstream_delay={upstream_delay_ms}ms"
                if upstream_delay_ms is not None
                else " upstream_delay=unknown"
            )
            log(
                f"queued {key[:10]}:{timing}: {content[:80]}"
                f" images={len(image_paths)}"
            )
        self.save()
        return added

    def process_one(self) -> bool:
        pending = self.state.get("queue") or []
        if not pending:
            return False
        if self.thread_is_busy():
            if not self._busy_notice:
                log("Codex thread is busy; keeping the WeChat message queued")
                self._busy_notice = True
            return False
        self._busy_notice = False
        item = pending[0]
        key = str(item["key"])
        client_message_id = f"wechat-{key[:32]}"
        if not item.get("reply"):
            if item.get("delivery_state") == "ambiguous":
                recover = getattr(self.codex, "recover_reply", None)
                recovered = recover(client_message_id) if callable(recover) else None
                if not recovered:
                    if not item.get("ambiguity_logged"):
                        log(f"IPC delivery for {key[:10]} is unresolved; refusing to resend")
                        item["ambiguity_logged"] = True
                        self.save()
                    return False
                item["reply"] = recovered
                item.pop("delivery_state", None)
                item.pop("ambiguity_logged", None)
                self.save()
            else:
                log(f"waking Codex thread for {key[:10]}")
                try:
                    item["reply"] = self.codex.reply(
                        str(item["content"]),
                        client_message_id,
                        [Path(path) for path in (item.get("image_paths") or [])],
                    )
                except AmbiguousIpcDelivery:
                    item["delivery_state"] = "ambiguous"
                    self.save()
                    raise
                self.save()
        reply_text = sanitize_reply_text(str(item["reply"]))
        context_token = str(item.get("context_token") or "")
        if reply_text and not item.get("outbound_text_sent"):
            self.wechat.send(reply_text, context_token)
            item["outbound_text_sent"] = True
            self.save()
        if not reply_text:
            raise BridgeError("Codex completed without a sendable WeChat reply")
        self.state["queue"].pop(0)
        self.state.setdefault("seen", []).append(key)
        self.save()
        log(f"replied to {key[:10]}")
        return True

    def thread_is_busy(self) -> bool:
        path = self.config.rollout_path
        if path is None:
            return False
        try:
            latest = ""
            completed_turns: set[str] = set()
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "event_msg":
                        continue
                    kind = str((event.get("payload") or {}).get("type") or "")
                    if kind in ("task_started", "task_complete"):
                        latest = kind
                    if kind == "task_complete":
                        turn_id = str((event.get("payload") or {}).get("turn_id") or "")
                        if turn_id:
                            completed_turns.add(turn_id)
        except OSError as exc:
            raise BridgeError("cannot read the Codex thread lifecycle") from exc
        if (
            self.config.initial_blocking_turn_id
            and self.config.initial_blocking_turn_id not in completed_turns
        ):
            return True
        # A missing lifecycle is not safe to write into. The normal idle state
        # always ends in task_complete.
        return latest != "task_complete"

    def run_once(self) -> None:
        self.poll_once()
        while self.process_one():
            pass

    def run_forever(self) -> None:
        backoff = self.config.poll_seconds
        if self.config.codex_mode in ("queue", "desktop-ipc"):
            target = f"visible Codex thread {self.config.thread_id}"
        elif self.config.codex_mode == "app-server":
            target = self.config.thread_id
        else:
            target = "isolated Codex turns"
        log(f"bridge started for {target}")
        while True:
            try:
                self.run_once()
                backoff = self.config.poll_seconds
                self.sleeper(self.config.poll_seconds)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                log(f"bridge error: {type(exc).__name__}: {exc}")
                if isinstance(self.codex, CodexAppServer):
                    self.codex.stop()
                self.sleeper(backoff)
                backoff = min(60.0, max(self.config.poll_seconds, backoff * 2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--check-codex", action="store_true")
    args = parser.parse_args()
    config = Config.read(args.config)
    bridge = Bridge(config)
    if args.health:
        queued = len(bridge.state.get("queue") or [])
        checks = {
            "workspace": config.workspace.is_dir(),
            "credentials": config.credentials_path.is_file(),
            "desktop_ipc_socket": (
                config.desktop_ipc_socket_path.exists()
                if config.codex_mode == "desktop-ipc"
                else None
            ),
            "rollout": config.rollout_path.is_file() if config.rollout_path else None,
        }
        print(
            json.dumps(
                {
                    "ok": all(value is not False for value in checks.values()),
                    "mode": config.codex_mode,
                    "seeded": bool(bridge.state.get("seeded")),
                    "queued": queued,
                    "checks": checks,
                },
                ensure_ascii=False,
            )
        )
        return
    if args.check_codex:
        if isinstance(bridge.codex, CodexAppServer):
            bridge.codex.ensure_resumed()
            bridge.codex.stop()
        elif isinstance(bridge.codex, CodexDesktopIpc):
            if not config.desktop_ipc_socket_path.exists():
                raise BridgeError("Codex Desktop IPC socket is unavailable")
            bridge.codex._rollout()
        else:
            raise BridgeError("--check-codex is supported for app-server and desktop-ipc")
        print(json.dumps({"ok": True, "thread_id": config.thread_id}))
        return
    if args.once:
        bridge.run_once()
        return
    bridge.run_forever()


if __name__ == "__main__":
    main()
