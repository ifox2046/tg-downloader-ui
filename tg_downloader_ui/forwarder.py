#!/usr/bin/env python3
"""Telegram message forwarder for tg-downloader-ui."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("TGDL_STATE_DIR", "/mnt/sata1-5/tg-downloader-ui"))
LOG_PATH = Path(os.environ.get("TGDL_FORWARDER_LOG", str(STATE_DIR / "forwarder.log")))
STATUS_PATH = Path(
    os.environ.get("TGDL_FORWARDER_STATUS", str(STATE_DIR / "forwarder_status.json"))
)
API_ID = int(os.environ.get("TGDL_API_ID", "26375241"))
API_HASH = os.environ.get("TGDL_API_HASH", "")
SESSION_FILE = Path(os.environ.get("TGDL_SESSION_FILE", "/opt/tg_session.txt"))
PROXY_URL = os.environ.get("TGDL_PROXY", "socks5://127.0.0.1:7891")
SOURCE = os.environ.get("TGDL_FORWARD_SOURCE", "@Youxiu_bot")
CHANNEL_ID = int(os.environ.get("TGDL_FORWARD_CHANNEL_ID", "-1004496489706"))


def utcish_now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


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


def format_forward_message(message: Any) -> str:
    parts: list[str] = []
    text = getattr(message, "text", "")
    if text:
        parts.append(str(text))

    media = getattr(message, "media", None)
    caption = getattr(message, "caption", "")
    if media and caption and caption != text:
        parts.append(str(caption))

    if media:
        doc = getattr(media, "document", None)
        if doc:
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
    return "\n".join(part for part in parts if part)


def log_line(text: str, log_path: Path = LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"{utcish_now()} {text}\n")


def write_status(**fields: Any) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": utcish_now(),
        "updated_at_epoch": time.time(),
        "source": SOURCE,
        "channel_id": CHANNEL_ID,
        **fields,
    }
    tmp_path = STATUS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(STATUS_PATH)


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
    if not API_HASH:
        raise RuntimeError("TGDL_API_HASH is required")
    if not SESSION_FILE.exists():
        raise RuntimeError(f"session file not found: {SESSION_FILE}")

    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    session_str = SESSION_FILE.read_text(encoding="utf-8").strip()
    client = TelegramClient(
        StringSession(session_str),
        API_ID,
        API_HASH,
        proxy=parse_proxy_url(PROXY_URL),
    )
    sent_count = 0
    write_status(state="starting", sent_count=sent_count, last_error="")
    await client.start()

    me = await client.get_me()
    channel = await client.get_entity(CHANNEL_ID)
    log_line(f"USER={getattr(me, 'first_name', '')}(@{getattr(me, 'username', '') or ''})")
    log_line(f"CHANNEL={getattr(channel, 'title', CHANNEL_ID)}")
    write_status(
        state="running",
        channel_title=getattr(channel, "title", ""),
        sent_count=sent_count,
        last_error="",
    )

    @client.on(events.NewMessage(from_users=SOURCE))
    async def handler(event: Any) -> None:
        nonlocal sent_count
        info = format_forward_message(event.message)
        write_status(
            state="running",
            channel_title=getattr(channel, "title", ""),
            sent_count=sent_count,
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
                channel_title=getattr(channel, "title", ""),
                sent_count=sent_count,
                last_forward_at=utcish_now(),
                last_error="",
            )
        except Exception as exc:  # noqa: BLE001 - event boundary
            log_line(f"SENT_ERR: {exc}")
            write_status(
                state="running",
                channel_title=getattr(channel, "title", ""),
                sent_count=sent_count,
                last_error=str(exc),
            )

    log_line(f"LISTENING: {SOURCE} -> {getattr(channel, 'title', CHANNEL_ID)}")
    await client.run_until_disconnected()
    write_status(state="stopped", sent_count=sent_count)
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
