"""Unit tests for Telegram control bot (no network)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tg_downloader_ui.app import ConfigStore, JobStore, is_telegram_url, parse_urls
from tg_downloader_ui.bot import (
    BotController,
    format_job_terminal_message,
    is_private_chat,
    normalize_bot_config,
    parse_operator_text,
    public_bot_config,
    status_label_zh,
    token_hint,
)


class BotHelpersTests(unittest.TestCase):
    def test_token_hint_masks(self):
        self.assertEqual(token_hint(""), "")
        self.assertEqual(token_hint("ab"), "••••")
        self.assertTrue(token_hint("test-bot-token").startswith("••••"))

    def test_normalize_and_public_config(self):
        raw = normalize_bot_config({"enabled": True, "token": "test-bot-token", "notify_chat_id": "42"})
        self.assertTrue(raw["enabled"])
        self.assertEqual(raw["notify_chat_id"], 42)
        pub = public_bot_config(raw)
        self.assertTrue(pub["token_set"])
        self.assertNotIn("token", pub)
        self.assertNotEqual(pub.get("token_hint"), "test-bot-token")

    def test_parse_operator_text(self):
        def check(text, kind, **kwargs):
            parsed = parse_operator_text(text, is_telegram_url=is_telegram_url)
            self.assertEqual(parsed.kind, kind)
            for key, value in kwargs.items():
                self.assertEqual(getattr(parsed, key), value)

        check("/help", "help")
        check("/start@MyBot", "help")
        check("/jobs", "jobs")
        check("/status 12", "status", args="12")
        check("/cancel 3", "cancel", args="3")
        check("https://t.me/foo/99", "url", urls=["https://t.me/foo/99"])
        check("/dl https://t.me/foo/99", "url")
        check("23311", "message_id", message_id=23311)
        check("/dl 23311", "message_id", message_id=23311)
        check("nope", "unknown")
        # Chinese bare words are not commands (commands stay English)
        check("帮助", "unknown")
        check("取消 3", "unknown")

    def test_is_private_chat(self):
        self.assertTrue(is_private_chat({"type": "private", "id": 1}))
        self.assertFalse(is_private_chat({"type": "group", "id": 1}))
        self.assertFalse(is_private_chat(None))


class BotConfigStoreTests(unittest.TestCase):
    def test_get_set_bot_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp), default_download_dir=Path(tmp) / "dl")
            store.init()
            cfg = store.get_bot_config()
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["token"], "")
            store.set_bot_config(enabled=True, token="test-bot-token")
            cfg = store.get_bot_config()
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["token"], "test-bot-token")
            # empty token preserves
            store.set_bot_config(enabled=False, token="", preserve_token=True)
            cfg = store.get_bot_config()
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["token"], "test-bot-token")
            store.set_bot_notify_chat_id(1001)
            self.assertEqual(store.get_bot_config()["notify_chat_id"], 1001)


class FakeApi:
    def __init__(self, token: str = "test-bot-token") -> None:
        self.token = token
        self.sent: list[tuple[int, str, dict | None]] = []
        self.answered: list[str] = []
        self.updates: list[dict] = []

    def get_updates(self, offset=None, timeout=25):
        return list(self.updates)

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((int(chat_id), str(text), reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered.append(callback_query_id)
        return {}


class BotControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = ConfigStore(root, default_download_dir=root / "dl")
        self.config.init()
        self.config.set_bot_config(enabled=True, token="test-bot-token")
        self.config.set_sources(
            [
                {
                    "id": "alpha_bot",
                    "label": "Alpha",
                    "chat": "alpha_bot",
                    "forward_source": "@alpha_bot",
                    "enabled": True,
                }
            ],
            "alpha_bot",
        )
        self.store = JobStore(root, self.config)
        self.store.init()
        self.stop = threading.Event()
        self.api = FakeApi()
        self.bot = BotController(
            self.store,
            self.config,
            stop_event=self.stop,
            worker_pool=None,
            is_telegram_url=is_telegram_url,
            parse_urls=parse_urls,
            api_factory=lambda token: self.api,
            health_interval=3600,
        )
        self.bot._api = self.api

    def tearDown(self):
        self.stop.set()
        self.tmp.cleanup()

    def test_url_enqueue_private(self):
        self.bot._dispatch_text(42, "https://t.me/channel/123", self.config.get_bot_config())
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "url")
        self.assertTrue(any("已入队" in text for _, text, _ in self.api.sent))

    def test_message_id_single_source(self):
        self.bot._dispatch_text(42, "23311", self.config.get_bot_config())
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mode"], "message_id")
        self.assertEqual(int(jobs[0]["message_id"]), 23311)
        self.assertEqual(jobs[0]["source_id"], "alpha_bot")

    def test_message_id_multi_source_picker(self):
        self.config.set_sources(
            [
                {
                    "id": "alpha_bot",
                    "label": "Alpha",
                    "chat": "alpha_bot",
                    "forward_source": "@alpha_bot",
                    "enabled": True,
                },
                {
                    "id": "beta_bot",
                    "label": "Beta",
                    "chat": "beta_bot",
                    "forward_source": "@beta_bot",
                    "enabled": True,
                },
            ],
            "alpha_bot",
        )
        self.bot._dispatch_text(42, "100", self.config.get_bot_config())
        self.assertEqual(len(self.store.list_jobs()), 0)
        self.assertTrue(any(m is not None for _, _, m in self.api.sent))
        # pick beta
        pending_id = next(iter(self.bot._pending))
        self.bot._handle_callback(
            {
                "id": "cq1",
                "data": f"src:beta_bot:{pending_id}",
                "message": {"chat": {"type": "private", "id": 42}},
            },
            self.config.get_bot_config(),
        )
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source_id"], "beta_bot")

    def test_group_ignored(self):
        self.bot._handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"type": "group", "id": -5},
                    "text": "https://t.me/channel/1",
                },
            },
            self.config.get_bot_config(),
        )
        self.assertEqual(len(self.store.list_jobs()), 0)

    def test_cancel_command(self):
        job = self.store.create_job(
            0,
            mode="url",
            input_payload={"urls": ["https://t.me/c/1/2"]},
            source_label="url",
        )
        self.bot._dispatch_text(42, f"/cancel {job['id']}", self.config.get_bot_config())
        updated = self.store.get_job(int(job["id"]))
        self.assertEqual(updated["status"], "canceled")

    def test_backend_error_reply(self):
        with mock.patch.object(self.store, "list_jobs", side_effect=RuntimeError("db down")):
            # status still works via get_job path; force jobs failure
            self.bot._cmd_jobs(99)
        self.assertTrue(any("后端不可用" in text or "db down" in text for _, text, _ in self.api.sent))

    def test_notify_job_finished(self):
        job = self.store.create_job(
            0,
            mode="url",
            input_payload={"urls": ["https://t.me/c/1/2"]},
            source_label="url",
        )
        job_id = int(job["id"])
        self.store.update_job(
            job_id,
            title="示例影片",
            final_filename="示例影片 (2024).mp4",
            final_path="/downloads/Movies/示例影片 (2024)/示例影片 (2024).mp4",
            downloaded="1.2 GB",
        )
        self.bot._job_chats[job_id] = 77
        self.bot.notify_job_finished(job_id, "done")
        text = next(t for chat, t, _ in self.api.sent if chat == 77)
        self.assertIn(f"任务 #{job_id} 已完成", text)
        self.assertIn("片名：示例影片", text)
        self.assertIn("文件：示例影片 (2024).mp4", text)
        self.assertIn("路径：", text)
        self.assertIn("大小：1.2 GB", text)

    def test_format_job_terminal_failed(self):
        text = format_job_terminal_message(
            9,
            "failed",
            {
                "title": "坏片",
                "source_file": "bad.mp4",
                "error": "export missing",
                "mode": "message_id",
                "message_id": 12,
                "source_label": "Alpha",
            },
            error="export missing",
            lang="zh",
        )
        self.assertIn("任务 #9 失败", text)
        self.assertIn("片名：坏片", text)
        self.assertIn("文件：bad.mp4", text)
        self.assertIn("原因：export missing", text)
        self.assertEqual(status_label_zh("canceled"), "已取消")
        en = format_job_terminal_message(
            9,
            "canceled",
            {"title": "Film", "error": "canceled by user"},
            error="canceled by user",
            lang="en",
        )
        self.assertIn("Job #9 canceled", en)
        self.assertIn("Reason: canceled by user", en)
        self.assertNotIn("→", en)


if __name__ == "__main__":
    unittest.main()
