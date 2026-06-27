#!/usr/bin/env python3
"""Lightweight Telegram download manager for OpenWRT."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import glob
import hashlib
import hmac
import html
import json
import os
import re
import secrets
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

try:  # Package import locally, flat import on OpenWRT deployment.
    from .sources import DEFAULT_SOURCE_ID, DEFAULT_SOURCES, normalize_sources
except ImportError:  # pragma: no cover - exercised by OpenWRT flat deployment
    from sources import DEFAULT_SOURCE_ID, DEFAULT_SOURCES, normalize_sources


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
SESSION_COOKIE = "tgdl_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("TGDL_SESSION_MAX_AGE", str(7 * 24 * 60 * 60)))
PASSWORD_ITERATIONS = 200_000
ACTIVE_STATUSES = {"exporting", "downloading", "renaming"}
CANCEL_EXIT_CODE = 130


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


def hash_password(password: str, salt_hex: str | None = None) -> dict[str, Any]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return {
        "salt": salt.hex(),
        "hash": digest.hex(),
        "iterations": PASSWORD_ITERATIONS,
    }


class ConfigStore:
    def __init__(
        self,
        state_dir: Path,
        default_download_dir: Path | None = None,
        default_user: str | None = None,
        default_password: str | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "config.json"
        self.default_download_dir = Path(default_download_dir or DOWNLOAD_DIR)
        self.default_user = default_user or AUTH_USER
        self.default_password = default_password or AUTH_PASSWORD
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {}

    def init(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            else:
                self.data = {}

            changed = False
            if not self.data.get("download_dir"):
                self.data["download_dir"] = str(self.default_download_dir)
                changed = True

            sources, default_source_id = normalize_sources(
                self.data.get("sources"),
                self.data.get("default_source_id"),
            )
            if self.data.get("sources") != sources:
                self.data["sources"] = sources
                changed = True
            if self.data.get("default_source_id") != default_source_id:
                self.data["default_source_id"] = default_source_id
                changed = True

            auth = self.data.setdefault("auth", {})
            if not auth.get("password_hash") or not auth.get("password_salt"):
                hashed = hash_password(self.default_password)
                auth.update(
                    {
                        "username": self.default_user,
                        "password_hash": hashed["hash"],
                        "password_salt": hashed["salt"],
                        "password_iterations": hashed["iterations"],
                        "session_version": int(auth.get("session_version") or 1),
                    }
                )
                changed = True
            else:
                if not auth.get("username"):
                    auth["username"] = self.default_user
                    changed = True
                if not auth.get("session_version"):
                    auth["session_version"] = 1
                    changed = True

            if changed:
                self.save()

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def get_download_dir(self) -> Path:
        with self.lock:
            return Path(self.data.get("download_dir") or self.default_download_dir)

    def list_sources(self) -> list[dict[str, Any]]:
        with self.lock:
            sources, _ = normalize_sources(
                self.data.get("sources"),
                self.data.get("default_source_id"),
            )
            return [dict(source) for source in sources]

    def get_default_source_id(self) -> str:
        with self.lock:
            _, default_source_id = normalize_sources(
                self.data.get("sources"),
                self.data.get("default_source_id"),
            )
            return default_source_id

    def get_default_source(self) -> dict[str, Any]:
        return self.get_source(self.get_default_source_id())

    def get_source(self, source_id: str | None = None) -> dict[str, Any]:
        selected = str(source_id or self.get_default_source_id()).strip()
        with self.lock:
            sources, default_source_id = normalize_sources(
                self.data.get("sources"),
                self.data.get("default_source_id"),
            )
            selected = selected or default_source_id
            for source in sources:
                if source["id"] == selected:
                    if not source.get("enabled", True):
                        raise ValueError("source is disabled")
                    return dict(source)
        raise ValueError("source not found")

    def set_sources(
        self,
        sources_value: Any,
        default_source_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        sources, selected_default = normalize_sources(sources_value, default_source_id)
        with self.lock:
            self.data["sources"] = sources
            self.data["default_source_id"] = selected_default
            self.save()
        return [dict(source) for source in sources], selected_default

    def set_download_dir(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("download dir must be an absolute path")
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".tgdl_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            if probe.exists():
                probe.unlink()
        with self.lock:
            self.data["download_dir"] = str(path)
            self.save()
        return path

    def get_username(self) -> str:
        with self.lock:
            return str(self.data.get("auth", {}).get("username") or self.default_user)

    def get_session_version(self) -> int:
        with self.lock:
            return int(self.data.get("auth", {}).get("session_version") or 1)

    def verify_password(self, username: str, password: str) -> bool:
        with self.lock:
            auth = self.data.get("auth", {})
            if username != str(auth.get("username") or self.default_user):
                return False
            salt = str(auth.get("password_salt") or "")
            expected = str(auth.get("password_hash") or "")
            if not salt or not expected:
                return False
            actual = hash_password(password, salt)["hash"]
            return hmac.compare_digest(actual, expected)

    def set_password(self, username: str, new_password: str) -> None:
        if not new_password:
            raise ValueError("new password cannot be empty")
        hashed = hash_password(new_password)
        with self.lock:
            auth = self.data.setdefault("auth", {})
            auth["username"] = username
            auth["password_hash"] = hashed["hash"]
            auth["password_salt"] = hashed["salt"]
            auth["password_iterations"] = hashed["iterations"]
            auth["session_version"] = int(auth.get("session_version") or 1) + 1
            self.save()


class AuthManager:
    def __init__(
        self,
        config_store: ConfigStore,
        session_max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
    ) -> None:
        self.config_store = config_store
        self.session_max_age_seconds = session_max_age_seconds
        self.sessions: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

    def verify_password(self, username: str, password: str) -> bool:
        return self.config_store.verify_password(username, password)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.session_max_age_seconds
        with self.lock:
            self.sessions[token] = {
                "username": username,
                "expires_at": expires_at,
                "session_version": self.config_store.get_session_version(),
            }
        return token

    def get_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.lock:
            session = self.sessions.get(token)
            if not session:
                return None
            if float(session["expires_at"]) < time.time():
                self.sessions.pop(token, None)
                return None
            if int(session["session_version"]) != self.config_store.get_session_version():
                self.sessions.pop(token, None)
                return None
            return dict(session)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            self.sessions.pop(token, None)

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        if not self.verify_password(username, current_password):
            raise ValueError("current password is incorrect")
        self.config_store.set_password(username, new_password)
        with self.lock:
            self.sessions.clear()


class JobCanceled(RuntimeError):
    pass


class JobStore:
    def __init__(self, state_dir: Path, config_store: ConfigStore | None = None) -> None:
        self.state_dir = state_dir
        self.db_path = state_dir / "state.db"
        self.logs_dir = state_dir / "logs"
        self.exports_dir = state_dir / "exports"
        self.config_store = config_store or ConfigStore(state_dir)
        self.lock = threading.RLock()

    def init(self) -> None:
        self.config_store.init()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.config_store.get_download_dir().mkdir(parents=True, exist_ok=True)
        now = utcish_now()
        with contextlib.closing(self.connect()) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    source_label TEXT NOT NULL DEFAULT '',
                    source_chat TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    final_filename TEXT NOT NULL DEFAULT '',
                    final_path TEXT NOT NULL DEFAULT '',
                    download_dir TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    downloaded TEXT NOT NULL DEFAULT '',
                    speed TEXT NOT NULL DEFAULT '',
                    eta TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL DEFAULT '',
                    export_path TEXT NOT NULL DEFAULT '',
                    process_pid INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self.ensure_column(db, "download_dir", "download_dir TEXT NOT NULL DEFAULT ''")
            self.ensure_column(db, "process_pid", "process_pid INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(db, "cancel_requested", "cancel_requested INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(db, "source_id", "source_id TEXT NOT NULL DEFAULT ''")
            self.ensure_column(db, "source_label", "source_label TEXT NOT NULL DEFAULT ''")
            self.ensure_column(db, "source_chat", "source_chat TEXT NOT NULL DEFAULT ''")
            db.execute(
                "UPDATE jobs SET download_dir = ? WHERE download_dir = ''",
                (str(self.config_store.get_download_dir()),),
            )
            default_source = self.config_store.get_default_source()
            db.execute(
                """
                UPDATE jobs
                SET source_id = ?, source_label = ?, source_chat = ?
                WHERE source_id = '' OR source_chat = ''
                """,
                (
                    str(default_source["id"]),
                    str(default_source["label"]),
                    str(default_source["chat"]),
                ),
            )
            db.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = 'service restarted while job was active',
                    updated_at = ?,
                    finished_at = ?,
                    process_pid = 0,
                    cancel_requested = 0
                WHERE status IN ('exporting', 'downloading', 'renaming')
                """,
                (now, now),
            )
            db.commit()

    def ensure_column(self, db: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
        if name not in columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {definition}")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def create_job(
        self,
        message_id: int,
        download_dir: str | Path | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        now = utcish_now()
        job_download_dir = str(Path(download_dir or self.config_store.get_download_dir()))
        source = self.config_store.get_source(source_id)
        with self.lock, contextlib.closing(self.connect()) as db:
            cur = db.execute(
                """
                INSERT INTO jobs (
                    message_id, source_id, source_label, source_chat, status,
                    download_dir, log_path, export_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, '', '', ?, ?)
                """,
                (
                    message_id,
                    str(source["id"]),
                    str(source["label"]),
                    str(source["chat"]),
                    job_download_dir,
                    now,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
            log_path = str(self.logs_dir / f"{job_id}.log")
            export_path = str(self.exports_dir / f"{job_id}.json")
            db.execute(
                "UPDATE jobs SET log_path = ?, export_path = ? WHERE id = ?",
                (log_path, export_path, job_id),
            )
            db.commit()
        self.append_log(job_id, f"Queued message {message_id} from {source['label']}\n")
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
                    downloaded = '', speed = '', eta = '',
                    process_pid = 0, cancel_requested = 0
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
        fields["process_pid"] = 0
        if status != "canceled":
            fields.setdefault("cancel_requested", 0)
        fields["finished_at"] = utcish_now()
        self.update_job(job_id, **fields)

    def retry_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if job["status"] not in {"failed", "canceled"}:
            raise ValueError("only failed or canceled jobs can be retried")
        self.update_job(
            job_id,
            status="queued",
            progress=0,
            downloaded="",
            speed="",
            eta="",
            error="",
            process_pid=0,
            cancel_requested=0,
            started_at="",
            finished_at="",
        )
        self.append_log(job_id, "\nRetry queued\n")
        return self.get_job(job_id) or {}

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        status = str(job["status"])
        if status == "queued":
            self.finish_job(
                job_id,
                "canceled",
                error="canceled by user",
                cancel_requested=1,
            )
            self.append_log(job_id, "\nCanceled while queued\n")
            return self.get_job(job_id) or {}
        if status in ACTIVE_STATUSES:
            self.update_job(job_id, cancel_requested=1, error="cancel requested")
            self.append_log(job_id, "\nCancel requested\n")
            return self.get_job(job_id) or {}
        raise ValueError("only queued or active jobs can be canceled")

    def delete_job(self, job_id: int) -> None:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if str(job["status"]) in ACTIVE_STATUSES:
            raise ValueError("active jobs must be canceled before deletion")

        paths = [Path(job["log_path"]), Path(job["export_path"])]
        with self.lock, contextlib.closing(self.connect()) as db:
            db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            db.commit()
        for path in paths:
            try:
                if path.exists() and path.is_file():
                    path.unlink()
            except OSError:
                pass

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
            except JobCanceled as exc:
                self.store.append_log(job["id"], f"\nCANCELED: {exc}\n")
                self.store.finish_job(
                    job["id"],
                    "canceled",
                    error=str(exc) or "canceled by user",
                    cancel_requested=1,
                )
            except Exception as exc:  # noqa: BLE001 - job isolation boundary
                self.store.append_log(job["id"], f"\nFAILED: {exc}\n")
                self.store.finish_job(job["id"], "failed", error=str(exc))

    def process_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        message_id = int(job["message_id"])
        download_dir = Path(job.get("download_dir") or self.store.config_store.get_download_dir())
        source_chat = str(job.get("source_chat") or self.store.config_store.get_default_source()["chat"])
        source_label = str(job.get("source_label") or source_chat)
        download_dir.mkdir(parents=True, exist_ok=True)
        export_path = Path(job["export_path"])
        log_path = Path(job["log_path"])
        log_path.write_text("", encoding="utf-8")

        self.store.append_log(job_id, f"Start message {message_id} from {source_label}\n")
        self.check_canceled(job_id)
        export_cmd = build_tdl_base_args() + [
            "chat",
            "export",
            "-c",
            source_chat,
            "-T",
            "id",
            "-i",
            str(message_id),
            "-o",
            str(export_path),
            "--with-content",
        ]
        export_code = self.run_command(job_id, export_cmd, status="exporting")
        if export_code == CANCEL_EXIT_CODE:
            raise JobCanceled("canceled by user")
        if export_code != 0:
            raise RuntimeError(f"tdl export failed with exit code {export_code}")

        self.check_canceled(job_id)
        metadata = extract_export_metadata(export_path.read_text(encoding="utf-8"), message_id)
        final_filename = build_final_filename(metadata)
        final_path = download_dir / final_filename
        source_name = sanitize_filename(metadata.source_file, fallback=f"message_{message_id}")
        default_path = download_dir / f"{metadata.dialog_id}_{metadata.message_id}_{source_name}"

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
                rename_path = download_dir / self.unique_final_filename(
                    final_filename, message_id, download_dir
                )
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
            str(download_dir),
            "-f",
            str(export_path),
            "--skip-same",
        ]
        self.store.update_job(job_id, status="downloading")
        download_code = self.run_command(job_id, download_cmd, status="downloading")
        if download_code == CANCEL_EXIT_CODE:
            raise JobCanceled("canceled by user")
        if download_code != 0:
            raise RuntimeError(f"tdl download failed with exit code {download_code}")

        self.check_canceled(job_id)
        downloaded_path = self.find_downloaded_path(metadata, default_path, final_path, download_dir)
        if not downloaded_path:
            raise RuntimeError("tdl exited successfully but downloaded file was not found")

        if downloaded_path != final_path:
            if final_path.exists():
                final_filename = self.unique_final_filename(final_filename, message_id, download_dir)
                final_path = download_dir / final_filename
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

    def check_canceled(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if job and int(job.get("cancel_requested") or 0):
            raise JobCanceled("canceled by user")

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
        self.store.update_job(job_id, status=status, process_pid=getattr(proc, "pid", 0) or 0)
        cancel_watch_done = threading.Event()

        def watch_cancel() -> None:
            while not cancel_watch_done.wait(0.5):
                current = self.store.get_job(job_id)
                if current and int(current.get("cancel_requested") or 0):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    return

        watcher = threading.Thread(target=watch_cancel, name=f"cancel-watch-{job_id}", daemon=True)
        watcher.start()

        try:
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

            code = proc.wait()
            current = self.store.get_job(job_id)
            if current and int(current.get("cancel_requested") or 0):
                return CANCEL_EXIT_CODE
            return code
        finally:
            cancel_watch_done.set()
            self.store.update_job(job_id, process_pid=0)

    def read_latest_flood_wait(self) -> int | None:
        if not TDL_LOG_PATH.exists():
            return None
        data = TDL_LOG_PATH.read_bytes()[-256 * 1024 :]
        text = data.decode("utf-8", errors="replace")
        matches = re.findall(r"FLOOD_WAIT_(\d+)", text)
        if not matches:
            return None
        return int(matches[-1])

    def unique_final_filename(
        self,
        desired_name: str,
        message_id: int,
        download_dir: Path | None = None,
    ) -> str:
        download_dir = download_dir or DOWNLOAD_DIR
        desired_name = sanitize_filename(Path(desired_name).stem) + Path(desired_name).suffix
        candidate = download_dir / desired_name
        if not candidate.exists():
            return desired_name

        stem = Path(desired_name).stem
        suffix = Path(desired_name).suffix
        first = f"{stem} - {message_id}{suffix}"
        if not (download_dir / first).exists():
            return first

        for index in range(2, 1000):
            name = f"{stem} - {message_id} - {index}{suffix}"
            if not (download_dir / name).exists():
                return name
        raise RuntimeError(f"too many filename collisions for {desired_name}")

    def find_downloaded_path(
        self,
        metadata: ExportMetadata,
        default_path: Path,
        final_path: Path,
        download_dir: Path | None = None,
    ) -> Path | None:
        download_dir = download_dir or DOWNLOAD_DIR
        if final_path.exists() and final_path.stat().st_size > 0:
            return final_path
        if default_path.exists():
            return default_path
        pattern = str(download_dir / f"{metadata.dialog_id}_{metadata.message_id}_*")
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
        pattern = str(default_path.parent / f"{metadata.dialog_id}_{metadata.message_id}_*.tmp")
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Download Manager</title>
  <style>
    :root { color-scheme: light; --bg:#f4f6f1; --panel:#fff; --line:#d8ded2; --text:#18201b; --muted:#667267; --accent:#28735f; --accent-2:#315f8a; --warn:#9a6a00; --bad:#b33b32; --good:#26734d; --soft:#edf1ea; --side:#17201b; --side-muted:#9cad9d; font-family: Arial, sans-serif; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); }
    h1 { font-size:22px; margin:0; letter-spacing:0; }
    h2 { font-size:16px; margin:0 0 12px; letter-spacing:0; }
    label { display:block; margin-bottom:6px; color:var(--muted); font-size:12px; font-weight:700; }
    input, textarea, select { width:100%; border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:6px; padding:10px 11px; font-size:15px; outline:none; }
    textarea { min-height:72px; resize:vertical; line-height:1.4; }
    input[type=checkbox], input[type=radio] { width:auto; }
    input:focus, textarea:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(40,115,95,.12); }
    button { border:0; border-radius:6px; background:var(--accent); color:#fff; min-height:40px; padding:0 14px; font-size:14px; font-weight:700; cursor:pointer; white-space:nowrap; }
    button.secondary { background:#e3e8df; color:var(--text); border:1px solid var(--line); }
    button.danger { background:var(--bad); color:#fff; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:220px minmax(0, 1fr); }
    .sidebar { background:var(--side); color:#eef5ef; display:flex; flex-direction:column; padding:18px 14px; border-right:1px solid #0d130f; }
    .brand { min-height:54px; display:flex; align-items:center; padding:0 10px 14px; border-bottom:1px solid rgba(255,255,255,.1); font-size:16px; font-weight:800; line-height:1.25; }
    .nav-list { display:flex; flex-direction:column; gap:6px; margin-top:16px; }
    .nav-item { width:100%; justify-content:flex-start; text-align:left; background:transparent; color:var(--side-muted); border:1px solid transparent; min-height:42px; }
    .nav-item.active, .nav-item:hover { color:#fff; background:#223329; border-color:rgba(255,255,255,.08); }
    .sidebar-footer { margin-top:auto; display:flex; flex-direction:column; gap:10px; color:var(--side-muted); font-size:12px; }
    .sidebar-footer button { width:100%; }
    .content { min-width:0; padding:0 24px 30px; }
    .content-header { min-height:70px; display:flex; align-items:center; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); }
    .top { display:flex; align-items:center; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:13px; }
    .page { display:none; max-width:1280px; margin:0 auto; }
    .page.active { display:block; }
    .band { padding:16px 0; border-bottom:1px solid var(--line); }
    .submit-band { display:grid; grid-template-columns:minmax(180px, .35fr) minmax(260px, 1fr) auto; gap:12px; align-items:end; }
    .form-row { display:grid; grid-template-columns:minmax(260px, 1fr) auto auto; gap:10px; align-items:end; }
    .password-grid { display:grid; grid-template-columns:minmax(220px, 1fr) minmax(220px, 1fr) auto; gap:10px; align-items:end; }
    .summary { display:flex; gap:10px; flex-wrap:wrap; }
    .metric { min-width:146px; padding:10px 12px; border:1px solid var(--line); background:var(--panel); border-radius:6px; }
    .metric strong { display:block; font-size:20px; line-height:1.2; overflow-wrap:anywhere; }
    .metric span { color:var(--muted); font-size:12px; }
    .forwarder { display:grid; grid-template-columns:repeat(5, minmax(120px, 1fr)) auto; gap:10px; align-items:stretch; }
    table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); }
    th, td { padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
    th { font-size:12px; color:var(--muted); background:var(--soft); font-weight:700; }
    td { font-size:14px; }
    .mono { font-family:Consolas, monospace; }
    .title-cell { max-width:320px; overflow-wrap:anywhere; }
    .path-cell { max-width:360px; overflow-wrap:anywhere; color:var(--muted); }
    .status { display:inline-flex; align-items:center; min-width:82px; justify-content:center; border-radius:999px; padding:4px 8px; font-size:12px; font-weight:700; background:#e8ece6; color:var(--muted); }
    .status.done, .status.skipped, .status.running { color:var(--good); background:#e3f1e9; }
    .status.failed, .status.canceled, .status.stale { color:var(--bad); background:#f7e5e2; }
    .status.downloading, .status.exporting, .status.renaming, .status.queued { color:var(--warn); background:#fff0c9; }
    .bar { width:120px; height:8px; border-radius:999px; background:#dce2d8; overflow:hidden; }
    .bar > i { display:block; height:100%; background:var(--accent); width:0%; }
    .actions { display:flex; gap:7px; flex-wrap:wrap; }
    .source-row { display:grid; grid-template-columns:minmax(130px, .8fr) minmax(150px, 1fr) minmax(160px, 1fr) auto auto auto; gap:10px; align-items:end; padding:10px 0; border-bottom:1px solid var(--line); }
    .source-row:last-child { border-bottom:0; }
    .check-row { display:flex; align-items:center; gap:7px; min-height:40px; color:var(--muted); font-size:13px; font-weight:700; }
    .log { margin-top:14px; border:1px solid var(--line); background:#101511; color:#dfe7dc; border-radius:6px; min-height:170px; max-height:380px; overflow:auto; padding:12px; white-space:pre-wrap; font:12px/1.5 Consolas, monospace; }
    .muted { color:var(--muted); }
    .message { min-height:20px; margin-top:8px; color:var(--muted); font-size:13px; }
    .message.error { color:var(--bad); }
    .table-wrap { overflow-x:auto; }
    .modal { position:fixed; inset:0; display:grid; place-items:center; background:rgba(11,16,13,.48); padding:20px; z-index:10; }
    .modal.hidden { display:none; }
    .dialog { width:min(760px, 100%); max-height:min(720px, calc(100vh - 40px)); display:flex; flex-direction:column; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    .dialog-header, .dialog-footer { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 16px; border-bottom:1px solid var(--line); }
    .dialog-footer { border-top:1px solid var(--line); border-bottom:0; justify-content:flex-end; }
    .dialog-body { padding:14px 16px; overflow:auto; }
    .path-toolbar { display:grid; grid-template-columns:auto auto minmax(220px, 1fr) auto; gap:8px; margin-bottom:10px; }
    .dir-list { border:1px solid var(--line); border-radius:6px; overflow:hidden; background:#fbfcfa; }
    .dir-row { width:100%; min-height:40px; display:flex; align-items:center; justify-content:space-between; gap:10px; padding:0 12px; background:#fff; color:var(--text); border:0; border-bottom:1px solid var(--line); border-radius:0; text-align:left; font-weight:600; }
    .dir-row:last-child { border-bottom:0; }
    .dir-row:hover { background:#f0f4ec; }
    .dir-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .dir-writable { color:var(--muted); font-size:12px; font-weight:700; }
    @media (max-width:900px) {
      .app-shell { grid-template-columns:1fr; }
      .sidebar { position:sticky; top:0; z-index:5; flex-direction:row; align-items:center; gap:10px; padding:10px; border-right:0; border-bottom:1px solid #0d130f; }
      .brand { min-height:0; border-bottom:0; padding:0 6px; white-space:nowrap; }
      .nav-list { flex-direction:row; margin-top:0; overflow:auto; }
      .nav-item { width:auto; min-height:38px; }
      .sidebar-footer { margin-top:0; margin-left:auto; }
      .content { padding:0 14px 24px; }
      .content-header { align-items:flex-start; flex-direction:column; justify-content:center; padding:14px 0; }
      .submit-band, .form-row, .password-grid, .path-toolbar, .source-row { grid-template-columns:1fr; }
      .forwarder { grid-template-columns:1fr 1fr; }
      button { min-height:42px; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">Telegram Downloads</div>
      <nav class="nav-list" aria-label="Main">
        <button class="nav-item active" data-page="downloads" type="button">Downloads</button>
        <button class="nav-item" data-page="paths" type="button">Paths</button>
        <button class="nav-item" data-page="sources" type="button">Sources</button>
        <button class="nav-item" data-page="password" type="button">Password</button>
      </nav>
      <div class="sidebar-footer"><button class="secondary" id="logoutBtn" type="button">Logout</button></div>
    </aside>
    <main class="content">
      <header class="content-header"><h1 id="pageTitle">Downloads</h1><div class="top"><span id="userLabel"></span><span id="clock"></span></div></header>

      <section class="page active" id="page-downloads">
        <section class="band submit-band">
          <div><label for="sourceSelect">Source</label><select id="sourceSelect"></select></div>
          <textarea id="messageIds" aria-label="Message IDs" placeholder="23311"></textarea>
          <button id="submitBtn" type="button">Queue</button>
        </section>
        <section class="band"><h2>Forwarder</h2><div class="forwarder" id="forwarderStatus"></div></section>
        <section class="band summary" id="summary"></section>
        <section class="band"><div class="table-wrap"><table><thead><tr><th>ID</th><th>Source</th><th>Message</th><th>Status</th><th>Title</th><th>Progress</th><th>Speed</th><th>PID</th><th>Download Dir</th><th>File</th><th>Actions</th></tr></thead><tbody id="jobsBody"></tbody></table></div><pre class="log" id="logPanel"></pre></section>
      </section>

      <section class="page" id="page-paths">
        <section class="band">
          <h2>Download Directory</h2>
          <div class="form-row">
            <div><label for="downloadDir">Current path</label><input id="downloadDir"></div>
            <button class="secondary" id="browseDirBtn" type="button">Browse</button>
            <button id="saveConfigBtn" type="button">Save</button>
          </div>
          <div class="message" id="configMessage"></div>
        </section>
      </section>

      <section class="page" id="page-sources">
        <section class="band">
          <h2>Sources</h2>
          <div id="sourceList"></div>
          <div class="actions"><button class="secondary" id="addSourceBtn" type="button">Add</button><button id="saveSourcesBtn" type="button">Save</button></div>
          <div class="message" id="sourcesMessage"></div>
        </section>
      </section>

      <section class="page" id="page-password">
        <section class="band">
          <h2>Admin Password</h2>
          <div class="password-grid">
            <div><label for="currentPassword">Current password</label><input id="currentPassword" type="password" autocomplete="current-password"></div>
            <div><label for="newPassword">New password</label><input id="newPassword" type="password" autocomplete="new-password"></div>
            <button id="changePasswordBtn" type="button">Change</button>
          </div>
          <div class="message" id="passwordMessage"></div>
        </section>
      </section>
    </main>
  </div>

  <div class="modal hidden" id="dirDialog" role="dialog" aria-modal="true" aria-labelledby="dirDialogTitle">
    <div class="dialog">
      <div class="dialog-header"><h2 id="dirDialogTitle">Select Directory</h2><button class="secondary" id="closeDirBtn" type="button">Close</button></div>
      <div class="dialog-body">
        <div class="path-toolbar">
          <button class="secondary" id="dirParentBtn" type="button">Up</button>
          <button class="secondary" id="dirRootBtn" type="button">Root</button>
          <input id="dirDialogPath" aria-label="Directory path">
          <button class="secondary" id="goDirBtn" type="button">Go</button>
        </div>
        <div class="message" id="dirMessage"></div>
        <div class="dir-list" id="dirList"></div>
      </div>
      <div class="dialog-footer"><button id="selectDirBtn" type="button">Use Current</button></div>
    </div>
  </div>

  <script>
    let selectedJob = null;
    let currentDir = '';
    let currentDirParent = '';
    let sources = [];
    let defaultSourceId = '';
    const pageTitles = {downloads:'Downloads', paths:'Paths', sources:'Sources', password:'Password'};
    function statusLabel(status) { const labels = {queued:'Queued', exporting:'Exporting', downloading:'Downloading', renaming:'Renaming', done:'Done', skipped:'Exists', failed:'Failed', canceled:'Canceled', running:'Running', stale:'Stale', missing:'Missing', unknown:'Unknown'}; return labels[status] || status; }
    function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch])); }
    async function api(path, options = {}) { const headers = {'Content-Type':'application/json', ...(options.headers || {})}; const res = await fetch(path, {...options, headers}); if (res.status === 401) { location.href = '/login'; throw new Error('authentication required'); } if (!res.ok) { const text = await res.text(); throw new Error(text || res.statusText); } const type = res.headers.get('Content-Type') || ''; return type.includes('application/json') ? res.json() : res.text(); }
    function showPage(name) { const next = pageTitles[name] ? name : 'downloads'; document.querySelectorAll('.page').forEach(page => page.classList.toggle('active', page.id === `page-${next}`)); document.querySelectorAll('.nav-item').forEach(btn => btn.classList.toggle('active', btn.dataset.page === next)); document.getElementById('pageTitle').textContent = pageTitles[next]; if (location.hash !== `#${next}`) { history.replaceState(null, '', `#${next}`); } }
    async function loadMe() { const data = await api('/api/auth/me'); document.getElementById('userLabel').textContent = data.username; }
    async function loadConfig() { const data = await api('/api/config'); document.getElementById('downloadDir').value = data.download_dir || ''; }
    async function loadSources() { const data = await api('/api/sources'); sources = data.sources || []; defaultSourceId = data.default_source_id || ''; renderSourceOptions(); renderSources(); }
    function sourceIdFrom(value) { return String(value || '').trim().replace(/^@/, '').toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'source'; }
    function renderSourceOptions() { const select = document.getElementById('sourceSelect'); select.innerHTML = sources.filter(source => source.enabled !== false).map(source => `<option value="${escapeHtml(source.id)}">${escapeHtml(source.label)} (${escapeHtml(source.chat)})</option>`).join(''); select.value = defaultSourceId || (select.options[0] ? select.options[0].value : ''); }
    function addSourceRow(source = {}, isDefault = false) { const row = document.createElement('div'); row.className = 'source-row'; row.dataset.sourceId = source.id || ''; row.innerHTML = `<div><label>Label</label><input data-field="label" value="${escapeHtml(source.label || '')}"></div><div><label>tdl chat</label><input data-field="chat" value="${escapeHtml(source.chat || '')}" placeholder="youyou0_bot"></div><div><label>Forward source</label><input data-field="forward_source" value="${escapeHtml(source.forward_source || '')}" placeholder="@youyou0_bot"></div><label class="check-row"><input data-field="enabled" type="checkbox" ${source.enabled === false ? '' : 'checked'}> Enabled</label><label class="check-row"><input data-field="default" name="defaultSource" type="radio" ${isDefault ? 'checked' : ''}> Default</label><button class="secondary" type="button" data-action="remove">Remove</button>`; row.querySelector('[data-action="remove"]').addEventListener('click', () => { if (document.querySelectorAll('.source-row').length > 1) { row.remove(); } }); document.getElementById('sourceList').appendChild(row); }
    function renderSources() { const list = document.getElementById('sourceList'); list.innerHTML = ''; sources.forEach(source => addSourceRow(source, source.id === defaultSourceId)); if (!sources.length) { addSourceRow({}, true); } }
    function collectSources() { const rows = Array.from(document.querySelectorAll('.source-row')); const items = rows.map(row => { const chat = row.querySelector('[data-field="chat"]').value.trim(); const id = row.dataset.sourceId || sourceIdFrom(chat || row.querySelector('[data-field="forward_source"]').value); return {id, label:row.querySelector('[data-field="label"]').value.trim() || id, chat, forward_source:row.querySelector('[data-field="forward_source"]').value.trim(), enabled:row.querySelector('[data-field="enabled"]').checked}; }); const defaultRow = rows.find(row => row.querySelector('[data-field="default"]').checked) || rows[0]; const defaultIndex = rows.indexOf(defaultRow); return {sources:items, default_source_id:items[defaultIndex] ? items[defaultIndex].id : ''}; }
    async function saveSources() { const el = document.getElementById('sourcesMessage'); el.className = 'message'; try { const payload = collectSources(); const data = await api('/api/sources', {method:'PUT', body:JSON.stringify(payload)}); sources = data.sources || []; defaultSourceId = data.default_source_id || ''; renderSourceOptions(); renderSources(); el.textContent = 'Saved'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function saveConfig() { const el = document.getElementById('configMessage'); el.className = 'message'; try { await api('/api/config', {method:'PUT', body:JSON.stringify({download_dir:document.getElementById('downloadDir').value})}); el.textContent = 'Saved'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function changePassword() { const el = document.getElementById('passwordMessage'); el.className = 'message'; try { await api('/api/auth/password', {method:'POST', body:JSON.stringify({current_password:document.getElementById('currentPassword').value, new_password:document.getElementById('newPassword').value})}); location.href = '/login'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function logout() { await api('/api/auth/logout', {method:'POST', body:'{}'}); location.href = '/login'; }
    async function submitJobs() { const btn = document.getElementById('submitBtn'); btn.disabled = true; try { await api('/api/jobs', {method:'POST', body:JSON.stringify({message_ids:document.getElementById('messageIds').value, source_id:document.getElementById('sourceSelect').value})}); document.getElementById('messageIds').value = ''; await refreshJobs(); } catch (err) { alert(err.message); } finally { btn.disabled = false; } }
    async function retryJob(id) { await api(`/api/jobs/${id}/retry`, {method:'POST', body:'{}'}); await refreshJobs(); }
    async function cancelJob(id) { await api(`/api/jobs/${id}/cancel`, {method:'POST', body:'{}'}); await refreshJobs(); }
    async function deleteJob(id) { await api(`/api/jobs/${id}`, {method:'DELETE'}); if (selectedJob === id) { selectedJob = null; document.getElementById('logPanel').textContent = ''; } await refreshJobs(); }
    async function loadLog(id) { selectedJob = id; document.getElementById('logPanel').textContent = await api(`/api/jobs/${id}/log`) || ''; }
    async function loadForwarderLog() { selectedJob = null; document.getElementById('logPanel').textContent = await api('/api/forwarder/log') || ''; }
    async function openDirectory(path) { const msg = document.getElementById('dirMessage'); msg.className = 'message'; msg.textContent = ''; try { const data = await api(`/api/fs/dirs?path=${encodeURIComponent(path || '')}`); renderDirectory(data); } catch (err) { msg.className = 'message error'; msg.textContent = err.message; } }
    function renderDirectory(data) { currentDir = data.path || ''; currentDirParent = data.parent || ''; document.getElementById('dirDialogPath').value = currentDir; document.getElementById('dirParentBtn').disabled = !currentDirParent; const list = document.getElementById('dirList'); list.innerHTML = ''; if (!data.entries.length) { const empty = document.createElement('div'); empty.className = 'dir-row muted'; empty.textContent = 'No directories'; list.appendChild(empty); return; } data.entries.forEach(entry => { const row = document.createElement('button'); row.className = 'dir-row'; row.type = 'button'; const name = document.createElement('span'); name.className = 'dir-name'; name.textContent = entry.name; const flag = document.createElement('span'); flag.className = 'dir-writable'; flag.textContent = entry.writable ? 'Writable' : 'Read only'; row.append(name, flag); row.addEventListener('click', () => openDirectory(entry.path)); list.appendChild(row); }); }
    async function openDirectoryDialog() { document.getElementById('dirDialog').classList.remove('hidden'); await openDirectory(document.getElementById('downloadDir').value); document.getElementById('dirDialogPath').focus(); }
    function closeDirectoryDialog() { document.getElementById('dirDialog').classList.add('hidden'); }
    function selectCurrentDirectory() { if (currentDir) { document.getElementById('downloadDir').value = currentDir; } closeDirectoryDialog(); }
    function renderSummary(jobs) { const counts = jobs.reduce((acc, job) => { acc[job.status] = (acc[job.status] || 0) + 1; return acc; }, {}); const active = jobs.find(job => ['exporting','downloading','renaming'].includes(job.status)); const items = [['Active', active ? `#${active.id}` : '0'], ['Queued', counts.queued || 0], ['Complete', (counts.done || 0) + (counts.skipped || 0)], ['Failed', counts.failed || 0], ['Canceled', counts.canceled || 0]]; document.getElementById('summary').innerHTML = items.map(([label, value]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${label}</span></div>`).join(''); }
    function renderForwarder(status) { const sourceText = status.source_count ? `${status.source_count} enabled` : (status.source || ''); const items = [['State', `<span class="status ${escapeHtml(status.state || 'unknown')}">${statusLabel(status.state || 'unknown')}</span>`], ['Sources', escapeHtml(sourceText)], ['Last source', escapeHtml(status.last_source || '')], ['Sent', escapeHtml(status.sent_count || 0)], ['Error', escapeHtml(status.last_error || '')]]; document.getElementById('forwarderStatus').innerHTML = items.map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`).join('') + '<button class="secondary" onclick="loadForwarderLog()">Log</button>'; }
    function renderJobs(jobs) { const body = document.getElementById('jobsBody'); if (!jobs.length) { body.innerHTML = '<tr><td colspan="11" class="muted">No jobs</td></tr>'; return; } body.innerHTML = jobs.map(job => { const pct = Math.max(0, Math.min(100, Number(job.progress || 0))); const active = ['queued','exporting','downloading','renaming'].includes(job.status); const retry = ['failed','canceled'].includes(job.status) ? `<button class="secondary" onclick="retryJob(${job.id})">Retry</button>` : ''; const cancel = active ? `<button class="secondary" onclick="cancelJob(${job.id})">Cancel</button>` : ''; const remove = !['exporting','downloading','renaming'].includes(job.status) ? `<button class="danger" onclick="deleteJob(${job.id})">Delete</button>` : ''; return `<tr><td class="mono">#${job.id}</td><td>${escapeHtml(job.source_label || job.source_chat || '')}</td><td class="mono">${job.message_id}</td><td><span class="status ${job.status}">${statusLabel(job.status)}</span></td><td class="title-cell">${escapeHtml(job.title || job.error || '')}</td><td><div class="bar"><i style="width:${pct}%"></i></div><span class="muted">${pct.toFixed(1)}%</span></td><td>${escapeHtml(job.speed || '')}</td><td class="mono">${job.process_pid || ''}</td><td class="path-cell">${escapeHtml(job.download_dir || '')}</td><td class="title-cell">${escapeHtml(job.final_path || job.source_file || '')}</td><td class="actions"><button class="secondary" onclick="loadLog(${job.id})">Log</button>${cancel}${retry}${remove}</td></tr>`; }).join(''); }
    async function refreshJobs() { const data = await api('/api/jobs'); renderSummary(data.jobs); renderJobs(data.jobs); if (selectedJob) { document.getElementById('logPanel').textContent = await api(`/api/jobs/${selectedJob}/log`) || ''; } }
    async function refreshForwarder() { renderForwarder(await api('/api/forwarder/status')); }
    async function refreshAll() { await Promise.all([refreshJobs(), refreshForwarder()]); }
    document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => showPage(btn.dataset.page)));
    document.getElementById('submitBtn').addEventListener('click', submitJobs);
    document.getElementById('addSourceBtn').addEventListener('click', () => addSourceRow({}, false));
    document.getElementById('saveSourcesBtn').addEventListener('click', saveSources);
    document.getElementById('saveConfigBtn').addEventListener('click', saveConfig);
    document.getElementById('browseDirBtn').addEventListener('click', openDirectoryDialog);
    document.getElementById('closeDirBtn').addEventListener('click', closeDirectoryDialog);
    document.getElementById('dirParentBtn').addEventListener('click', () => openDirectory(currentDirParent));
    document.getElementById('dirRootBtn').addEventListener('click', () => openDirectory('/'));
    document.getElementById('goDirBtn').addEventListener('click', () => openDirectory(document.getElementById('dirDialogPath').value));
    document.getElementById('selectDirBtn').addEventListener('click', selectCurrentDirectory);
    document.getElementById('changePasswordBtn').addEventListener('click', changePassword);
    document.getElementById('logoutBtn').addEventListener('click', logout);
    document.getElementById('dirDialog').addEventListener('click', event => { if (event.target.id === 'dirDialog') { closeDirectoryDialog(); } });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') { closeDirectoryDialog(); } });
    setInterval(() => { document.getElementById('clock').textContent = new Date().toLocaleString(); }, 1000);
    showPage((location.hash || '#downloads').slice(1));
    loadMe(); loadConfig(); loadSources(); refreshAll(); setInterval(refreshAll, 2500);
  </script>
</body>
</html>
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login - Telegram Download Manager</title>
  <style>
    :root { font-family: Arial, sans-serif; color:#18201b; background:#f6f7f4; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; display:grid; place-items:center; }
    form { width:min(360px, calc(100vw - 32px)); background:#fff; border:1px solid #d9ded4; border-radius:8px; padding:22px; }
    h1 { margin:0 0 18px; font-size:20px; }
    label { display:block; margin:12px 0 6px; font-size:13px; color:#657064; }
    input { width:100%; height:42px; border:1px solid #d9ded4; border-radius:6px; padding:0 10px; font-size:15px; }
    button { width:100%; height:42px; margin-top:18px; border:0; border-radius:6px; background:#28735f; color:#fff; font-weight:700; cursor:pointer; }
    .error { min-height:20px; margin-top:12px; color:#b33b32; font-size:13px; }
  </style>
</head>
<body>
  <form id="loginForm">
    <h1>Telegram Download Manager</h1>
    <label for="username">Admin</label>
    <input id="username" autocomplete="username" value="admin">
    <label for="password">Password</label>
    <input id="password" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Login</button>
    <div class="error" id="error"></div>
  </form>
  <script>
    document.getElementById('loginForm').addEventListener('submit', async event => {
      event.preventDefault();
      const error = document.getElementById('error');
      error.textContent = '';
      const res = await fetch('/api/auth/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username:document.getElementById('username').value, password:document.getElementById('password').value }) });
      if (res.ok) { location.href = '/'; } else { error.textContent = await res.text() || 'Login failed'; }
    });
  </script>
</body>
</html>
"""

def read_forwarder_status(
    path: Path | None = None,
    now_epoch: float | None = None,
    stale_seconds: int = 90,
) -> dict[str, Any]:
    status_path = path or (STATE_DIR / "forwarder_status.json")
    if not status_path.exists():
        return {"state": "missing"}
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8") or "{}")
        updated = float(payload.get("updated_at_epoch") or 0)
        now = time.time() if now_epoch is None else now_epoch
        if payload.get("state") == "running" and now - updated > stale_seconds:
            payload["state"] = "stale"
        return payload
    except Exception as exc:  # noqa: BLE001 - status endpoint boundary
        return {"state": "unknown", "last_error": str(exc)}


def tail_text_file(path: Path, limit: int = 200) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()[-256 * 1024 :]
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-limit:])


def list_directory_choices(path_value: str | None, default_path: Path) -> dict[str, Any]:
    raw_path = str(path_value or default_path)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError("path must be an absolute path")
    current = path.resolve()
    if not current.exists():
        raise ValueError("path does not exist")
    if not current.is_dir():
        raise ValueError("path is not a directory")

    entries: list[dict[str, Any]] = []
    try:
        children = list(current.iterdir())
    except OSError as exc:
        raise ValueError(f"cannot read directory: {exc}") from exc

    for child in children:
        try:
            if child.is_dir():
                resolved = child.resolve()
                entries.append(
                    {
                        "name": child.name,
                        "path": str(resolved),
                        "writable": os.access(resolved, os.W_OK),
                    }
                )
        except OSError:
            continue

    entries.sort(key=lambda item: str(item["name"]).lower())
    parent = current.parent
    return {
        "path": str(current),
        "parent": "" if parent == current else str(parent),
        "writable": os.access(current, os.W_OK),
        "entries": entries,
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "TGDownloaderUI/1.0"

    @property
    def store(self) -> JobStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def config_store(self) -> ConfigStore:
        return self.server.config_store  # type: ignore[attr-defined]

    @property
    def auth(self) -> AuthManager:
        return self.server.auth_manager  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            if self.authorized():
                return self.redirect("/")
            return self.send_html(LOGIN_HTML)
        if not self.authorized():
            return self.require_auth()
        if parsed.path == "/":
            return self.send_html(INDEX_HTML)
        if parsed.path == "/api/auth/me":
            return self.send_json({"username": self.config_store.get_username()})
        if parsed.path == "/api/config":
            return self.send_json({"download_dir": str(self.config_store.get_download_dir())})
        if parsed.path == "/api/sources":
            return self.send_json(
                {
                    "sources": self.config_store.list_sources(),
                    "default_source_id": self.config_store.get_default_source_id(),
                }
            )
        if parsed.path == "/api/fs/dirs":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                path_value = query.get("path", [""])[0]
                return self.send_json(
                    list_directory_choices(path_value, self.config_store.get_download_dir())
                )
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/jobs":
            return self.send_json({"jobs": self.store.list_jobs()})
        if parsed.path == "/api/forwarder/status":
            return self.send_json(read_forwarder_status())
        if parsed.path == "/api/forwarder/log":
            return self.send_text(tail_text_file(STATE_DIR / "forwarder.log"))

        match = re.fullmatch(r"/api/jobs/(\d+)/log", parsed.path)
        if match:
            try:
                return self.send_text(self.store.tail_log(int(match.group(1))))
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.NOT_FOUND, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/auth/login":
            try:
                payload = self.read_json()
                username = str(payload.get("username") or "")
                password = str(payload.get("password") or "")
                if not self.auth.verify_password(username, password):
                    return self.send_error_text(HTTPStatus.UNAUTHORIZED, "invalid username or password")
                token = self.auth.create_session(username)
                return self.send_json(
                    {"username": username},
                    headers={"Set-Cookie": self.build_session_cookie(token)},
                )
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if not self.authorized():
            return self.require_auth()

        if parsed.path == "/api/auth/logout":
            self.auth.logout(self.get_session_token())
            return self.send_json(
                {"ok": True},
                headers={"Set-Cookie": self.build_session_cookie("", max_age=0)},
            )

        if parsed.path == "/api/auth/password":
            try:
                payload = self.read_json()
                self.auth.change_password(
                    self.config_store.get_username(),
                    str(payload.get("current_password") or ""),
                    str(payload.get("new_password") or ""),
                )
                return self.send_json(
                    {"ok": True},
                    headers={"Set-Cookie": self.build_session_cookie("", max_age=0)},
                )
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/jobs":
            try:
                payload = self.read_json()
                message_ids = parse_message_ids(payload.get("message_ids"))
                source_id = str(payload.get("source_id") or "")
                jobs = [
                    self.store.create_job(message_id, source_id=source_id)
                    for message_id in message_ids
                ]
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

        match = re.fullmatch(r"/api/jobs/(\d+)/cancel", parsed.path)
        if match:
            try:
                job = self.store.cancel_job(int(match.group(1)))
                return self.send_json({"job": job})
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        if not self.authorized():
            return self.require_auth()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            try:
                payload = self.read_json()
                download_dir = self.config_store.set_download_dir(
                    str(payload.get("download_dir") or "")
                )
                return self.send_json({"download_dir": str(download_dir)})
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))
        if parsed.path == "/api/sources":
            try:
                payload = self.read_json()
                sources, default_source_id = self.config_store.set_sources(
                    payload.get("sources"),
                    str(payload.get("default_source_id") or ""),
                )
                return self.send_json(
                    {"sources": sources, "default_source_id": default_source_id}
                )
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))
        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        if not self.authorized():
            return self.require_auth()
        parsed = urllib.parse.urlparse(self.path)
        match = re.fullmatch(r"/api/jobs/(\d+)", parsed.path)
        if match:
            try:
                self.store.delete_job(int(match.group(1)))
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))
        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def authorized(self) -> bool:
        return self.auth.get_session(self.get_session_token()) is not None

    def get_session_token(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == SESSION_COOKIE:
                return value
        return ""

    def build_session_cookie(self, token: str, max_age: int | None = None) -> str:
        age = SESSION_MAX_AGE_SECONDS if max_age is None else max_age
        return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}"

    def require_auth(self) -> None:
        if not urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return self.redirect("/login")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"authentication required\n")

    def redirect(self, target: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", target)
        self.end_headers()

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

    def send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_text(self, status: HTTPStatus, message: str) -> None:
        self.send_text(message + "\n", status=status)


class DownloadServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        store: JobStore,
        config_store: ConfigStore,
        auth_manager: AuthManager,
    ):
        super().__init__(address, handler)
        self.store = store
        self.config_store = config_store
        self.auth_manager = auth_manager


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    config_store = ConfigStore(STATE_DIR)
    config_store.init()
    store = JobStore(STATE_DIR, config_store)
    store.init()
    auth_manager = AuthManager(config_store)
    stop_event = threading.Event()
    worker = DownloadWorker(store, stop_event)
    worker.start()

    httpd = DownloadServer((host, port), RequestHandler, store, config_store, auth_manager)

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
        config_store = ConfigStore(STATE_DIR)
        config_store.init()
        store = JobStore(STATE_DIR, config_store)
        store.init()
        print("ok")
        return 0
    run_server(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
