import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tg_downloader_ui import forwarder


class ForwarderFormattingTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_forwarder_log_and_status_files_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "forwarder.log"
            status_path = root / "forwarder_status.json"

            forwarder.log_line("hello", log_path=log_path)
            forwarder.write_status_file(status_path, {"state": "running"})

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)

    def test_format_forward_message_includes_file_size_and_message_id(self):
        message = SimpleNamespace(
            id=23311,
            text="片名：Demo Movie",
            media=SimpleNamespace(
                document=SimpleNamespace(
                    mime_type="video/mp4",
                    size=1536,
                    attributes=[SimpleNamespace(file_name="Demo.Movie.mp4")],
                )
            ),
        )

        text = forwarder.format_forward_message(message, source_label="Beta Bot")
        self.assertIn("Source: Beta Bot", text)

        self.assertIn("片名：Demo Movie", text)
        self.assertIn("文件: Demo.Movie.mp4", text)
        self.assertIn("大小: 1.5 KB", text)
        self.assertIn("消息ID: 23311", text)

    def test_format_forward_message_skips_text_only_message(self):
        message = SimpleNamespace(id=23312, text="纯文本通知", media=None)

        self.assertEqual(forwarder.format_forward_message(message, source_label="Beta Bot"), "")

    def test_format_forward_message_skips_non_video_document(self):
        message = SimpleNamespace(
            id=23313,
            text="文档说明",
            media=SimpleNamespace(
                document=SimpleNamespace(
                    mime_type="application/pdf",
                    size=1536,
                    attributes=[SimpleNamespace(file_name="manual.pdf")],
                )
            ),
        )

        self.assertEqual(forwarder.format_forward_message(message, source_label="Beta Bot"), "")

    def test_format_forward_message_skips_video_without_text(self):
        message = SimpleNamespace(
            id=23314,
            text="",
            caption="",
            media=SimpleNamespace(
                document=SimpleNamespace(
                    mime_type="video/mp4",
                    size=1536,
                    attributes=[SimpleNamespace(file_name="Demo.Movie.mp4")],
                )
            ),
        )

        self.assertEqual(forwarder.format_forward_message(message, source_label="Beta Bot"), "")

    def test_load_forward_sources_reads_enabled_config_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "id": "alpha_bot",
                                "label": "Alpha Bot",
                                "chat": "alpha_bot",
                                "forward_source": "@alpha_bot",
                                "enabled": True,
                            },
                            {
                                "id": "beta_bot",
                                "label": "Beta Bot",
                                "chat": "beta_bot",
                                "forward_source": "@beta_bot",
                                "enabled": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            sources = forwarder.load_forward_sources(config_path)

            self.assertEqual(
                [(source["id"], source["forward_source"]) for source in sources],
                [("alpha_bot", "@alpha_bot"), ("beta_bot", "@beta_bot")],
            )

    def test_format_forward_message_ignores_empty_message_even_with_source(self):
        message = SimpleNamespace(id=23312, text="", media=None)

        self.assertEqual(
            forwarder.format_forward_message(message, source_label="Beta Bot"),
            "",
        )

    def test_parse_proxy_url_for_telethon(self):
        self.assertEqual(
            forwarder.parse_proxy_url("socks5://127.0.0.1:1080"),
            {
                "proxy_type": "socks5",
                "addr": "127.0.0.1",
                "port": 1080,
                "username": None,
                "password": None,
                "rdns": True,
            },
        )
        self.assertEqual(
            forwarder.parse_proxy_url("http://user:p%40ss@10.0.0.1:7890"),
            {
                "proxy_type": "http",
                "addr": "10.0.0.1",
                "port": 7890,
                "username": "user",
                "password": "p@ss",
                "rdns": True,
            },
        )
        self.assertIsNone(forwarder.parse_proxy_url(""))

    def test_validate_runtime_config_requires_telegram_credentials_and_channel(self):
        with self.assertRaisesRegex(RuntimeError, "TGDL_API_ID is required"):
            forwarder.validate_runtime_config(api_id="", api_hash="hash", channel_id="-1001")
        with self.assertRaisesRegex(RuntimeError, "TGDL_API_HASH is required"):
            forwarder.validate_runtime_config(api_id="12345", api_hash="", channel_id="-1001")
        with self.assertRaisesRegex(RuntimeError, "TGDL_FORWARD_CHANNEL_ID is required"):
            forwarder.validate_runtime_config(api_id="12345", api_hash="hash", channel_id="")

    def test_validate_runtime_config_normalizes_channel_internal_id(self):
        self.assertEqual(
            forwarder.validate_runtime_config(
                api_id="12345",
                api_hash="hash",
                channel_id="1234567890",
            ),
            (12345, "hash", -1001234567890),
        )
        self.assertEqual(
            forwarder.validate_runtime_config(
                api_id="12345",
                api_hash="hash",
                channel_id="-1001234567890",
            ),
            (12345, "hash", -1001234567890),
        )

    def test_resolve_runtime_config_falls_back_to_web_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.txt"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "telegram": {
                            "api_id": "12345",
                            "api_hash": "hash-value",
                            "session_file": str(session_file),
                            "forward_channel_id": "-1001234567890",
                            "proxy": "socks5://127.0.0.1:1080",
                        }
                    }
                ),
                encoding="utf-8",
            )

            runtime = forwarder.resolve_runtime_config(config_path=config_path)

            self.assertEqual(runtime["api_id"], 12345)
            self.assertEqual(runtime["api_hash"], "hash-value")
            self.assertEqual(runtime["session_file"], session_file)
            self.assertEqual(runtime["channel_id"], -1001234567890)
            self.assertEqual(runtime["proxy"], "socks5://127.0.0.1:1080")

    def test_read_status_marks_old_heartbeat_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forwarder_status.json"
            path.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "updated_at_epoch": 1000,
                        "source": "@alpha_bot",
                        "channel_id": -1001234567890,
                    }
                ),
                encoding="utf-8",
            )

            status = forwarder.read_status(path, now_epoch=1200, stale_seconds=90)

            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["source"], "@alpha_bot")
            self.assertEqual(status["channel_id"], -1001234567890)


if __name__ == "__main__":
    unittest.main()
