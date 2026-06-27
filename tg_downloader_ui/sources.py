"""Shared source configuration helpers for tg-downloader-ui."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_ID = "youxiu_bot"
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {
        "id": "youxiu_bot",
        "label": "Youxiu Bot",
        "chat": "Youxiu_bot",
        "forward_source": "@Youxiu_bot",
        "enabled": True,
    },
    {
        "id": "youyou0_bot",
        "label": "Youyou0 Bot",
        "chat": "youyou0_bot",
        "forward_source": "@youyou0_bot",
        "enabled": True,
    },
]


def source_id_from(value: str, fallback: str) -> str:
    text = str(value or "").strip().lstrip("@").lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    return text or fallback


def normalize_source(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    fallback = f"source_{index + 1}"
    chat = str(raw.get("chat") or raw.get("source_chat") or raw.get("tdl_chat") or "").strip()
    forward_source = str(raw.get("forward_source") or "").strip()
    if not chat and forward_source:
        chat = forward_source.lstrip("@")
    if not forward_source and chat:
        forward_source = "@" + chat.lstrip("@")
    source_id = source_id_from(str(raw.get("id") or chat or forward_source), fallback)
    label = str(raw.get("label") or raw.get("name") or source_id).strip() or source_id
    enabled = bool(raw.get("enabled", True))

    if not chat:
        raise ValueError("source chat is required")
    if not forward_source:
        raise ValueError("source forward_source is required")

    return {
        "id": source_id,
        "label": label,
        "chat": chat.lstrip("@"),
        "forward_source": forward_source,
        "enabled": enabled,
    }


def normalize_sources(
    value: Any,
    default_source_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    raw_sources = value if isinstance(value, list) and value else DEFAULT_SOURCES
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise ValueError("source must be an object")
        source = normalize_source(raw, index=index)
        if source["id"] in seen:
            raise ValueError(f"duplicate source id: {source['id']}")
        seen.add(str(source["id"]))
        sources.append(source)

    if not sources:
        raise ValueError("at least one source is required")

    requested_default = source_id_from(str(default_source_id or ""), "")
    enabled_ids = {str(source["id"]) for source in sources if source.get("enabled", True)}
    all_ids = {str(source["id"]) for source in sources}
    if requested_default in enabled_ids:
        selected_default = requested_default
    elif DEFAULT_SOURCE_ID in enabled_ids:
        selected_default = DEFAULT_SOURCE_ID
    elif enabled_ids:
        selected_default = next(source["id"] for source in sources if source["id"] in enabled_ids)
    elif requested_default in all_ids:
        selected_default = requested_default
    else:
        selected_default = str(sources[0]["id"])

    return sources, selected_default


def read_sources_from_config(config_path: Path) -> tuple[list[dict[str, Any]], str]:
    if not config_path.exists():
        return normalize_sources(DEFAULT_SOURCES, DEFAULT_SOURCE_ID)
    data = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    return normalize_sources(data.get("sources"), data.get("default_source_id"))
