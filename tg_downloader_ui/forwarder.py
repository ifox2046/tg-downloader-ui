#!/usr/bin/env python3
"""Telegram message forwarder for tg-downloader-ui."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:  # Package import locally, flat import on OpenWRT deployment.
    from .sources import read_sources_from_config
except ImportError:  # pragma: no cover - exercised by OpenWRT flat deployment
    from sources import read_sources_from_config


STATE_DIR = Path(
    os.environ.get("TGDL_STATE_DIR", str(Path.home() / ".local/state/tg-downloader-ui"))
)
CONFIG_PATH = STATE_DIR / "config.json"
LOG_PATH = Path(os.environ.get("TGDL_FORWARDER_LOG", str(STATE_DIR / "forwarder.log")))
STATUS_PATH = Path(
    os.environ.get("TGDL_FORWARDER_STATUS", str(STATE_DIR / "forwarder_status.json"))
)
API_ID = os.environ.get("TGDL_API_ID", "")
API_HASH = os.environ.get("TGDL_API_HASH", "")
SESSION_FILE = Path(os.environ.get("TGDL_SESSION_FILE", str(STATE_DIR / "session.txt")))
PROXY_URL = os.environ.get("TGDL_TELEGRAM_PROXY", os.environ.get("TGDL_PROXY", ""))
SOURCE = os.environ.get("TGDL_FORWARD_SOURCE", "")
CHANNEL_ID = os.environ.get("TGDL_FORWARD_CHANNEL_ID", "")


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


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def parse_proxy_url(value: str) -> tuple[str, str, int] | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError(f"invalid proxy url: {value}")
    return (parsed.scheme, parsed.hostname, int(parsed.port))


def normalize_forward_channel_id(value: str) -> int:
    text = str(value or "").strip()
    try:
        parsed = int(text)
    except ValueError as exc:
        raise RuntimeError("TGDL_FORWARD_CHANNEL_ID must be an integer") from exc
    if parsed > 0:
        digits = text.lstrip("+")
        if digits.startswith("100") and len(digits) > 10:
            return -parsed
        return int(f"-100{digits}")
    return parsed


def validate_runtime_config(
    api_id: str = API_ID,
    api_hash: str = API_HASH,
    channel_id: str = CHANNEL_ID,
) -> tuple[int, str, int]:
    api_id_text = str(api_id or "").strip()
    api_hash_text = str(api_hash or "").strip()
    channel_id_text = str(channel_id or "").strip()
    if not api_id_text:
        raise RuntimeError("TGDL_API_ID is required")
    if not api_hash_text:
        raise RuntimeError("TGDL_API_HASH is required")
    if not channel_id_text:
        raise RuntimeError("TGDL_FORWARD_CHANNEL_ID is required")
    try:
        parsed_api_id = int(api_id_text)
    except ValueError as exc:
        raise RuntimeError("TGDL_API_ID must be an integer") from exc
    parsed_channel_id = normalize_forward_channel_id(channel_id_text)
    return parsed_api_id, api_hash_text, parsed_channel_id


def load_telegram_config(config_path: Path = CONFIG_PATH) -> dict[str, str]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return {}
    telegram = data.get("telegram") or {}
    return {
        "api_id": str(telegram.get("api_id") or ""),
        "api_hash": str(telegram.get("api_hash") or ""),
        "session_file": str(telegram.get("session_file") or ""),
        "proxy": str(telegram.get("proxy") or ""),
        "forward_channel_id": str(telegram.get("forward_channel_id") or ""),
    }


def resolve_runtime_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    telegram = load_telegram_config(config_path)
    api_id, api_hash, channel_id = validate_runtime_config(
        api_id=os.environ.get("TGDL_API_ID") or telegram.get("api_id") or "",
        api_hash=os.environ.get("TGDL_API_HASH") or telegram.get("api_hash") or "",
        channel_id=(
            os.environ.get("TGDL_FORWARD_CHANNEL_ID")
            or telegram.get("forward_channel_id")
            or ""
        ),
    )
    session_file = Path(
        os.environ.get("TGDL_SESSION_FILE")
        or telegram.get("session_file")
        or str(STATE_DIR / "session.txt")
    )
    proxy = (
        os.environ.get("TGDL_TELEGRAM_PROXY")
        or os.environ.get("TGDL_PROXY")
        or telegram.get("proxy")
        or ""
    )
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "channel_id": channel_id,
        "session_file": session_file,
        "proxy": proxy,
    }


def load_forward_sources(config_path: Path = CONFIG_PATH) -> list[dict[str, Any]]:
    try:
        sources, _ = read_sources_from_config(config_path)
    except Exception:
        sources = [
            {
                "id": SOURCE.strip("@").lower() or "source",
                "label": SOURCE,
                "chat": SOURCE.strip("@"),
                "forward_source": SOURCE,
                "enabled": True,
            }
        ]
    enabled = [
        source
        for source in sources
        if source.get("enabled", True) and str(source.get("forward_source") or "").strip()
    ]
    return enabled


def source_label_for_sender(sender: Any, sources: list[dict[str, Any]]) -> str:
    username = str(getattr(sender, "username", "") or "").lstrip("@").lower()
    for source in sources:
        forward_source = str(source.get("forward_source") or "").lstrip("@").lower()
        chat = str(source.get("chat") or "").lstrip("@").lower()
        if username and username in {forward_source, chat}:
            return str(source.get("label") or source.get("id") or username)
    return ""


def is_video_document(doc: Any) -> bool:
    mime_type = str(getattr(doc, "mime_type", "") or "").lower()
    if mime_type.startswith("video/"):
        return True
    for attr in getattr(doc, "attributes", []) or []:
        if "video" in type(attr).__name__.lower():
            return True
    return False


def video_document_from_message(message: Any) -> Any | None:
    media = getattr(message, "media", None)
    doc = getattr(media, "document", None) or getattr(message, "document", None)
    if doc and is_video_document(doc):
        return doc
    video = getattr(message, "video", None)
    if video and is_video_document(video):
        return video
    return None


def format_forward_message(message: Any, source_label: str = "") -> str:
    doc = video_document_from_message(message)
    if not doc:
        return ""

    parts: list[str] = []
    text = getattr(message, "text", "")
    if text:
        parts.append(str(text))

    caption = getattr(message, "caption", "")
    if caption and caption != text:
        parts.append(str(caption))

    if not parts:
        return ""

    filename = ""
    for attr in getattr(doc, "attributes", []) or []:
        value = getattr(attr, "file_name", None)
        if value:
            filename = str(value)
            break
    size = int(getattr(doc, "size", 0) or 0)
    parts.append(
        f"\n\n文件: {filename}\n大小: {human_size(size)}\n消息ID: {getattr(message, 'id', '')}"
    )
    if source_label and parts:
        parts.insert(0, f"Source: {source_label}")
    return "\n".join(part for part in parts if part)


def log_line(text: str, log_path: Path = LOG_PATH) -> None:
    ensure_private_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"{utcish_now()} {text}\n")
    ensure_private_file(log_path)


def write_status_file(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ensure_private_file(tmp_path)
    tmp_path.replace(path)
    ensure_private_file(path)


def write_status(**fields: Any) -> None:
    sources = load_forward_sources()
    payload = {
        "updated_at": utcish_now(),
        "updated_at_epoch": time.time(),
        "source": ", ".join(str(source.get("forward_source") or "") for source in sources),
        "sources": [
            {
                "id": source.get("id"),
                "label": source.get("label"),
                "forward_source": source.get("forward_source"),
            }
            for source in sources
        ],
        "source_count": len(sources),
        "channel_id": CHANNEL_ID,
        **fields,
    }
    write_status_file(STATUS_PATH, payload)


def read_status(
    path: Path = STATUS_PATH,
    now_epoch: float | None = None,
    stale_seconds: int = 90,
) -> dict[str, Any]:
    if not path.exists():
        return {"state": "missing"}
    payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    updated = float(payload.get("updated_at_epoch") or 0)
    now = time.time() if now_epoch is None else now_epoch
    if payload.get("state") == "running" and now - updated > stale_seconds:
        payload["state"] = "stale"
    return payload


async def amain() -> int:
    runtime = resolve_runtime_config()
    session_file = runtime["session_file"]
    if not session_file.exists():
        raise RuntimeError(f"session file not found: {session_file}")

    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    session_str = session_file.read_text(encoding="utf-8").strip()
    client = TelegramClient(
        StringSession(session_str),
        runtime["api_id"],
        runtime["api_hash"],
        proxy=parse_proxy_url(runtime["proxy"]),
    )
    sources = load_forward_sources()
    forward_users = [str(source["forward_source"]) for source in sources]
    if not forward_users:
        raise RuntimeError("no enabled forward sources configured")
    sent_count = 0
    write_status(
        state="starting",
        channel_id=runtime["channel_id"],
        sent_count=sent_count,
        last_error="",
    )
    await client.start()

    me = await client.get_me()
    channel = await client.get_entity(runtime["channel_id"])
    log_line(f"USER={getattr(me, 'first_name', '')}(@{getattr(me, 'username', '') or ''})")
    log_line(f"CHANNEL={getattr(channel, 'title', runtime['channel_id'])}")
    write_status(
        state="running",
        channel_id=runtime["channel_id"],
        channel_title=getattr(channel, "title", ""),
        sent_count=sent_count,
        last_error="",
    )

    @client.on(events.NewMessage(from_users=forward_users))
    async def handler(event: Any) -> None:
        nonlocal sent_count
        sender = await event.get_sender()
        source_label = source_label_for_sender(sender, sources) or str(
            getattr(sender, "username", "") or ""
        )
        info = format_forward_message(event.message, source_label=source_label)
        write_status(
            state="running",
            channel_id=runtime["channel_id"],
            channel_title=getattr(channel, "title", ""),
            sent_count=sent_count,
            last_source=source_label,
            last_event_at=utcish_now(),
            last_error="",
        )
        if not info:
            return
        try:
            await client.send_message(channel, info)
            sent_count += 1
            preview = info.splitlines()[0][:80] if info.splitlines() else ""
            log_line(f"SENT_OK: {preview}")
            write_status(
                state="running",
                channel_id=runtime["channel_id"],
                channel_title=getattr(channel, "title", ""),
                sent_count=sent_count,
                last_source=source_label,
                last_forward_at=utcish_now(),
                last_error="",
            )
        except Exception as exc:  # noqa: BLE001 - event boundary
            log_line(f"SENT_ERR: {exc}")
            write_status(
                state="running",
                channel_id=runtime["channel_id"],
                channel_title=getattr(channel, "title", ""),
                sent_count=sent_count,
                last_source=source_label,
                last_error=str(exc),
            )

    log_line(
        f"LISTENING: {', '.join(forward_users)} -> {getattr(channel, 'title', runtime['channel_id'])}"
    )
    await client.run_until_disconnected()
    write_status(state="stopped", channel_id=runtime["channel_id"], sent_count=sent_count)
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except Exception as exc:  # noqa: BLE001 - service boundary
        log_line(f"FAILED: {exc}")
        write_status(state="failed", last_error=str(exc))
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
