import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tg_downloader_ui import forwarder


class ForwarderFormattingTests(unittest.TestCase):
    def test_format_forward_message_includes_file_size_and_message_id(self):
        message = SimpleNamespace(
            id=23311,
            text="片名：Demo Movie",
            media=SimpleNamespace(
                document=SimpleNamespace(
                    size=1536,
                    attributes=[SimpleNamespace(file_name="Demo.Movie.mp4")],
                )
            ),
        )

        text = forwarder.format_forward_message(message)

        self.assertIn("片名：Demo Movie", text)
        self.assertIn("文件: Demo.Movie.mp4", text)
        self.assertIn("大小: 1.5 KB", text)
        self.assertIn("消息ID: 23311", text)

    def test_parse_proxy_url_for_telethon(self):
        self.assertEqual(
            forwarder.parse_proxy_url("socks5://127.0.0.1:7891"),
            ("socks5", "127.0.0.1", 7891),
        )
        self.assertIsNone(forwarder.parse_proxy_url(""))

    def test_read_status_marks_old_heartbeat_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forwarder_status.json"
            path.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "updated_at_epoch": 1000,
                        "source": "@Youxiu_bot",
                        "channel_id": -1004496489706,
                    }
                ),
                encoding="utf-8",
            )

            status = forwarder.read_status(path, now_epoch=1200, stale_seconds=90)

            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["source"], "@Youxiu_bot")
            self.assertEqual(status["channel_id"], -1004496489706)


if __name__ == "__main__":
    unittest.main()
