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

    def _video_message(self, **overrides):
        message = SimpleNamespace(
            id=23311,
            text="片名：Demo Movie",
            caption="",
            photo=None,
            video=None,
            document=None,
            media=SimpleNamespace(
                document=SimpleNamespace(
                    mime_type="video/mp4",
                    size=1536,
                    attributes=[SimpleNamespace(file_name="Demo.Movie.mp4")],
                )
            ),
        )
        for key, value in overrides.items():
            setattr(message, key, value)
        return message

    def test_format_forward_message_includes_file_size_and_message_id(self):
        message = self._video_message()

        text = forwarder.format_forward_message(message, source_label="Beta Bot")
        self.assertIn("Source: Beta Bot", text)

        self.assertIn("片名：Demo Movie", text)
        self.assertIn("File: Demo.Movie.mp4", text)
        self.assertIn("Size: 1.5 KB", text)
        self.assertIn("Message ID: 23311", text)

    def test_format_forward_message_skips_text_only_message(self):
        message = SimpleNamespace(
            id=23312, text="纯文本通知", caption="", media=None, photo=None, document=None, video=None
        )

        self.assertEqual(forwarder.format_forward_message(message, source_label="Beta Bot"), "")

    def test_format_forward_message_skips_non_video_document(self):
        message = SimpleNamespace(
            id=23313,
            text="文档说明",
            caption="",
            photo=None,
            video=None,
            document=None,
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
        message = self._video_message(text="", caption="")

        self.assertEqual(forwarder.format_forward_message(message, source_label="Beta Bot"), "")

    def test_normalize_forwarder_filters_defaults(self):
        self.assertEqual(
            forwarder.normalize_forwarder_filters(None),
            forwarder.DEFAULT_FORWARDER_FILTERS,
        )
        self.assertEqual(
            forwarder.normalize_forwarder_filters({}),
            {
                "media_video": True,
                "media_photo": False,
                "media_document": False,
                "require_text": True,
                "min_size_bytes": 0,
                "max_size_bytes": 0,
                "include_keywords": [],
                "exclude_keywords": [],
            },
        )

    def test_normalize_forwarder_filters_mib_and_keywords(self):
        filters = forwarder.normalize_forwarder_filters(
            {
                "media_photo": True,
                "min_size_mib": 1,
                "max_size_mib": 2,
                "include_keywords": "Alpha, beta\nGamma",
                "exclude_keywords": ["Skip Me"],
            }
        )
        self.assertTrue(filters["media_photo"])
        self.assertEqual(filters["min_size_bytes"], 1024 * 1024)
        self.assertEqual(filters["max_size_bytes"], 2 * 1024 * 1024)
        self.assertEqual(filters["include_keywords"], ["Alpha", "beta", "Gamma"])
        self.assertEqual(filters["exclude_keywords"], ["Skip Me"])

    def test_normalize_forwarder_filters_rejects_max_below_min(self):
        with self.assertRaisesRegex(ValueError, "max_size_bytes"):
            forwarder.normalize_forwarder_filters(
                {"min_size_bytes": 200, "max_size_bytes": 100}
            )

    def test_evaluate_filters_default_skips_photo_and_allows_video(self):
        video = self._video_message()
        ok, reason = forwarder.evaluate_forwarder_filters(video)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

        photo = SimpleNamespace(
            id=1,
            text="pic",
            caption="",
            photo=SimpleNamespace(size=100),
            media=SimpleNamespace(photo=object()),
            document=None,
            video=None,
        )
        ok, reason = forwarder.evaluate_forwarder_filters(photo)
        self.assertFalse(ok)
        self.assertEqual(reason, "media_photo_disabled")

    def test_evaluate_filters_photo_enabled_and_require_text_off(self):
        filters = forwarder.normalize_forwarder_filters(
            {"media_photo": True, "require_text": False}
        )
        photo = SimpleNamespace(
            id=2,
            text="",
            caption="",
            photo=SimpleNamespace(size=200),
            media=SimpleNamespace(photo=object()),
            document=None,
            video=None,
        )
        ok, reason = forwarder.evaluate_forwarder_filters(photo, filters)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        text = forwarder.format_forward_message(
            photo, source_label="Cam", filters=filters
        )
        self.assertEqual(
            text,
            "Source: Cam\nFile: \nSize: 200 B\nMessage ID: 2",
        )

    def test_format_forward_message_media_only_has_no_extra_blank_lines(self):
        message = self._video_message(text="", caption="")
        filters = forwarder.normalize_forwarder_filters({"require_text": False})
        text = forwarder.format_forward_message(
            message, source_label="Beta Bot", filters=filters
        )
        self.assertEqual(
            text,
            "Source: Beta Bot\nFile: Demo.Movie.mp4\nSize: 1.5 KB\nMessage ID: 23311",
        )

    def test_evaluate_filters_document_size_and_keywords(self):
        message = SimpleNamespace(
            id=3,
            text="Alpha release notes",
            caption="",
            photo=None,
            video=None,
            document=None,
            media=SimpleNamespace(
                document=SimpleNamespace(
                    mime_type="application/pdf",
                    size=5000,
                    attributes=[SimpleNamespace(file_name="notes.pdf")],
                )
            ),
        )
        filters = forwarder.normalize_forwarder_filters(
            {
                "media_document": True,
                "min_size_bytes": 1000,
                "max_size_bytes": 10000,
                "include_keywords": ["alpha"],
                "exclude_keywords": [],
            }
        )
        ok, reason = forwarder.evaluate_forwarder_filters(message, filters)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

        too_small = forwarder.normalize_forwarder_filters(
            {"media_document": True, "min_size_bytes": 9000}
        )
        ok, reason = forwarder.evaluate_forwarder_filters(message, too_small)
        self.assertFalse(ok)
        self.assertEqual(reason, "below_min_size")

        excluded = forwarder.normalize_forwarder_filters(
            {"media_document": True, "exclude_keywords": ["RELEASE"]}
        )
        ok, reason = forwarder.evaluate_forwarder_filters(message, excluded)
        self.assertFalse(ok)
        self.assertEqual(reason, "exclude_keyword")

        missing_include = forwarder.normalize_forwarder_filters(
            {"media_document": True, "include_keywords": ["zzz"]}
        )
        ok, reason = forwarder.evaluate_forwarder_filters(message, missing_include)
        self.assertFalse(ok)
        self.assertEqual(reason, "include_keyword")

    def test_load_forwarder_filters_missing_key_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                forwarder.load_forwarder_filters(config_path),
                forwarder.DEFAULT_FORWARDER_FILTERS,
            )

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
