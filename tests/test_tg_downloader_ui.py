import contextlib
import http.client
import json
import tempfile
import threading
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            "又名：小黄人大眼萌 / Mega Minions\n"
            "地区：美国"
        )

        self.assertEqual(extract_title(text), "小黄人与大怪兽 抢先版")

    def test_sanitizes_filename_but_keeps_chinese_and_spaces(self):
        raw = '小黄人与大怪兽 抢先版/\\*?"<>|'

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


class ConfigAuthTests(unittest.TestCase):
    def test_default_admin_password_can_be_changed_and_invalidates_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="admin123",
            )
            config.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)

            self.assertTrue(auth.verify_password("admin", "admin123"))
            token = auth.create_session("admin")
            self.assertIsNotNone(auth.get_session(token))

            auth.change_password("admin", "admin123", "new-password")

            self.assertFalse(auth.verify_password("admin", "admin123"))
            self.assertTrue(auth.verify_password("admin", "new-password"))
            self.assertIsNone(auth.get_session(token))

    def test_download_dir_is_persisted_and_must_be_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="admin123",
            )
            config.init()
            target = root / "new-downloads"

            config.set_download_dir(target)

            self.assertEqual(config.get_download_dir(), target)
            self.assertTrue(target.exists())
            reloaded = app.ConfigStore(
                root / "state",
                default_download_dir=root / "fallback",
                default_user="admin",
                default_password="admin123",
            )
            reloaded.init()
            self.assertEqual(reloaded.get_download_dir(), target)
            with self.assertRaises(ValueError):
                config.set_download_dir(Path("relative/path"))


class IndexTemplateTests(unittest.TestCase):
    def test_index_has_left_navigation_password_page_and_directory_dialog(self):
        html = app.INDEX_HTML

        self.assertIn('class="sidebar"', html)
        self.assertIn('data-page="downloads"', html)
        self.assertIn('data-page="paths"', html)
        self.assertIn('data-page="password"', html)
        self.assertIn('id="page-password"', html)
        self.assertIn('id="dirDialog"', html)


class JobManagementTests(unittest.TestCase):
    def make_store(self, root):
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_user="admin",
            default_password="admin123",
        )
        config.init()
        store = JobStore(root / "state", config)
        store.init()
        return config, store

    def test_job_snapshots_current_download_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store = self.make_store(root)

            first = store.create_job(23311)
            next_dir = root / "other-downloads"
            config.set_download_dir(next_dir)
            second = store.create_job(23312)

            self.assertEqual(Path(first["download_dir"]), root / "downloads")
            self.assertEqual(Path(store.get_job(first["id"])["download_dir"]), root / "downloads")
            self.assertEqual(Path(second["download_dir"]), next_dir)

    def test_cancel_queued_job_and_retry_canceled_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)

            canceled = store.cancel_job(job["id"])

            self.assertEqual(canceled["status"], "canceled")
            self.assertEqual(canceled["cancel_requested"], 1)
            retried = store.retry_job(job["id"])
            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["cancel_requested"], 0)

    def test_cancel_active_job_sets_cancel_requested_and_keeps_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)
            store.update_job(job["id"], status="downloading", process_pid=12345)

            canceled = store.cancel_job(job["id"])

            self.assertEqual(canceled["status"], "downloading")
            self.assertEqual(canceled["cancel_requested"], 1)
            self.assertEqual(canceled["process_pid"], 12345)

    def test_delete_finished_job_removes_row_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)
            export_path = Path(job["export_path"])
            log_path = Path(job["log_path"])
            export_path.write_text("{}", encoding="utf-8")
            store.finish_job(job["id"], "failed", error="boom")

            store.delete_job(job["id"])

            self.assertIsNone(store.get_job(job["id"]))
            self.assertFalse(export_path.exists())
            self.assertFalse(log_path.exists())


class AuthHttpTests(unittest.TestCase):
    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=request_headers)
        response = conn.getresponse()
        payload = response.read().decode("utf-8")
        headers_out = dict(response.getheaders())
        conn.close()
        return response.status, headers_out, payload

    def test_login_cookie_allows_api_access_and_logout_revokes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="admin123",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)
            server = app.DownloadServer(("127.0.0.1", 0), app.RequestHandler, store, config, auth)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                status, _, _ = self.request(port, "GET", "/api/jobs")
                self.assertEqual(status, 401)

                status, headers, payload = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "admin123"},
                )
                self.assertEqual(status, 200)
                self.assertIn("tgdl_session=", headers["Set-Cookie"])
                cookie = headers["Set-Cookie"].split(";", 1)[0]

                status, _, payload = self.request(
                    port,
                    "GET",
                    "/api/jobs",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)["jobs"], [])

                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                status, _, _ = self.request(
                    port,
                    "GET",
                    "/api/jobs",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 401)
            finally:
                server.shutdown()
                server.server_close()

    def test_directory_browser_requires_auth_and_lists_child_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            (downloads / "movies").mkdir()
            (downloads / "music").mkdir()
            (downloads / "readme.txt").write_text("not a directory", encoding="utf-8")
            config = app.ConfigStore(
                root / "state",
                default_download_dir=downloads,
                default_user="admin",
                default_password="admin123",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)
            server = app.DownloadServer(("127.0.0.1", 0), app.RequestHandler, store, config, auth)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                query_path = urllib.parse.quote(str(downloads))

                status, _, _ = self.request(port, "GET", f"/api/fs/dirs?path={query_path}")
                self.assertEqual(status, 401)

                status, headers, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "admin123"},
                )
                self.assertEqual(status, 200)
                cookie = headers["Set-Cookie"].split(";", 1)[0]

                status, _, payload = self.request(
                    port,
                    "GET",
                    f"/api/fs/dirs?path={query_path}",
                    headers={"Cookie": cookie},
                )

                self.assertEqual(status, 200)
                data = json.loads(payload)
                self.assertEqual(Path(data["path"]), downloads.resolve())
                self.assertEqual(Path(data["parent"]), downloads.parent.resolve())
                self.assertEqual(
                    [(item["name"], Path(item["path"])) for item in data["entries"]],
                    [
                        ("movies", (downloads / "movies").resolve()),
                        ("music", (downloads / "music").resolve()),
                    ],
                )
            finally:
                server.shutdown()
                server.server_close()


class ForwarderStatusApiTests(unittest.TestCase):
    def test_app_reads_forwarder_status_json_without_importing_forwarder_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forwarder_status.json"
            path.write_text(
                json.dumps(
                    {
                        "state": "running",
                        "updated_at_epoch": 1000,
                        "channel_title": "专享的moment",
                    }
                ),
                encoding="utf-8",
            )

            status = app.read_forwarder_status(path, now_epoch=1010)

            self.assertEqual(status["state"], "running")
            self.assertEqual(status["channel_title"], "专享的moment")


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
