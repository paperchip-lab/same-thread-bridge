import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from same_thread_bridge.bridge import (
    Bridge,
    CodexDesktopIpc,
    CodexExec,
    CodexQueue,
    Config,
    WeChatClient,
    message_key,
    sanitize_reply_text,
    split_text,
)


class FakeWeChat:
    def __init__(self, polls):
        self.polls = list(polls)
        self.sent = []
        self.sent_images = []
        self.downloaded_image = None

    def poll(self, poll_buf):
        return self.polls.pop(0)

    def send(self, text, context_token):
        self.sent.append((text, context_token))

    def send_image(self, image_path, context_token):
        self.sent_images.append((image_path, context_token))

    def download_image(self, image_item):
        del image_item
        if self.downloaded_image is None:
            raise AssertionError("test did not configure an inbound image")
        return self.downloaded_image


class FakeCodex:
    def __init__(self):
        self.calls = []

    def reply(self, text, client_message_id, image_paths=None):
        self.calls.append((text, client_message_id, image_paths or []))
        return "收到。"


class BridgeTests(unittest.TestCase):
    def make_config(self, root):
        return Config(
            thread_id="thread-1",
            user_id="wechat-user-1",
            credentials_path=root / "creds.json",
            inbox_path=root / "inbox.jsonl",
            state_path=root / "state.json",
            workspace=root,
            rollout_path=None,
            initial_blocking_turn_id="",
            codex_path="codex",
            poll_seconds=1,
            reply_limit=1800,
        )

    def test_split_text_preserves_all_content(self):
        source = "第一段。" * 500
        parts = split_text(source, 500)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), source)

    def test_first_poll_only_seeds_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chat = FakeWeChat([("cursor-1", [{"from_user_id": "wechat-user-1"}])])
            bridge = Bridge(self.make_config(root), chat, FakeCodex(), lambda _: None)
            self.assertEqual(bridge.poll_once(), 0)
            state = json.loads((root / "state.json").read_text())
            self.assertTrue(state["seeded"])
            self.assertEqual(state["poll_buf"], "cursor-1")

    def test_message_is_queued_replied_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"version": 1, "poll_buf": "c1", "seeded": True, "queue": [], "seen": []}
            (root / "state.json").write_text(json.dumps(state))
            message = {
                "from_user_id": "wechat-user-1",
                "context_token": "ctx",
                "create_time_ms": 1_700_000_000_000,
                "item_list": [{"type": 1, "text_item": {"text": "测试消息"}}],
            }
            chat = FakeWeChat([("c2", [message]), ("c3", [message])])
            codex = FakeCodex()
            bridge = Bridge(self.make_config(root), chat, codex, lambda _: None)
            with patch("same_thread_bridge.bridge.time.time", return_value=1_700_000_012.345):
                self.assertEqual(bridge.poll_once(), 1)
            inbox = json.loads((root / "inbox.jsonl").read_text())
            self.assertEqual(inbox["created_at_ms"], 1_700_000_000_000)
            self.assertEqual(inbox["upstream_delay_ms"], 12_345)
            self.assertTrue(bridge.process_one())
            self.assertEqual(chat.sent, [("收到。", "ctx")])
            self.assertEqual(len(codex.calls), 1)
            self.assertEqual(bridge.poll_once(), 0)
            self.assertFalse(bridge.process_one())
            self.assertEqual(len(codex.calls), 1)

    def test_saved_reply_is_reused_after_send_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = message_key("wechat-user-1", "ctx", 0, "测试消息")
            state = {
                "version": 1,
                "poll_buf": "c1",
                "seeded": True,
                "queue": [{"key": key, "content": "测试消息", "context_token": "ctx", "reply": "已经生成"}],
                "seen": [],
            }
            (root / "state.json").write_text(json.dumps(state))
            chat = FakeWeChat([])
            codex = FakeCodex()
            bridge = Bridge(self.make_config(root), chat, codex, lambda _: None)
            self.assertTrue(bridge.process_one())
            self.assertEqual(chat.sent, [("已经生成", "ctx")])
            self.assertEqual(codex.calls, [])

    def test_busy_thread_keeps_message_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n"
            )
            config = self.make_config(root)
            config = Config(**{**config.__dict__, "rollout_path": rollout})
            state = {
                "version": 1,
                "poll_buf": "c1",
                "seeded": True,
                "queue": [{"key": "k", "content": "测试消息", "context_token": "ctx", "reply": ""}],
                "seen": [],
            }
            (root / "state.json").write_text(json.dumps(state))
            chat = FakeWeChat([])
            codex = FakeCodex()
            bridge = Bridge(config, chat, codex, lambda _: None)
            self.assertFalse(bridge.process_one())
            self.assertEqual(codex.calls, [])
            self.assertEqual(len(bridge.state["queue"]), 1)

            with rollout.open("a") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            self.assertTrue(bridge.process_one())
            self.assertEqual(len(codex.calls), 1)

    def test_exec_mode_selects_isolated_codex_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.make_config(root)
            config = Config(**{**config.__dict__, "codex_mode": "exec"})
            bridge = Bridge(config, FakeWeChat([]), sleeper=lambda _: None)
            self.assertIsInstance(bridge.codex, CodexExec)

    def test_queue_mode_selects_visible_thread_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.make_config(root)
            config = Config(**{**config.__dict__, "codex_mode": "queue"})
            bridge = Bridge(config, FakeWeChat([]), sleeper=lambda _: None)
            self.assertIsInstance(bridge.codex, CodexQueue)

    def test_desktop_ipc_mode_selects_visible_thread_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.make_config(root)
            config = Config(**{**config.__dict__, "codex_mode": "desktop-ipc"})
            bridge = Bridge(config, FakeWeChat([]), sleeper=lambda _: None)
            self.assertIsInstance(bridge.codex, CodexDesktopIpc)

    def test_desktop_ipc_frame_and_start_shape(self):
        value = {"type": "request", "message": "hello"}
        frame = CodexDesktopIpc._frame(value)
        size = int.from_bytes(frame[:4], "little")
        self.assertEqual(size, len(frame) - 4)
        self.assertEqual(json.loads(frame[4:]), value)

        params = CodexDesktopIpc._start_params("thread-1", "【WeChat】\nhello", "wechat-1")
        text_item = params["turnStart"]["request"]["input"][0]
        self.assertEqual(text_item["text_elements"], [])
        self.assertTrue(params["turnStart"]["context"]["inheritThreadSettings"])
        self.assertEqual(
            params["turnStart"]["context"]["restoreMessage"]["text"],
            "【WeChat】\nhello",
        )

    def test_desktop_ipc_reads_reply_from_the_started_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rollout = root / "rollout.jsonl"
            rollout.write_text("", encoding="utf-8")
            config = self.make_config(root)
            config = Config(
                **{
                    **config.__dict__,
                    "codex_mode": "desktop-ipc",
                    "rollout_path": rollout,
                }
            )
            runner = CodexDesktopIpc(config, sleeper=lambda _: None)

            def start_turn(message, client_message_id, image_paths=None):
                self.assertEqual(message, "【WeChat】\n延迟测试")
                self.assertEqual(client_message_id, "wechat-message-1")
                self.assertEqual(image_paths, None)
                events = [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": "turn-ipc",
                            "item": {
                                "type": "UserMessage",
                                "client_id": client_message_id,
                                "content": [
                                    {"type": "text", "text": message, "text_elements": []}
                                ],
                            },
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-ipc",
                            "last_agent_message": "收到。",
                        },
                    },
                ]
                with rollout.open("a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                return "turn-ipc"

            with patch.object(runner, "_start_turn", side_effect=start_turn):
                self.assertEqual(
                    runner.reply("延迟测试", "wechat-message-1"),
                    "收到。",
                )

    def test_desktop_ipc_start_shape_includes_local_image(self):
        path = Path("/private/tmp/wechat-photo.jpg")
        params = CodexDesktopIpc._start_params(
            "thread-1", "【WeChat】\n照片", "wechat-image-1", [path]
        )
        self.assertEqual(
            params["turnStart"]["request"]["input"][1],
            {"type": "localImage", "path": str(path)},
        )
        self.assertEqual(
            params["turnStart"]["context"]["restoreMessage"]["context"][
                "imageAttachments"
            ][0]["localPath"],
            str(path),
        )

    def test_inbound_image_is_downloaded_and_queued_with_local_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"version": 1, "poll_buf": "c1", "seeded": True, "queue": [], "seen": []}
            (root / "state.json").write_text(json.dumps(state))
            local_image = root / "received.jpg"
            local_image.write_bytes(b"\xff\xd8\xfftest")
            message = {
                "from_user_id": "wechat-user-1",
                "context_token": "ctx-image",
                "item_list": [
                    {
                        "type": 2,
                        "image_item": {
                            "aeskey": "00" * 16,
                            "media": {"encrypt_query_param": "secret-cdn-parameter"},
                        },
                    }
                ],
            }
            chat = FakeWeChat([("c2", [message])])
            chat.downloaded_image = local_image
            codex = FakeCodex()
            bridge = Bridge(self.make_config(root), chat, codex, lambda _: None)
            self.assertEqual(bridge.poll_once(), 1)
            self.assertEqual(bridge.state["queue"][0]["image_paths"], [str(local_image)])
            self.assertTrue(bridge.process_one())
            self.assertEqual(codex.calls[0][2], [local_image])

    def test_local_image_markdown_is_not_sent_back_as_a_path(self):
        text = sanitize_reply_text(
            "图片已保存。\n\n![照片](</Users/example/private photo.png>)"
        )
        self.assertEqual(text, "图片已保存。\n\n[local image omitted]")

    def test_remote_image_markdown_is_preserved(self):
        text = "参考：![diagram](https://example.com/diagram.png)"
        self.assertEqual(sanitize_reply_text(text), text)

    def test_queue_mode_reads_same_visible_turn_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rollout = root / "rollout.jsonl"
            rollout.write_text("", encoding="utf-8")
            config = self.make_config(root)
            config = Config(
                **{
                    **config.__dict__,
                    "codex_mode": "queue",
                    "rollout_path": rollout,
                    "thread_id": "visible-thread",
                }
            )

            def queue_message(command, **kwargs):
                del kwargs
                self.assertEqual(command[1:4], ["queue", "--thread", "visible-thread"])
                self.assertEqual(command[-1], "【WeChat】\n来自微信的测试")
                events = [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": "desktop-turn",
                            "turn_attribution": {"turn_trigger": "composer"},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "desktop-turn",
                            "last_agent_message": "这条电脑回复不能回微信。",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": "turn-1",
                            "turn_attribution": {"turn_trigger": "queue"},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": "turn-1",
                            "item": {
                                "type": "UserMessage",
                                "content": [
                                    {"type": "text", "text": "【WeChat】\n来自微信的测试"}
                                ],
                            },
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "收到。"}],
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-1",
                            "last_agent_message": "收到。",
                        },
                    },
                ]
                with rollout.open("a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                return subprocess.CompletedProcess(command, 0, "queued", "")

            runner = CodexQueue(config, sleeper=lambda _: None)
            with patch("same_thread_bridge.bridge.subprocess.run", side_effect=queue_message):
                self.assertEqual(runner.reply("来自微信的测试", "message-1"), "收到。")


if __name__ == "__main__":
    unittest.main()
