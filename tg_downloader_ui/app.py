#!/usr/bin/env python3
"""Lightweight Telegram download manager for OpenWRT."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_NAME = "tg-downloader-ui"
DEFAULT_HOST = os.environ.get("TGDL_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("TGDL_PORT", "9910"))
STATE_DIR = Path(os.environ.get("TGDL_STATE_DIR", "/mnt/sata1-5/tg-downloader-ui"))
DOWNLOAD_DIR = Path(os.environ.get("TGDL_DOWNLOAD_DIR", "/mnt/sata1-5/telegram_downloads"))
TDL_BIN = os.environ.get("TGDL_TDL_BIN", "/opt/bin/tdl")
TDL_PROXY = os.environ.get("TGDL_PROXY", "socks5://127.0.0.1:7891")
TDL_STORAGE = os.environ.get("TGDL_TDL_STORAGE", "type=bolt,path=/root/.tdl/data")
TDL_CHAT = os.environ.get("TGDL_CHAT", "Youxiu_bot")
AUTH_USER = os.environ.get("TGDL_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("TGDL_AUTH_PASSWORD", "admin123")
TDL_LOG_PATH = Path(os.environ.get("TGDL_TDL_LOG", "/root/.tdl/log/latest.log"))


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RE = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True)
class ExportMetadata:
    dialog_id: int
    message_id: int
    source_file: str
    title: str
    extension: str
    text: str


def utcish_now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def human_seconds(seconds: int) -> str:
    minutes, sec = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{sec}s"
    if minutes:
        return f"{minutes}m{sec}s"
    return f"{sec}s"


def extract_title(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("片名："):
            return line.split("：", 1)[1].strip()
        if line.startswith("片名:"):
            return line.split(":", 1)[1].strip()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line:
            return line[:80].strip()
    return ""


def sanitize_filename(value: str, fallback: str = "download") -> str:
    cleaned = INVALID_FILENAME_RE.sub("", value)
    cleaned = SPACE_RE.sub(" ", cleaned).strip(" .")
    return cleaned or fallback


def extract_export_metadata(export_json: str, expected_message_id: int) -> ExportMetadata:
    data = json.loads(export_json)
    dialog_id = int(data["id"])
    messages = data.get("messages") or []
    if not messages:
        raise ValueError(f"message {expected_message_id} was not exported")

    selected = None
    for message in messages:
        if int(message.get("id", -1)) == int(expected_message_id):
            selected = message
            break
    if selected is None:
        selected = messages[0]

    source_file = str(selected.get("file") or "").strip()
    if not source_file:
        raise ValueError(f"message {expected_message_id} has no downloadable file")

    text = str(selected.get("text") or "")
    title = extract_title(text) or Path(source_file).stem or f"message_{expected_message_id}"
    extension = Path(source_file).suffix

    return ExportMetadata(
        dialog_id=dialog_id,
        message_id=int(selected.get("id", expected_message_id)),
        source_file=source_file,
        title=title,
        extension=extension,
        text=text,
    )


def build_final_filename(metadata: ExportMetadata) -> str:
    title = sanitize_filename(metadata.title, fallback=f"message_{metadata.message_id}")
    extension = metadata.extension if metadata.extension.startswith(".") else ""
    return f"{title}{extension}"


def parse_tdl_progress(text: str) -> dict[str, Any]:
    clean = strip_ansi(text).replace("\r", "\n")
    result: dict[str, Any] = {
        "percent": None,
        "downloaded": None,
        "eta": None,
        "speed": None,
        "flood_wait_seconds": None,
    }

    flood = re.search(r"FLOOD_WAIT_(\d+)", clean)
    if flood:
        result["flood_wait_seconds"] = int(flood.group(1))

    percents = re.findall(r"(\d+(?:\.\d+)?)%", clean)
    if percents:
        result["percent"] = float(percents[-1])

    downloaded = re.findall(r"(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB))\s+in\b", clean)
    if downloaded:
        result["downloaded"] = downloaded[-1].replace("  ", " ")

    eta = re.findall(r"~ETA:\s*([^;\]\n]+)", clean)
    if eta:
        result["eta"] = eta[-1].strip()

    speed = re.findall(r"(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB))/s", clean)
    if speed:
        result["speed"] = speed[-1].replace("  ", " ") + "/s"

    return result


def parse_message_ids(raw: Any) -> list[int]:
    if isinstance(raw, list):
        tokens = raw
    else:
        tokens = re.split(r"[\s,;，；]+", str(raw or ""))

    ids: list[int] = []
    for token in tokens:
        text = str(token).strip()
        if not text:
            continue
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"invalid message id: {text}")
        ids.append(int(text))
    if not ids:
        raise ValueError("no message ids provided")
    return ids


def build_tdl_base_args() -> list[str]:
    return [TDL_BIN, "--storage", TDL_STORAGE, "--proxy", TDL_PROXY]


class JobStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.db_path = state_dir / "state.db"
        self.logs_dir = state_dir / "logs"
        self.exports_dir = state_dir / "exports"
        self.lock = threading.RLock()

    def init(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        now = utcish_now()
        with contextlib.closing(self.connect()) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    final_filename TEXT NOT NULL DEFAULT '',
                    final_path TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    downloaded TEXT NOT NULL DEFAULT '',
                    speed TEXT NOT NULL DEFAULT '',
                    eta TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL DEFAULT '',
                    export_path TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = 'service restarted while job was active',
                    updated_at = ?,
                    finished_at = ?
                WHERE status IN ('exporting', 'downloading', 'renaming')
                """,
                (now, now),
            )
            db.commit()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def create_job(self, message_id: int) -> dict[str, Any]:
        now = utcish_now()
        with self.lock, contextlib.closing(self.connect()) as db:
            cur = db.execute(
                """
                INSERT INTO jobs (
                    message_id, status, log_path, export_path, created_at, updated_at
                ) VALUES (?, 'queued', '', '', ?, ?)
                """,
                (message_id, now, now),
            )
            job_id = int(cur.lastrowid)
            log_path = str(self.logs_dir / f"{job_id}.log")
            export_path = str(self.exports_dir / f"{job_id}.json")
            db.execute(
                "UPDATE jobs SET log_path = ?, export_path = ? WHERE id = ?",
                (log_path, export_path, job_id),
            )
            db.commit()
        self.append_log(job_id, f"Queued message {message_id}\n")
        return self.get_job(job_id) or {}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.lock, contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock, contextlib.closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]

    def claim_next(self) -> dict[str, Any] | None:
        now = utcish_now()
        with self.lock, contextlib.closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                """
                UPDATE jobs
                SET status = 'exporting', started_at = ?, updated_at = ?,
                    attempts = attempts + 1, error = '', progress = 0,
                    downloaded = '', speed = '', eta = ''
                WHERE id = ?
                """,
                (now, now, row["id"]),
            )
            db.commit()
        return self.get_job(int(row["id"]))

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcish_now()
        keys = list(fields.keys())
        values = [fields[key] for key in keys]
        assignments = ", ".join(f"{key} = ?" for key in keys)
        with self.lock, contextlib.closing(self.connect()) as db:
            db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values + [job_id])
            db.commit()

    def finish_job(self, job_id: int, status: str, **fields: Any) -> None:
        fields["status"] = status
        fields["finished_at"] = utcish_now()
        self.update_job(job_id, **fields)

    def retry_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if job["status"] != "failed":
            raise ValueError("only failed jobs can be retried")
        self.update_job(
            job_id,
            status="queued",
            progress=0,
            downloaded="",
            speed="",
            eta="",
            error="",
            started_at="",
            finished_at="",
        )
        self.append_log(job_id, "\nRetry queued\n")
        return self.get_job(job_id) or {}

    def append_log(self, job_id: int, text: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return
        path = Path(job["log_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)

    def tail_log(self, job_id: int, limit: int = 200) -> str:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        path = Path(job["log_path"])
        if not path.exists():
            return ""
        data = path.read_bytes()[-256 * 1024 :]
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-limit:])


class DownloadWorker(threading.Thread):
    def __init__(self, store: JobStore, stop_event: threading.Event) -> None:
        super().__init__(name="download-worker", daemon=True)
        self.store = store
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            job = self.store.claim_next()
            if not job:
                self.stop_event.wait(1.0)
                continue
            try:
                self.process_job(job)
            except Exception as exc:  # noqa: BLE001 - job isolation boundary
                self.store.append_log(job["id"], f"\nFAILED: {exc}\n")
                self.store.finish_job(job["id"], "failed", error=str(exc))

    def process_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        message_id = int(job["message_id"])
        export_path = Path(job["export_path"])
        log_path = Path(job["log_path"])
        log_path.write_text("", encoding="utf-8")

        self.store.append_log(job_id, f"Start message {message_id}\n")
        export_cmd = build_tdl_base_args() + [
            "chat",
            "export",
            "-c",
            TDL_CHAT,
            "-T",
            "id",
            "-i",
            str(message_id),
            "-o",
            str(export_path),
            "--with-content",
        ]
        export_code = self.run_command(job_id, export_cmd, status="exporting")
        if export_code != 0:
            raise RuntimeError(f"tdl export failed with exit code {export_code}")

        metadata = extract_export_metadata(export_path.read_text(encoding="utf-8"), message_id)
        final_filename = build_final_filename(metadata)
        final_path = DOWNLOAD_DIR / final_filename
        source_name = sanitize_filename(metadata.source_file, fallback=f"message_{message_id}")
        default_path = DOWNLOAD_DIR / f"{metadata.dialog_id}_{metadata.message_id}_{source_name}"

        self.store.update_job(
            job_id,
            title=metadata.title,
            source_file=metadata.source_file,
            final_filename=final_filename,
            final_path=str(final_path),
        )

        if final_path.exists() and final_path.stat().st_size > 0:
            self.store.append_log(job_id, f"Already exists: {final_path}\n")
            self.cleanup_partial_files(job_id, metadata, default_path)
            self.store.finish_job(
                job_id,
                "skipped",
                progress=100,
                downloaded=self.format_size(final_path.stat().st_size),
            )
            return

        if default_path.exists():
            rename_path = final_path
            if rename_path.exists():
                rename_path = DOWNLOAD_DIR / self.unique_final_filename(final_filename, message_id)
            self.store.append_log(job_id, f"Rename existing file: {default_path} -> {rename_path}\n")
            default_path.rename(rename_path)
            self.store.finish_job(
                job_id,
                "done",
                progress=100,
                downloaded=self.format_size(rename_path.stat().st_size),
                final_filename=rename_path.name,
                final_path=str(rename_path),
            )
            return

        download_cmd = build_tdl_base_args() + [
            "-t",
            "1",
            "-l",
            "1",
            "--pool",
            "1",
            "--disable-progress-ps",
            "download",
            "-d",
            str(DOWNLOAD_DIR),
            "-f",
            str(export_path),
            "--skip-same",
        ]
        self.store.update_job(job_id, status="downloading")
        download_code = self.run_command(job_id, download_cmd, status="downloading")
        if download_code != 0:
            raise RuntimeError(f"tdl download failed with exit code {download_code}")

        downloaded_path = self.find_downloaded_path(metadata, default_path, final_path)
        if not downloaded_path:
            raise RuntimeError("tdl exited successfully but downloaded file was not found")

        if downloaded_path != final_path:
            if final_path.exists():
                final_filename = self.unique_final_filename(final_filename, message_id)
                final_path = DOWNLOAD_DIR / final_filename
            self.store.append_log(job_id, f"Rename: {downloaded_path} -> {final_path}\n")
            downloaded_path.rename(final_path)

        self.store.finish_job(
            job_id,
            "done",
            progress=100,
            downloaded=self.format_size(final_path.stat().st_size),
            final_filename=final_path.name,
            final_path=str(final_path),
            eta="",
        )

    def run_command(self, job_id: int, cmd: list[str], status: str) -> int:
        self.store.append_log(job_id, "$ " + " ".join(cmd) + "\n")
        started = time.time()
        last_update = 0.0
        last_progress = 0.0
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(STATE_DIR),
            env={**os.environ, "TERM": "xterm"},
        )
        assert proc.stdout is not None

        with Path(self.store.get_job(job_id)["log_path"]).open("ab") as log:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                log.write(chunk)
                log.flush()

                text = chunk.decode("utf-8", errors="replace")
                progress = parse_tdl_progress(text)
                if progress["flood_wait_seconds"]:
                    seconds = int(progress["flood_wait_seconds"])
                    message = f"Telegram flood wait: {human_seconds(seconds)}"
                    self.store.update_job(job_id, error=message)

                fields: dict[str, Any] = {"status": status}
                if progress["percent"] is not None:
                    fields["progress"] = progress["percent"]
                    last_progress = float(progress["percent"])
                if progress["downloaded"]:
                    fields["downloaded"] = progress["downloaded"]
                if progress["speed"]:
                    fields["speed"] = progress["speed"]
                if progress["eta"]:
                    fields["eta"] = progress["eta"]

                now = time.time()
                if len(fields) > 1 and now - last_update > 1.0:
                    self.store.update_job(job_id, **fields)
                    last_update = now

                if status == "downloading" and time.time() - started > 90 and last_progress <= 0:
                    flood_wait = self.read_latest_flood_wait()
                    if flood_wait:
                        self.store.update_job(
                            job_id,
                            error=f"Telegram flood wait: {human_seconds(flood_wait)}",
                        )
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return 124

        return proc.wait()

    def read_latest_flood_wait(self) -> int | None:
        if not TDL_LOG_PATH.exists():
            return None
        data = TDL_LOG_PATH.read_bytes()[-256 * 1024 :]
        text = data.decode("utf-8", errors="replace")
        matches = re.findall(r"FLOOD_WAIT_(\d+)", text)
        if not matches:
            return None
        return int(matches[-1])

    def unique_final_filename(self, desired_name: str, message_id: int) -> str:
        desired_name = sanitize_filename(Path(desired_name).stem) + Path(desired_name).suffix
        candidate = DOWNLOAD_DIR / desired_name
        if not candidate.exists():
            return desired_name

        stem = Path(desired_name).stem
        suffix = Path(desired_name).suffix
        first = f"{stem} - {message_id}{suffix}"
        if not (DOWNLOAD_DIR / first).exists():
            return first

        for index in range(2, 1000):
            name = f"{stem} - {message_id} - {index}{suffix}"
            if not (DOWNLOAD_DIR / name).exists():
                return name
        raise RuntimeError(f"too many filename collisions for {desired_name}")

    def find_downloaded_path(
        self,
        metadata: ExportMetadata,
        default_path: Path,
        final_path: Path,
    ) -> Path | None:
        if final_path.exists() and final_path.stat().st_size > 0:
            return final_path
        if default_path.exists():
            return default_path
        pattern = str(DOWNLOAD_DIR / f"{metadata.dialog_id}_{metadata.message_id}_*")
        candidates = [
            Path(path)
            for path in glob.glob(pattern)
            if not path.endswith(".tmp") and Path(path).is_file()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def cleanup_partial_files(
        self,
        job_id: int,
        metadata: ExportMetadata,
        default_path: Path,
    ) -> None:
        candidates = {Path(str(default_path) + ".tmp")}
        pattern = str(DOWNLOAD_DIR / f"{metadata.dialog_id}_{metadata.message_id}_*.tmp")
        candidates.update(Path(path) for path in glob.glob(pattern))

        for path in sorted(candidates):
            if not path.exists() or not path.is_file():
                continue
            try:
                path.unlink()
                self.store.append_log(job_id, f"Removed partial file: {path}\n")
            except OSError as exc:
                self.store.append_log(job_id, f"Could not remove partial file {path}: {exc}\n")

    def format_size(self, size: int) -> str:
        value = float(size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024 or unit == "TB":
                return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.2f} TB"


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram 下载管理</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f4;
      --panel: #ffffff;
      --line: #d9ded4;
      --text: #18201b;
      --muted: #657064;
      --accent: #28735f;
      --accent-ink: #ffffff;
      --warn: #9a6a00;
      --bad: #b33b32;
      --good: #26734d;
      --soft: #edf1ea;
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header {
      display: flex; align-items: center; justify-content: space-between;
      min-height: 64px; padding: 0 28px; border-bottom: 1px solid var(--line);
      background: #fbfcfa;
    }
    h1 { font-size: 20px; font-weight: 700; margin: 0; letter-spacing: 0; }
    main { max-width: 1180px; margin: 0 auto; padding: 22px; }
    .submit-band {
      display: grid; grid-template-columns: minmax(220px, 1fr) auto;
      gap: 12px; align-items: stretch; padding: 16px 0 22px;
      border-bottom: 1px solid var(--line);
    }
    textarea {
      width: 100%; min-height: 72px; resize: vertical; border: 1px solid var(--line);
      background: var(--panel); color: var(--text); border-radius: 6px;
      padding: 12px; font-size: 16px; line-height: 1.4; outline: none;
    }
    textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(40,115,95,.12); }
    button {
      border: 0; border-radius: 6px; background: var(--accent); color: var(--accent-ink);
      padding: 0 18px; font-size: 15px; font-weight: 700; cursor: pointer;
      min-width: 112px;
    }
    button.secondary { background: #e3e8df; color: var(--text); border: 1px solid var(--line); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .summary { display: flex; gap: 10px; flex-wrap: wrap; padding: 18px 0; }
    .metric {
      min-width: 150px; padding: 10px 12px; border: 1px solid var(--line);
      background: var(--panel); border-radius: 6px;
    }
    .metric strong { display: block; font-size: 20px; line-height: 1.2; }
    .metric span { color: var(--muted); font-size: 12px; }
    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }
    th, td { padding: 10px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
    th { font-size: 12px; color: var(--muted); background: var(--soft); font-weight: 700; }
    td { font-size: 14px; }
    .mono { font-family: Consolas, "Cascadia Mono", monospace; }
    .title-cell { max-width: 320px; overflow-wrap: anywhere; }
    .status {
      display: inline-flex; align-items: center; min-width: 82px; justify-content: center;
      border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 700;
      background: #e8ece6; color: var(--muted);
    }
    .status.done, .status.skipped { color: var(--good); background: #e3f1e9; }
    .status.failed { color: var(--bad); background: #f7e5e2; }
    .status.downloading, .status.exporting, .status.renaming { color: var(--warn); background: #fff0c9; }
    .bar { width: 132px; height: 8px; border-radius: 999px; background: #dce2d8; overflow: hidden; }
    .bar > i { display: block; height: 100%; background: var(--accent); width: 0%; }
    .actions { display: flex; gap: 8px; }
    .log {
      margin-top: 14px; border: 1px solid var(--line); background: #101511; color: #dfe7dc;
      border-radius: 6px; min-height: 160px; max-height: 360px; overflow: auto;
      padding: 12px; white-space: pre-wrap; font: 12px/1.5 Consolas, "Cascadia Mono", monospace;
    }
    .muted { color: var(--muted); }
    .error { color: var(--bad); }
    .table-wrap { overflow-x: auto; }
    @media (max-width: 720px) {
      header { padding: 0 16px; }
      main { padding: 14px; }
      .submit-band { grid-template-columns: 1fr; }
      button { min-height: 44px; }
      th, td { padding: 9px 7px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Telegram 下载管理</h1>
    <span class="muted" id="clock"></span>
  </header>
  <main>
    <section class="submit-band">
      <textarea id="messageIds" aria-label="消息 ID" placeholder="23311"></textarea>
      <button id="submitBtn">加入队列</button>
    </section>

    <section class="summary" id="summary"></section>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>消息</th><th>状态</th><th>片名</th><th>进度</th>
            <th>速度</th><th>文件</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="jobsBody"></tbody>
      </table>
    </div>
    <pre class="log" id="logPanel">选择任务查看日志</pre>
  </main>
  <script>
    let selectedJob = null;

    function statusLabel(status) {
      const labels = {
        queued: '排队', exporting: '导出', downloading: '下载',
        renaming: '重命名', done: '完成', skipped: '已存在', failed: '失败'
      };
      return labels[status] || status;
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {'Content-Type': 'application/json'},
        ...options
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      const type = res.headers.get('Content-Type') || '';
      return type.includes('application/json') ? res.json() : res.text();
    }

    async function submitJobs() {
      const raw = document.getElementById('messageIds').value;
      const btn = document.getElementById('submitBtn');
      btn.disabled = true;
      try {
        await api('/api/jobs', {method: 'POST', body: JSON.stringify({message_ids: raw})});
        document.getElementById('messageIds').value = '';
        await refreshJobs();
      } catch (err) {
        alert(err.message);
      } finally {
        btn.disabled = false;
      }
    }

    async function retryJob(id) {
      await api(`/api/jobs/${id}/retry`, {method: 'POST', body: '{}'});
      await refreshJobs();
    }

    async function loadLog(id) {
      selectedJob = id;
      const text = await api(`/api/jobs/${id}/log`);
      document.getElementById('logPanel').textContent = text || '暂无日志';
    }

    function renderSummary(jobs) {
      const counts = jobs.reduce((acc, job) => {
        acc[job.status] = (acc[job.status] || 0) + 1;
        return acc;
      }, {});
      const active = jobs.find(job => ['exporting', 'downloading', 'renaming'].includes(job.status));
      const items = [
        ['进行中', active ? `#${active.id}` : '0'],
        ['排队', counts.queued || 0],
        ['完成', (counts.done || 0) + (counts.skipped || 0)],
        ['失败', counts.failed || 0],
      ];
      document.getElementById('summary').innerHTML = items.map(([label, value]) =>
        `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${label}</span></div>`
      ).join('');
    }

    function renderJobs(jobs) {
      const body = document.getElementById('jobsBody');
      if (!jobs.length) {
        body.innerHTML = '<tr><td colspan="8" class="muted">暂无任务</td></tr>';
        return;
      }
      body.innerHTML = jobs.map(job => {
        const pct = Math.max(0, Math.min(100, Number(job.progress || 0)));
        const title = job.title || job.error || '';
        const file = job.final_path || job.source_file || '';
        const retry = job.status === 'failed'
          ? `<button class="secondary" onclick="retryJob(${job.id})">重试</button>` : '';
        return `<tr>
          <td class="mono">#${job.id}</td>
          <td class="mono">${job.message_id}</td>
          <td><span class="status ${job.status}">${statusLabel(job.status)}</span></td>
          <td class="title-cell">${escapeHtml(title)}</td>
          <td><div class="bar"><i style="width:${pct}%"></i></div><span class="muted">${pct.toFixed(1)}%</span></td>
          <td>${escapeHtml(job.speed || '')}</td>
          <td class="title-cell">${escapeHtml(file)}</td>
          <td class="actions"><button class="secondary" onclick="loadLog(${job.id})">日志</button>${retry}</td>
        </tr>`;
      }).join('');
    }

    async function refreshJobs() {
      try {
        const data = await api('/api/jobs');
        renderSummary(data.jobs);
        renderJobs(data.jobs);
        if (selectedJob) {
          const text = await api(`/api/jobs/${selectedJob}/log`);
          document.getElementById('logPanel').textContent = text || '暂无日志';
        }
      } catch (err) {
        document.getElementById('logPanel').textContent = err.message;
      }
    }

    document.getElementById('submitBtn').addEventListener('click', submitJobs);
    setInterval(() => {
      document.getElementById('clock').textContent = new Date().toLocaleString();
    }, 1000);
    refreshJobs();
    setInterval(refreshJobs, 2500);
  </script>
</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "TGDownloaderUI/1.0"

    @property
    def store(self) -> JobStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self.authorized():
            return self.require_auth()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            return self.send_html(INDEX_HTML)
        if parsed.path == "/api/jobs":
            return self.send_json({"jobs": self.store.list_jobs()})

        match = re.fullmatch(r"/api/jobs/(\d+)/log", parsed.path)
        if match:
            try:
                return self.send_text(self.store.tail_log(int(match.group(1))))
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.NOT_FOUND, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self.authorized():
            return self.require_auth()
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/jobs":
            try:
                payload = self.read_json()
                message_ids = parse_message_ids(payload.get("message_ids"))
                jobs = [self.store.create_job(message_id) for message_id in message_ids]
                return self.send_json({"jobs": jobs}, status=HTTPStatus.CREATED)
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        match = re.fullmatch(r"/api/jobs/(\d+)/retry", parsed.path)
        if match:
            try:
                job = self.store.retry_job(int(match.group(1)))
                return self.send_json({"job": job})
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:  # noqa: BLE001 - invalid auth header
            return False
        username, sep, password = decoded.partition(":")
        return sep == ":" and username == AUTH_USER and password == AUTH_PASSWORD

    def require_auth(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Telegram Downloader"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"authentication required\n")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_text(self, status: HTTPStatus, message: str) -> None:
        self.send_text(message + "\n", status=status)


class DownloadServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], store: JobStore):
        super().__init__(address, handler)
        self.store = store


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    store = JobStore(STATE_DIR)
    store.init()
    stop_event = threading.Event()
    worker = DownloadWorker(store, stop_event)
    worker.start()

    httpd = DownloadServer((host, port), RequestHandler, store)

    def stop(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"{APP_NAME} listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop_event.set()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--check" in argv:
        store = JobStore(STATE_DIR)
        store.init()
        print("ok")
        return 0
    run_server(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
