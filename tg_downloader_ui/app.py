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
import io
import json
import os
import queue
import re
import secrets
import shlex
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
DEFAULT_HOST = os.environ.get("TGDL_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("TGDL_PORT", "9910"))
STATE_DIR = Path(
    os.environ.get("TGDL_STATE_DIR", str(Path.home() / ".local/state/tg-downloader-ui"))
)
DOWNLOAD_DIR = Path(
    os.environ.get("TGDL_DOWNLOAD_DIR", str(Path.home() / "Downloads/telegram"))
)
TDL_BIN = os.environ.get("TGDL_TDL_BIN", "tdl")
GLOBAL_PROXY = os.environ.get("TGDL_PROXY", "")
TDL_PROXY = os.environ.get("TGDL_TDL_PROXY", GLOBAL_PROXY)
TDL_STORAGE = os.environ.get(
    "TGDL_TDL_STORAGE",
    f"type=bolt,path={Path(os.environ.get('TGDL_TDL_DIR', str(STATE_DIR / 'tdl')))}",
)
TDL_CHAT = os.environ.get("TGDL_CHAT", "")
AUTH_USER = os.environ.get("TGDL_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("TGDL_AUTH_PASSWORD", "")
TDL_LOG_PATH = Path(os.environ.get("TGDL_TDL_LOG", str(STATE_DIR / "tdl.log")))
TELEGRAM_AUTH_STATE_PATH = STATE_DIR / "telegram_auth.json"
SESSION_COOKIE = "tgdl_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("TGDL_SESSION_MAX_AGE", str(7 * 24 * 60 * 60)))
PASSWORD_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_BLOCK_SECONDS = 15 * 60
MAX_JSON_BODY_BYTES = 1024 * 1024
COOKIE_SECURE = os.environ.get("TGDL_COOKIE_SECURE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ACTIVE_STATUSES = {"exporting", "downloading", "renaming"}
CANCEL_EXIT_CODE = 130
QR_LOGIN_TTL_SECONDS = 120
TDL_LOGIN_OUTPUT_LIMIT = 40000
FORWARDER_DISABLED_HINT = (
    "forwarder 未启用，请设置 TGDL_FORWARDER_ENABLED=1 并重启部署。"
)
FORWARDER_RESTART_HINT = (
    "forwarder 重启命令未配置，请设置 TGDL_FORWARDER_RESTART_CMD。"
)
FORWARDER_CONFIGURATION_HINT = (
    "尚未配置 Telegram API，请前往“Telegram 授权”填写 API ID 和 API Hash。"
)
FORWARDER_CONFIGURATION_ERRORS = {
    "TGDL_API_ID is required",
    "TGDL_API_HASH is required",
}

TELEGRAM_QR_LOGINS: dict[str, dict[str, Any]] = {}
TELEGRAM_QR_LOCK = threading.RLock()
TDL_LOGIN_LOCK = threading.RLock()
TDL_LOGIN_ENTRY: dict[str, Any] | None = None


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


@dataclasses.dataclass(frozen=True)
class MediaPlan:
    media_type: str
    title: str
    year: str
    season: int | None
    episode: int | None
    final_filename: str
    final_path: Path


def utcish_now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def redact_command_args(args: list[str]) -> list[str]:
    redacted = list(args)
    for index, value in enumerate(redacted):
        if index and redacted[index - 1] == "--proxy":
            redacted[index] = "<redacted>"
        elif value.startswith("--proxy="):
            redacted[index] = "--proxy=<redacted>"
    return redacted


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


def extract_labeled_value(text: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.*?)\s*$")
    for raw_line in text.splitlines():
        match = pattern.match(raw_line.strip())
        if match:
            return match.group(1).strip()
    return ""


def extract_year(text: str) -> str:
    match = re.search(r"(?:19|20)\d{2}", text)
    return match.group(0) if match else ""


def clean_media_title(value: str, fallback: str = "download") -> str:
    title = sanitize_filename(value, fallback=fallback)
    title = re.sub(r"\s+", " ", title).strip()
    noisy_suffixes = (
        "国语",
        "英语",
        "粤语",
        "中字",
        "双语",
        "正片",
        "高清",
        "完整版",
        "无水印",
        "1080p",
        "2160p",
        "4k",
        "hd",
        "web-dl",
        "webrip",
        "bluray",
        "h264",
        "h265",
        "x264",
        "x265",
    )
    suffix_pattern = "|".join(re.escape(item) for item in noisy_suffixes)
    while True:
        cleaned = re.sub(
            rf"(?:[\s._-]+|\b)(?:{suffix_pattern})$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip(" ._-")
        if cleaned == title:
            break
        title = cleaned
    return title or fallback


def parse_small_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return digits.get(text)
    return None


def extract_episode_info(metadata: ExportMetadata) -> tuple[int | None, int | None]:
    number = r"([0-9]{1,3}|[零〇一二两三四五六七八九十]{1,4})"
    haystacks = [metadata.source_file, metadata.title, metadata.text]

    for value in haystacks:
        match = re.search(r"(?i)\bS(\d{1,2})[\s._-]*E(\d{1,3})\b", value)
        if match:
            return int(match.group(1)), int(match.group(2))

    for value in haystacks:
        match = re.search(rf"第\s*{number}\s*季.*?第\s*{number}\s*[集话話]", value)
        if match:
            season = parse_small_number(match.group(1))
            episode = parse_small_number(match.group(2))
            if season and episode:
                return season, episode

    for value in haystacks:
        match = re.search(rf"第\s*{number}\s*[集话話]", value)
        if match:
            episode = parse_small_number(match.group(1))
            if episode:
                return 1, episode

    for value in haystacks:
        match = re.search(r"(?i)(?:^|[\s._-])EP?(\d{1,3})(?:$|[\s._-])", value)
        if match:
            return 1, int(match.group(1))

    return None, None


def build_media_plan(metadata: ExportMetadata, download_dir: Path) -> MediaPlan:
    extension = metadata.extension if metadata.extension.startswith(".") else ""
    structured_title = extract_labeled_value(metadata.text, ["片名", "剧名", "名称", "标题"])
    year = extract_year(
        extract_labeled_value(metadata.text, ["首映", "上映", "年份", "发行"])
        or metadata.text
        or metadata.source_file
    )
    season, episode = extract_episode_info(metadata)

    if season and episode:
        title = clean_media_title(structured_title or metadata.title, fallback=f"message_{metadata.message_id}")
        show_dir = f"{title} ({year})" if year else title
        final_filename = f"{title} - S{season:02d}E{episode:02d}{extension}"
        final_path = download_dir / "TV" / show_dir / f"Season {season:02d}" / final_filename
        return MediaPlan("tv", title, year, season, episode, final_filename, final_path)

    if structured_title:
        title = clean_media_title(structured_title, fallback=f"message_{metadata.message_id}")
        stem = f"{title} ({year})" if year else title
        final_filename = f"{stem}{extension}"
        final_path = download_dir / "Movies" / stem / final_filename
        return MediaPlan("movie", title, year, None, None, final_filename, final_path)

    final_filename = build_final_filename(metadata)
    final_path = download_dir / final_filename
    return MediaPlan("file", sanitize_filename(metadata.title), year, None, None, final_filename, final_path)


def sidecar_stem(final_path: Path) -> Path:
    return final_path.with_suffix("")


def write_sidecar_metadata(
    plan: MediaPlan,
    metadata: ExportMetadata,
    source_label: str = "",
) -> list[Path]:
    stem = sidecar_stem(plan.final_path)
    json_path = stem.with_name(stem.name + ".telegram.json")
    text_path = stem.with_name(stem.name + ".telegram.txt")
    json_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "dialog_id": metadata.dialog_id,
        "message_id": metadata.message_id,
        "source_file": metadata.source_file,
        "source_label": source_label,
        "telegram_text": metadata.text,
        "media": {
            "type": plan.media_type,
            "title": plan.title,
            "year": plan.year,
            "season": plan.season,
            "episode": plan.episode,
            "final_filename": plan.final_filename,
            "final_path": str(plan.final_path),
        },
        "written_at": utcish_now(),
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    lines = [
        f"source: {source_label}",
        f"dialog_id: {metadata.dialog_id}",
        f"message_id: {metadata.message_id}",
        f"source_file: {metadata.source_file}",
        f"media_type: {plan.media_type}",
        f"title: {plan.title}",
    ]
    if plan.year:
        lines.append(f"year: {plan.year}")
    if plan.season is not None and plan.episode is not None:
        lines.append(f"season: {plan.season:02d}")
        lines.append(f"episode: {plan.episode:02d}")
    lines.extend(["", "--- telegram text ---", metadata.text])
    text_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return [json_path, text_path]


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


def build_tdl_base_args(
    tdl_bin: str | None = None,
    storage: str | None = None,
    global_proxy: str | None = None,
    tdl_proxy: str | None = None,
) -> list[str]:
    selected_bin = tdl_bin or TDL_BIN
    selected_storage = storage or TDL_STORAGE
    selected_global_proxy = GLOBAL_PROXY if global_proxy is None else global_proxy
    if tdl_proxy is None:
        selected_proxy = os.environ.get("TGDL_TDL_PROXY", selected_global_proxy)
    else:
        selected_proxy = tdl_proxy

    args = [selected_bin, "--storage", selected_storage]
    if selected_proxy:
        args.extend(["--proxy", selected_proxy])
    return args


def stop_download_process(proc: Any, timeout: float = 5.0) -> None:
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()


def append_tdl_login_output(entry: dict[str, Any], text: str) -> None:
    if not text:
        return
    with TDL_LOGIN_LOCK:
        current = str(entry.get("output") or "") + strip_ansi(text)
        entry["output"] = current[-TDL_LOGIN_OUTPUT_LIMIT:]
        entry["updated_at"] = utcish_now()


def tdl_qr_login_public_state(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    with TDL_LOGIN_LOCK:
        current = TDL_LOGIN_ENTRY if entry is None else entry
        if not current:
            return {"state": "idle", "mode": "", "output": "", "returncode": None}
        proc = current.get("process")
        if current.get("state") == "running" and proc is not None:
            returncode = proc.poll()
            if returncode is not None:
                current["returncode"] = returncode
                current["state"] = "done" if returncode == 0 else "failed"
        return {
            "state": current.get("state", "idle"),
            "mode": current.get("mode", ""),
            "output": current.get("output", ""),
            "returncode": current.get("returncode"),
            "started_at": current.get("started_at", ""),
            "updated_at": current.get("updated_at", ""),
            "error": current.get("error", ""),
        }


def tdl_login_status() -> dict[str, Any]:
    return tdl_qr_login_public_state()


def tdl_qr_login_status() -> dict[str, Any]:
    return tdl_login_status()


def read_tdl_login_output(entry: dict[str, Any]) -> None:
    proc = entry["process"]
    try:
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            for line in stream:
                append_tdl_login_output(entry, line)
        returncode = proc.wait()
        with TDL_LOGIN_LOCK:
            if entry.get("state") == "running":
                entry["returncode"] = returncode
                entry["state"] = "done" if returncode == 0 else "failed"
                entry["updated_at"] = utcish_now()
    except Exception as exc:  # noqa: BLE001 - process reader boundary
        with TDL_LOGIN_LOCK:
            entry.update(
                {
                    "state": "failed",
                    "error": str(exc),
                    "returncode": entry.get("returncode"),
                    "updated_at": utcish_now(),
                }
            )


def start_tdl_login(
    mode: str = "qr",
    popen_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    global TDL_LOGIN_ENTRY
    selected_mode = str(mode or "").strip().lower()
    if selected_mode not in {"qr", "code"}:
        raise RuntimeError("unsupported tdl login mode")

    with TDL_LOGIN_LOCK:
        if TDL_LOGIN_ENTRY and TDL_LOGIN_ENTRY.get("state") == "running":
            proc = TDL_LOGIN_ENTRY.get("process")
            if proc is not None and proc.poll() is None:
                return tdl_qr_login_public_state(TDL_LOGIN_ENTRY)

        entry: dict[str, Any] = {
            "state": "starting",
            "mode": selected_mode,
            "output": "",
            "returncode": None,
            "started_at": utcish_now(),
            "updated_at": utcish_now(),
            "error": "",
        }
        TDL_LOGIN_ENTRY = entry

    args = build_tdl_base_args() + ["login", "-T", selected_mode]
    try:
        proc = popen_factory(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        with TDL_LOGIN_LOCK:
            entry.update({"state": "failed", "error": str(exc), "updated_at": utcish_now()})
        raise RuntimeError(f"tdl login failed to start: {exc}") from exc

    with TDL_LOGIN_LOCK:
        entry["process"] = proc
        entry["state"] = "running"
        entry["updated_at"] = utcish_now()
        state = {
            "state": "running",
            "mode": selected_mode,
            "output": "",
            "returncode": None,
            "started_at": entry["started_at"],
            "updated_at": entry["updated_at"],
            "error": "",
        }
    threading.Thread(target=read_tdl_login_output, args=(entry,), daemon=True).start()
    return state


def start_tdl_qr_login(popen_factory: Any = subprocess.Popen) -> dict[str, Any]:
    return start_tdl_login("qr", popen_factory=popen_factory)


def start_tdl_code_login(popen_factory: Any = subprocess.Popen) -> dict[str, Any]:
    return start_tdl_login("code", popen_factory=popen_factory)


def send_tdl_login_input(text: str) -> dict[str, Any]:
    value = str(text or "")
    if not value.strip():
        raise RuntimeError("tdl login input is required")
    with TDL_LOGIN_LOCK:
        current = TDL_LOGIN_ENTRY
        if not current or current.get("state") != "running":
            raise RuntimeError("tdl login is not running")
        proc = current.get("process")
        if proc is None or proc.poll() is not None or getattr(proc, "stdin", None) is None:
            raise RuntimeError("tdl login is not running")
        proc.stdin.write(value.rstrip("\r\n") + "\n")
        proc.stdin.flush()
        current["updated_at"] = utcish_now()
        return tdl_qr_login_public_state(current)


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


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"password must contain at least {MIN_PASSWORD_LENGTH} characters"
        )


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
        self.default_user = AUTH_USER if default_user is None else default_user
        self.default_password = AUTH_PASSWORD if default_password is None else default_password
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {}

    def init(self) -> None:
        ensure_private_dir(self.state_dir)
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
            if (
                (not auth.get("password_hash") or not auth.get("password_salt"))
                and self.default_password
            ):
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
                if auth and not auth.get("username"):
                    auth["username"] = self.default_user
                    changed = True
                if auth and not auth.get("session_version"):
                    auth["session_version"] = 1
                    changed = True

            setup_completed = bool(
                self.data.get("download_dir")
                and auth.get("password_hash")
                and auth.get("password_salt")
            )
            if bool(self.data.get("setup_completed")) != setup_completed:
                self.data["setup_completed"] = setup_completed
                changed = True

            if changed:
                self.save()

    def save(self) -> None:
        ensure_private_dir(self.state_dir)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        ensure_private_file(tmp_path)
        tmp_path.replace(self.path)
        ensure_private_file(self.path)

    def get_download_dir(self) -> Path:
        with self.lock:
            return Path(self.data.get("download_dir") or self.default_download_dir)

    def requires_setup(self) -> bool:
        with self.lock:
            auth = self.data.get("auth") or {}
            return not (
                self.data.get("download_dir")
                and auth.get("password_hash")
                and auth.get("password_salt")
            )

    def initialize(
        self,
        username: str,
        password: str,
        download_dir: str | Path,
        telegram: dict[str, Any] | None = None,
    ) -> None:
        username = str(username or "").strip()
        if not username:
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        self.set_download_dir(download_dir)
        self.set_password(username, password)
        with self.lock:
            if telegram is not None:
                self.data["telegram"] = {
                    "api_id": str(telegram.get("api_id") or "").strip(),
                    "api_hash": str(telegram.get("api_hash") or "").strip(),
                    "session_file": str(telegram.get("session_file") or "").strip(),
                    "proxy": str(telegram.get("proxy") or "").strip(),
                    "forward_channel_id": str(
                        telegram.get("forward_channel_id") or ""
                    ).strip(),
                }
            self.data["setup_completed"] = True
            self.save()

    def set_telegram_config(
        self, telegram: dict[str, Any], preserve_secret: bool = False
    ) -> dict[str, str]:
        with self.lock:
            existing = self.data.get("telegram") or {}
            api_hash = str(telegram.get("api_hash") or "").strip()
            if preserve_secret and not api_hash:
                api_hash = str(existing.get("api_hash") or "")
            self.data["telegram"] = {
                "api_id": str(telegram.get("api_id") or "").strip(),
                "api_hash": api_hash,
                "session_file": str(telegram.get("session_file") or "").strip(),
                "proxy": str(telegram.get("proxy") or "").strip(),
                "forward_channel_id": str(
                    telegram.get("forward_channel_id") or ""
                ).strip(),
            }
            self.save()
            return self.get_telegram_config()

    def get_telegram_config(self) -> dict[str, str]:
        with self.lock:
            data = self.data.get("telegram") or {}
            return {
                "api_id": str(os.environ.get("TGDL_API_ID") or data.get("api_id") or ""),
                "api_hash": str(
                    os.environ.get("TGDL_API_HASH") or data.get("api_hash") or ""
                ),
                "session_file": str(
                    os.environ.get("TGDL_SESSION_FILE") or data.get("session_file") or ""
                ),
                "proxy": str(
                    os.environ.get("TGDL_TELEGRAM_PROXY")
                    or os.environ.get("TGDL_PROXY")
                    or data.get("proxy")
                    or ""
                ),
                "forward_channel_id": str(
                    os.environ.get("TGDL_FORWARD_CHANNEL_ID")
                    or data.get("forward_channel_id")
                    or ""
                ),
            }

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
        text = str(value or "").strip()
        path = Path(text) if text else self.default_download_dir
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
        validate_new_password(new_password)
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
        self.login_failures: dict[str, list[float]] = {}
        self.login_blocked_until: dict[str, float] = {}
        self.lock = threading.RLock()

    def verify_password(self, username: str, password: str) -> bool:
        return self.config_store.verify_password(username, password)

    def record_login_failure(self, key: str, now: float | None = None) -> None:
        current = time.time() if now is None else now
        cutoff = current - LOGIN_FAILURE_WINDOW_SECONDS
        with self.lock:
            failures = [
                value for value in self.login_failures.get(key, []) if value >= cutoff
            ]
            failures.append(current)
            self.login_failures[key] = failures
            if len(failures) >= LOGIN_FAILURE_LIMIT:
                self.login_blocked_until[key] = current + LOGIN_BLOCK_SECONDS

    def login_retry_after(self, key: str, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self.lock:
            blocked_until = self.login_blocked_until.get(key, 0)
            if blocked_until <= current:
                self.login_blocked_until.pop(key, None)
                return 0
            return max(1, int(blocked_until - current + 0.999))

    def clear_login_failures(self, key: str) -> None:
        with self.lock:
            self.login_failures.pop(key, None)
            self.login_blocked_until.pop(key, None)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.session_max_age_seconds
        with self.lock:
            self.sessions[token] = {
                "username": username,
                "expires_at": expires_at,
                "session_version": self.config_store.get_session_version(),
                "csrf_token": secrets.token_urlsafe(24),
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
        ensure_private_dir(self.state_dir)
        ensure_private_dir(self.logs_dir)
        ensure_private_dir(self.exports_dir)
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
                    resume_requested INTEGER NOT NULL DEFAULT 0,
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
            self.ensure_column(db, "resume_requested", "resume_requested INTEGER NOT NULL DEFAULT 0")
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
        ensure_private_file(self.db_path)
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
                    attempts = attempts + 1, error = '',
                    progress = CASE WHEN resume_requested = 1 THEN progress ELSE 0 END,
                    downloaded = CASE WHEN resume_requested = 1 THEN downloaded ELSE '' END,
                    speed = '', eta = '',
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
            resume_requested=0,
            started_at="",
            finished_at="",
        )
        self.append_log(job_id, "\nRetry queued\n")
        return self.get_job(job_id) or {}

    def resume_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if job["status"] not in {"failed", "canceled"}:
            raise ValueError("only failed or canceled jobs can be resumed")
        export_path = Path(str(job.get("export_path") or ""))
        can_continue = bool(
            export_path.exists()
            and (str(job.get("source_file") or "") or str(job.get("final_path") or ""))
        )
        fields: dict[str, Any] = {
            "status": "queued",
            "speed": "",
            "eta": "",
            "error": "",
            "process_pid": 0,
            "cancel_requested": 0,
            "resume_requested": 1 if can_continue else 0,
            "started_at": "",
            "finished_at": "",
        }
        if not can_continue:
            fields["progress"] = 0
            fields["downloaded"] = ""
        self.update_job(job_id, **fields)
        self.append_log(job_id, "\nResume queued\n" if can_continue else "\nRetry queued\n")
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

    def pause_job(self, job_id: int) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        status = str(job["status"])
        if status == "queued":
            self.finish_job(
                job_id,
                "canceled",
                error="paused by user",
                cancel_requested=1,
            )
            self.append_log(job_id, "\nPaused while queued\n")
            return self.get_job(job_id) or {}
        if status in ACTIVE_STATUSES:
            self.update_job(job_id, cancel_requested=1, error="pause requested")
            self.append_log(job_id, "\nPause requested\n")
            return self.get_job(job_id) or {}
        raise ValueError("only queued or active jobs can be paused")

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
        ensure_private_dir(path.parent)
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
        ensure_private_file(path)

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
        resume_requested = bool(int(job.get("resume_requested") or 0) and export_path.exists())
        if not resume_requested:
            log_path.write_text("", encoding="utf-8")
            ensure_private_file(log_path)

        action = "Resume" if resume_requested else "Start"
        self.store.append_log(job_id, f"{action} message {message_id} from {source_label}\n")
        if not resume_requested:
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
        media_plan = build_media_plan(metadata, download_dir)
        final_filename = media_plan.final_filename
        final_path = media_plan.final_path
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
            write_sidecar_metadata(media_plan, metadata, source_label)
            self.cleanup_partial_files(job_id, metadata, default_path)
            self.store.finish_job(
                job_id,
                "skipped",
                progress=100,
                downloaded=self.format_size(final_path.stat().st_size),
                resume_requested=0,
            )
            return

        if default_path.exists():
            rename_path = final_path
            if rename_path.exists():
                rename_path = self.unique_final_path(rename_path, message_id)
                final_filename = rename_path.name
            self.store.append_log(job_id, f"Rename existing file: {default_path} -> {rename_path}\n")
            rename_path.parent.mkdir(parents=True, exist_ok=True)
            default_path.rename(rename_path)
            media_plan = dataclasses.replace(
                media_plan,
                final_filename=rename_path.name,
                final_path=rename_path,
            )
            write_sidecar_metadata(media_plan, metadata, source_label)
            self.store.finish_job(
                job_id,
                "done",
                progress=100,
                downloaded=self.format_size(rename_path.stat().st_size),
                final_filename=rename_path.name,
                final_path=str(rename_path),
                resume_requested=0,
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
        ]
        if resume_requested:
            download_cmd += [
                "--continue",
                "-d",
                str(download_dir),
                "-f",
                str(export_path),
                "--skip-same",
            ]
        else:
            download_cmd += [
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
                final_path = self.unique_final_path(final_path, message_id)
                final_filename = final_path.name
            self.store.append_log(job_id, f"Rename: {downloaded_path} -> {final_path}\n")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded_path.rename(final_path)

        media_plan = dataclasses.replace(
            media_plan,
            final_filename=final_path.name,
            final_path=final_path,
        )
        write_sidecar_metadata(media_plan, metadata, source_label)
        self.store.finish_job(
            job_id,
            "done",
            progress=100,
            downloaded=self.format_size(final_path.stat().st_size),
            final_filename=final_path.name,
            final_path=str(final_path),
            eta="",
            resume_requested=0,
        )

    def check_canceled(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if job and int(job.get("cancel_requested") or 0):
            raise JobCanceled("canceled by user")

    def run_command(self, job_id: int, cmd: list[str], status: str) -> int:
        self.store.append_log(job_id, "$ " + " ".join(redact_command_args(cmd)) + "\n")
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
                    stop_download_process(proc)
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
                            stop_download_process(proc)
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

    def unique_final_path(self, desired_path: Path, message_id: int) -> Path:
        if not desired_path.exists():
            return desired_path
        return desired_path.parent / self.unique_final_filename(
            desired_path.name,
            message_id,
            desired_path.parent,
        )

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
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram 下载管理</title>
  <style>
    :root { color-scheme: light; --bg:#ece8df; --panel:#fffdf8; --panel-soft:#f7f3ea; --line:#d9d2c5; --line-strong:#c7beae; --text:#202927; --muted:#68736f; --accent:#2b6f66; --accent-dark:#1f554f; --accent-soft:#dcebe7; --warn:#8b651d; --bad:#a64235; --good:#2d7048; --soft:#f1ede4; --side:#172320; --side-2:#22332f; --side-muted:#bdc9c4; --focus:rgba(43,111,102,.16); --shadow:0 12px 34px rgba(49,43,34,.10); font-family:system-ui,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",Arial,sans-serif; font-variant-numeric:tabular-nums; }
    * { box-sizing: border-box; }
    html { scroll-behavior:smooth; }
    body { margin:0; min-height:100vh; min-height:100dvh; background:radial-gradient(circle at 20% 0%, rgba(43,111,102,.08), transparent 32rem), linear-gradient(180deg,var(--bg),#f3efe7 48rem); color:var(--text); font-size:14px; line-height:1.55; }
    h1 { font-size:clamp(24px,2.3vw,34px); margin:0; line-height:1.15; letter-spacing:0; text-wrap:balance; }
    h2 { font-size:17px; margin:0 0 12px; line-height:1.25; letter-spacing:0; text-wrap:balance; }
    h3 { font-size:14px; margin:0 0 10px; line-height:1.25; letter-spacing:0; }
    label { display:block; margin-bottom:6px; color:var(--muted); font-size:12px; font-weight:700; }
    input, textarea, select { width:100%; border:1px solid var(--line-strong); background:#fffefa; color:var(--text); border-radius:7px; padding:10px 11px; font-size:14px; outline:none; box-shadow:0 1px 0 rgba(255,255,255,.8) inset; transition:border-color .18s ease, box-shadow .18s ease, background .18s ease; }
    textarea { min-height:74px; resize:vertical; line-height:1.45; }
    input[type=checkbox], input[type=radio] { width:auto; }
    input:focus, textarea:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--focus); }
    button { border:0; border-radius:7px; background:var(--accent); color:#fff; min-height:40px; padding:0 14px; font-size:14px; font-weight:700; cursor:pointer; white-space:nowrap; box-shadow:0 1px 0 rgba(255,255,255,.32) inset, 0 8px 24px rgba(49,43,34,.08); transition:background .18s ease, transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
    button:hover { background:var(--accent-dark); box-shadow:0 1px 0 rgba(255,255,255,.32) inset, 0 12px 28px rgba(31,85,79,.18); }
    button:active { transform:translateY(1px) scale(.99); }
    button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, a:focus-visible { outline:3px solid rgba(43,111,102,.25); outline-offset:2px; }
    button.secondary { background:#fbfaf6; color:var(--text); border:1px solid var(--line-strong); box-shadow:none; }
    button.secondary:hover { background:var(--panel-soft); border-color:var(--accent); box-shadow:0 8px 20px rgba(49,43,34,.08); }
    button.danger { background:var(--bad); color:#fff; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .app-shell { min-height:100vh; min-height:100dvh; display:grid; grid-template-columns:248px minmax(0, 1fr); }
    .sidebar { background:linear-gradient(180deg,var(--side),var(--side-2) 56%,#1b2a27); color:#f6f0e6; display:flex; flex-direction:column; padding:18px 14px; border-right:1px solid rgba(17,25,24,.22); }
    .brand { min-height:74px; display:flex; flex-direction:column; justify-content:center; gap:6px; padding:4px 10px 18px; border-bottom:1px solid rgba(255,255,255,.12); font-size:17px; font-weight:800; line-height:1.2; }
    .brand-main { display:flex; align-items:center; gap:10px; }
    .mark { width:32px; height:32px; display:inline-grid; place-items:center; border-radius:8px; background:#efe1bb; color:#172320; font-size:12px; font-weight:800; }
    .brand small { color:var(--side-muted); font-size:12px; font-weight:700; }
    .nav-list { display:flex; flex-direction:column; gap:6px; margin-top:16px; }
    .nav-item { width:100%; justify-content:flex-start; text-align:left; background:transparent; color:var(--side-muted); border:1px solid transparent; min-height:42px; padding:0 12px; box-shadow:none; }
    .nav-item.active, .nav-item:hover { color:#fff; background:rgba(255,255,255,.08); border-color:rgba(255,255,255,.12); box-shadow:none; }
    .nav-item.active { box-shadow:inset 3px 0 0 #d8c78c; }
    .sidebar-footer { margin-top:auto; display:flex; flex-direction:column; gap:10px; color:var(--side-muted); font-size:12px; }
    .sidebar-footer button { width:100%; }
    .sidebar .secondary { background:rgba(255,255,255,.08); color:#eef5f3; border-color:rgba(255,255,255,.14); }
    .content { min-width:0; padding:0 24px 34px; }
    .content-header { min-height:88px; display:flex; align-items:center; justify-content:space-between; gap:16px; max-width:1420px; margin:0 auto; border-bottom:1px solid rgba(32,41,39,.1); }
    .eyebrow { margin-bottom:5px; color:var(--muted); font-size:12px; font-weight:760; }
    .top { display:flex; align-items:center; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:13px; }
    .pill { display:inline-flex; align-items:center; min-height:28px; border:1px solid var(--line); border-radius:999px; padding:0 10px; background:rgba(255,255,255,.55); color:var(--muted); font-size:12px; font-weight:680; }
    .page { display:none; max-width:1420px; margin:0 auto; padding-top:18px; }
    .page.active { display:block; }
    .band { padding:16px; margin-bottom:14px; border:1px solid rgba(32,41,39,.1); border-radius:10px; background:rgba(255,253,248,.92); box-shadow:var(--shadow); }
    .band-head { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; margin-bottom:12px; }
    .band-head p { margin:4px 0 0; color:var(--muted); }
    .panel { border:1px solid rgba(32,41,39,.1); border-radius:10px; background:rgba(255,253,248,.92); box-shadow:var(--shadow); }
    .panel.pad { padding:16px; }
    .workbench { display:grid; grid-template-columns:minmax(0, 1fr) 320px; gap:14px; align-items:stretch; margin-bottom:14px; }
    .submit-band { display:grid; grid-template-columns:minmax(180px, .32fr) minmax(300px, 1fr) auto; gap:12px; align-items:end; }
    .form-row { display:grid; grid-template-columns:minmax(260px, 1fr) auto auto; gap:10px; align-items:end; }
    .password-grid { display:grid; grid-template-columns:minmax(220px, 1fr) minmax(220px, 1fr) auto; gap:10px; align-items:end; }
    .telegram-grid { display:grid; grid-template-columns:repeat(2, minmax(220px, 1fr)); gap:10px; align-items:end; }
    .telegram-actions { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:10px; }
    .auth-layout { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:14px; }
    .auth-group { padding:16px; border:1px solid rgba(32,41,39,.1); border-radius:10px; background:var(--panel); box-shadow:var(--shadow); }
    .auth-group h2 { margin:0; }
    .auth-note { margin:5px 0 0; color:var(--muted); font-size:13px; line-height:1.45; }
    .auth-block { margin-top:14px; padding-top:14px; border-top:1px solid var(--line); }
    .tdl-input-row { display:grid; grid-template-columns:minmax(220px, 1fr) auto; gap:10px; align-items:end; margin-top:10px; }
    .qr-box { margin-top:10px; min-height:46px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .qr-box img { width:180px; height:180px; border:1px solid var(--line); border-radius:6px; background:#fff; padding:8px; }
    .tdl-qr-box { align-items:flex-start; }
    .tdl-qr-text { margin:0; max-width:100%; overflow:auto; padding:10px; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--text); white-space:pre; font:10px/1 Consolas, monospace; }
    .summary, .forwarder-grid { display:grid; grid-template-columns:repeat(6, minmax(120px, 1fr)); gap:10px; }
    .metric { min-width:0; min-height:86px; display:grid; align-content:space-between; padding:12px; border:1px solid rgba(32,41,39,.1); background:rgba(255,253,248,.86); border-radius:7px; box-shadow:none; }
    .metric strong { display:block; font-size:22px; line-height:1.2; overflow-wrap:anywhere; }
    .metric span { color:var(--muted); font-size:12px; font-weight:680; }
    .metric.forwarder { background:var(--accent-soft); }
    table { width:100%; border-collapse:separate; border-spacing:0; background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
    th, td { padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
    th { font-size:12px; color:var(--muted); background:var(--panel-soft); font-weight:800; }
    td { font-size:13px; }
    tbody tr:hover { background:#fbf8f1; }
    tbody tr:last-child td { border-bottom:0; }
    .mono { font-family:Consolas,"SFMono-Regular","Liberation Mono",monospace; font-variant-numeric:tabular-nums; }
    .title-cell { max-width:320px; overflow-wrap:anywhere; }
    .path-cell { max-width:360px; overflow-wrap:anywhere; color:var(--muted); }
    .status { display:inline-flex; align-items:center; min-width:82px; justify-content:center; border-radius:999px; padding:4px 8px; font-size:12px; font-weight:700; background:#e8ece6; color:var(--muted); }
    .status.done, .status.skipped, .status.running { color:var(--good); background:#e3f1e9; }
    .status.failed, .status.canceled, .status.stale { color:var(--bad); background:#f7e5e2; }
    .status.downloading, .status.exporting, .status.renaming, .status.queued { color:var(--warn); background:#fff0c9; }
    .bar { width:128px; height:8px; border-radius:999px; background:#ddd6ca; overflow:hidden; }
    .bar > i { display:block; height:100%; background:linear-gradient(90deg,var(--accent),#6a927f); width:0%; }
    .actions { display:flex; gap:7px; flex-wrap:wrap; }
    .actions button { min-height:32px; padding:0 10px; font-size:12px; }
    .source-row { display:grid; grid-template-columns:minmax(140px, .8fr) minmax(150px, 1fr) minmax(160px, 1fr) auto auto auto; gap:10px; align-items:end; padding:10px 0; border-bottom:1px solid var(--line); }
    .source-row:last-child { border-bottom:0; }
    .check-row { display:flex; align-items:center; gap:7px; min-height:40px; color:var(--muted); font-size:13px; font-weight:700; }
    .log { margin-top:14px; border:1px solid #263836; background:#111918; color:#dfe7dc; border-radius:10px; min-height:210px; max-height:420px; overflow:auto; padding:14px; white-space:pre-wrap; font:12px/1.55 Consolas,"SFMono-Regular","Liberation Mono",monospace; }
    .muted { color:var(--muted); }
    .message { min-height:20px; margin-top:8px; color:var(--muted); font-size:13px; }
    .message.error { color:var(--bad); }
    .message.good { color:var(--good); }
    .empty-state { min-height:130px; display:grid; align-content:center; gap:8px; padding:18px; border:1px dashed var(--line-strong); border-radius:10px; background:rgba(255,253,248,.72); }
    .empty-state strong { font-size:18px; }
    .table-wrap { overflow-x:auto; }
    .modal { position:fixed; inset:0; display:grid; place-items:center; background:rgba(11,16,22,.48); padding:20px; z-index:10; }
    .modal.hidden { display:none; }
    .dialog { width:min(760px, 100%); max-height:min(720px, calc(100vh - 40px)); display:flex; flex-direction:column; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; box-shadow:0 24px 80px rgba(16,24,32,.22); }
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
      .workbench, .auth-layout, .submit-band, .form-row, .password-grid, .telegram-grid, .tdl-input-row, .path-toolbar, .source-row { grid-template-columns:1fr; }
      .summary, .forwarder-grid { grid-template-columns:1fr 1fr; }
      button { min-height:42px; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-main"><span class="mark">TG</span><span>TG 下载中控</span></div><small>媒体归档服务</small></div>
      <nav class="nav-list" aria-label="主菜单">
        <button class="nav-item active" data-page="downloads" type="button">下载任务</button>
        <button class="nav-item" data-page="paths" type="button">路径设置</button>
        <button class="nav-item" data-page="sources" type="button">资源来源</button>
        <button class="nav-item" data-page="telegram" type="button">Telegram 授权</button>
        <button class="nav-item" data-page="password" type="button">密码管理</button>
      </nav>
      <div class="sidebar-footer"><button class="secondary" id="logoutBtn" type="button">退出登录</button></div>
    </aside>
    <main class="content">
      <header class="content-header"><div><div class="eyebrow">媒体归档工作台</div><h1 id="pageTitle">下载任务</h1></div><div class="top"><span class="pill" id="userLabel"></span><span class="pill mono" id="clock"></span></div></header>

      <section class="page active" id="page-downloads">
        <section class="workbench">
          <section class="panel pad">
            <h2>提交下载</h2>
            <div class="submit-band">
              <div><label for="sourceSelect">资源来源</label><select id="sourceSelect"></select></div>
              <div><label for="messageIds">消息 ID</label><textarea id="messageIds" aria-label="消息 ID" placeholder="23311"></textarea></div>
              <button id="submitBtn" type="button">提交下载</button>
            </div>
            <div class="message" id="submitMessage"></div>
          </section>
          <aside class="panel pad">
            <span class="pill">当前活动</span>
            <h2>媒体下载队列</h2>
            <p class="muted">任务会按媒体信息写入 Movies、TV 或普通文件目录。暂停后的任务可继续下载。</p>
          </aside>
        </section>
        <section class="band"><div class="band-head"><div><h2>转发监控</h2><p>监听来源、转发统计和 forwarder 状态。</p></div></div><div class="forwarder-grid" id="forwarderStatus"></div></section>
        <section class="band summary" id="summary"></section>
        <section class="band"><div class="band-head"><div><h2>下载队列</h2><p>保留表格密度，突出媒体标题、进度和可恢复操作。</p></div></div><div class="table-wrap"><table><thead><tr><th>任务</th><th>来源</th><th>消息</th><th>状态</th><th>片名/错误</th><th>进度</th><th>速度</th><th>PID</th><th>目录</th><th>文件</th><th>操作</th></tr></thead><tbody id="jobsBody"></tbody></table></div><div class="band-head"><div><h2>任务输出</h2><p>查看选中任务或 forwarder 的运行日志。</p></div></div><pre class="log" id="logPanel"></pre></section>
      </section>

      <section class="page" id="page-paths">
        <section class="band">
          <h2>下载目录</h2>
          <div class="form-row">
            <div><label for="downloadDir">当前路径</label><input id="downloadDir"></div>
            <button class="secondary" id="browseDirBtn" type="button">选择目录</button>
            <button id="saveConfigBtn" type="button">保存</button>
          </div>
          <div class="message" id="configMessage"></div>
        </section>
      </section>

      <section class="page" id="page-sources">
        <section class="band">
          <h2>资源来源</h2>
          <div id="sourceList"></div>
          <div class="actions"><button class="secondary" id="addSourceBtn" type="button">添加来源</button><button id="saveSourcesBtn" type="button">保存</button></div>
          <div class="message" id="sourcesMessage"></div>
        </section>
      </section>

      <section class="page" id="page-telegram">
        <div class="auth-layout">
        <section class="auth-group">
          <h2>Telethon 用户授权</h2>
          <p class="auth-note">用于转发监听和消息处理；这里生成的 Session 不会自动登录 tdl。</p>
          <div class="auth-block">
            <h3>Telegram API</h3>
            <div class="telegram-grid">
              <div><label for="telegramApiId">API ID</label><input id="telegramApiId" inputmode="numeric"></div>
              <div><label for="telegramApiHash">API hash</label><input id="telegramApiHash" placeholder="留空保持不变"></div>
              <div><label for="telegramSessionFile">Session 文件</label><input id="telegramSessionFile" placeholder="/etc/tg-downloader-ui/session.txt"></div>
              <div><label for="telegramChannelId">转发目标频道 ID</label><input id="telegramChannelId" placeholder="-100..."></div>
              <div><label for="telegramProxy">Telegram 代理</label><input id="telegramProxy" placeholder="socks5://127.0.0.1:1080"></div>
            </div>
            <div class="telegram-actions"><button id="saveTelegramBtn" type="button">保存配置</button><span class="muted" id="telegramSessionState"></span></div>
            <div class="message" id="telegramConfigMessage"></div>
          </div>
          <div class="auth-block">
            <h3>验证码登录</h3>
            <div class="telegram-grid">
              <div><label for="telegramPhone">手机号</label><input id="telegramPhone" placeholder="+8613..."></div>
              <div><label for="telegramCode">验证码</label><input id="telegramCode" inputmode="numeric"></div>
              <div><label for="telegramPassword">两步验证密码</label><input id="telegramPassword" type="password" autocomplete="one-time-code"></div>
            </div>
            <div class="telegram-actions"><button class="secondary" id="sendTelegramCodeBtn" type="button">发送验证码</button><button id="confirmTelegramCodeBtn" type="button">确认登录</button></div>
            <div class="message" id="telegramCodeMessage"></div>
          </div>
          <div class="auth-block">
            <h3>二维码登录</h3>
            <div class="telegram-actions"><button class="secondary" id="startTelegramQrBtn" type="button">生成二维码</button><button id="checkTelegramQrBtn" type="button">检查扫码状态</button></div>
            <div class="qr-box" id="telegramQr"></div>
            <div class="message" id="telegramQrMessage"></div>
          </div>
        </section>
        <section class="auth-group">
          <h2>tdl 下载授权</h2>
          <p class="auth-note">用于实际 Telegram 文件下载；tdl 需要单独登录，不会读取上方 Telethon Session。</p>
          <div class="auth-block">
            <h3>验证码登录</h3>
            <div class="telegram-grid">
              <div><label for="tdlPhone">手机号</label><input id="tdlPhone" placeholder="+8613..."></div>
              <div><label for="tdlCode">验证码</label><input id="tdlCode" inputmode="numeric" autocomplete="one-time-code"></div>
              <div><label for="tdlPassword">两步验证密码</label><input id="tdlPassword" type="password" autocomplete="one-time-code"></div>
            </div>
            <div class="telegram-actions"><button class="secondary" id="startTdlCodeLoginBtn" type="button">启动验证码登录</button><button class="secondary" id="sendTdlPhoneBtn" type="button">发送手机号</button><button class="secondary" id="sendTdlCodeBtn" type="button">发送验证码</button><button id="sendTdlPasswordBtn" type="button">发送两步验证密码</button></div>
          </div>
          <div class="auth-block">
            <h3>二维码登录</h3>
            <div class="telegram-actions"><button class="secondary" id="startTdlQrLoginBtn" type="button">生成二维码</button><button id="refreshTdlQrLoginBtn" type="button">刷新状态</button></div>
            <div class="qr-box tdl-qr-box" id="tdlQr"></div>
          </div>
          <div class="auth-block">
            <h3>tdl 输出</h3>
            <pre class="log" id="tdlLoginOutput"></pre>
            <div class="message" id="tdlLoginMessage"></div>
          </div>
        </section>
        </div>
      </section>

      <section class="page" id="page-password">
        <section class="band">
          <h2>密码管理</h2>
          <div class="password-grid">
            <div><label for="currentPassword">当前密码</label><input id="currentPassword" type="password" autocomplete="current-password"></div>
            <div><label for="newPassword">新密码</label><input id="newPassword" type="password" autocomplete="new-password"></div>
            <button id="changePasswordBtn" type="button">修改密码</button>
          </div>
          <div class="message" id="passwordMessage"></div>
        </section>
      </section>
    </main>
  </div>

  <div class="modal hidden" id="dirDialog" role="dialog" aria-modal="true" aria-labelledby="dirDialogTitle">
    <div class="dialog">
      <div class="dialog-header"><h2 id="dirDialogTitle">选择目录</h2><button class="secondary" id="closeDirBtn" type="button">关闭</button></div>
      <div class="dialog-body">
        <div class="path-toolbar">
          <button class="secondary" id="dirParentBtn" type="button">上级</button>
          <button class="secondary" id="dirRootBtn" type="button">根目录</button>
          <input id="dirDialogPath" aria-label="目录路径">
          <button class="secondary" id="goDirBtn" type="button">打开</button>
        </div>
        <div class="message" id="dirMessage"></div>
        <div class="dir-list" id="dirList"></div>
      </div>
      <div class="dialog-footer"><button id="selectDirBtn" type="button">使用当前目录</button></div>
    </div>
  </div>

  <script>
    let selectedJob = null;
    let csrfToken = '';
    let currentDir = '';
    let currentDirParent = '';
    let sources = [];
    let defaultSourceId = '';
    const pageTitles = {downloads:'下载任务', paths:'路径设置', sources:'资源来源', telegram:'Telegram 授权', password:'密码管理'};
    function statusLabel(status) { const labels = {queued:'排队中', exporting:'导出中', downloading:'下载中', renaming:'重命名', done:'已完成', skipped:'已存在', failed:'失败', canceled:'已暂停', running:'运行中', stale:'已失联', missing:'未启动', unknown:'未知'}; return labels[status] || status; }
    function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch])); }
    async function api(path, options = {}) { const method = String(options.method || 'GET').toUpperCase(); const headers = {'Content-Type':'application/json', ...(options.headers || {})}; if (!['GET','HEAD'].includes(method) && csrfToken) { headers['X-CSRF-Token'] = csrfToken; } const res = await fetch(path, {...options, headers}); if (res.status === 401) { location.href = '/login'; throw new Error('需要登录'); } if (!res.ok) { const text = await res.text(); throw new Error(text || res.statusText); } const type = res.headers.get('Content-Type') || ''; return type.includes('application/json') ? res.json() : res.text(); }
    function showPage(name) { const next = pageTitles[name] ? name : 'downloads'; document.querySelectorAll('.page').forEach(page => page.classList.toggle('active', page.id === `page-${next}`)); document.querySelectorAll('.nav-item').forEach(btn => btn.classList.toggle('active', btn.dataset.page === next)); document.getElementById('pageTitle').textContent = pageTitles[next]; if (location.hash !== `#${next}`) { history.replaceState(null, '', `#${next}`); } }
    async function loadMe() { const data = await api('/api/auth/me'); csrfToken = data.csrf_token || ''; document.getElementById('userLabel').textContent = data.username; }
    async function loadConfig() { const data = await api('/api/config'); document.getElementById('downloadDir').value = data.download_dir || ''; }
    async function loadTelegramConfig() { const data = await api('/api/telegram/config'); document.getElementById('telegramApiId').value = data.api_id || ''; document.getElementById('telegramApiHash').value = ''; document.getElementById('telegramApiHash').placeholder = data.api_hash_set ? '已保存，留空保持不变' : '必填'; document.getElementById('telegramSessionFile').value = data.session_file || ''; document.getElementById('telegramProxy').value = data.proxy || ''; document.getElementById('telegramChannelId').value = data.forward_channel_id || ''; document.getElementById('telegramSessionState').textContent = data.session_exists ? 'Session 文件已存在' : 'Session 文件未生成'; }
    async function loadSources() { const data = await api('/api/sources'); sources = data.sources || []; defaultSourceId = data.default_source_id || ''; renderSourceOptions(); renderSources(); }
    function sourceIdFrom(value) { return String(value || '').trim().replace(/^@/, '').toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'source'; }
    function renderSourceOptions() { const select = document.getElementById('sourceSelect'); select.innerHTML = sources.filter(source => source.enabled !== false).map(source => `<option value="${escapeHtml(source.id)}">${escapeHtml(source.label)} (${escapeHtml(source.chat)})</option>`).join(''); select.value = defaultSourceId || (select.options[0] ? select.options[0].value : ''); }
    function addSourceRow(source = {}, isDefault = false) { const row = document.createElement('div'); row.className = 'source-row'; row.dataset.sourceId = source.id || ''; row.innerHTML = `<div><label>名称</label><input data-field="label" value="${escapeHtml(source.label || '')}"></div><div><label>tdl 会话</label><input data-field="chat" value="${escapeHtml(source.chat || '')}" placeholder="beta_bot"></div><div><label>转发来源</label><input data-field="forward_source" value="${escapeHtml(source.forward_source || '')}" placeholder="@beta_bot"></div><label class="check-row"><input data-field="enabled" type="checkbox" ${source.enabled === false ? '' : 'checked'}> 启用</label><label class="check-row"><input data-field="default" name="defaultSource" type="radio" ${isDefault ? 'checked' : ''}> 默认</label><button class="secondary" type="button" data-action="remove">移除</button>`; row.querySelector('[data-action="remove"]').addEventListener('click', () => { if (document.querySelectorAll('.source-row').length > 1) { row.remove(); } }); document.getElementById('sourceList').appendChild(row); }
    function renderSources() { const list = document.getElementById('sourceList'); list.innerHTML = ''; sources.forEach(source => addSourceRow(source, source.id === defaultSourceId)); if (!sources.length) { addSourceRow({}, true); } }
    function collectSources() { const rows = Array.from(document.querySelectorAll('.source-row')); const items = rows.map(row => { const chat = row.querySelector('[data-field="chat"]').value.trim(); const id = row.dataset.sourceId || sourceIdFrom(chat || row.querySelector('[data-field="forward_source"]').value); return {id, label:row.querySelector('[data-field="label"]').value.trim() || id, chat, forward_source:row.querySelector('[data-field="forward_source"]').value.trim(), enabled:row.querySelector('[data-field="enabled"]').checked}; }); const defaultRow = rows.find(row => row.querySelector('[data-field="default"]').checked) || rows[0]; const defaultIndex = rows.indexOf(defaultRow); return {sources:items, default_source_id:items[defaultIndex] ? items[defaultIndex].id : ''}; }
    async function saveSources() { const el = document.getElementById('sourcesMessage'); el.className = 'message'; try { const payload = collectSources(); const data = await api('/api/sources', {method:'PUT', body:JSON.stringify(payload)}); sources = data.sources || []; defaultSourceId = data.default_source_id || ''; renderSourceOptions(); renderSources(); el.textContent = '已保存'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function saveConfig() { const el = document.getElementById('configMessage'); el.className = 'message'; try { await api('/api/config', {method:'PUT', body:JSON.stringify({download_dir:document.getElementById('downloadDir').value})}); el.textContent = '已保存'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function saveTelegramConfig() { const el = document.getElementById('telegramConfigMessage'); el.className = 'message'; try { await api('/api/telegram/config', {method:'PUT', body:JSON.stringify({api_id:document.getElementById('telegramApiId').value, api_hash:document.getElementById('telegramApiHash').value, session_file:document.getElementById('telegramSessionFile').value, proxy:document.getElementById('telegramProxy').value, forward_channel_id:document.getElementById('telegramChannelId').value})}); await loadTelegramConfig(); el.textContent = '已保存'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function sendTelegramCode() { const el = document.getElementById('telegramCodeMessage'); el.className = 'message'; try { await api('/api/telegram/auth/code/send', {method:'POST', body:JSON.stringify({phone:document.getElementById('telegramPhone').value})}); el.textContent = '验证码已发送'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function confirmTelegramCode() { const el = document.getElementById('telegramCodeMessage'); el.className = 'message'; try { await api('/api/telegram/auth/code/confirm', {method:'POST', body:JSON.stringify({phone:document.getElementById('telegramPhone').value, code:document.getElementById('telegramCode').value, password:document.getElementById('telegramPassword').value})}); await loadTelegramConfig(); el.textContent = '授权完成'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    let telegramQrToken = '';
    function renderTelegramQr(data) { telegramQrToken = data.token || telegramQrToken; const box = document.getElementById('telegramQr'); if (data.qr_svg) { box.innerHTML = `<img alt="Telegram 登录二维码" src="${data.qr_svg}"><span class="muted">${escapeHtml(data.state || '')}</span>`; } else { box.innerHTML = data.url ? `<a href="${escapeHtml(data.url)}">${escapeHtml(data.url)}</a>` : ''; } document.getElementById('telegramQrMessage').textContent = data.error || (data.state === 'authorized' ? '授权完成' : (data.state === 'password_required' ? '请输入两步验证密码后再次检查' : '')); }
    async function startTelegramQr() { const el = document.getElementById('telegramQrMessage'); el.className = 'message'; try { renderTelegramQr(await api('/api/telegram/auth/qr/start', {method:'POST', body:'{}'})); } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function checkTelegramQr() { const el = document.getElementById('telegramQrMessage'); el.className = 'message'; try { renderTelegramQr(await api('/api/telegram/auth/qr/check', {method:'POST', body:JSON.stringify({token:telegramQrToken, password:document.getElementById('telegramPassword').value})})); await loadTelegramConfig(); } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    let tdlLoginTimer = null;
    function latestTdlQrOutput(output) { const text = String(output || '').trimEnd(); const index = text.lastIndexOf('Scan QR'); if (index >= 0) { return text.slice(index).trim(); } const chunks = text.split(/\n{2,}/).map(item => item.trim()).filter(Boolean); return chunks.length ? chunks[chunks.length - 1] : text; }
    function renderTdlLogin(data) { const output = data.output || ''; const qr = latestTdlQrOutput(output); document.getElementById('tdlQr').innerHTML = data.mode === 'qr' && qr ? `<pre class="tdl-qr-text">${escapeHtml(qr)}</pre>` : ''; document.getElementById('tdlLoginOutput').textContent = data.mode === 'qr' ? '' : output; const msg = document.getElementById('tdlLoginMessage'); msg.className = data.state === 'failed' ? 'message error' : 'message'; const runningText = data.mode === 'code' ? '按终端输出提示发送手机号、验证码或两步验证密码' : '请使用 Telegram 扫描上方二维码'; msg.textContent = data.error || (data.state === 'done' ? 'tdl 登录完成' : (data.state === 'running' ? runningText : '')); if (data.state === 'running' && !tdlLoginTimer) { tdlLoginTimer = setInterval(refreshTdlLogin, 2000); } if (data.state !== 'running' && tdlLoginTimer) { clearInterval(tdlLoginTimer); tdlLoginTimer = null; } }
    async function startTdlQrLogin() { if (!confirm('开始 tdl 扫码登录？已有 tdl 登录数据可能被覆盖。')) { return; } const el = document.getElementById('tdlLoginMessage'); el.className = 'message'; try { renderTdlLogin(await api('/api/tdl/login/qr/start', {method:'POST', body:'{}'})); } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function startTdlCodeLogin() { if (!confirm('开始 tdl 验证码登录？已有 tdl 登录数据可能被覆盖。')) { return; } const el = document.getElementById('tdlLoginMessage'); el.className = 'message'; try { renderTdlLogin(await api('/api/tdl/login/code/start', {method:'POST', body:'{}'})); } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function sendTdlLoginInput(inputId) { const input = document.getElementById(inputId); const el = document.getElementById('tdlLoginMessage'); el.className = 'message'; try { renderTdlLogin(await api('/api/tdl/login/input', {method:'POST', body:JSON.stringify({text:input.value})})); input.value = ''; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function refreshTdlLogin() { const el = document.getElementById('tdlLoginMessage'); try { renderTdlLogin(await api('/api/tdl/login/status')); } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function refreshTdlQrLogin() { return refreshTdlLogin(); }
    async function changePassword() { const el = document.getElementById('passwordMessage'); el.className = 'message'; try { await api('/api/auth/password', {method:'POST', body:JSON.stringify({current_password:document.getElementById('currentPassword').value, new_password:document.getElementById('newPassword').value})}); location.href = '/login'; } catch (err) { el.className = 'message error'; el.textContent = err.message; } }
    async function logout() { await api('/api/auth/logout', {method:'POST', body:'{}'}); location.href = '/login'; }
    async function submitJobs() { const btn = document.getElementById('submitBtn'); const msg = document.getElementById('submitMessage'); btn.disabled = true; msg.className = 'message'; msg.textContent = ''; try { await api('/api/jobs', {method:'POST', body:JSON.stringify({message_ids:document.getElementById('messageIds').value, source_id:document.getElementById('sourceSelect').value})}); document.getElementById('messageIds').value = ''; msg.className = 'message good'; msg.textContent = '任务已加入队列'; await refreshJobs(); } catch (err) { msg.className = 'message error'; msg.textContent = err.message; } finally { btn.disabled = false; } }
    async function resumeJob(id) { await api(`/api/jobs/${id}/resume`, {method:'POST', body:'{}'}); await refreshJobs(); }
    async function pauseJob(id) { await api(`/api/jobs/${id}/pause`, {method:'POST', body:'{}'}); await refreshJobs(); }
    async function retryJob(id) { await resumeJob(id); }
    async function cancelJob(id) { await pauseJob(id); }
    async function deleteJob(id) { await api(`/api/jobs/${id}`, {method:'DELETE'}); if (selectedJob === id) { selectedJob = null; document.getElementById('logPanel').textContent = ''; } await refreshJobs(); }
    async function loadLog(id) { selectedJob = id; document.getElementById('logPanel').textContent = await api(`/api/jobs/${id}/log`) || ''; }
    async function loadForwarderLog() { selectedJob = null; document.getElementById('logPanel').textContent = await api('/api/forwarder/log') || ''; }
    async function restartForwarder() { if (!confirm('确认重启 forwarder？')) { return; } selectedJob = null; try { const data = await api('/api/forwarder/restart', {method:'POST', body:'{}'}); const output = [data.stdout || '', data.stderr || ''].filter(Boolean).join('\n').trim(); document.getElementById('logPanel').textContent = output || 'forwarder restart requested'; await refreshForwarder(); } catch (err) { alert(err.message); } }
    async function openDirectory(path) { const msg = document.getElementById('dirMessage'); msg.className = 'message'; msg.textContent = ''; try { const data = await api(`/api/fs/dirs?path=${encodeURIComponent(path || '')}`); renderDirectory(data); } catch (err) { msg.className = 'message error'; msg.textContent = err.message; } }
    function renderDirectory(data) { currentDir = data.path || ''; currentDirParent = data.parent || ''; document.getElementById('dirDialogPath').value = currentDir; document.getElementById('dirParentBtn').disabled = !currentDirParent; const list = document.getElementById('dirList'); list.innerHTML = ''; if (!data.entries.length) { const empty = document.createElement('div'); empty.className = 'dir-row muted'; empty.textContent = '没有子目录'; list.appendChild(empty); return; } data.entries.forEach(entry => { const row = document.createElement('button'); row.className = 'dir-row'; row.type = 'button'; const name = document.createElement('span'); name.className = 'dir-name'; name.textContent = entry.name; const flag = document.createElement('span'); flag.className = 'dir-writable'; flag.textContent = entry.writable ? '可写' : '只读'; row.append(name, flag); row.addEventListener('click', () => openDirectory(entry.path)); list.appendChild(row); }); }
    async function openDirectoryDialog() { document.getElementById('dirDialog').classList.remove('hidden'); await openDirectory(document.getElementById('downloadDir').value); document.getElementById('dirDialogPath').focus(); }
    function closeDirectoryDialog() { document.getElementById('dirDialog').classList.add('hidden'); }
    function selectCurrentDirectory() { if (currentDir) { document.getElementById('downloadDir').value = currentDir; } closeDirectoryDialog(); }
    function renderSummary(jobs) { const counts = jobs.reduce((acc, job) => { acc[job.status] = (acc[job.status] || 0) + 1; return acc; }, {}); const active = jobs.find(job => ['exporting','downloading','renaming'].includes(job.status)); const items = [['活动', active ? `#${active.id}` : '0'], ['排队', counts.queued || 0], ['完成', (counts.done || 0) + (counts.skipped || 0)], ['失败', counts.failed || 0], ['暂停', counts.canceled || 0]]; document.getElementById('summary').innerHTML = items.map(([label, value]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${label}</span></div>`).join(''); }
    function renderForwarder(status) {
      const sourceText = status.source_count ? `${status.source_count} 个已启用` : (status.source || '');
      const items = [['状态', `<span class="status ${escapeHtml(status.state || 'unknown')}">${statusLabel(status.state || 'unknown')}</span>`], ['来源', escapeHtml(sourceText)], ['最近来源', escapeHtml(status.last_source || '')], ['已转发', escapeHtml(status.sent_count || 0)], ['错误', escapeHtml(status.last_error || '')]];
      const configurationAction = status.configuration_hint ? `<button class="secondary" type="button" onclick="showPage('telegram')">配置 Telegram API</button><span class="muted">${escapeHtml(status.configuration_hint)}</span>` : '';
      const restartHint = status.restart_hint || '重启命令未配置';
      const restartButton = status.restart_configured ? '<button class="secondary" onclick="restartForwarder()">重启</button>' : `<button class="secondary" type="button" disabled title="${escapeHtml(restartHint)}">重启</button><span class="muted">${escapeHtml(restartHint)}</span>`;
      document.getElementById('forwarderStatus').innerHTML = items.map(([label, value], index) => index === 0 ? `<div class="metric forwarder"><strong>${value}</strong><span>${label}</span></div>` : `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`).join('') + configurationAction + '<button class="secondary" onclick="loadForwarderLog()">日志</button>' + restartButton;
    }
    function renderJobs(jobs) { const body = document.getElementById('jobsBody'); if (!jobs.length) { body.innerHTML = '<tr><td colspan="11"><div class="empty-state"><strong>还没有下载任务</strong><p class="muted">选择来源并输入消息 ID，任务会进入队列。完成后文件会按媒体信息写入归档目录。</p></div></td></tr>'; return; } body.innerHTML = jobs.map(job => { const pct = Math.max(0, Math.min(100, Number(job.progress || 0))); const active = ['queued','exporting','downloading','renaming'].includes(job.status); const resume = ['failed','canceled'].includes(job.status) ? `<button class="secondary" onclick="resumeJob(${job.id})">继续</button>` : ''; const pause = active ? `<button class="secondary" onclick="pauseJob(${job.id})">暂停</button>` : ''; const remove = !['exporting','downloading','renaming'].includes(job.status) ? `<button class="danger" onclick="deleteJob(${job.id})">删除</button>` : ''; return `<tr><td class="mono">#${job.id}</td><td>${escapeHtml(job.source_label || job.source_chat || '')}</td><td class="mono">${job.message_id}</td><td><span class="status ${job.status}">${statusLabel(job.status)}</span></td><td class="title-cell">${escapeHtml(job.title || job.error || '')}</td><td><div class="bar"><i style="width:${pct}%"></i></div><span class="muted">${pct.toFixed(1)}%</span></td><td>${escapeHtml(job.speed || '')}</td><td class="mono">${job.process_pid || ''}</td><td class="path-cell">${escapeHtml(job.download_dir || '')}</td><td class="title-cell">${escapeHtml(job.final_path || job.source_file || '')}</td><td class="actions"><button class="secondary" onclick="loadLog(${job.id})">日志</button>${pause}${resume}${remove}</td></tr>`; }).join(''); }
    async function refreshJobs() { const data = await api('/api/jobs'); renderSummary(data.jobs); renderJobs(data.jobs); if (selectedJob) { document.getElementById('logPanel').textContent = await api(`/api/jobs/${selectedJob}/log`) || ''; } }
    async function refreshForwarder() { renderForwarder(await api('/api/forwarder/status')); }
    async function refreshAll() { await Promise.all([refreshJobs(), refreshForwarder()]); }
    document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => showPage(btn.dataset.page)));
    document.getElementById('submitBtn').addEventListener('click', submitJobs);
    document.getElementById('addSourceBtn').addEventListener('click', () => addSourceRow({}, false));
    document.getElementById('saveSourcesBtn').addEventListener('click', saveSources);
    document.getElementById('saveConfigBtn').addEventListener('click', saveConfig);
    document.getElementById('saveTelegramBtn').addEventListener('click', saveTelegramConfig);
    document.getElementById('sendTelegramCodeBtn').addEventListener('click', sendTelegramCode);
    document.getElementById('confirmTelegramCodeBtn').addEventListener('click', confirmTelegramCode);
    document.getElementById('startTelegramQrBtn').addEventListener('click', startTelegramQr);
    document.getElementById('checkTelegramQrBtn').addEventListener('click', checkTelegramQr);
    document.getElementById('startTdlQrLoginBtn').addEventListener('click', startTdlQrLogin);
    document.getElementById('startTdlCodeLoginBtn').addEventListener('click', startTdlCodeLogin);
    document.getElementById('sendTdlPhoneBtn').addEventListener('click', () => sendTdlLoginInput('tdlPhone'));
    document.getElementById('sendTdlCodeBtn').addEventListener('click', () => sendTdlLoginInput('tdlCode'));
    document.getElementById('sendTdlPasswordBtn').addEventListener('click', () => sendTdlLoginInput('tdlPassword'));
    document.getElementById('tdlPhone').addEventListener('keydown', event => { if (event.key === 'Enter') { sendTdlLoginInput('tdlPhone'); } });
    document.getElementById('tdlCode').addEventListener('keydown', event => { if (event.key === 'Enter') { sendTdlLoginInput('tdlCode'); } });
    document.getElementById('tdlPassword').addEventListener('keydown', event => { if (event.key === 'Enter') { sendTdlLoginInput('tdlPassword'); } });
    document.getElementById('refreshTdlQrLoginBtn').addEventListener('click', refreshTdlQrLogin);
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
    loadMe(); loadConfig(); loadTelegramConfig(); loadSources(); refreshAll(); setInterval(refreshAll, 2500);
  </script>
</body>
</html>
"""


SETUP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>初始化下载中控</title>
  <style>
    :root { --bg:#ece8df; --panel:#fffdf8; --line:#d9d2c5; --line-strong:#c7beae; --text:#202927; --muted:#68736f; --accent:#2b6f66; --accent-dark:#1f554f; --bad:#a64235; font-family:system-ui,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",Arial,sans-serif; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center; padding:28px 18px; background:radial-gradient(circle at 20% 0%, rgba(43,111,102,.08), transparent 32rem), linear-gradient(180deg,var(--bg),#f3efe7 48rem); color:var(--text); font-size:14px; line-height:1.55; }
    .auth-shell { width:min(1040px, 100%); display:grid; grid-template-columns:minmax(280px,.9fr) minmax(320px,1fr); gap:18px; align-items:stretch; }
    .auth-intro { padding:clamp(22px,4vw,42px); border-radius:12px; background:linear-gradient(145deg,#172320,#2c403b); color:#f7efe1; box-shadow:0 18px 50px rgba(49,43,34,.12); }
    .mark { width:32px; height:32px; display:inline-grid; place-items:center; border-radius:8px; background:#efe1bb; color:#172320; font-size:12px; font-weight:800; }
    h1 { margin:26px 0 10px; font-size:clamp(30px,4vw,48px); line-height:1.12; letter-spacing:0; }
    h2 { margin:0 0 8px; font-size:20px; line-height:1.2; }
    p { margin:0; color:#c5d0ca; text-wrap:pretty; }
    main { padding:clamp(20px,3vw,30px); border:1px solid rgba(32,41,39,.1); border-radius:12px; background:rgba(255,253,248,.94); box-shadow:0 18px 50px rgba(49,43,34,.12); }
    .setup-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:16px; }
    .full { grid-column:1 / -1; }
    label { display:block; margin-bottom:6px; color:var(--muted); font-size:12px; font-weight:700; }
    input { width:100%; height:42px; border:1px solid var(--line-strong); border-radius:7px; padding:0 11px; background:#fffefa; color:var(--text); font-size:14px; outline:none; }
    input:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(43,111,102,.16); }
    button { width:100%; min-height:42px; margin-top:18px; border:0; border-radius:7px; background:var(--accent); color:#fff; font-weight:700; cursor:pointer; }
    button:hover { background:var(--accent-dark); }
    button:active { transform:translateY(1px); }
    .message { min-height:20px; margin-top:12px; font-size:13px; color:var(--muted); }
    .message.error { color:var(--bad); }
    @media (max-width:760px) { .auth-shell, .setup-grid { grid-template-columns:1fr; } .full { grid-column:auto; } }
  </style>
</head>
<body>
<div class="auth-shell">
  <aside class="auth-intro">
    <span class="mark">TG</span>
    <h1>初始化下载中控</h1>
    <p>首次启动时创建管理员账号并设置下载目录。Telegram 转发器字段可以稍后在授权页补充。</p>
  </aside>
  <main>
    <h2>完成初始设置</h2>
    <div class="setup-grid">
      <div><label for="username">管理员账号</label><input id="username" autocomplete="username" value="admin"></div>
      <div><label for="password">管理员密码</label><input id="password" type="password" autocomplete="new-password"></div>
      <div class="full"><label for="downloadDir">下载目录</label><input id="downloadDir" placeholder="/downloads"></div>
      <div><label for="apiId">Telegram API ID（仅转发器需要）</label><input id="apiId" inputmode="numeric"></div>
      <div><label for="apiHash">Telegram API hash（仅转发器需要）</label><input id="apiHash"></div>
      <div><label for="sessionFile">Telegram Session 文件（仅转发器需要）</label><input id="sessionFile" placeholder="/config/session.txt"></div>
      <div><label for="channelId">转发目标频道 ID（仅转发器需要）</label><input id="channelId" placeholder="-100..."></div>
    </div>
    <button id="saveBtn" type="button">完成初始化</button>
    <div class="message" id="message">Telegram 授权和 tdl 登录可以初始化后再配置。</div>
  </main>
</div>
<script>
document.getElementById('saveBtn').addEventListener('click', async () => {
  const message = document.getElementById('message');
  message.className = 'message';
  message.textContent = '';
  const payload = {
    username: document.getElementById('username').value,
    password: document.getElementById('password').value,
    download_dir: document.getElementById('downloadDir').value,
    telegram: {
      api_id: document.getElementById('apiId').value,
      api_hash: document.getElementById('apiHash').value,
      session_file: document.getElementById('sessionFile').value,
      forward_channel_id: document.getElementById('channelId').value
    }
  };
  const res = await fetch('/api/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  if (res.ok) location.href = '/login';
  else { message.className = 'message error'; message.textContent = await res.text(); }
});
fetch('/api/setup').then(res => res.json()).then(data => {
  document.getElementById('downloadDir').value = data.default_download_dir || '/downloads';
}).catch(() => {
  document.getElementById('downloadDir').value = '/downloads';
});
</script>
</body>
</html>
"""

LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>登录 - Telegram 下载管理</title>
  <style>
    :root { font-family:system-ui,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",Arial,sans-serif; color:#202927; background:#ece8df; --line:#d9d2c5; --line-strong:#c7beae; --muted:#68736f; --accent:#2b6f66; --accent-dark:#1f554f; --bad:#a64235; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center; padding:28px 18px; background:radial-gradient(circle at 20% 0%, rgba(43,111,102,.08), transparent 32rem), linear-gradient(180deg,#ece8df,#f3efe7 48rem); }
    .auth-shell { width:min(940px, 100%); display:grid; grid-template-columns:minmax(280px,.95fr) minmax(320px, .85fr); gap:18px; align-items:stretch; }
    .auth-intro { padding:clamp(22px,4vw,42px); border-radius:12px; background:linear-gradient(145deg,#172320,#2c403b); color:#f7efe1; box-shadow:0 18px 50px rgba(49,43,34,.12); }
    .mark { width:32px; height:32px; display:inline-grid; place-items:center; border-radius:8px; background:#efe1bb; color:#172320; font-size:12px; font-weight:800; }
    .auth-intro h1 { margin:26px 0 10px; font-size:clamp(30px,4vw,48px); line-height:1.12; letter-spacing:0; }
    .auth-intro p { margin:0; color:#c5d0ca; line-height:1.6; }
    form { padding:clamp(20px,3vw,30px); background:rgba(255,253,248,.94); border:1px solid rgba(32,41,39,.1); border-radius:12px; box-shadow:0 18px 50px rgba(49,43,34,.12); }
    h1 { margin:0 0 8px; font-size:22px; line-height:1.2; letter-spacing:0; }
    h2 { margin:0 0 16px; color:var(--muted); font-size:13px; font-weight:700; }
    label { display:block; margin:12px 0 6px; font-size:13px; font-weight:700; color:var(--muted); }
    input { width:100%; height:42px; border:1px solid var(--line-strong); border-radius:7px; padding:0 10px; background:#fffefa; font-size:15px; outline:none; }
    input:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(43,111,102,.16); }
    button { width:100%; height:42px; margin-top:18px; border:0; border-radius:7px; background:var(--accent); color:#fff; font-weight:700; cursor:pointer; }
    button:hover { background:var(--accent-dark); }
    button:active { transform:translateY(1px); }
    .error { min-height:20px; margin-top:12px; color:var(--bad); font-size:13px; }
    @media (max-width:760px) { .auth-shell { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="auth-shell">
    <aside class="auth-intro">
      <span class="mark">TG</span>
      <h1>TG 下载中控</h1>
      <p>媒体归档服务。登录后管理下载队列、资源来源、归档目录和 Telegram 授权。</p>
    </aside>
    <form id="loginForm">
      <h1>Telegram 下载管理</h1>
      <h2>登录下载中控</h2>
      <label for="username">管理员</label>
      <input id="username" autocomplete="username" value="admin">
      <label for="password">密码</label>
      <input id="password" type="password" autocomplete="current-password" autofocus>
      <button type="submit">登录</button>
      <div class="error" id="error"></div>
    </form>
  </div>
  <script>
    document.getElementById('loginForm').addEventListener('submit', async event => {
      event.preventDefault();
      const error = document.getElementById('error');
      error.textContent = '';
      const res = await fetch('/api/auth/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username:document.getElementById('username').value, password:document.getElementById('password').value }) });
      if (res.ok) { location.href = '/'; } else { error.textContent = await res.text() || '登录失败'; }
    });
  </script>
</body>
</html>
"""


def public_telegram_config(config: dict[str, str]) -> dict[str, Any]:
    session_file = config.get("session_file") or ""
    return {
        "api_id": config.get("api_id", ""),
        "api_hash_set": bool(config.get("api_hash")),
        "session_file": session_file,
        "session_exists": bool(session_file and Path(session_file).exists()),
        "proxy": config.get("proxy", ""),
        "forward_channel_id": config.get("forward_channel_id", ""),
    }


def validate_telegram_auth_config(config: dict[str, str]) -> tuple[int, str, Path]:
    api_id_text = str(config.get("api_id") or "").strip()
    api_hash = str(config.get("api_hash") or "").strip()
    session_file = str(config.get("session_file") or "").strip()
    if not api_id_text:
        raise RuntimeError("Telegram API ID is required")
    if not api_hash:
        raise RuntimeError("Telegram API hash is required")
    if not session_file:
        raise RuntimeError("Telegram session file is required")
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise RuntimeError("Telegram API ID must be an integer") from exc
    return api_id, api_hash, Path(session_file)


def parse_telegram_proxy_url(value: str | None) -> tuple[str, str, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text)
    if (
        parsed.scheme not in {"socks4", "socks5", "http"}
        or not parsed.hostname
        or not parsed.port
    ):
        raise RuntimeError("Telegram proxy must be socks4://, socks5://, or http://host:port")
    return (parsed.scheme, parsed.hostname, int(parsed.port))


def telethon_classes() -> tuple[Any, Any]:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError("Telethon is required for Telegram authorization") from exc
    return TelegramClient, StringSession


def is_telegram_password_error(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "SessionPasswordNeededError"


def save_string_session(session_file: Path, session_text: str) -> None:
    if not session_text:
        raise RuntimeError("Telegram authorization did not produce a session")
    ensure_private_dir(session_file.parent)
    session_file.write_text(session_text.strip() + "\n", encoding="utf-8")
    ensure_private_file(session_file)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def asyncio_to_thread(func: Any, *args: Any) -> Any:
    import asyncio

    return asyncio.to_thread(func, *args)


async def _telegram_send_login_code_async(
    config: dict[str, str],
    phone: str,
    state_path: Path,
    client_factory: Any | None = None,
    session_cls: Any | None = None,
) -> dict[str, Any]:
    api_id, api_hash, _ = validate_telegram_auth_config(config)
    phone_text = str(phone or "").strip()
    if not phone_text:
        raise RuntimeError("phone number is required")
    if client_factory is None or session_cls is None:
        client_factory, session_cls = telethon_classes()
    client = client_factory(
        session_cls(),
        api_id,
        api_hash,
        proxy=parse_telegram_proxy_url(config.get("proxy")),
    )
    await client.connect()
    try:
        sent = await client.send_code_request(phone_text)
    finally:
        await client.disconnect()
    ensure_private_dir(state_path.parent)
    state_path.write_text(
        json.dumps(
            {
                "phone": phone_text,
                "phone_code_hash": sent.phone_code_hash,
                "created_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    ensure_private_file(state_path)
    return {"ok": True, "phone": phone_text}


def telegram_send_login_code(
    config: dict[str, str],
    phone: str,
    state_path: Path = TELEGRAM_AUTH_STATE_PATH,
    client_factory: Any | None = None,
    session_cls: Any | None = None,
) -> dict[str, Any]:
    return asyncio_run(
        _telegram_send_login_code_async(
            config,
            phone,
            state_path,
            client_factory=client_factory,
            session_cls=session_cls,
        )
    )


async def _telegram_confirm_login_code_async(
    config: dict[str, str],
    phone: str,
    code: str,
    password: str = "",
    state_path: Path = TELEGRAM_AUTH_STATE_PATH,
    client_factory: Any | None = None,
    session_cls: Any | None = None,
) -> dict[str, Any]:
    api_id, api_hash, session_file = validate_telegram_auth_config(config)
    phone_text = str(phone or "").strip()
    code_text = str(code or "").strip()
    if not phone_text:
        raise RuntimeError("phone number is required")
    if not code_text:
        raise RuntimeError("verification code is required")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError as exc:
        raise RuntimeError("send a verification code first") from exc
    if state.get("phone") != phone_text:
        raise RuntimeError("phone number does not match the pending code")
    if client_factory is None or session_cls is None:
        client_factory, session_cls = telethon_classes()
    client = client_factory(
        session_cls(),
        api_id,
        api_hash,
        proxy=parse_telegram_proxy_url(config.get("proxy")),
    )
    await client.connect()
    try:
        try:
            await client.sign_in(
                phone=phone_text,
                code=code_text,
                phone_code_hash=state.get("phone_code_hash"),
            )
        except Exception as exc:  # noqa: BLE001 - Telethon exception boundary
            if not is_telegram_password_error(exc):
                raise
            password_text = str(password or "")
            if not password_text:
                raise RuntimeError("Telegram two-step password is required") from exc
            await client.sign_in(password=password_text)
        save_string_session(session_file, session_cls.save(client.session))
    finally:
        await client.disconnect()
    with contextlib.suppress(FileNotFoundError):
        state_path.unlink()
    return {"ok": True, "session_file": str(session_file)}


def telegram_confirm_login_code(
    config: dict[str, str],
    phone: str,
    code: str,
    password: str = "",
    state_path: Path = TELEGRAM_AUTH_STATE_PATH,
    client_factory: Any | None = None,
    session_cls: Any | None = None,
) -> dict[str, Any]:
    return asyncio_run(
        _telegram_confirm_login_code_async(
            config,
            phone,
            code,
            password=password,
            state_path=state_path,
            client_factory=client_factory,
            session_cls=session_cls,
        )
    )


def cleanup_qr_logins(now: float | None = None) -> None:
    current = time.time() if now is None else now
    with TELEGRAM_QR_LOCK:
        expired = [
            token
            for token, entry in TELEGRAM_QR_LOGINS.items()
            if current - float(entry.get("created_at") or 0) > QR_LOGIN_TTL_SECONDS * 2
        ]
        for token in expired:
            TELEGRAM_QR_LOGINS.pop(token, None)


def qr_public_state(token: str, entry: dict[str, Any]) -> dict[str, Any]:
    url = entry.get("url", "")
    return {
        "token": token,
        "state": entry.get("state", "starting"),
        "url": url,
        "qr_svg": qr_svg_data_uri(url) if url else "",
        "expires_at": str(entry.get("expires_at", "")),
        "session_file": entry.get("session_file", ""),
        "error": entry.get("error", ""),
    }


def qr_svg_data_uri(value: str) -> str:
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        return ""
    image = qrcode.make(value, image_factory=SvgPathImage, box_size=8, border=2)
    out = io.BytesIO()
    image.save(out)
    return "data:image/svg+xml;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def _telegram_qr_worker(
    token: str,
    entry: dict[str, Any],
    config: dict[str, str],
    client_factory: Any | None,
    session_cls: Any | None,
) -> None:
    client = None
    try:
        api_id, api_hash, session_file = validate_telegram_auth_config(config)
        if client_factory is None or session_cls is None:
            client_factory, session_cls = telethon_classes()
        client = client_factory(
            session_cls(),
            api_id,
            api_hash,
            proxy=parse_telegram_proxy_url(config.get("proxy")),
        )
        await client.connect()
        qr_login = await client.qr_login()
        with TELEGRAM_QR_LOCK:
            entry.update(
                {
                    "state": "pending",
                    "url": qr_login.url,
                    "expires_at": getattr(qr_login, "expires", ""),
                }
            )
            entry["ready"].set()
        try:
            await qr_login.wait()
        except Exception as exc:  # noqa: BLE001 - Telethon exception boundary
            if not is_telegram_password_error(exc):
                raise
            with TELEGRAM_QR_LOCK:
                entry["state"] = "password_required"
            password = await asyncio_to_thread(entry["passwords"].get)
            if not password:
                raise RuntimeError("Telegram two-step password is required")
            await client.sign_in(password=password)
        save_string_session(session_file, session_cls.save(client.session))
        with TELEGRAM_QR_LOCK:
            entry.update({"state": "authorized", "session_file": str(session_file)})
    except Exception as exc:  # noqa: BLE001 - auth boundary
        with TELEGRAM_QR_LOCK:
            entry.update({"state": "failed", "error": str(exc)})
            entry["ready"].set()
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()


def start_telegram_qr_login(
    config: dict[str, str],
    client_factory: Any | None = None,
    session_cls: Any | None = None,
) -> dict[str, Any]:
    cleanup_qr_logins()
    token = secrets.token_urlsafe(18)
    entry: dict[str, Any] = {
        "state": "starting",
        "created_at": time.time(),
        "ready": threading.Event(),
        "passwords": queue.Queue(maxsize=1),
    }

    def run_worker() -> None:
        asyncio_run(
            _telegram_qr_worker(
                token,
                entry,
                config,
                client_factory=client_factory,
                session_cls=session_cls,
            )
        )

    with TELEGRAM_QR_LOCK:
        TELEGRAM_QR_LOGINS[token] = entry
    threading.Thread(target=run_worker, name=f"telegram-qr-{token}", daemon=True).start()
    entry["ready"].wait(10)
    with TELEGRAM_QR_LOCK:
        return qr_public_state(token, entry)


def check_telegram_qr_login(token: str, password: str = "") -> dict[str, Any]:
    cleanup_qr_logins()
    with TELEGRAM_QR_LOCK:
        entry = TELEGRAM_QR_LOGINS.get(str(token or ""))
        if not entry:
            raise RuntimeError("QR login session not found")
        if password and entry.get("state") == "password_required":
            with contextlib.suppress(queue.Full):
                entry["passwords"].put_nowait(password)
        return qr_public_state(str(token), entry)


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


def default_forwarder_restart_command(
    which: Any = shutil.which,
    exists: Any | None = None,
) -> list[str]:
    path_exists = exists or (lambda path: Path(path).exists())
    init_script = f"/etc/init.d/{APP_NAME}"
    if path_exists(init_script):
        return ["/bin/sh", "-c", f"(sleep 1; {init_script} restart >/dev/null 2>&1) &"]
    if which("systemctl") and any(
        path_exists(path)
        for path in (
            "/etc/systemd/system/tg-downloader-forwarder.service",
            "/lib/systemd/system/tg-downloader-forwarder.service",
            "/usr/lib/systemd/system/tg-downloader-forwarder.service",
        )
    ):
        return ["systemctl", "restart", "tg-downloader-forwarder.service"]
    return []


def resolve_forwarder_restart_command(command: str | list[str] | None = None) -> list[str]:
    if command is None:
        command = os.environ.get("TGDL_FORWARDER_RESTART_CMD", "")
        if not command:
            command = default_forwarder_restart_command()

    try:
        return shlex.split(command) if isinstance(command, str) else list(command or [])
    except ValueError as exc:
        raise RuntimeError(f"forwarder restart command is invalid: {exc}") from exc


def forwarder_restart_configured() -> bool:
    try:
        return bool(resolve_forwarder_restart_command())
    except RuntimeError:
        return False


def forwarder_enabled() -> bool:
    return os.environ.get("TGDL_FORWARDER_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def forwarder_status_response() -> dict[str, Any]:
    status = read_forwarder_status()
    enabled = forwarder_enabled()
    restart_configured = enabled and forwarder_restart_configured()
    status["forwarder_enabled"] = enabled
    status["restart_configured"] = restart_configured
    if not enabled:
        status["restart_hint"] = FORWARDER_DISABLED_HINT
    elif not restart_configured:
        status["restart_hint"] = FORWARDER_RESTART_HINT
    configuration_required = status.get("last_error") in FORWARDER_CONFIGURATION_ERRORS
    status["configuration_required"] = configuration_required
    if configuration_required:
        status["configuration_hint"] = FORWARDER_CONFIGURATION_HINT
    return status


def restart_forwarder(command: str | list[str] | None = None) -> dict[str, Any]:
    args = resolve_forwarder_restart_command(command)
    if not args:
        raise RuntimeError("forwarder restart command is not configured")

    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("forwarder restart timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"forwarder restart failed to start: {exc}") from exc

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "args": redact_command_args(args),
    }


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

    def end_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/setup":
            if not self.config_store.requires_setup():
                return self.redirect("/login")
            return self.send_html(SETUP_HTML)
        if parsed.path == "/api/setup":
            return self.send_json(
                {
                    "required": self.config_store.requires_setup(),
                    "default_download_dir": str(self.config_store.default_download_dir),
                }
            )
        if parsed.path == "/login":
            if self.config_store.requires_setup():
                return self.redirect("/setup")
            if self.authorized():
                return self.redirect("/")
            return self.send_html(LOGIN_HTML)
        if self.config_store.requires_setup():
            return self.require_setup()
        if not self.authorized():
            return self.require_auth()
        if parsed.path == "/":
            return self.send_html(INDEX_HTML)
        if parsed.path == "/api/auth/me":
            session = self.auth.get_session(self.get_session_token()) or {}
            return self.send_json(
                {
                    "username": self.config_store.get_username(),
                    "csrf_token": session.get("csrf_token", ""),
                }
            )
        if parsed.path == "/api/config":
            return self.send_json({"download_dir": str(self.config_store.get_download_dir())})
        if parsed.path == "/api/telegram/config":
            return self.send_json(
                public_telegram_config(self.config_store.get_telegram_config())
            )
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
            return self.send_json(forwarder_status_response())
        if parsed.path == "/api/forwarder/log":
            return self.send_text(tail_text_file(STATE_DIR / "forwarder.log"))
        if parsed.path == "/api/tdl/login/status":
            return self.send_json(tdl_login_status())
        if parsed.path == "/api/tdl/login/qr/status":
            return self.send_json(tdl_qr_login_status())

        match = re.fullmatch(r"/api/jobs/(\d+)/log", parsed.path)
        if match:
            try:
                return self.send_text(self.store.tail_log(int(match.group(1))))
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.NOT_FOUND, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.reject_oversized_request():
            return
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/auth/login":
            if self.config_store.requires_setup():
                return self.send_error_text(HTTPStatus.PRECONDITION_REQUIRED, "setup required")
            try:
                payload = self.read_json()
                username = str(payload.get("username") or "")
                password = str(payload.get("password") or "")
                login_key = f"{self.client_address[0]}:{username.strip().lower()}"
                retry_after = self.auth.login_retry_after(login_key)
                if retry_after:
                    return self.send_error_text(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "too many login attempts",
                        headers={"Retry-After": str(retry_after)},
                    )
                if not self.auth.verify_password(username, password):
                    self.auth.record_login_failure(login_key)
                    return self.send_error_text(HTTPStatus.UNAUTHORIZED, "invalid username or password")
                self.auth.clear_login_failures(login_key)
                token = self.auth.create_session(username)
                return self.send_json(
                    {"username": username},
                    headers={"Set-Cookie": self.build_session_cookie(token)},
                )
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/setup":
            if not self.config_store.requires_setup():
                return self.send_error_text(HTTPStatus.BAD_REQUEST, "setup already completed")
            try:
                payload = self.read_json()
                self.config_store.initialize(
                    username=str(payload.get("username") or ""),
                    password=str(payload.get("password") or ""),
                    download_dir=str(payload.get("download_dir") or ""),
                    telegram=dict(payload.get("telegram") or {}),
                )
                return self.send_json({"ok": True}, status=HTTPStatus.CREATED)
            except Exception as exc:  # noqa: BLE001 - setup boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if not self.authorized():
            return self.require_auth()
        if not self.require_csrf():
            return

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

        if parsed.path == "/api/forwarder/restart":
            try:
                result = restart_forwarder()
                if result["ok"]:
                    return self.send_json(result)
                return self.send_json(result, status=HTTPStatus.BAD_GATEWAY)
            except RuntimeError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/tdl/login/qr/start":
            try:
                return self.send_json(start_tdl_qr_login())
            except RuntimeError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/tdl/login/code/start":
            try:
                return self.send_json(start_tdl_code_login())
            except RuntimeError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/tdl/login/input":
            try:
                payload = self.read_json()
                return self.send_json(send_tdl_login_input(str(payload.get("text") or "")))
            except RuntimeError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/telegram/auth/code/send":
            try:
                payload = self.read_json()
                result = telegram_send_login_code(
                    self.config_store.get_telegram_config(),
                    str(payload.get("phone") or ""),
                )
                return self.send_json(result)
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/telegram/auth/code/confirm":
            try:
                payload = self.read_json()
                result = telegram_confirm_login_code(
                    self.config_store.get_telegram_config(),
                    str(payload.get("phone") or ""),
                    str(payload.get("code") or ""),
                    password=str(payload.get("password") or ""),
                )
                return self.send_json(result)
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/telegram/auth/qr/start":
            try:
                return self.send_json(
                    start_telegram_qr_login(self.config_store.get_telegram_config())
                )
            except Exception as exc:  # noqa: BLE001 - API boundary
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        if parsed.path == "/api/telegram/auth/qr/check":
            try:
                payload = self.read_json()
                return self.send_json(
                    check_telegram_qr_login(
                        str(payload.get("token") or ""),
                        password=str(payload.get("password") or ""),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - API boundary
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

        match = re.fullmatch(r"/api/jobs/(\d+)/resume", parsed.path)
        if match:
            try:
                job = self.store.resume_job(int(match.group(1)))
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

        match = re.fullmatch(r"/api/jobs/(\d+)/pause", parsed.path)
        if match:
            try:
                job = self.store.pause_job(int(match.group(1)))
                return self.send_json({"job": job})
            except ValueError as exc:
                return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))

        return self.send_error_text(HTTPStatus.NOT_FOUND, "not found")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        if self.reject_oversized_request():
            return
        if not self.authorized():
            return self.require_auth()
        if not self.require_csrf():
            return
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
        if parsed.path == "/api/telegram/config":
            try:
                payload = self.read_json()
                config = self.config_store.set_telegram_config(
                    dict(payload), preserve_secret=True
                )
                return self.send_json(public_telegram_config(config))
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
        if not self.require_csrf():
            return
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
        secure = "; Secure" if self.server.cookie_secure else ""  # type: ignore[attr-defined]
        return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}{secure}"

    def require_csrf(self) -> bool:
        session = self.auth.get_session(self.get_session_token())
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = str((session or {}).get("csrf_token") or "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self.send_error_text(HTTPStatus.FORBIDDEN, "invalid csrf token")
            return False
        return True

    def reject_oversized_request(self) -> bool:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return False
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error_text(HTTPStatus.BAD_REQUEST, "invalid content length")
            return True
        if length < 0:
            self.send_error_text(HTTPStatus.BAD_REQUEST, "invalid content length")
            return True
        if length > MAX_JSON_BODY_BYTES:
            self.send_error_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return True
        return False

    def require_auth(self) -> None:
        if not urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return self.redirect("/login")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"authentication required\n")

    def require_setup(self) -> None:
        if not urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return self.redirect("/setup")
        self.send_response(HTTPStatus.PRECONDITION_REQUIRED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"setup required\n")

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

    def send_text(
        self,
        body: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
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

    def send_error_text(
        self,
        status: HTTPStatus,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_text(message + "\n", status=status, headers=headers)


class DownloadServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        store: JobStore,
        config_store: ConfigStore,
        auth_manager: AuthManager,
        cookie_secure: bool = COOKIE_SECURE,
    ):
        super().__init__(address, handler)
        self.store = store
        self.config_store = config_store
        self.auth_manager = auth_manager
        self.cookie_secure = cookie_secure


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    config_store = ConfigStore(STATE_DIR)
    config_store.init()
    store = JobStore(STATE_DIR, config_store)
    store.init()
    auth_manager = AuthManager(config_store)
    stop_event = threading.Event()
    worker = DownloadWorker(store, stop_event)
    worker.start()

    httpd = DownloadServer(
        (host, port),
        RequestHandler,
        store,
        config_store,
        auth_manager,
    )

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
