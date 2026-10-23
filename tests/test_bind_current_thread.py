import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from same_thread_bridge.bind_current_thread import (
    BindError,
    bind,
    current_thread_id,
    find_rollout,
)


THREAD_A = "01a162dd-0b61-7b11-b9f9-b81c6e084bd5"
THREAD_B = "01b222dd-0b61-7b11-b9f9-b81c6e084bd5"


class BindCurrentThreadTests(unittest.TestCase):
    def make_files(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        rollout = root / f"rollout-2026-10-23T12-00-00-{THREAD_B}.jsonl"
        rollout.write_text("", encoding="utf-8")
        state = root / "state.json"
        state.write_text(
            json.dumps({"seeded": True, "queue": [], "seen": []}), encoding="utf-8"
        )
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "thread_id": THREAD_A,
                    "workspace": str(root / "old-workspace"),
                    "rollout_path": str(root / "old.jsonl"),
                    "state_path": str(state),
                    "codex_mode": "queue",
                    "initial_blocking_turn_id": "old-turn",
                }
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
        return config, state, workspace, rollout

    def test_current_thread_id_prefers_codex_thread_env(self):
        self.assertEqual(
            current_thread_id("", {"CODEX_THREAD_ID": THREAD_B, "CODEX_SESSION_ID": THREAD_A}),
            THREAD_B,
        )

    def test_find_rollout_uses_exact_thread_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exact = root / "2026/10/23" / f"rollout-now-{THREAD_B}.jsonl"
            exact.parent.mkdir(parents=True)
            exact.write_text("", encoding="utf-8")
            (exact.parent / f"rollout-now-prefix-{THREAD_B}-wrong.jsonl").write_text(
                "", encoding="utf-8"
            )
            self.assertEqual(find_rollout(THREAD_B, root), exact.resolve())

    def test_dry_run_does_not_change_config_or_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _, workspace, rollout = self.make_files(root)
            before = config.read_bytes()
            with patch(
                "same_thread_bridge.bind_current_thread.restart_launch_agent"
            ) as restart:
                result = bind(
                    config_path=config,
                    thread_id=THREAD_B,
                    workspace=workspace,
                    rollout_path=rollout,
                    launch_agent_label="test.agent",
                    dry_run=True,
                )
            self.assertTrue(result["changed"])
            self.assertEqual(config.read_bytes(), before)
            restart.assert_not_called()

    def test_binding_updates_three_fields_clears_blocker_and_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _, workspace, rollout = self.make_files(root)
            with (
                patch("same_thread_bridge.bind_current_thread.restart_launch_agent") as restart,
                patch("same_thread_bridge.bind_current_thread.launch_agent_is_loaded", return_value=True),
                patch("same_thread_bridge.bind_current_thread.time.sleep"),
            ):
                result = bind(
                    config_path=config,
                    thread_id=THREAD_B,
                    workspace=workspace,
                    rollout_path=rollout,
                    launch_agent_label="test.agent",
                    dry_run=False,
                )
            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(saved["thread_id"], THREAD_B)
            self.assertEqual(saved["workspace"], str(workspace.resolve()))
            self.assertEqual(saved["rollout_path"], str(rollout.resolve()))
            self.assertEqual(saved["initial_blocking_turn_id"], "")
            self.assertTrue(Path(result["backup_path"]).is_file())
            restart.assert_called_once_with("test.agent")

    def test_pending_message_stops_switch_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, state, workspace, rollout = self.make_files(root)
            state.write_text(json.dumps({"queue": [{"key": "pending"}]}), encoding="utf-8")
            before = config.read_bytes()
            with self.assertRaises(BindError):
                bind(
                    config_path=config,
                    thread_id=THREAD_B,
                    workspace=workspace,
                    rollout_path=rollout,
                    launch_agent_label="test.agent",
                    dry_run=False,
                )
            self.assertEqual(config.read_bytes(), before)

    def test_failed_verification_restores_original_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _, workspace, rollout = self.make_files(root)
            original = json.loads(config.read_text(encoding="utf-8"))
            with (
                patch("same_thread_bridge.bind_current_thread.restart_launch_agent") as restart,
                patch(
                    "same_thread_bridge.bind_current_thread.verify_binding",
                    side_effect=BindError("verification failed"),
                ),
                patch("same_thread_bridge.bind_current_thread.time.sleep"),
            ):
                with self.assertRaises(BindError):
                    bind(
                        config_path=config,
                        thread_id=THREAD_B,
                        workspace=workspace,
                        rollout_path=rollout,
                        launch_agent_label="test.agent",
                        dry_run=False,
                    )
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
            self.assertEqual(restart.call_count, 2)


if __name__ == "__main__":
    unittest.main()
