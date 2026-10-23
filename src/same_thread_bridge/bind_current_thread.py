#!/usr/bin/env python3
"""Safely rebind Same Thread Bridge to the Codex thread running this command."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG = Path(
    os.environ.get(
        "SAME_THREAD_BRIDGE_CONFIG",
        Path.home() / ".config" / "same-thread-bridge" / "config.json",
    )
).expanduser()
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")


class BindError(RuntimeError):
    pass


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BindError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BindError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise BindError(f"{label} must contain a JSON object: {path}")
    return value


def current_thread_id(explicit: str, env: Mapping[str, str]) -> str:
    thread_id = (
        explicit.strip()
        or env.get("CODEX_THREAD_ID", "").strip()
        or env.get("CODEX_SESSION_ID", "").strip()
    )
    if not thread_id:
        raise BindError("current Codex thread id is unavailable")
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise BindError("current Codex thread id has an unexpected format")
    return thread_id


def find_rollout(thread_id: str, sessions_root: Path) -> Path:
    if not sessions_root.is_dir():
        raise BindError(f"Codex sessions directory is unavailable: {sessions_root}")
    suffix = f"-{thread_id}.jsonl"
    matches = [
        path
        for path in sessions_root.rglob("rollout-*.jsonl")
        if path.name.endswith(suffix) and path.is_file()
    ]
    if not matches:
        raise BindError("the current Codex thread rollout file was not found")
    return max(matches, key=lambda path: path.stat().st_mtime).resolve()


def queued_messages(config: Mapping[str, Any]) -> int:
    raw_path = str(config.get("state_path") or "").strip()
    if not raw_path:
        return 0
    state_path = Path(raw_path).expanduser()
    if not state_path.exists():
        return 0
    state = load_object(state_path, "bridge state")
    queue = state.get("queue") or []
    if not isinstance(queue, list):
        raise BindError(f"bridge state queue is invalid: {state_path}")
    return len(queue)


def write_json_atomic(path: Path, value: Mapping[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restart_launch_agent(label: str) -> None:
    if not label:
        return
    target = f"gui/{os.getuid()}/{label}"
    completed = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "launchctl failed").strip()
        raise BindError(f"bridge restart failed: {detail}")


def launch_agent_is_loaded(label: str) -> bool:
    if not label:
        return True
    completed = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0


def verify_binding(config_path: Path, expected: Mapping[str, str], label: str) -> None:
    current = load_object(config_path, "bridge config")
    for key, expected_value in expected.items():
        if str(current.get(key) or "") != expected_value:
            raise BindError(f"bridge config verification failed for {key}")
    workspace = Path(expected["workspace"])
    rollout = Path(expected["rollout_path"])
    if not workspace.is_dir() or not rollout.is_file():
        raise BindError("the rebound workspace or rollout is unavailable")
    socket_path = Path(
        str(current.get("desktop_ipc_socket_path") or Path.home() / ".codex/ipc/ipc.sock")
    ).expanduser()
    if str(current.get("codex_mode") or "desktop-ipc") == "desktop-ipc" and not socket_path.exists():
        raise BindError("Codex Desktop IPC socket is unavailable")
    if not launch_agent_is_loaded(label):
        raise BindError("the bridge LaunchAgent is not loaded")


def bind(
    *,
    config_path: Path,
    thread_id: str,
    workspace: Path,
    rollout_path: Path,
    launch_agent_label: str,
    dry_run: bool,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    rollout_path = rollout_path.expanduser().resolve()
    if not workspace.is_dir():
        raise BindError(f"current workspace is unavailable: {workspace}")
    if not rollout_path.is_file():
        raise BindError(f"current rollout is unavailable: {rollout_path}")

    original = load_object(config_path, "bridge config")
    pending = queued_messages(original)
    if pending:
        raise BindError(f"bridge still has {pending} queued message(s); wait for them before switching")

    expected = {
        "thread_id": thread_id,
        "workspace": str(workspace),
        "rollout_path": str(rollout_path),
    }
    changed = any(str(original.get(key) or "") != value for key, value in expected.items())
    result = {
        "ok": True,
        "changed": changed,
        "dry_run": dry_run,
        "thread_id": thread_id,
        "workspace": str(workspace),
        "rollout_path": str(rollout_path),
        "previous_thread_id": str(original.get("thread_id") or ""),
        "queued": pending,
    }
    if dry_run or not changed:
        return result

    updated = dict(original)
    updated.update(expected)
    updated["initial_blocking_turn_id"] = ""
    mode = config_path.stat().st_mode & 0o777 or 0o600
    backup_path = config_path.with_name(f"{config_path.name}.before-bind")
    shutil.copy2(config_path, backup_path)
    backup_path.chmod(mode)

    try:
        write_json_atomic(config_path, updated, mode)
        restart_launch_agent(launch_agent_label)
        time.sleep(0.25)
        verify_binding(config_path, expected, launch_agent_label)
    except Exception as exc:
        shutil.copy2(backup_path, config_path)
        try:
            restart_launch_agent(launch_agent_label)
        except Exception as rollback_exc:
            raise BindError(
                f"binding failed and automatic restart after rollback also failed: {rollback_exc}"
            ) from exc
        if isinstance(exc, BindError):
            raise
        raise BindError(f"binding failed: {type(exc).__name__}") from exc

    result["backup_path"] = str(backup_path)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind Same Thread Bridge to the Codex thread running this command."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--rollout-path", type=Path)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    parser.add_argument("--launch-agent-label", default="com.paperchip.same-thread-bridge")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        thread_id = current_thread_id(args.thread_id, os.environ)
        rollout_path = args.rollout_path or find_rollout(thread_id, args.sessions_root.expanduser())
        result = bind(
            config_path=args.config,
            thread_id=thread_id,
            workspace=args.workspace,
            rollout_path=rollout_path,
            launch_agent_label=args.launch_agent_label,
            dry_run=args.dry_run,
        )
    except BindError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
