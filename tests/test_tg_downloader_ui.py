import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

import tg_downloader_ui.app as app
from tg_downloader_ui.app import (
    DownloadWorker,
    JobStore,
    build_tdl_base_args,
    build_final_filename,
    extract_export_metadata,
    extract_title,
    parse_tdl_progress,
    sanitize_filename,
)


class MetadataParsingTests(unittest.TestCase):
    def test_extracts_title_from_message_text(self):
        text = (
            "片名：小黄人与大怪兽 抢先版\n"
            "又名：小黄人大眼萌3 / Mega Minions\n"
            "地区：美国"
        )

        self.assertEqual(extract_title(text), "小黄人与大怪兽 抢先版")

    def test_sanitizes_filename_but_keeps_chinese_and_spaces(self):
        raw = '小黄人与大怪兽 抢先版:/\\*?"<>|'

        self.assertEqual(sanitize_filename(raw), "小黄人与大怪兽 抢先版")

    def test_extracts_metadata_from_tdl_export_json(self):
        payload = {
            "id": 7487350635,
            "messages": [
                {
                    "id": 23311,
                    "file": "[youxiu]正片.mp4",
                    "text": "片名：小黄人与大怪兽 抢先版\n地区：美国",
                }
            ],
        }

        metadata = extract_export_metadata(json.dumps(payload, ensure_ascii=False), 23311)

        self.assertEqual(metadata.dialog_id, 7487350635)
        self.assertEqual(metadata.message_id, 23311)
        self.assertEqual(metadata.source_file, "[youxiu]正片.mp4")
        self.assertEqual(metadata.title, "小黄人与大怪兽 抢先版")
        self.assertEqual(metadata.extension, ".mp4")
        self.assertEqual(
            build_final_filename(metadata),
            "小黄人与大怪兽 抢先版.mp4",
        )


class ProgressParsingTests(unittest.TestCase):
    def test_parses_tdl_progress_line_with_ansi_sequences(self):
        line = (
            "\x1b[34m优影臻享(7487350635):23311 -> /mn~\x1b[0m "
            "\x1b[91m72.8%\x1b[0m [\x1b[36m2.23 GB\x1b[0m "
            "in \x1b[32m12m2.261s\x1b[0m; ~ETA: \x1b[32m4m41s\x1b[0m; "
            "\x1b[35m3.16 MB\x1b[0m/s]"
        )

        progress = parse_tdl_progress(line)

        self.assertEqual(progress["percent"], 72.8)
        self.assertEqual(progress["downloaded"], "2.23 GB")
        self.assertEqual(progress["eta"], "4m41s")
        self.assertEqual(progress["speed"], "3.16 MB/s")
        self.assertIsNone(progress["flood_wait_seconds"])

    def test_parses_flood_wait_error(self):
        progress = parse_tdl_progress('err_msg": "FLOOD_WAIT_620"')

        self.assertEqual(progress["flood_wait_seconds"], 620)


class CommandConstructionTests(unittest.TestCase):
    def test_tdl_base_args_pin_root_storage(self):
        args = build_tdl_base_args()

        self.assertIn("--storage", args)
        self.assertIn("type=bolt,path=/root/.tdl/data", args)
        self.assertLess(args.index("--storage"), args.index("--proxy"))


class WorkerSkipTests(unittest.TestCase):
    def test_existing_desired_final_file_skips_before_collision_name(self):
        class ExportOnlyWorker(DownloadWorker):
            def __init__(self, store, stop_event, payload):
                super().__init__(store, stop_event)
                self.payload = payload
                self.download_called = False

            def run_command(self, job_id, cmd, status):
                if status == "exporting":
                    job = self.store.get_job(job_id)
                    Path(job["export_path"]).write_text(
                        json.dumps(self.payload), encoding="utf-8"
                    )
                    return 0
                if status == "downloading":
                    self.download_called = True
                    raise AssertionError("download should not run when final file exists")
                return 0

        original_download_dir = app.DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app.DOWNLOAD_DIR = root / "downloads"
                app.DOWNLOAD_DIR.mkdir()
                existing = app.DOWNLOAD_DIR / "Existing Movie.mp4"
                existing.write_bytes(b"already downloaded")
                stale_tmp = app.DOWNLOAD_DIR / "7487350635_23311_Existing Movie.mp4.tmp"
                stale_tmp.write_bytes(b"partial duplicate")

                store = JobStore(root / "state")
                store.init()
                queued = store.create_job(23311)
                job = store.claim_next()

                payload = {
                    "id": 7487350635,
                    "messages": [
                        {
                            "id": queued["message_id"],
                            "file": "Existing Movie.mp4",
                            "text": "",
                        }
                    ],
                }
                worker = ExportOnlyWorker(store, threading.Event(), payload)
                worker.process_job(job)

                result = store.get_job(job["id"])
                self.assertFalse(worker.download_called)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["progress"], 100)
                self.assertEqual(result["final_filename"], "Existing Movie.mp4")
                self.assertEqual(Path(result["final_path"]), existing)
                self.assertFalse((app.DOWNLOAD_DIR / "Existing Movie - 23311.mp4").exists())
                self.assertFalse(stale_tmp.exists())
        finally:
            app.DOWNLOAD_DIR = original_download_dir


class JobStoreInitTests(unittest.TestCase):
    def test_init_marks_stale_active_jobs_failed(self):
        original_download_dir = app.DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app.DOWNLOAD_DIR = root / "downloads"

                store = JobStore(root / "state")
                store.init()
                now = app.utcish_now()
                with contextlib.closing(store.connect()) as db:
                    for status in ["exporting", "downloading", "renaming"]:
                        db.execute(
                            """
                            INSERT INTO jobs (message_id, status, created_at, updated_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (23311, status, now, now),
                        )
                    db.execute(
                        """
                        INSERT INTO jobs (message_id, status, created_at, updated_at)
                        VALUES (?, 'queued', ?, ?)
                        """,
                        (23312, now, now),
                    )
                    db.commit()

                store.init()
                jobs = sorted(store.list_jobs(), key=lambda item: item["message_id"])
                active_results = [job for job in jobs if job["message_id"] == 23311]
                queued = next(job for job in jobs if job["message_id"] == 23312)

                self.assertEqual([job["status"] for job in active_results], ["failed"] * 3)
                self.assertTrue(
                    all(
                        job["error"] == "service restarted while job was active"
                        for job in active_results
                    )
                )
                self.assertEqual(queued["status"], "queued")
        finally:
            app.DOWNLOAD_DIR = original_download_dir


if __name__ == "__main__":
    unittest.main()
