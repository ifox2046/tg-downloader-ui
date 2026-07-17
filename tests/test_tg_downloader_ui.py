import contextlib
import http.client
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tg_downloader_ui.app as app
from tg_downloader_ui.app import (
    DownloadWorker,
    ExportMetadata,
    JobStore,
    build_media_plan,
    build_tdl_base_args,
    build_final_filename,
    extract_export_metadata,
    extract_title,
    parse_tdl_progress,
    sanitize_filename,
    write_sidecar_metadata,
)


def extract_shell_function(script: str, name: str) -> str:
    declaration = f"{name}() {{"
    lines = script.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == declaration:
            break
    else:
        raise ValueError(f"shell function not found: {name}")

    depth = 0
    function_lines = []
    for line in lines[start:]:
        function_lines.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "\n".join(function_lines)
    raise ValueError(f"shell function is not closed: {name}")


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
                    "file": "[source]正片.mp4",
                    "text": "片名：小黄人与大怪兽 抢先版\n地区：美国",
                }
            ],
        }

        metadata = extract_export_metadata(json.dumps(payload, ensure_ascii=False), 23311)

        self.assertEqual(metadata.dialog_id, 7487350635)
        self.assertEqual(metadata.message_id, 23311)
        self.assertEqual(metadata.source_file, "[source]正片.mp4")
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


class TelegramProxyParsingTests(unittest.TestCase):
    def test_parse_telegram_proxy_url_keeps_credentials(self):
        self.assertEqual(
            app.parse_telegram_proxy_url("http://proxy:proxy@10.72.40.221:7890"),
            {
                "proxy_type": "http",
                "addr": "10.72.40.221",
                "port": 7890,
                "username": "proxy",
                "password": "proxy",
                "rdns": True,
            },
        )

    def test_parse_telegram_proxy_url_none_and_invalid(self):
        self.assertIsNone(app.parse_telegram_proxy_url(""))
        with self.assertRaisesRegex(RuntimeError, "Telegram proxy must be"):
            app.parse_telegram_proxy_url("ftp://127.0.0.1:1080")


class CommandConstructionTests(unittest.TestCase):
    def test_tdl_base_args_uses_tdl_specific_proxy_before_global_proxy(self):
        args = app.build_tdl_base_args(
            tdl_bin="/usr/local/bin/tdl",
            storage="type=bolt,path=/data/tdl",
            global_proxy="socks5://127.0.0.1:1080",
            tdl_proxy="http://127.0.0.1:8080",
        )

        self.assertEqual(args[0], "/usr/local/bin/tdl")
        self.assertIn("--storage", args)
        self.assertIn("type=bolt,path=/data/tdl", args)
        self.assertLess(args.index("--storage"), args.index("--proxy"))
        self.assertEqual(args[args.index("--proxy") + 1], "http://127.0.0.1:8080")

    def test_tdl_base_args_omits_proxy_when_none_is_configured(self):
        args = app.build_tdl_base_args(
            tdl_bin="/usr/local/bin/tdl",
            storage="type=bolt,path=/data/tdl",
            global_proxy="",
            tdl_proxy="",
        )

        self.assertNotIn("--proxy", args)

    def test_tdl_base_args_falls_back_to_telegram_config_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "config.json").write_text(
                json.dumps(
                    {
                        "telegram": {
                            "proxy": "http://proxy:proxy@10.0.0.1:7890",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "STATE_DIR", state_dir), mock.patch.dict(
                os.environ, {"TGDL_TDL_PROXY": "", "TGDL_PROXY": ""}, clear=False
            ), mock.patch.object(app, "GLOBAL_PROXY", ""):
                args = app.build_tdl_base_args(
                    tdl_bin="/usr/local/bin/tdl",
                    storage="type=bolt,path=/data/tdl",
                    global_proxy="",
                    tdl_proxy=None,
                )

        self.assertIn("--proxy", args)
        self.assertEqual(
            args[args.index("--proxy") + 1], "http://proxy:proxy@10.0.0.1:7890"
        )

    def test_shutdown_stopped_process_continues_before_sigint(self):
        continue_signal = getattr(app, "PROCESS_CONTINUE_SIGNAL", None) or 1002

        class FakeProcess:
            def __init__(self):
                self.signals = []
                self.killed = False

            def send_signal(self, value):
                self.signals.append(value)

            def wait(self, timeout=None):
                return 130

            def kill(self):
                self.killed = True

        proc = FakeProcess()

        with mock.patch.object(app, "PROCESS_CONTINUE_SIGNAL", continue_signal, create=True):
            app.stop_download_process(proc)

        self.assertEqual(proc.signals, [continue_signal, signal.SIGINT])
        self.assertFalse(proc.killed)


class SecurityHelperTests(unittest.TestCase):
    def test_redact_command_args_hides_proxy_credentials(self):
        args = app.redact_command_args(
            [
                "tdl",
                "--proxy",
                "socks5://user:password@127.0.0.1:1080",
                "download",
            ]
        )

        self.assertEqual(args, ["tdl", "--proxy", "<redacted>", "download"])

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_config_database_and_job_log_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_password="test-password",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            job = store.create_job(23311)
            store.append_log(job["id"], "hello\n")

            self.assertEqual(stat.S_IMODE(config.state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.db_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(Path(job["log_path"]).stat().st_mode), 0o600
            )


class MediaPlanTests(unittest.TestCase):
    def test_movie_plan_uses_title_and_year_directory(self):
        root = Path("/downloads")
        metadata = ExportMetadata(
            dialog_id=7487350635,
            message_id=23402,
            source_file="[source]正片.mp4",
            title="绵羊侦探团",
            extension=".mp4",
            text=(
                "片名：绵羊侦探团\n"
                "首映：2026-05-08(美国)\n"
                "类型：喜剧 / 动作 / 悬疑\n"
                "简介：牧羊人乔治最爱给羊群读侦探小说。"
            ),
        )

        plan = build_media_plan(metadata, root)

        self.assertEqual(plan.media_type, "movie")
        self.assertEqual(plan.title, "绵羊侦探团")
        self.assertEqual(plan.year, "2026")
        self.assertEqual(plan.final_filename, "绵羊侦探团 (2026).mp4")
        self.assertEqual(
            plan.final_path,
            root / "Movies" / "绵羊侦探团 (2026)" / "绵羊侦探团 (2026).mp4",
        )

    def test_tv_plan_uses_season_episode_directory(self):
        root = Path("/downloads")
        metadata = ExportMetadata(
            dialog_id=7487350635,
            message_id=24001,
            source_file="庆余年.S02E03.mkv",
            title="庆余年",
            extension=".mkv",
            text="片名：庆余年\n首映：2024-05-16(中国大陆)\n类型：剧情 / 古装",
        )

        plan = build_media_plan(metadata, root)

        self.assertEqual(plan.media_type, "tv")
        self.assertEqual(plan.title, "庆余年")
        self.assertEqual(plan.year, "2024")
        self.assertEqual(plan.season, 2)
        self.assertEqual(plan.episode, 3)
        self.assertEqual(plan.final_filename, "庆余年 - S02E03.mkv")
        self.assertEqual(
            plan.final_path,
            root / "TV" / "庆余年 (2024)" / "Season 02" / "庆余年 - S02E03.mkv",
        )

    def test_plain_file_without_structured_text_keeps_legacy_flat_path(self):
        root = Path("/downloads")
        metadata = ExportMetadata(
            dialog_id=7487350635,
            message_id=23311,
            source_file="Existing Movie.mp4",
            title="Existing Movie",
            extension=".mp4",
            text="",
        )

        plan = build_media_plan(metadata, root)

        self.assertEqual(plan.media_type, "file")
        self.assertEqual(plan.final_filename, "Existing Movie.mp4")
        self.assertEqual(plan.final_path, root / "Existing Movie.mp4")

    def test_sidecars_preserve_original_telegram_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = ExportMetadata(
                dialog_id=7487350635,
                message_id=23402,
                source_file="[source]正片.mp4",
                title="绵羊侦探团",
                extension=".mp4",
                text="片名：绵羊侦探团\n首映：2026-05-08(美国)",
            )
            plan = build_media_plan(metadata, root)
            plan.final_path.parent.mkdir(parents=True)
            plan.final_path.write_bytes(b"movie")

            sidecars = write_sidecar_metadata(plan, metadata, source_label="优影臻享")

            self.assertEqual(
                {path.name for path in sidecars},
                {"绵羊侦探团 (2026).telegram.json", "绵羊侦探团 (2026).telegram.txt"},
            )
            payload = json.loads(
                (plan.final_path.parent / "绵羊侦探团 (2026).telegram.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["message_id"], 23402)
            self.assertEqual(payload["source_file"], "[source]正片.mp4")
            self.assertEqual(payload["media"]["year"], "2026")
            text = (plan.final_path.parent / "绵羊侦探团 (2026).telegram.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("source: 优影臻享", text)
            self.assertIn("片名：绵羊侦探团", text)


class ConfigAuthTests(unittest.TestCase):
    def test_login_failures_block_and_success_clears_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_password="test-password",
            )
            config.init()
            auth = app.AuthManager(config)
            key = "127.0.0.1:admin"

            for _ in range(5):
                auth.record_login_failure(key, now=1000)

            self.assertEqual(auth.login_retry_after(key, now=1000), 900)
            auth.clear_login_failures(key)
            self.assertEqual(auth.login_retry_after(key, now=1000), 0)

    def test_new_passwords_require_eight_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_password="",
            )
            config.init()

            with self.assertRaisesRegex(ValueError, "at least 8"):
                config.initialize("owner", "short", root / "downloads")

    def test_missing_auth_requires_initial_setup_without_default_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="",
            )

            config.init()

            self.assertTrue(config.requires_setup())
            self.assertFalse(config.verify_password("admin", "test-password"))

    def test_initialize_sets_admin_and_download_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "fallback",
                default_user="admin",
                default_password="",
            )
            config.init()
            downloads = root / "downloads"

            config.initialize(
                username="owner",
                password="strong-password",
                download_dir=downloads,
                telegram={
                    "api_id": "12345",
                    "api_hash": "hash-value",
                    "session_file": "/config/session.txt",
                    "forward_channel_id": "-1001234567890",
                },
            )

            self.assertFalse(config.requires_setup())
            self.assertEqual(config.get_download_dir(), downloads)
            self.assertTrue(config.verify_password("owner", "strong-password"))
            self.assertEqual(config.get_telegram_config()["api_id"], "12345")

    def test_initialize_uses_default_download_dir_when_input_is_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            config = app.ConfigStore(
                root / "state",
                default_download_dir=downloads,
                default_user="admin",
                default_password="",
            )
            config.init()

            config.initialize(username="owner", password="strong-password", download_dir="")

            self.assertEqual(config.get_download_dir(), downloads)
            self.assertTrue(downloads.exists())

    def test_telegram_config_can_be_updated_after_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
            )
            config.init()

            config.set_telegram_config(
                {
                    "api_id": "12345",
                    "api_hash": "hash-value",
                    "session_file": "/config/session.txt",
                    "forward_channel_id": "-1001234567890",
                    "proxy": "socks5://127.0.0.1:1080",
                }
            )

            telegram = config.get_telegram_config()
            self.assertEqual(telegram["api_id"], "12345")
            self.assertEqual(telegram["api_hash"], "hash-value")
            self.assertEqual(telegram["session_file"], "/config/session.txt")
            self.assertEqual(telegram["forward_channel_id"], "-1001234567890")
            self.assertEqual(telegram["proxy"], "socks5://127.0.0.1:1080")

    def test_missing_sources_are_migrated_to_default_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
            )

            config.init()

            sources = config.list_sources()
            self.assertEqual([source["id"] for source in sources], ["alpha_bot", "beta_bot"])
            self.assertEqual(config.get_default_source()["id"], "alpha_bot")
            self.assertEqual(config.get_source("beta_bot")["chat"], "beta_bot")
            self.assertEqual(config.get_source("beta_bot")["forward_source"], "@beta_bot")

    def test_default_admin_password_can_be_changed_and_invalidates_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
            )
            config.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)

            self.assertTrue(auth.verify_password("admin", "test-password"))
            token = auth.create_session("admin")
            self.assertIsNotNone(auth.get_session(token))

            auth.change_password("admin", "test-password", "new-password")

            self.assertFalse(auth.verify_password("admin", "test-password"))
            self.assertTrue(auth.verify_password("admin", "new-password"))
            self.assertIsNone(auth.get_session(token))

    def test_download_dir_is_persisted_and_must_be_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
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
                default_password="test-password",
            )
            reloaded.init()
            self.assertEqual(reloaded.get_download_dir(), target)
            with self.assertRaises(ValueError):
                config.set_download_dir(Path("relative/path"))


class IndexTemplateTests(unittest.TestCase):
    def test_setup_form_does_not_collect_setup_token(self):
        self.assertNotIn('id="setupToken"', app.SETUP_HTML)
        self.assertNotIn("X-TGDL-Setup-Token", app.SETUP_HTML)

    def test_default_host_is_loopback(self):
        self.assertEqual(app.DEFAULT_HOST, "127.0.0.1")

    def test_index_has_left_navigation_sources_page_and_download_source_select(self):
        html = app.INDEX_HTML

        self.assertIn('class="sidebar"', html)
        self.assertIn('data-page="downloads"', html)
        self.assertIn('data-page="paths"', html)
        self.assertIn('data-page="sources"', html)
        self.assertIn('data-page="telegram"', html)
        self.assertIn('data-page="password"', html)
        self.assertIn('id="page-sources"', html)
        self.assertIn('id="sourceSelect"', html)
        self.assertIn('id="page-telegram"', html)
        self.assertIn('id="page-password"', html)
        self.assertIn('id="dirDialog"', html)

    def test_forwarder_restart_button_is_rendered_only_when_configured(self):
        html = app.INDEX_HTML

        self.assertIn("status.restart_configured", html)
        self.assertIn("status.restart_hint", html)
        self.assertIn("restartForwarder()", html)

    def test_forwarder_configuration_hint_links_to_telegram_page(self):
        html = app.INDEX_HTML

        self.assertIn("status.configuration_hint", html)
        self.assertIn("showPage('telegram')", html)
        self.assertNotIn("docker compose restart forwarder", html)

    def test_index_visible_labels_are_chinese_admin_console_copy(self):
        html = app.INDEX_HTML

        for label in [
            "下载任务",
            "路径设置",
            "资源来源",
            "Telegram 授权",
            "密码管理",
            "退出登录",
            "提交下载",
            "转发监控",
            "选择目录",
            "保存",
        ]:
            self.assertIn(label, html)

        for old_label in [
            ">Downloads<",
            ">Paths<",
            ">Sources<",
            ">Telegram Auth<",
            ">Password<",
            ">Logout<",
            ">Queue<",
            ">Forwarder<",
            ">Select Directory<",
        ]:
            self.assertNotIn(old_label, html)

    def test_index_uses_media_control_center_shell(self):
        html = app.INDEX_HTML

        for marker in [
            "TG 下载中控",
            "媒体归档服务",
            "媒体归档工作台",
            'id="submitMessage"',
            'class="metric forwarder"',
            "任务输出",
            "还没有下载任务",
        ]:
            self.assertIn(marker, html)

    def test_forwarder_restart_requires_browser_confirmation(self):
        html = app.INDEX_HTML

        self.assertIn("confirm(t('confirm_restart_forwarder'))", html)
        self.assertIn("confirm_restart_forwarder: '确认重启 forwarder？'", html)
        self.assertLess(
            html.index("confirm(t('confirm_restart_forwarder'))"),
            html.index("/api/forwarder/restart"),
        )

    def test_index_has_telegram_code_and_qr_authorization_controls(self):
        html = app.INDEX_HTML

        for marker in [
            'id="telegramApiId"',
            'id="telegramPhone"',
            'id="telegramCode"',
            'id="telegramPassword"',
            'id="telegramQr"',
            "/api/telegram/auth/code/send",
            "/api/telegram/auth/code/confirm",
            "/api/telegram/auth/qr/start",
            "/api/telegram/auth/qr/check",
        ]:
            self.assertIn(marker, html)

    def test_index_has_tdl_qr_login_controls(self):
        html = app.INDEX_HTML

        for marker in [
            'class="qr-box tdl-qr-box" id="tdlQr"',
            'id="tdlLoginOutput"',
            'id="startTdlQrLoginBtn"',
            "/api/tdl/login/qr/start",
            "/api/tdl/login/status",
            "function latestTdlQrOutput(output)",
            "lastIndexOf('Scan QR')",
            "data.mode === 'qr'",
            "latestTdlQrOutput(output)",
            "document.getElementById('tdlQr').innerHTML",
        ]:
            self.assertIn(marker, html)

    def test_index_separates_telethon_and_tdl_authorization(self):
        html = app.INDEX_HTML

        for marker in [
            "Telethon 用户授权",
            "用于转发监听和消息处理",
            "tdl 下载授权",
            "用于实际 Telegram 文件下载",
            'id="tdlPhone"',
            'id="tdlCode"',
            'id="tdlPassword"',
            'id="startTdlCodeLoginBtn"',
            'id="sendTdlPhoneBtn"',
            'id="sendTdlCodeBtn"',
            'id="sendTdlPasswordBtn"',
            "tdl 输出",
            "/api/tdl/login/code/start",
            "/api/tdl/login/input",
            "/api/tdl/login/status",
        ]:
            self.assertIn(marker, html)
        self.assertNotIn('id="tdlLoginInput"', html)
        self.assertNotIn('id="sendTdlLoginInputBtn"', html)

    def test_index_uses_pause_and_resume_job_controls(self):
        html = app.INDEX_HTML

        for marker in [
            "paused: t('status_paused')",
            "canceled: t('status_canceled')",
            "status_paused: '已暂停'",
            "status_canceled: '已取消'",
            "function pauseJob(id)",
            "function resumeJob(id)",
            "/api/jobs/${id}/pause",
            "/api/jobs/${id}/resume",
            "onclick=\"pauseJob(${job.id})\">${t('pause')}",
            "onclick=\"resumeJob(${job.id})\">${t('resume')}",
            "pause: '暂停'",
            "resume: '继续'",
        ]:
            self.assertIn(marker, html)

        self.assertNotIn("onclick=\"cancelJob(${job.id})\">取消</button>", html)
        self.assertNotIn("onclick=\"retryJob(${job.id})\">重试</button>", html)

    def test_index_has_client_side_i18n_language_switch(self):
        html = app.INDEX_HTML

        for marker in [
            "const I18N",
            "function resolveLang()",
            "function t(key)",
            "function applyI18n(lang)",
            "tgdl_lang",
            'class="lang-switch"',
            'data-lang="zh"',
            'data-lang="en"',
            'data-i18n="nav_downloads"',
            "lastTelegramConfig",
            "applyTelegramConfig",
            "function onI18nApplied()",
        ]:
            self.assertIn(marker, html)

        # Language switch must re-apply server-derived Telegram labels without wiping hash input.
        self.assertIn("applyTelegramConfig(lastTelegramConfig, { resetHash: false })", html)
        self.assertLess(html.index("function onI18nApplied()"), html.index("applyTelegramConfig(lastTelegramConfig"))

    def test_login_visible_labels_are_chinese(self):
        html = app.LOGIN_HTML

        for label in ["登录 - Telegram 下载管理", "Telegram 下载管理", "管理员", "密码", "登录"]:
            self.assertIn(label, html)

        for old_label in [">Admin<", ">Password<", ">Login<", "|| 'Login failed'"]:
            self.assertNotIn(old_label, html)
        self.assertIn("login_failed: '登录失败'", html)

    def test_login_uses_media_control_center_shell(self):
        html = app.LOGIN_HTML

        for marker in ["TG 下载中控", "媒体归档服务", "登录下载中控"]:
            self.assertIn(marker, html)

    def test_setup_visible_labels_are_chinese(self):
        html = app.SETUP_HTML

        for label in [
            "初始化下载中控",
            "管理员账号",
            "管理员密码",
            "下载目录",
            "Telegram API ID（仅转发器需要）",
            "Telegram API hash（仅转发器需要）",
            "Telegram Session 文件（仅转发器需要）",
            "转发目标频道 ID（仅转发器需要）",
            "完成初始化",
        ]:
            self.assertIn(label, html)

        for old_label in [
            "Initialize tg-downloader-ui",
            ">Admin username<",
            ">Admin password<",
            ">Download directory<",
            ">Initialize<",
        ]:
            self.assertNotIn(old_label, html)

    def test_setup_prefills_default_download_dir_from_api(self):
        html = app.SETUP_HTML

        self.assertIn("default_download_dir", html)
        self.assertIn("document.getElementById('downloadDir').value", html)


class TelegramAuthHelperTests(unittest.TestCase):
    class FakeSession:
        def __init__(self, value=""):
            self.value = value

        @staticmethod
        def save(session):
            return "STRING_SESSION"

    class FakeClient:
        def __init__(self, session, api_id, api_hash, proxy=None):
            self.session = session
            self.api_id = api_id
            self.api_hash = api_hash
            self.proxy = proxy
            self.connected = False
            self.signed_in = None

        async def connect(self):
            self.connected = True

        async def disconnect(self):
            self.connected = False

        async def send_code_request(self, phone):
            return SimpleNamespace(phone_code_hash="phone-hash")

        async def sign_in(self, **kwargs):
            self.signed_in = kwargs

    def test_send_code_saves_phone_code_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "api_id": "12345",
                "api_hash": "hash-value",
                "session_file": str(root / "session.txt"),
                "proxy": "",
            }
            state_path = root / "telegram_auth.json"

            result = app.telegram_send_login_code(
                config,
                "+15551234567",
                state_path=state_path,
                client_factory=self.FakeClient,
                session_cls=self.FakeSession,
            )

            self.assertEqual(result["phone"], "+15551234567")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phone_code_hash"], "phone-hash")

    def test_confirm_code_writes_string_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "session.txt"
            state_path = root / "telegram_auth.json"
            state_path.write_text(
                json.dumps(
                    {
                        "phone": "+15551234567",
                        "phone_code_hash": "phone-hash",
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "api_id": "12345",
                "api_hash": "hash-value",
                "session_file": str(session_file),
                "proxy": "",
            }

            result = app.telegram_confirm_login_code(
                config,
                "+15551234567",
                "12345",
                state_path=state_path,
                client_factory=self.FakeClient,
                session_cls=self.FakeSession,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(session_file.read_text(encoding="utf-8"), "STRING_SESSION\n")
            self.assertFalse(state_path.exists())


class TdlLoginHelperTests(unittest.TestCase):
    def tearDown(self):
        with app.TDL_LOGIN_LOCK:
            app.TDL_LOGIN_ENTRY = None

    def test_tdl_qr_login_starts_process_and_captures_output(self):
        seen = {}

        class FakeProcess:
            def __init__(self, args, **kwargs):
                seen["args"] = args
                self.stdout = iter(["Scan QR code\n", "████\n"])
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

        result = app.start_tdl_qr_login(popen_factory=FakeProcess)

        for _ in range(20):
            status = app.tdl_qr_login_status()
            if status["state"] == "done":
                break
            threading.Event().wait(0.01)

        self.assertEqual(result["state"], "running")
        self.assertIn("login", seen["args"])
        self.assertIn("-T", seen["args"])
        self.assertIn("qr", seen["args"])
        self.assertEqual(status["state"], "done")
        self.assertIn("Scan QR code", status["output"])
        self.assertIn("████", status["output"])

    def test_tdl_code_login_starts_code_mode(self):
        seen = {}

        class FakeProcess:
            def __init__(self, args, **kwargs):
                seen["args"] = args
                self.stdout = iter([])
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

        result = app.start_tdl_login("code", popen_factory=FakeProcess)

        self.assertEqual(result["state"], "running")
        self.assertEqual(result["mode"], "code")
        self.assertIn("login", seen["args"])
        self.assertIn("-T", seen["args"])
        self.assertIn("code", seen["args"])

    def test_tdl_login_input_writes_to_running_process(self):
        written = []

        class FakeStdin:
            def write(self, text):
                written.append(text)

            def flush(self):
                written.append("<flush>")

        class FakeProcess:
            stdin = FakeStdin()

            def poll(self):
                return None

        with app.TDL_LOGIN_LOCK:
            app.TDL_LOGIN_ENTRY = {
                "state": "running",
                "mode": "code",
                "process": FakeProcess(),
                "output": "",
                "returncode": None,
                "started_at": "",
                "updated_at": "",
                "error": "",
            }

        result = app.send_tdl_login_input("12345")

        self.assertEqual(result["state"], "running")
        self.assertEqual(written, ["12345\n", "<flush>"])

    def test_tdl_login_input_requires_running_process(self):
        with self.assertRaisesRegex(RuntimeError, "tdl login is not running"):
            app.send_tdl_login_input("12345")


class JobManagementTests(unittest.TestCase):
    def make_store(self, root):
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_user="admin",
            default_password="test-password",
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

    def test_job_snapshots_selected_source_and_worker_uses_source_chat(self):
        class ExportOnlyWorker(DownloadWorker):
            def __init__(self, store, stop_event, payload):
                super().__init__(store, stop_event)
                self.payload = payload
                self.export_command = []

            def run_command(self, job_id, cmd, status):
                if status == "exporting":
                    self.export_command = cmd
                    job = self.store.get_job(job_id)
                    Path(job["export_path"]).write_text(
                        json.dumps(self.payload), encoding="utf-8"
                    )
                    return 0
                raise AssertionError("download should be skipped by existing final file")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            existing = root / "downloads" / "Demo.mp4"
            existing.write_bytes(b"already downloaded")
            queued = store.create_job(23311, source_id="beta_bot")
            job = store.claim_next()
            payload = {
                "id": 7487350635,
                "messages": [{"id": queued["message_id"], "file": "Demo.mp4", "text": ""}],
            }
            worker = ExportOnlyWorker(store, threading.Event(), payload)

            worker.process_job(job)

            result = store.get_job(job["id"])
            self.assertEqual(result["source_id"], "beta_bot")
            self.assertEqual(result["source_chat"], "beta_bot")
            self.assertEqual(
                worker.export_command[worker.export_command.index("-c") + 1],
                "beta_bot",
            )

    def test_pause_queued_job_and_resume_paused_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)

            paused = store.pause_job(job["id"])

            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["pause_requested"], 1)
            self.assertEqual(paused["cancel_requested"], 0)
            resumed = store.resume_job(job["id"])
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["pause_requested"], 0)
            self.assertEqual(resumed["cancel_requested"], 0)

    def test_resume_download_job_preserves_progress_and_marks_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)
            Path(job["export_path"]).write_text(
                json.dumps(
                    {
                        "id": 7487350635,
                        "messages": [
                            {"id": 23311, "file": "movie.mp4", "text": "片名：Movie"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store.update_job(
                job["id"],
                status="canceled",
                source_file="movie.mp4",
                final_path=str(root / "downloads" / "movie.mp4"),
                progress=37.5,
                downloaded="512.00 MB",
                speed="2.00 MB/s",
                eta="10m",
                cancel_requested=1,
            )

            resumed = store.resume_job(job["id"])

            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["progress"], 37.5)
            self.assertEqual(resumed["downloaded"], "512.00 MB")
            self.assertEqual(resumed["speed"], "")
            self.assertEqual(resumed["eta"], "")
            self.assertEqual(resumed["cancel_requested"], 0)
            self.assertEqual(resumed["resume_requested"], 1)

    def test_resume_download_job_requires_existing_export_metadata_for_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)
            store.update_job(
                job["id"],
                status="canceled",
                source_file="movie.mp4",
                final_path=str(root / "downloads" / "movie.mp4"),
                progress=37.5,
                downloaded="512.00 MB",
                cancel_requested=1,
            )

            resumed = store.resume_job(job["id"])

            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["progress"], 0)
            self.assertEqual(resumed["downloaded"], "")
            self.assertEqual(resumed["resume_requested"], 0)

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

    def test_pause_active_job_sets_pause_requested_without_canceling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            job = store.create_job(23311)
            store.update_job(job["id"], status="downloading", process_pid=12345)

            paused = store.pause_job(job["id"])

            self.assertEqual(paused["status"], "downloading")
            self.assertEqual(paused["pause_requested"], 1)
            self.assertEqual(paused["cancel_requested"], 0)
            self.assertEqual(paused["process_pid"], 12345)

    def test_live_pause_and_resume_keep_same_process_and_partial_bytes(self):
        class BlockingStdout:
            def __init__(self, finished):
                self.finished = finished

            def read(self, size):  # noqa: ARG002
                self.finished.wait(3)
                return b""

        class FakeProcess:
            def __init__(self, partial_path):
                self.pid = 43210
                self.partial_path = partial_path
                self.finished = threading.Event()
                self.running = threading.Event()
                self.running.set()
                self.signals = []
                self.returncode = None
                self.stdout = BlockingStdout(self.finished)
                self.writer = threading.Thread(target=self.write_bytes, daemon=True)
                self.writer.start()

            def write_bytes(self):
                while not self.finished.is_set():
                    if self.running.wait(0.01):
                        with self.partial_path.open("ab") as out:
                            out.write(b"x")
                    threading.Event().wait(0.01)

            def send_signal(self, value):
                self.signals.append(value)
                if value == app.PROCESS_PAUSE_SIGNAL:
                    self.running.clear()
                elif value == app.PROCESS_CONTINUE_SIGNAL:
                    self.running.set()
                elif value == signal.SIGINT:
                    self.returncode = 130
                    self.finished.set()

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.finished.wait(timeout)
                return self.returncode or 0

            def kill(self):
                self.returncode = -9
                self.finished.set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, store = self.make_store(root)
            queued = store.create_job(23311)
            job = store.claim_next()
            partial_path = root / "downloads" / "movie.mp4.tmp"
            partial_path.parent.mkdir(exist_ok=True)
            partial_path.write_bytes(b"partial")
            proc = FakeProcess(partial_path)
            worker = DownloadWorker(store, threading.Event())
            result = []

            with (
                mock.patch.object(app, "PROCESS_PAUSE_SIGNAL", 1001, create=True),
                mock.patch.object(app, "PROCESS_CONTINUE_SIGNAL", 1002, create=True),
                mock.patch.object(app.subprocess, "Popen", return_value=proc),
            ):
                runner = threading.Thread(
                    target=lambda: result.append(
                        worker.run_command(queued["id"], ["tdl", "download"], "downloading")
                    )
                )
                runner.start()
                self.assertTrue(self.wait_for(lambda: store.get_job(job["id"])["process_pid"] == proc.pid))

                store.pause_job(job["id"])
                self.assertTrue(self.wait_for(lambda: store.get_job(job["id"])["status"] == "paused"))
                paused_size = partial_path.stat().st_size
                threading.Event().wait(0.1)
                self.assertEqual(partial_path.stat().st_size, paused_size)
                self.assertEqual(store.get_job(job["id"])["process_pid"], proc.pid)

                store.resume_job(job["id"])
                self.assertTrue(self.wait_for(lambda: store.get_job(job["id"])["status"] == "downloading"))
                self.assertTrue(self.wait_for(lambda: partial_path.stat().st_size > paused_size))
                self.assertEqual(store.get_job(job["id"])["process_pid"], proc.pid)

                store.cancel_job(job["id"])
                runner.join(3)

            self.assertEqual(result, [app.CANCEL_EXIT_CODE])
            self.assertEqual(
                proc.signals,
                [1001, 1002, 1002, signal.SIGINT],
            )

    def wait_for(self, predicate, attempts=100):
        for _ in range(attempts):
            if predicate():
                return True
            threading.Event().wait(0.02)
        return False


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

    @contextlib.contextmanager
    def running_server(
        self,
        root,
        default_password="test-password",
        cookie_secure=False,
    ):
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_user="admin",
            default_password=default_password,
        )
        config.init()
        store = JobStore(root / "state", config)
        store.init()
        auth = app.AuthManager(config, session_max_age_seconds=604800)
        server = app.DownloadServer(
            ("127.0.0.1", 0),
            app.RequestHandler,
            store,
            config,
            auth,
            cookie_secure=cookie_secure,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], config
        finally:
            server.shutdown()
            server.server_close()

    def login_headers(self, port, password="test-password"):
        status, headers, _ = self.request(
            port,
            "POST",
            "/api/auth/login",
            {"username": "admin", "password": password},
        )
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, payload = self.request(
            port, "GET", "/api/auth/me", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        csrf_token = json.loads(payload)["csrf_token"]
        return {"Cookie": cookie, "X-CSRF-Token": csrf_token}

    def test_authenticated_mutation_requires_csrf_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.running_server(Path(tmp)) as (port, _):
                status, headers, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "test-password"},
                )
                self.assertEqual(status, 200)
                cookie = headers["Set-Cookie"].split(";", 1)[0]

                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 403)

                status, _, payload = self.request(
                    port, "GET", "/api/auth/me", headers={"Cookie": cookie}
                )
                csrf_token = json.loads(payload)["csrf_token"]
                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers={"Cookie": cookie, "X-CSRF-Token": csrf_token},
                )
                self.assertEqual(status, 200)

    def test_oversized_json_request_returns_413(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.running_server(Path(tmp)) as (port, _):
                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    headers={"Content-Length": str(app.MAX_JSON_BODY_BYTES + 1)},
                )
                self.assertEqual(status, 413)

    def test_security_headers_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.running_server(Path(tmp)) as (port, _):
                status, headers, _ = self.request(port, "GET", "/login")
                self.assertEqual(status, 200)
                self.assertIn(
                    "default-src 'self'", headers["Content-Security-Policy"]
                )
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_secure_cookie_can_be_enabled_per_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.running_server(Path(tmp), cookie_secure=True) as (port, _):
                status, headers, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "test-password"},
                )
                self.assertEqual(status, 200)
                self.assertIn("; Secure", headers["Set-Cookie"])

    def test_login_rate_limit_returns_429(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_password="test-password",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            auth = app.AuthManager(config)
            server = app.DownloadServer(
                ("127.0.0.1", 0), app.RequestHandler, store, config, auth
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                for _ in range(5):
                    status, _, _ = self.request(
                        port,
                        "POST",
                        "/api/auth/login",
                        {"username": "admin", "password": "wrong-password"},
                    )
                    self.assertEqual(status, 401)

                status, headers, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "test-password"},
                )

                self.assertEqual(status, 429)
                self.assertGreater(int(headers["Retry-After"]), 0)
            finally:
                server.shutdown()
                server.server_close()

    def test_login_cookie_allows_api_access_and_logout_revokes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)
            server = app.DownloadServer(
                ("127.0.0.1", 0),
                app.RequestHandler,
                store,
                config,
                auth,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                status, _, _ = self.request(port, "GET", "/api/jobs")
                self.assertEqual(status, 401)

                auth_headers = self.login_headers(port)
                cookie = auth_headers["Cookie"]

                status, _, payload = self.request(
                    port,
                    "GET",
                    "/api/jobs",
                    headers=auth_headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)["jobs"], [])

                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers=auth_headers,
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

    def test_job_pause_resume_api_requires_auth_and_updates_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            job = store.create_job(23311)
            auth = app.AuthManager(config, session_max_age_seconds=604800)
            server = app.DownloadServer(
                ("127.0.0.1", 0),
                app.RequestHandler,
                store,
                config,
                auth,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                status, _, _ = self.request(port, "POST", f"/api/jobs/{job['id']}/pause")
                self.assertEqual(status, 401)
                status, _, _ = self.request(port, "POST", f"/api/jobs/{job['id']}/resume")
                self.assertEqual(status, 401)

                auth_headers = self.login_headers(port)

                status, _, payload = self.request(
                    port,
                    "POST",
                    f"/api/jobs/{job['id']}/pause",
                    {},
                    headers=auth_headers,
                )
                self.assertEqual(status, 200)
                paused = json.loads(payload)["job"]
                self.assertEqual(paused["status"], "paused")
                self.assertEqual(paused["pause_requested"], 1)
                self.assertEqual(paused["cancel_requested"], 0)

                status, _, payload = self.request(
                    port,
                    "POST",
                    f"/api/jobs/{job['id']}/resume",
                    {},
                    headers=auth_headers,
                )
                self.assertEqual(status, 200)
                resumed = json.loads(payload)["job"]
                self.assertEqual(resumed["status"], "queued")
                self.assertEqual(resumed["pause_requested"], 0)
                self.assertEqual(resumed["cancel_requested"], 0)
            finally:
                server.shutdown()
                server.server_close()

    def test_setup_api_returns_default_download_dir_and_accepts_blank_download_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            config = app.ConfigStore(
                root / "state",
                default_download_dir=downloads,
                default_user="admin",
                default_password="",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            auth = app.AuthManager(config, session_max_age_seconds=604800)
            server = app.DownloadServer(
                ("127.0.0.1", 0),
                app.RequestHandler,
                store,
                config,
                auth,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                status, _, payload = self.request(port, "GET", "/api/setup")
                self.assertEqual(status, 200)
                setup = json.loads(payload)
                self.assertTrue(setup["required"])
                self.assertEqual(setup["default_download_dir"], str(downloads))

                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/setup",
                    {"username": "owner", "password": "strong-password", "download_dir": ""},
                )
                self.assertEqual(status, 201)
                self.assertEqual(config.get_download_dir(), downloads)
                self.assertEqual(config.get_username(), "owner")

                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/setup",
                    {"username": "other", "password": "other-password", "download_dir": ""},
                )
                self.assertEqual(status, 400)
                self.assertEqual(config.get_username(), "owner")
            finally:
                server.shutdown()
                server.server_close()

    def test_tdl_qr_login_api_requires_auth_and_returns_status(self):
        original_start = app.start_tdl_qr_login
        original_status = app.tdl_qr_login_status
        try:
            app.start_tdl_qr_login = lambda: {
                "state": "running",
                "output": "Scan QR code\n",
            }
            app.tdl_qr_login_status = lambda: {
                "state": "running",
                "output": "Scan QR code\n",
            }
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = app.ConfigStore(
                    root / "state",
                    default_download_dir=root / "downloads",
                    default_user="admin",
                    default_password="test-password",
                )
                config.init()
                store = JobStore(root / "state", config)
                store.init()
                auth = app.AuthManager(config, session_max_age_seconds=604800)
                server = app.DownloadServer(
                    ("127.0.0.1", 0), app.RequestHandler, store, config, auth
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    port = server.server_address[1]

                    status, _, _ = self.request(port, "POST", "/api/tdl/login/qr/start")
                    self.assertEqual(status, 401)

                    auth_headers = self.login_headers(port)

                    status, _, payload = self.request(
                        port,
                        "POST",
                        "/api/tdl/login/qr/start",
                        {},
                        headers=auth_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(payload)["state"], "running")

                    status, _, payload = self.request(
                        port,
                        "GET",
                        "/api/tdl/login/qr/status",
                        headers=auth_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("Scan QR code", json.loads(payload)["output"])
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            app.start_tdl_qr_login = original_start
            app.tdl_qr_login_status = original_status

    def test_tdl_code_login_api_requires_auth_and_accepts_input(self):
        had_start = hasattr(app, "start_tdl_code_login")
        had_status = hasattr(app, "tdl_login_status")
        had_input = hasattr(app, "send_tdl_login_input")
        original_start = getattr(app, "start_tdl_code_login", None)
        original_status = getattr(app, "tdl_login_status", None)
        original_input = getattr(app, "send_tdl_login_input", None)
        try:
            app.start_tdl_code_login = lambda: {
                "state": "running",
                "mode": "code",
                "output": "Enter phone\n",
            }
            app.tdl_login_status = lambda: {
                "state": "running",
                "mode": "code",
                "output": "Enter code\n",
            }
            app.send_tdl_login_input = lambda text: {
                "state": "running",
                "mode": "code",
                "output": f"sent:{text}",
            }
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = app.ConfigStore(
                    root / "state",
                    default_download_dir=root / "downloads",
                    default_user="admin",
                    default_password="test-password",
                )
                config.init()
                store = JobStore(root / "state", config)
                store.init()
                auth = app.AuthManager(config, session_max_age_seconds=604800)
                server = app.DownloadServer(
                    ("127.0.0.1", 0), app.RequestHandler, store, config, auth
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    port = server.server_address[1]

                    status, _, _ = self.request(port, "POST", "/api/tdl/login/code/start")
                    self.assertEqual(status, 401)
                    status, _, _ = self.request(port, "POST", "/api/tdl/login/input")
                    self.assertEqual(status, 401)

                    auth_headers = self.login_headers(port)

                    status, _, payload = self.request(
                        port,
                        "POST",
                        "/api/tdl/login/code/start",
                        {},
                        headers=auth_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(payload)["mode"], "code")

                    status, _, payload = self.request(
                        port,
                        "GET",
                        "/api/tdl/login/status",
                        headers=auth_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("Enter code", json.loads(payload)["output"])

                    status, _, payload = self.request(
                        port,
                        "POST",
                        "/api/tdl/login/input",
                        {"text": "+8613000000000"},
                        headers=auth_headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("+8613000000000", json.loads(payload)["output"])
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            if had_start:
                app.start_tdl_code_login = original_start
            else:
                delattr(app, "start_tdl_code_login")
            if had_status:
                app.tdl_login_status = original_status
            else:
                delattr(app, "tdl_login_status")
            if had_input:
                app.send_tdl_login_input = original_input
            else:
                delattr(app, "send_tdl_login_input")

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
                default_password="test-password",
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

                auth_headers = self.login_headers(port)

                status, _, payload = self.request(
                    port,
                    "GET",
                    f"/api/fs/dirs?path={query_path}",
                    headers=auth_headers,
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

    def test_sources_api_requires_auth_and_updates_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_user="admin",
                default_password="test-password",
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

                status, _, _ = self.request(port, "GET", "/api/sources")
                self.assertEqual(status, 401)

                auth_headers = self.login_headers(port)

                status, _, payload = self.request(
                    port,
                    "GET",
                    "/api/sources",
                    headers=auth_headers,
                )
                self.assertEqual(status, 200)
                data = json.loads(payload)
                self.assertEqual(data["default_source_id"], "alpha_bot")
                self.assertEqual([source["id"] for source in data["sources"]], ["alpha_bot", "beta_bot"])

                status, _, payload = self.request(
                    port,
                    "PUT",
                    "/api/sources",
                    {
                        "default_source_id": "beta_bot",
                        "sources": data["sources"],
                    },
                    headers=auth_headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)["default_source_id"], "beta_bot")
                self.assertEqual(config.get_default_source()["id"], "beta_bot")
            finally:
                server.shutdown()
                server.server_close()


class ForwarderStatusApiTests(unittest.TestCase):
    def test_forwarder_enabled_parses_supported_values(self):
        key = "TGDL_FORWARDER_ENABLED"
        had_original = key in app.os.environ
        original = app.os.environ.get(key)
        try:
            app.os.environ.pop(key, None)
            self.assertTrue(app.forwarder_enabled())

            for value in ("1", "true", "TRUE", "Yes", "oN", " true ", " YES "):
                with self.subTest(value=value):
                    app.os.environ[key] = value
                    self.assertTrue(app.forwarder_enabled())

            for value in ("0", "false", "no", "off", "", "   ", "garbage"):
                with self.subTest(value=value):
                    app.os.environ[key] = value
                    self.assertFalse(app.forwarder_enabled())
        finally:
            if had_original:
                app.os.environ[key] = original
            else:
                app.os.environ.pop(key, None)

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

    def test_forwarder_status_api_reports_restart_not_configured(self):
        original_default = app.default_forwarder_restart_command
        original_restart_env = app.os.environ.get("TGDL_FORWARDER_RESTART_CMD")
        original_enabled_env = app.os.environ.get("TGDL_FORWARDER_ENABLED")
        try:
            app.default_forwarder_restart_command = lambda: []
            app.os.environ.pop("TGDL_FORWARDER_RESTART_CMD", None)
            app.os.environ.pop("TGDL_FORWARDER_ENABLED", None)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = app.ConfigStore(
                    root / "state",
                    default_download_dir=root / "downloads",
                    default_user="admin",
                    default_password="test-password",
                )
                config.init()
                store = JobStore(root / "state", config)
                store.init()
                auth = app.AuthManager(config, session_max_age_seconds=604800)
                server = app.DownloadServer(
                    ("127.0.0.1", 0), app.RequestHandler, store, config, auth
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    helper = AuthHttpTests()
                    port = server.server_address[1]

                    auth_headers = helper.login_headers(port)

                    status, _, payload = helper.request(
                        port,
                        "GET",
                        "/api/forwarder/status",
                        headers=auth_headers,
                    )

                    self.assertEqual(status, 200)
                    data = json.loads(payload)
                    self.assertEqual(data["state"], "missing")
                    self.assertFalse(data["restart_configured"])
                    self.assertIn("TGDL_FORWARDER_RESTART_CMD", data["restart_hint"])
                    self.assertNotIn(
                        "docker compose restart forwarder", data["restart_hint"]
                    )
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            app.default_forwarder_restart_command = original_default
            if original_restart_env is None:
                app.os.environ.pop("TGDL_FORWARDER_RESTART_CMD", None)
            else:
                app.os.environ["TGDL_FORWARDER_RESTART_CMD"] = original_restart_env
            if original_enabled_env is None:
                app.os.environ.pop("TGDL_FORWARDER_ENABLED", None)
            else:
                app.os.environ["TGDL_FORWARDER_ENABLED"] = original_enabled_env

    def test_forwarder_status_prompts_for_missing_telegram_api(self):
        original_read = app.read_forwarder_status
        original_restart_configured = app.forwarder_restart_configured
        try:
            app.read_forwarder_status = lambda: {
                "state": "failed",
                "last_error": "TGDL_API_ID is required",
            }
            app.forwarder_restart_configured = lambda: True

            status = app.forwarder_status_response()

            self.assertTrue(status["configuration_required"])
            self.assertIn("Telegram 授权", status["configuration_hint"])
            self.assertEqual(status["last_error"], "TGDL_API_ID is required")
        finally:
            app.read_forwarder_status = original_read
            app.forwarder_restart_configured = original_restart_configured

    def test_forwarder_status_ignores_structured_configuration_error(self):
        original_read = app.read_forwarder_status
        original_restart_configured = app.forwarder_restart_configured
        try:
            app.read_forwarder_status = lambda: {
                "state": "failed",
                "last_error": [],
            }
            app.forwarder_restart_configured = lambda: True

            status = app.forwarder_status_response()

            self.assertFalse(status["configuration_required"])
        finally:
            app.read_forwarder_status = original_read
            app.forwarder_restart_configured = original_restart_configured

    def test_forwarder_status_reports_explicitly_disabled_forwarder(self):
        original_enabled_env = app.os.environ.get("TGDL_FORWARDER_ENABLED")
        original_restart_configured = app.forwarder_restart_configured
        try:
            app.os.environ["TGDL_FORWARDER_ENABLED"] = "0"
            app.forwarder_restart_configured = lambda: True

            status = app.forwarder_status_response()

            self.assertFalse(status["forwarder_enabled"])
            self.assertFalse(status["restart_configured"])
            self.assertIn("TGDL_FORWARDER_ENABLED=1", status["restart_hint"])
            self.assertNotIn(
                "docker compose restart forwarder", status["restart_hint"]
            )
        finally:
            app.forwarder_restart_configured = original_restart_configured
            if original_enabled_env is None:
                app.os.environ.pop("TGDL_FORWARDER_ENABLED", None)
            else:
                app.os.environ["TGDL_FORWARDER_ENABLED"] = original_enabled_env

    def test_restart_forwarder_runs_configured_command_without_shell(self):
        result = app.restart_forwarder(
            command=[
                sys.executable,
                "-c",
                "print('forwarder restarted')",
                "--proxy",
                "socks5://user:password@127.0.0.1:1080",
            ]
        )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"].strip(), "forwarder restarted")
        self.assertEqual(result["args"][-2:], ["--proxy", "<redacted>"])

    def test_restart_forwarder_requires_configured_or_openwrt_command(self):
        with self.assertRaisesRegex(RuntimeError, "restart command is not configured"):
            app.restart_forwarder(command="")

    def test_default_forwarder_restart_command_supports_systemd_service(self):
        command = app.default_forwarder_restart_command(
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            exists=lambda path: path == "/etc/systemd/system/tg-downloader-forwarder.service",
        )

        self.assertEqual(
            command,
            ["systemctl", "restart", "tg-downloader-forwarder.service"],
        )

    def test_default_forwarder_restart_command_restarts_openwrt_init_service(self):
        command = app.default_forwarder_restart_command(
            which=lambda name: None,
            exists=lambda path: path == "/etc/init.d/tg-downloader-ui",
        )

        self.assertEqual(command[:2], ["/bin/sh", "-c"])
        self.assertIn("/etc/init.d/tg-downloader-ui restart", command[2])
        self.assertIn("sleep 1", command[2])

    def test_forwarder_restart_api_requires_auth_and_runs_restart(self):
        original_restart = app.restart_forwarder
        try:
            app.restart_forwarder = lambda: {
                "ok": True,
                "returncode": 0,
                "stdout": "restarted\n",
                "stderr": "",
                "args": ["fake"],
            }
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = app.ConfigStore(
                    root / "state",
                    default_download_dir=root / "downloads",
                    default_user="admin",
                    default_password="test-password",
                )
                config.init()
                store = JobStore(root / "state", config)
                store.init()
                auth = app.AuthManager(config, session_max_age_seconds=604800)
                server = app.DownloadServer(
                    ("127.0.0.1", 0), app.RequestHandler, store, config, auth
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    helper = AuthHttpTests()
                    port = server.server_address[1]

                    status, _, _ = helper.request(port, "POST", "/api/forwarder/restart")
                    self.assertEqual(status, 401)

                    auth_headers = helper.login_headers(port)

                    status, _, payload = helper.request(
                        port,
                        "POST",
                        "/api/forwarder/restart",
                        {},
                        headers=auth_headers,
                    )

                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(payload)["stdout"], "restarted\n")
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            app.restart_forwarder = original_restart


class DockerComposeTests(unittest.TestCase):
    def test_compose_publishes_only_to_loopback_and_enables_forwarder(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        env_example = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn(
            "${TGDL_PUBLISH_HOST:-127.0.0.1}:${TGDL_PUBLISH_PORT:-9910}:9910",
            compose,
        )
        self.assertIn(
            'TGDL_FORWARDER_ENABLED: "${TGDL_FORWARDER_ENABLED:-1}"',
            compose,
        )
        self.assertIn("TGDL_FORWARDER_ENABLED=1", dockerfile)
        self.assertIn("TGDL_FORWARDER_ENABLED=1", env_example)
        self.assertNotIn("TGDL_SETUP_TOKEN", compose)
        self.assertIn('TGDL_COOKIE_SECURE: "${TGDL_COOKIE_SECURE:-0}"', compose)

    def test_compose_runs_forwarder_inside_web_container(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(
            "TGDL_FORWARDER_RESTART_CMD: /usr/local/bin/tg-downloader-forwarder-restart",
            compose,
        )
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertNotIn("\n  forwarder:", compose)

    def test_dockerfile_installs_single_container_forwarder_scripts(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("TGDL_FORWARDER_RESTART_CMD=/usr/local/bin/tg-downloader-forwarder-restart", dockerfile)
        self.assertIn("COPY docker/entrypoint.sh /usr/local/bin/tg-downloader-ui-entrypoint", dockerfile)
        self.assertIn("COPY docker/forwarder-supervisor.sh /usr/local/bin/tg-downloader-forwarder-supervisor", dockerfile)
        self.assertIn("COPY docker/restart-forwarder.sh /usr/local/bin/tg-downloader-forwarder-restart", dockerfile)
        self.assertIn('ENTRYPOINT ["tg-downloader-ui-entrypoint"]', dockerfile)

    def test_dockerfile_verifies_tdl_and_drops_runtime_privileges(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        # Multi-arch pins: amd64 (64bit) + arm64, aligned with OpenWrt full IPKs.
        self.assertIn(
            "f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718",
            dockerfile,
        )
        self.assertIn(
            "8398784d5b9390d26450e3e3528e2ffd0e9fe75d374f63273d0247e7ab0378b7",
            dockerfile,
        )
        self.assertIn("tdl_Linux_64bit.tar.gz", dockerfile)
        self.assertIn("tdl_Linux_arm64.tar.gz", dockerfile)
        self.assertIn("TARGETARCH", dockerfile)
        self.assertIn("sha256sum -c -", dockerfile)
        self.assertIn("useradd", dockerfile)
        self.assertIn("setpriv", dockerfile)
        self.assertIn("unsupported TARGETARCH", dockerfile)

    def test_ci_builds_multiarch_and_publish_workflow_targets_docker_hub(self):
        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        publish = Path(".github/workflows/docker-publish.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("linux/amd64,linux/arm64", ci)
        self.assertIn("docker/setup-buildx-action", ci)
        self.assertIn("docker build", ci)
        self.assertIn("tdl version", ci)
        self.assertNotIn("docker push", ci)

        self.assertIn("ifox2046/tg-downloader-ui", publish)
        self.assertIn("linux/amd64,linux/arm64", publish)
        self.assertIn("DOCKERHUB_USERNAME", publish)
        self.assertIn("DOCKERHUB_TOKEN", publish)
        self.assertIn("push: true", publish)
        self.assertIn("tags:", publish)
        self.assertIn(":latest", publish)

    def test_docker_context_excludes_local_secrets(self):
        ignored = Path(".dockerignore").read_text(encoding="utf-8")

        for value in [
            ".env",
            ".env.release-safety.local",
            ".agents",
            ".codex",
            ".claude",
            "dist/",
        ]:
            self.assertIn(value, ignored)

    def test_docker_entrypoint_starts_forwarder_supervisor(self):
        entrypoint = Path("docker/entrypoint.sh").read_text(encoding="utf-8")
        restart = Path("docker/restart-forwarder.sh").read_text(encoding="utf-8")

        self.assertIn("chmod 700 /config /downloads /tdl", entrypoint)
        self.assertIn("tg-downloader-forwarder-supervisor", entrypoint)
        for script in (entrypoint, restart):
            self.assertIn("forwarder_enabled() {", script)
            self.assertIn("${TGDL_FORWARDER_ENABLED-1}", script)
            self.assertNotIn("${TGDL_FORWARDER_ENABLED:-1}", script)
            self.assertIn(
                "sed 's/^[[:space:]]*//; s/[[:space:]]*$//'",
                script,
            )
            self.assertIn("tr '[:upper:]' '[:lower:]'", script)
            self.assertIn('case "$forwarder_flag" in', script)
            self.assertIn("1|true|yes|on) return 0 ;;", script)
            self.assertIn("*) return 1 ;;", script)
            self.assertNotIn('[ "${TGDL_FORWARDER_ENABLED:-0}" = "1" ]', script)
            self.assertNotIn('[ "${TGDL_FORWARDER_ENABLED:-1}" = "1" ]', script)
        self.assertIn("if forwarder_enabled; then", entrypoint)
        self.assertIn("unset TGDL_FORWARDER_RESTART_CMD", entrypoint)
        self.assertIn("forwarder_enabled || {", restart)
        self.assertIn("TGDL_FORWARDER_PID_FILE", restart)
        self.assertIn("kill \"$forwarder_pid\"", restart)

    def test_docker_forwarder_helpers_parse_supported_values(self):
        scripts = {
            "entrypoint": Path("docker/entrypoint.sh").read_text(encoding="utf-8"),
            "restart": Path("docker/restart-forwarder.sh").read_text(encoding="utf-8"),
        }
        cases = (
            (None, True),
            ("1", True),
            ("TRUE", True),
            ("Yes", True),
            ("oN", True),
            (" true ", True),
            (" YES ", True),
            ("", False),
            ("   ", False),
            ("0", False),
            ("false", False),
            ("garbage", False),
        )

        for script_name, script in scripts.items():
            function = extract_shell_function(script, "forwarder_enabled")
            for value, expected in cases:
                with self.subTest(script=script_name, value=value):
                    env = os.environ.copy()
                    if value is None:
                        env.pop("TGDL_FORWARDER_ENABLED", None)
                    else:
                        env["TGDL_FORWARDER_ENABLED"] = value
                    result = subprocess.run(
                        ["sh", "-c", f"{function}\nforwarder_enabled"],
                        env=env,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0 if expected else 1)

    def test_extract_shell_function_uses_exact_declaration_and_matching_brace(self):
        script = """not_forwarder_enabled() {
  return 1
}
forwarder_enabled() {
  if true; then
    {
      return 0
    }
  fi
}
exit 99
"""

        function = extract_shell_function(script, "forwarder_enabled")
        result = subprocess.run(
            ["sh", "-c", f"{function}\nforwarder_enabled"],
            capture_output=True,
            check=False,
        )

        self.assertEqual(function.splitlines()[0], "forwarder_enabled() {")
        self.assertNotIn("not_forwarder_enabled", function)
        self.assertNotIn("exit 99", function)
        self.assertEqual(result.returncode, 0)


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

    def test_downloaded_structured_movie_moves_to_library_layout_with_sidecars(self):
        class CompleteDownloadWorker(DownloadWorker):
            def __init__(self, store, stop_event, payload, download_dir):
                super().__init__(store, stop_event)
                self.payload = payload
                self.download_dir = download_dir
                self.download_command = []

            def run_command(self, job_id, cmd, status):
                if status == "exporting":
                    job = self.store.get_job(job_id)
                    Path(job["export_path"]).write_text(
                        json.dumps(self.payload, ensure_ascii=False), encoding="utf-8"
                    )
                    return 0
                if status == "downloading":
                    self.download_command = cmd
                    default_path = self.download_dir / "7487350635_23402_[source]正片.mp4"
                    default_path.write_bytes(b"movie")
                    return 0
                return 0

        original_download_dir = app.DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app.DOWNLOAD_DIR = root / "downloads"

                store = JobStore(root / "state")
                store.init()
                queued = store.create_job(23402)
                job = store.claim_next()
                payload = {
                    "id": 7487350635,
                    "messages": [
                        {
                            "id": queued["message_id"],
                            "file": "[source]正片.mp4",
                            "text": "片名：绵羊侦探团\n首映：2026-05-08(美国)",
                        }
                    ],
                }

                worker = CompleteDownloadWorker(store, threading.Event(), payload, app.DOWNLOAD_DIR)
                worker.process_job(job)

                final_path = (
                    app.DOWNLOAD_DIR
                    / "Movies"
                    / "绵羊侦探团 (2026)"
                    / "绵羊侦探团 (2026).mp4"
                )
                result = store.get_job(job["id"])
                self.assertEqual(result["status"], "done")
                self.assertNotIn("--continue", worker.download_command)
                self.assertEqual(result["final_filename"], "绵羊侦探团 (2026).mp4")
                self.assertEqual(Path(result["final_path"]), final_path)
                self.assertEqual(final_path.read_bytes(), b"movie")
                self.assertTrue((final_path.parent / "绵羊侦探团 (2026).telegram.json").exists())
                self.assertTrue((final_path.parent / "绵羊侦探团 (2026).telegram.txt").exists())
        finally:
            app.DOWNLOAD_DIR = original_download_dir

    def test_resumed_structured_movie_uses_tdl_continue_with_export_file(self):
        class ResumeDownloadWorker(DownloadWorker):
            def __init__(self, store, stop_event, download_dir):
                super().__init__(store, stop_event)
                self.download_dir = download_dir
                self.download_command = []

            def run_command(self, job_id, cmd, status):
                if status == "exporting":
                    raise AssertionError("resume should reuse existing export metadata")
                if status == "downloading":
                    self.download_command = cmd
                    default_path = self.download_dir / "7487350635_23402_[source]姝ｇ墖.mp4"
                    default_path.write_bytes(b"movie")
                    return 0
                return 0

        original_download_dir = app.DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app.DOWNLOAD_DIR = root / "downloads"
                app.DOWNLOAD_DIR.mkdir()

                store = JobStore(root / "state")
                store.init()
                queued = store.create_job(23402)
                job = store.claim_next()
                payload = {
                    "id": 7487350635,
                    "messages": [
                        {
                            "id": queued["message_id"],
                            "file": "[source]姝ｇ墖.mp4",
                            "text": "鐗囧悕锛氱坏缇婁睛鎺㈠洟\n棣栨槧锛?026-05-08(缇庡浗)",
                        }
                    ],
                }
                Path(job["export_path"]).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                store.update_job(
                    job["id"],
                    source_file="[source]姝ｇ墖.mp4",
                    progress=37.5,
                    downloaded="512.00 MB",
                    resume_requested=1,
                )
                job = store.get_job(job["id"])

                worker = ResumeDownloadWorker(store, threading.Event(), app.DOWNLOAD_DIR)
                worker.process_job(job)

                self.assertIn("--continue", worker.download_command)
                self.assertIn("-f", worker.download_command)
                self.assertIn(str(job["export_path"]), worker.download_command)
                self.assertLess(
                    worker.download_command.index("download"),
                    worker.download_command.index("--continue"),
                )
                result = store.get_job(job["id"])
                self.assertEqual(result["status"], "done")
                self.assertEqual(result["resume_requested"], 0)
        finally:
            app.DOWNLOAD_DIR = original_download_dir

    def test_resumed_download_uses_job_download_dir_not_global_default(self):
        class ResumeDownloadWorker(DownloadWorker):
            def __init__(self, store, stop_event, download_dir):
                super().__init__(store, stop_event)
                self.download_dir = download_dir
                self.download_command = []

            def run_command(self, job_id, cmd, status):
                if status == "exporting":
                    raise AssertionError("resume should reuse existing export metadata")
                if status == "downloading":
                    self.download_command = cmd
                    default_path = self.download_dir / "7487350635_23402_[source]正片.mp4"
                    default_path.write_bytes(b"movie")
                    return 0
                return 0

        original_download_dir = app.DOWNLOAD_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app.DOWNLOAD_DIR = root / "default-downloads"
                app.DOWNLOAD_DIR.mkdir()
                custom_dir = root / "custom-downloads"
                custom_dir.mkdir()

                store = JobStore(root / "state")
                store.init()
                queued = store.create_job(23402, download_dir=custom_dir)
                job = store.claim_next()
                payload = {
                    "id": 7487350635,
                    "messages": [
                        {
                            "id": queued["message_id"],
                            "file": "[source]正片.mp4",
                            "text": "片名：绵羊侦探团\n首映：2026-05-08(美国)",
                        }
                    ],
                }
                Path(job["export_path"]).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                store.update_job(
                    job["id"],
                    source_file="[source]正片.mp4",
                    progress=37.5,
                    downloaded="512.00 MB",
                    resume_requested=1,
                )
                job = store.get_job(job["id"])

                worker = ResumeDownloadWorker(store, threading.Event(), custom_dir)
                worker.process_job(job)

                self.assertIn("--continue", worker.download_command)
                self.assertEqual(
                    worker.download_command[worker.download_command.index("-d") + 1],
                    str(custom_dir),
                )
                self.assertNotIn(str(app.DOWNLOAD_DIR), worker.download_command)
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
