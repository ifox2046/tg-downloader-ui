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


def parse_proxy_url(value: str) -> dict[str, object] | None:
    """Parse a proxy URL into the dict form Telethon expects.

    Must keep username/password; a bare (scheme, host, port) tuple causes
    authenticated HTTP proxies to reject the client with 407.
    """
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme not in {"socks4", "socks5", "http"}
        or not parsed.hostname
        or not parsed.port
    ):
        raise ValueError(f"invalid proxy url: {value}")
    username = urllib.parse.unquote(parsed.username) if parsed.username else None
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    return {
        "proxy_type": parsed.scheme,
        "addr": parsed.hostname,
        "port": int(parsed.port),
        "username": username,
        "password": password,
        "rdns": True,
    }


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


MIB = 1024 * 1024

DEFAULT_FORWARDER_FILTERS: dict[str, Any] = {
    "media_video": True,
    "media_photo": False,
    "media_document": False,
    "require_text": True,
    "min_size_bytes": 0,
    "max_size_bytes": 0,
    "include_keywords": [],
    "exclude_keywords": [],
}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_non_negative_int(value: Any, field: str) -> int:
    if value is None or value == "":
        return 0
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _as_keyword_list(value: Any, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items: list[str] = []
        for chunk in value.replace("\r", "\n").split("\n"):
            for part in chunk.split(","):
                text = part.strip()
                if text:
                    items.append(text)
        return items
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} must be a list of strings")
        text = item.strip()
        if text:
            items.append(text)
    return items


def normalize_forwarder_filters(raw: Any = None) -> dict[str, Any]:
    """Normalize config/API filter payload to the stored shape."""
    data = raw if isinstance(raw, dict) else {}
    filters = {
        "media_video": _as_bool(
            data.get("media_video"), DEFAULT_FORWARDER_FILTERS["media_video"]
        ),
        "media_photo": _as_bool(
            data.get("media_photo"), DEFAULT_FORWARDER_FILTERS["media_photo"]
        ),
        "media_document": _as_bool(
            data.get("media_document"), DEFAULT_FORWARDER_FILTERS["media_document"]
        ),
        "require_text": _as_bool(
            data.get("require_text"), DEFAULT_FORWARDER_FILTERS["require_text"]
        ),
        "min_size_bytes": 0,
        "max_size_bytes": 0,
        "include_keywords": _as_keyword_list(
            data.get("include_keywords"), "include_keywords"
        ),
        "exclude_keywords": _as_keyword_list(
            data.get("exclude_keywords"), "exclude_keywords"
        ),
    }

    if "min_size_bytes" in data or "max_size_bytes" in data:
        filters["min_size_bytes"] = _as_non_negative_int(
            data.get("min_size_bytes"), "min_size_bytes"
        )
        filters["max_size_bytes"] = _as_non_negative_int(
            data.get("max_size_bytes"), "max_size_bytes"
        )
    else:
        min_mib = data.get("min_size_mib")
        max_mib = data.get("max_size_mib")
        if min_mib is not None and min_mib != "":
            try:
                min_val = float(min_mib)
            except (TypeError, ValueError) as exc:
                raise ValueError("min_size_mib must be a non-negative number") from exc
            if min_val < 0:
                raise ValueError("min_size_mib must be a non-negative number")
            filters["min_size_bytes"] = int(min_val * MIB)
        if max_mib is not None and max_mib != "":
            try:
                max_val = float(max_mib)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_size_mib must be a non-negative number") from exc
            if max_val < 0:
                raise ValueError("max_size_mib must be a non-negative number")
            filters["max_size_bytes"] = int(max_val * MIB)

    min_size = filters["min_size_bytes"]
    max_size = filters["max_size_bytes"]
    if min_size > 0 and max_size > 0 and max_size < min_size:
        raise ValueError("max_size_bytes must be >= min_size_bytes when both are set")
    return filters


def forwarder_filters_for_api(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_forwarder_filters(filters)
    return {
        **normalized,
        "min_size_mib": (
            normalized["min_size_bytes"] / MIB if normalized["min_size_bytes"] else 0
        ),
        "max_size_mib": (
            normalized["max_size_bytes"] / MIB if normalized["max_size_bytes"] else 0
        ),
    }


def load_forwarder_filters(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return normalize_forwarder_filters()
    except (OSError, json.JSONDecodeError):
        return normalize_forwarder_filters()
    try:
        return normalize_forwarder_filters(data.get("forwarder_filters"))
    except ValueError:
        return normalize_forwarder_filters()


def is_video_document(doc: Any) -> bool:
    mime_type = str(getattr(doc, "mime_type", "") or "").lower()
    if mime_type.startswith("video/"):
        return True
    for attr in getattr(doc, "attributes", []) or []:
        if "video" in type(attr).__name__.lower():
            return True
    return False


def document_from_message(message: Any) -> Any | None:
    media = getattr(message, "media", None)
    doc = getattr(media, "document", None) if media is not None else None
    doc = doc or getattr(message, "document", None)
    if doc:
        return doc
    video = getattr(message, "video", None)
    if video:
        return video
    return None


def video_document_from_message(message: Any) -> Any | None:
    doc = document_from_message(message)
    if doc and is_video_document(doc):
        return doc
    return None


def media_kind_from_message(message: Any) -> str | None:
    """Classify message media as video | photo | document | other | None."""
    photo = getattr(message, "photo", None)
    media = getattr(message, "media", None)
    media_name = type(media).__name__.lower() if media is not None else ""
    has_photo = bool(photo) or (
        media is not None
        and ("photo" in media_name or getattr(media, "photo", None) is not None)
        and getattr(media, "document", None) is None
        and getattr(message, "document", None) is None
        and getattr(message, "video", None) is None
    )
    doc = document_from_message(message)
    if doc is not None:
        if is_video_document(doc):
            return "video"
        return "document"
    if has_photo:
        return "photo"
    if media is not None:
        return "other"
    return None


def message_text_for_filter(message: Any) -> str:
    text = str(getattr(message, "text", "") or "").strip()
    caption = str(getattr(message, "caption", "") or "").strip()
    if text and caption and caption != text:
        return f"{text}\n{caption}"
    return text or caption


def message_filename(message: Any) -> str:
    doc = document_from_message(message)
    if doc is not None:
        for attr in getattr(doc, "attributes", []) or []:
            value = getattr(attr, "file_name", None)
            if value:
                return str(value)
    return ""


def message_size_bytes(message: Any) -> int:
    doc = document_from_message(message)
    if doc is not None:
        return int(getattr(doc, "size", 0) or 0)
    photo = getattr(message, "photo", None)
    if photo is not None:
        size = getattr(photo, "size", None)
        if size is not None:
            return int(size or 0)
        sizes = getattr(photo, "sizes", None) or []
        best = 0
        for item in sizes:
            best = max(best, int(getattr(item, "size", 0) or 0))
        return best
    media = getattr(message, "media", None)
    if media is not None:
        size = getattr(media, "size", None)
        if size is not None:
            return int(size or 0)
    return 0


def evaluate_forwarder_filters(
    message: Any, filters: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """Return (ok, skip_reason). Empty skip_reason means accepted."""
    cfg = normalize_forwarder_filters(filters)
    kind = media_kind_from_message(message)
    if kind is None:
        return False, "no_media"
    if kind == "video" and not cfg["media_video"]:
        return False, "media_video_disabled"
    if kind == "photo" and not cfg["media_photo"]:
        return False, "media_photo_disabled"
    if kind == "document" and not cfg["media_document"]:
        return False, "media_document_disabled"
    if kind == "other":
        return False, "media_kind_unsupported"

    size = message_size_bytes(message)
    min_size = int(cfg["min_size_bytes"] or 0)
    max_size = int(cfg["max_size_bytes"] or 0)
    if min_size > 0 and size < min_size:
        return False, "below_min_size"
    if max_size > 0 and size > max_size:
        return False, "above_max_size"

    text = message_text_for_filter(message)
    if cfg["require_text"] and not text:
        return False, "require_text"

    folded = text.casefold()
    exclude = [str(item).casefold() for item in cfg["exclude_keywords"]]
    for keyword in exclude:
        if keyword and keyword in folded:
            return False, "exclude_keyword"
    include = [str(item).casefold() for item in cfg["include_keywords"]]
    if include and not any(keyword in folded for keyword in include if keyword):
        return False, "include_keyword"

    return True, ""


def format_forward_message(
    message: Any,
    source_label: str = "",
    filters: dict[str, Any] | None = None,
) -> str:
    ok, _reason = evaluate_forwarder_filters(message, filters)
    if not ok:
        return ""

    body_parts: list[str] = []
    text = str(getattr(message, "text", "") or "").strip()
    if text:
        body_parts.append(text)

    caption = str(getattr(message, "caption", "") or "").strip()
    if caption and caption != text:
        body_parts.append(caption)

    filename = message_filename(message)
    size = message_size_bytes(message)
    meta = (
        f"File: {filename}\nSize: {human_size(size)}\nMessage ID: {getattr(message, 'id', '')}"
    )

    parts: list[str] = []
    if source_label:
        parts.append(f"Source: {source_label}")
    if body_parts:
        parts.append("\n".join(body_parts))
        parts.append("")  # blank line before technical meta
    parts.append(meta)
    return "\n".join(parts)


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
    filters = load_forwarder_filters()
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
    log_line(
        "FILTERS: "
        f"video={filters['media_video']} photo={filters['media_photo']} "
        f"document={filters['media_document']} require_text={filters['require_text']} "
        f"min={filters['min_size_bytes']} max={filters['max_size_bytes']}"
    )
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
        ok, skip_reason = evaluate_forwarder_filters(event.message, filters)
        write_status(
            state="running",
            channel_id=runtime["channel_id"],
            channel_title=getattr(channel, "title", ""),
            sent_count=sent_count,
            last_source=source_label,
            last_event_at=utcish_now(),
            last_error="",
        )
        if not ok:
            log_line(f"SKIP: {skip_reason} source={source_label} id={getattr(event.message, 'id', '')}")
            return
        info = format_forward_message(
            event.message, source_label=source_label, filters=filters
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
