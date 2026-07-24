"""Telegram Bot API control console for tg-downloader-ui.

In-process long-poll bot: private DM only, reuses JobStore for downloads.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# MVP defaults (prd R14b)
HEALTH_INTERVAL_SECONDS = 300
HEALTH_FAIL_THRESHOLD = 2
PENDING_SOURCE_TTL_SECONDS = 300
TERMINAL_STATUSES = frozenset({"done", "failed", "canceled", "skipped"})
API_ERROR_THRESHOLD = 3
STOP_NOTIFY_TIMEOUT = 3.0

STATUS_LABELS = {
    "zh": {
        "done": "已完成",
        "failed": "失败",
        "canceled": "已取消",
        "skipped": "已跳过",
        "queued": "排队中",
        "exporting": "导出中",
        "downloading": "下载中",
        "renaming": "重命名中",
        "paused": "已暂停",
    },
    "en": {
        "done": "done",
        "failed": "failed",
        "canceled": "canceled",
        "skipped": "skipped",
        "queued": "queued",
        "exporting": "exporting",
        "downloading": "downloading",
        "renaming": "renaming",
        "paused": "paused",
    },
}

MODE_LABELS = {
    "zh": {"message_id": "消息 ID", "url": "链接", "export": "导出"},
    "en": {"message_id": "message ID", "url": "URL", "export": "export"},
}

ERROR_LABELS = {
    "zh": {
        "canceled by user": "用户取消",
        "cancel requested": "已请求取消",
        "pause requested": "已请求暂停",
        "job not found": "任务不存在",
        "source not found": "来源不存在",
        "source is disabled": "来源已禁用",
        "only queued or active jobs can be canceled": "只能取消排队中或进行中的任务",
        "only failed or canceled jobs can be retried": "只能重试失败或已取消的任务",
        "invalid message id": "消息 ID 无效",
        "no message ids provided": "未提供消息 ID",
        "no urls provided": "未提供链接",
        "invalid telegram url": "Telegram 链接无效",
        "invalid mode": "模式无效",
        "duplicate active job": "已有进行中/排队任务",
    },
    "en": {
        "canceled by user": "canceled by user",
        "cancel requested": "cancel requested",
        "pause requested": "pause requested",
        "job not found": "job not found",
        "source not found": "source not found",
        "source is disabled": "source is disabled",
        "only queued or active jobs can be canceled": "only queued or active jobs can be canceled",
        "only failed or canceled jobs can be retried": "only failed or canceled jobs can be retried",
        "invalid message id": "invalid message id",
        "no message ids provided": "no message ids provided",
        "no urls provided": "no urls provided",
        "invalid telegram url": "invalid telegram url",
        "invalid mode": "invalid mode",
        "duplicate active job": "duplicate active job",
    },
}

MESSAGES = {
    "zh": {
        "help": (
            "TG 下载控制 Bot\n"
            "• 发送 t.me 链接，或 /dl <url>\n"
            "• 发送消息 ID，或 /dl <id>（多来源时选源）\n"
            "• /jobs 最近任务\n"
            "• /status <id> 查询\n"
            "• /cancel <id> 取消（非暂停）\n"
            "• /help 帮助\n"
            "命令固定为英文；提示文案跟随 Web 语言设置。仅私聊；与 Web 转发器并存。"
        ),
        "online": "服务/Bot 已上线",
        "stopping": "服务即将停止（优雅退出）",
        "api_restored": "Bot 曾失联 Telegram API，已恢复",
        "backend_down": "后端异常/不可用（健康探测失败）",
        "backend_restored": "后端已恢复",
        "backend_url_init": "后端未正确初始化 URL 解析",
        "backend_unavailable": "后端不可用：{detail}",
        "unknown_cmd": "无法识别。发送 /help 查看用法。",
        "no_jobs": "暂无任务",
        "status_usage": "用法：/status <job_id>",
        "job_missing": "任务 #{id} 不存在",
        "cancel_usage": "用法：/cancel <job_id>",
        "op_failed": "操作失败：{detail}",
        "cancel_ok": "已请求取消 #{id}（{status}）",
        "url_queued": "已入队链接任务 #{id}（{n} 条链接）",
        "no_sources": "没有启用的来源，请先在 Web「资源来源」配置",
        "pick_source": "消息 {id}：请选择来源",
        "msg_queued": "已入队消息任务 #{id}\n消息 ID：{message_id}\n来源：{source}",
        "pick_expired": "选择已过期，请重新发送消息 ID",
        "unknown_action": "未知操作",
        "picked": "已选择",
        "job_line": "#{id} {status} {mode} {title}",
        "status_body": (
            "任务 #{id} {status}\n"
            "模式：{mode}\n"
            "来源：{source}\n"
            "进度：{progress}  速度：{speed}"
        ),
        "status_error": "原因：{error}",
        "terminal_head": "任务 #{id} {status}",
        "title": "片名：{v}",
        "file": "文件：{v}",
        "path": "路径：{v}",
        "size": "大小：{v}",
        "msg_id_bit": "消息 ID {v}",
        "source_bit": "来源 {v}",
        "mode_bit": "模式 {v}",
        "reason": "原因：{v}",
    },
    "en": {
        "help": (
            "TG download control bot\n"
            "• Send a t.me link, or /dl <url>\n"
            "• Send a message ID, or /dl <id> (pick source when multiple)\n"
            "• /jobs recent jobs\n"
            "• /status <id>\n"
            "• /cancel <id> (not pause)\n"
            "• /help\n"
            "Commands are English; reply language follows the Web UI language setting.\n"
            "Private chat only; complements the Web forwarder."
        ),
        "online": "Service/bot is online",
        "stopping": "Service is stopping (graceful shutdown)",
        "api_restored": "Bot lost Telegram API connectivity; restored",
        "backend_down": "Backend unhealthy (health probe failed)",
        "backend_restored": "Backend restored",
        "backend_url_init": "Backend URL parser is not initialized",
        "backend_unavailable": "Backend unavailable: {detail}",
        "unknown_cmd": "Unrecognized. Send /help for usage.",
        "no_jobs": "No jobs",
        "status_usage": "Usage: /status <job_id>",
        "job_missing": "Job #{id} not found",
        "cancel_usage": "Usage: /cancel <job_id>",
        "op_failed": "Operation failed: {detail}",
        "cancel_ok": "Cancel requested for #{id} ({status})",
        "url_queued": "Queued URL job #{id} ({n} link(s))",
        "no_sources": "No enabled sources; configure them in the Web UI first",
        "pick_source": "Message {id}: pick a source",
        "msg_queued": "Queued message job #{id}\nMessage ID: {message_id}\nSource: {source}",
        "pick_expired": "Selection expired; send the message ID again",
        "unknown_action": "Unknown action",
        "picked": "Selected",
        "job_line": "#{id} {status} {mode} {title}",
        "status_body": (
            "Job #{id} {status}\n"
            "Mode: {mode}\n"
            "Source: {source}\n"
            "Progress: {progress}  Speed: {speed}"
        ),
        "status_error": "Error: {error}",
        "terminal_head": "Job #{id} {status}",
        "title": "Title: {v}",
        "file": "File: {v}",
        "path": "Path: {v}",
        "size": "Size: {v}",
        "msg_id_bit": "message ID {v}",
        "source_bit": "source {v}",
        "mode_bit": "mode {v}",
        "reason": "Reason: {v}",
    },
}


def normalize_lang(value: Any = None) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("en"):
        return "en"
    return "zh"


def t(key: str, lang: str = "zh", **kwargs: Any) -> str:
    pack = MESSAGES.get(normalize_lang(lang)) or MESSAGES["zh"]
    template = pack.get(key) or MESSAGES["zh"].get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template
    return template


def status_label(status: str, lang: str = "zh") -> str:
    key = str(status or "").strip()
    pack = STATUS_LABELS.get(normalize_lang(lang)) or STATUS_LABELS["zh"]
    fallback = "未知" if normalize_lang(lang) == "zh" else "unknown"
    return pack.get(key, key or fallback)


def status_label_zh(status: str) -> str:
    return status_label(status, "zh")


def mode_label(mode: str, lang: str = "zh") -> str:
    key = str(mode or "message_id").strip() or "message_id"
    pack = MODE_LABELS.get(normalize_lang(lang)) or MODE_LABELS["zh"]
    return pack.get(key, key)


def error_label(error: str, lang: str = "zh") -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    pack = ERROR_LABELS.get(normalize_lang(lang)) or ERROR_LABELS["zh"]
    if text in pack:
        return pack[text]
    # Prefix matches for dynamic errors (url validation, active dedupe, …)
    low = text.lower()
    for eng, zh in pack.items():
        if low.startswith(eng.lower()) and eng in {
            "invalid telegram url",
            "invalid mode",
            "duplicate active job",
        }:
            if normalize_lang(lang) == "zh":
                suffix = text[len(eng) :].strip()
                return f"{zh}{suffix}" if suffix else zh
            return text
    return text


def format_job_terminal_message(
    job_id: int,
    status: str,
    job: dict[str, Any] | None = None,
    error: str = "",
    lang: str = "zh",
) -> str:
    """Terminal notify: job id + localized status + file summary."""
    lang = normalize_lang(lang)
    label = status_label(status, lang)
    lines = [t("terminal_head", lang, id=job_id, status=label)]
    data = job if isinstance(job, dict) else {}
    title = str(data.get("title") or "").strip()
    source_file = str(data.get("source_file") or "").strip()
    final_filename = str(data.get("final_filename") or "").strip()
    final_path = str(data.get("final_path") or "").strip()
    mode = str(data.get("mode") or "message_id").strip() or "message_id"
    source_label = str(data.get("source_label") or "").strip()
    downloaded = str(data.get("downloaded") or "").strip()
    message_id = data.get("message_id")

    if title:
        lines.append(t("title", lang, v=title))
    if final_filename:
        lines.append(t("file", lang, v=final_filename))
    elif source_file:
        lines.append(t("file", lang, v=source_file))
    if final_path:
        lines.append(t("path", lang, v=final_path))
    if downloaded:
        lines.append(t("size", lang, v=downloaded))

    meta_bits: list[str] = []
    if mode == "message_id" and message_id:
        meta_bits.append(t("msg_id_bit", lang, v=message_id))
    if source_label and source_label not in {"url", "export"}:
        meta_bits.append(t("source_bit", lang, v=source_label))
    elif mode in {"url", "export"}:
        meta_bits.append(t("mode_bit", lang, v=mode_label(mode, lang)))
    if meta_bits:
        lines.append(" · ".join(meta_bits))

    err = error_label(error or str(data.get("error") or ""), lang)
    if err and status in {"failed", "canceled"}:
        lines.append(t("reason", lang, v=err[:500]))
    return "\n".join(lines)


def token_hint(token: str) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "••••"
    return f"••••{text[-4:]}"


def normalize_bot_config(raw: Any = None) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    notify_raw = data.get("notify_chat_id")
    notify_chat_id: int | None
    if notify_raw is None or notify_raw == "":
        notify_chat_id = None
    else:
        try:
            notify_chat_id = int(notify_raw)
        except (TypeError, ValueError):
            notify_chat_id = None
    return {
        "enabled": bool(data.get("enabled")),
        "token": str(data.get("token") or "").strip(),
        "notify_chat_id": notify_chat_id,
    }


def public_bot_config(
    config: dict[str, Any],
    *,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = str(config.get("token") or "")
    return {
        "enabled": bool(config.get("enabled")),
        "token_set": bool(token),
        "token_hint": token_hint(token),
        "notify_chat_id": config.get("notify_chat_id"),
        "status": status
        or {
            "running": False,
            "error": "",
            "last_update_at": "",
            "backend_healthy": True,
            "telegram_api_ok": True,
        },
    }


def is_private_chat(chat: dict[str, Any] | None) -> bool:
    if not isinstance(chat, dict):
        return False
    return str(chat.get("type") or "") == "private"


def looks_like_message_id(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value.isdigit() and int(value) > 0)


def strip_bot_command(text: str) -> str:
    """Normalize '/cmd@BotName args' → '/cmd args'."""
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return raw
    parts = raw.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    if len(parts) == 1:
        return cmd
    return f"{cmd} {parts[1]}"


@dataclass
class ParsedCommand:
    kind: str
    args: str = ""
    message_id: int | None = None
    urls: list[str] = field(default_factory=list)


def _match_cmd(raw: str, *names: str) -> tuple[bool, str]:
    """Return (matched, args) for command names like /help or /帮助."""
    text = str(raw or "").strip()
    if not text:
        return False, ""
    # Chinese commands may not use lowercase
    first = text.split(maxsplit=1)[0]
    first_norm = first.split("@", 1)[0]
    first_low = first_norm.lower()
    for name in names:
        name_low = name.lower()
        if first_low == name_low or first_norm == name:
            rest = text[len(first) :].strip()
            return True, rest
    return False, ""


def parse_operator_text(text: str, *, is_telegram_url: Callable[[str], bool]) -> ParsedCommand:
    """Parse operator input. Commands are English-only; bare t.me / message id still work."""
    raw = strip_bot_command(text)
    if not raw:
        return ParsedCommand(kind="empty")

    ok, rest = _match_cmd(raw, "/start", "/help")
    if ok:
        return ParsedCommand(kind="help")
    ok, rest = _match_cmd(raw, "/jobs")
    if ok:
        return ParsedCommand(kind="jobs")
    ok, rest = _match_cmd(raw, "/status")
    if ok:
        return ParsedCommand(kind="status", args=rest)
    ok, rest = _match_cmd(raw, "/cancel")
    if ok:
        return ParsedCommand(kind="cancel", args=rest)
    ok, rest = _match_cmd(raw, "/dl")
    if ok:
        if not rest:
            return ParsedCommand(kind="help")
        if is_telegram_url(rest):
            return ParsedCommand(kind="url", urls=[rest])
        if looks_like_message_id(rest):
            return ParsedCommand(kind="message_id", message_id=int(rest))
        return ParsedCommand(kind="unknown", args=rest)

    # bare line: link or message id (not commands)
    if is_telegram_url(raw):
        return ParsedCommand(kind="url", urls=[raw])
    if looks_like_message_id(raw):
        return ParsedCommand(kind="message_id", message_id=int(raw))
    return ParsedCommand(kind="unknown", args=raw)


def resolve_bot_proxy_url(config_store: Any | None = None) -> str:
    """Env first, then config.json telegram.proxy (same order as tdl/Telethon)."""
    for key in ("TGDL_TELEGRAM_PROXY", "TGDL_PROXY", "TGDL_TDL_PROXY"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    if config_store is not None:
        getter = getattr(config_store, "get_telegram_config", None)
        if callable(getter):
            try:
                return str((getter() or {}).get("proxy") or "").strip()
            except Exception:  # noqa: BLE001
                return ""
    return ""


def build_bot_opener(proxy_url: str | None = None) -> urllib.request.OpenerDirector:
    """Build urllib opener; honor http(s)/socks proxy used by the rest of the app."""
    text = str(proxy_url or "").strip()
    if not text:
        return urllib.request.build_opener()
    parsed = urllib.parse.urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"http", "https"} and parsed.hostname and parsed.port:
        # urllib ProxyHandler uses http for HTTPS CONNECT too when only https key is set.
        proxy_handler = urllib.request.ProxyHandler(
            {
                "http": text,
                "https": text,
            }
        )
        return urllib.request.build_opener(proxy_handler)
    if scheme in {"socks4", "socks5", "socks5h"} and parsed.hostname and parsed.port:
        try:
            import socks  # type: ignore
            from sockshandler import SocksiPyHandler  # type: ignore
        except Exception:
            # PySocks is a project dependency; fall back to direct if import fails.
            return urllib.request.build_opener()
        proxy_type = socks.SOCKS5 if "socks5" in scheme else socks.SOCKS4
        username = urllib.parse.unquote(parsed.username) if parsed.username else None
        password = urllib.parse.unquote(parsed.password) if parsed.password else None
        handler = SocksiPyHandler(
            proxy_type,
            parsed.hostname,
            int(parsed.port),
            username=username,
            password=password,
            rdns=True,
        )
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


class TelegramBotApi:
    """Minimal Bot API client via stdlib urllib."""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        opener: urllib.request.OpenerDirector | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.token = str(token or "").strip()
        self.timeout = timeout
        self.proxy_url = str(proxy_url or "").strip()
        self._opener = opener or build_bot_opener(self.proxy_url)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self._url(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram API network error: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Telegram API returned invalid JSON") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            desc = ""
            if isinstance(data, dict):
                desc = str(data.get("description") or data)
            raise RuntimeError(f"Telegram API error: {desc or raw[:200]}")
        return data.get("result")

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 25,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(timeout),
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        old = self.timeout
        self.timeout = max(self.timeout, float(timeout) + 10.0)
        try:
            result = self.call("getUpdates", payload)
        finally:
            self.timeout = old
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": str(text)[:4000],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict[str, Any]:
        result = self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": str(text)[:200]},
        )
        return result if isinstance(result, dict) else {}


class BotController:
    """Daemon control loop for the operator bot."""

    def __init__(
        self,
        store: Any,
        config_store: Any,
        *,
        stop_event: threading.Event,
        worker_pool: Any | None = None,
        is_telegram_url: Callable[[str], bool] | None = None,
        parse_urls: Callable[[Any], list[str]] | None = None,
        api_factory: Callable[[str], TelegramBotApi] | None = None,
        health_interval: float = HEALTH_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.config_store = config_store
        self.stop_event = stop_event
        self.worker_pool = worker_pool
        self._is_telegram_url = is_telegram_url
        self._parse_urls = parse_urls
        self._api_factory = api_factory or (
            lambda token: TelegramBotApi(
                token,
                proxy_url=resolve_bot_proxy_url(self.config_store),
            )
        )
        self.health_interval = float(health_interval)
        self._clock = clock or time.monotonic
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._offset = 0
        self._pending: dict[str, dict[str, Any]] = {}
        self._job_chats: dict[int, int] = {}
        self._notified_jobs: set[int] = set()
        self._api: TelegramBotApi | None = None
        self._running = False
        self._last_error = ""
        self._last_update_at = ""
        self._api_fail_count = 0
        self._api_degraded = False
        self._backend_fail_count = 0
        self._backend_degraded = False
        self._last_health_check = 0.0
        self._online_sent_for_token = ""
        self._pending_seq = 0
        self._api_proxy_url = ""

    def _lang(self) -> str:
        getter = getattr(self.config_store, "get_ui_lang", None)
        if callable(getter):
            try:
                return normalize_lang(getter())
            except Exception:  # noqa: BLE001
                return "zh"
        return "zh"

    def _msg(self, key: str, **kwargs: Any) -> str:
        return t(key, self._lang(), **kwargs)

    def _proxy_url(self) -> str:
        return resolve_bot_proxy_url(self.config_store)

    def _make_api(self, token: str, *, timeout: float | None = None) -> TelegramBotApi:
        if self._api_factory is not None:
            return self._api_factory(token)
        kwargs: dict[str, Any] = {"proxy_url": self._proxy_url()}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return TelegramBotApi(token, **kwargs)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running and self._thread is not None and self._thread.is_alive(),
                "error": self._last_error,
                "last_update_at": self._last_update_at,
                "backend_healthy": not self._backend_degraded,
                "telegram_api_ok": not self._api_degraded,
            }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop,
                name="tg-control-bot",
                daemon=True,
            )
            self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def reload(self) -> None:
        """Force API client rebuild on next loop iteration (token/enable/proxy change)."""
        with self._lock:
            self._api = None
            self._api_proxy_url = ""
            self._online_sent_for_token = ""

    def notify_job_finished(self, job_id: int, status: str, error: str = "") -> None:
        with self._lock:
            if job_id in self._notified_jobs:
                return
            chat_id = self._job_chats.get(job_id)
            if chat_id is None:
                cfg = normalize_bot_config(self.config_store.get_bot_config())
                chat_id = cfg.get("notify_chat_id")
            if chat_id is None:
                return
            self._notified_jobs.add(job_id)
        job = None
        try:
            job = self.store.get_job(job_id)
        except Exception:  # noqa: BLE001
            pass
        line = format_job_terminal_message(
            job_id, status, job, error=error, lang=self._lang()
        )
        self._send_best_effort(int(chat_id), line)

    def send_graceful_stop(self) -> None:
        cfg = normalize_bot_config(self.config_store.get_bot_config())
        if not cfg["enabled"] or not cfg["token"] or not cfg["notify_chat_id"]:
            return
        self._send_with_timeout(
            cfg["token"],
            int(cfg["notify_chat_id"]),
            self._msg("stopping"),
            timeout=STOP_NOTIFY_TIMEOUT,
        )

    def backend_healthy(self) -> bool:
        try:
            if self.worker_pool is not None:
                if getattr(self.worker_pool, "stop_event", None) is not None:
                    if self.worker_pool.stop_event.is_set():
                        return False
                # At least desired workers concept: desired_size > 0
                if hasattr(self.worker_pool, "desired_size"):
                    if int(self.worker_pool.desired_size()) < 1:
                        return False
            # JobStore openable
            self.store.list_jobs()
            self.config_store.get_bot_config()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            cfg = normalize_bot_config(self.config_store.get_bot_config())
            if not cfg["enabled"] or not cfg["token"]:
                with self._lock:
                    self._running = False
                    self._api = None
                self.stop_event.wait(2.0)
                continue
            proxy = self._proxy_url()
            with self._lock:
                self._running = True
                if (
                    self._api is None
                    or self._api.token != cfg["token"]
                    or self._api_proxy_url != proxy
                ):
                    self._api = self._make_api(cfg["token"])
                    self._api_proxy_url = proxy
            self._maybe_send_online(cfg)
            self._maybe_health_check(cfg)
            try:
                assert self._api is not None
                updates = self._fetch_updates(self._api)
                with self._lock:
                    self._api_fail_count = 0
                    if self._api_degraded:
                        self._api_degraded = False
                        if cfg.get("notify_chat_id"):
                            self._send_best_effort(
                                int(cfg["notify_chat_id"]),
                                self._msg("api_restored"),
                            )
                for update in updates:
                    self._handle_update(update, cfg)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error = str(exc)[:500]
                    self._api_fail_count += 1
                    if self._api_fail_count >= API_ERROR_THRESHOLD and not self._api_degraded:
                        self._api_degraded = True
                self.stop_event.wait(min(30.0, 2.0 * self._api_fail_count))
        with self._lock:
            self._running = False

    def _fetch_updates(self, api: TelegramBotApi) -> list[dict[str, Any]]:
        updates = api.get_updates(offset=self._offset, timeout=25)
        for update in updates:
            uid = int(update.get("update_id") or 0)
            if uid >= self._offset:
                self._offset = uid + 1
            with self._lock:
                self._last_update_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._last_error = ""
        return updates

    def _maybe_send_online(self, cfg: dict[str, Any]) -> None:
        token = cfg.get("token") or ""
        chat_id = cfg.get("notify_chat_id")
        if not chat_id or not token:
            return
        with self._lock:
            if self._online_sent_for_token == token:
                return
            self._online_sent_for_token = token
        self._send_best_effort(int(chat_id), self._msg("online"))

    def _maybe_health_check(self, cfg: dict[str, Any]) -> None:
        now = self._clock()
        if now - self._last_health_check < self.health_interval:
            return
        self._last_health_check = now
        healthy = self.backend_healthy()
        chat_id = cfg.get("notify_chat_id")
        if healthy:
            with self._lock:
                self._backend_fail_count = 0
                was = self._backend_degraded
                self._backend_degraded = False
            if was and chat_id:
                self._send_best_effort(int(chat_id), self._msg("backend_restored"))
            return
        with self._lock:
            self._backend_fail_count += 1
            fails = self._backend_fail_count
            already = self._backend_degraded
            if fails >= HEALTH_FAIL_THRESHOLD and not already:
                self._backend_degraded = True
                should_notify = True
            else:
                should_notify = False
        if should_notify and chat_id:
            self._send_best_effort(int(chat_id), self._msg("backend_down"))

    def _handle_update(self, update: dict[str, Any], cfg: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"], cfg)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not is_private_chat(chat if isinstance(chat, dict) else None):
            return
        chat_id = int(chat["id"])
        self._remember_notify_chat(chat_id)
        text = str(message.get("text") or "").strip()
        if not text:
            return
        self._dispatch_text(chat_id, text, cfg)

    def _remember_notify_chat(self, chat_id: int) -> None:
        try:
            self.config_store.set_bot_notify_chat_id(chat_id)
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_text(self, chat_id: int, text: str, cfg: dict[str, Any]) -> None:
        if self._is_telegram_url is None or self._parse_urls is None:
            self._reply(chat_id, self._msg("backend_url_init"))
            return
        try:
            if not self.backend_healthy():
                # still try commands but warn if store fails
                pass
            parsed = parse_operator_text(text, is_telegram_url=self._is_telegram_url)
            if parsed.kind == "help" or parsed.kind == "empty":
                self._reply(chat_id, self._msg("help"))
                return
            if parsed.kind == "jobs":
                self._cmd_jobs(chat_id)
                return
            if parsed.kind == "status":
                self._cmd_status(chat_id, parsed.args)
                return
            if parsed.kind == "cancel":
                self._cmd_cancel(chat_id, parsed.args)
                return
            if parsed.kind == "url":
                self._cmd_url(chat_id, parsed.urls)
                return
            if parsed.kind == "message_id" and parsed.message_id is not None:
                self._cmd_message_id(chat_id, parsed.message_id)
                return
            self._reply(chat_id, self._msg("unknown_cmd"))
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("backend_unavailable", detail=error_label(str(exc), self._lang())),
            )

    def _cmd_jobs(self, chat_id: int) -> None:
        lang = self._lang()
        try:
            jobs = self.store.list_jobs()[:10]
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("backend_unavailable", detail=error_label(str(exc), lang)),
            )
            return
        if not jobs:
            self._reply(chat_id, self._msg("no_jobs"))
            return
        lines = []
        for job in jobs:
            lines.append(
                self._msg(
                    "job_line",
                    id=job.get("id"),
                    status=status_label(str(job.get("status") or ""), lang),
                    mode=mode_label(str(job.get("mode") or "message_id"), lang),
                    title=str(job.get("title") or job.get("source_label") or ""),
                ).strip()
            )
        self._reply(chat_id, "\n".join(lines))

    def _cmd_status(self, chat_id: int, args: str) -> None:
        lang = self._lang()
        if not args.isdigit():
            self._reply(chat_id, self._msg("status_usage"))
            return
        try:
            job = self.store.get_job(int(args))
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("backend_unavailable", detail=error_label(str(exc), lang)),
            )
            return
        if not job:
            self._reply(chat_id, self._msg("job_missing", id=args))
            return
        err = error_label(str(job.get("error") or ""), lang)
        text = self._msg(
            "status_body",
            id=job["id"],
            status=status_label(str(job.get("status") or ""), lang),
            mode=mode_label(str(job.get("mode") or "message_id"), lang),
            source=str(job.get("source_label") or "-"),
            progress=job.get("progress"),
            speed=job.get("speed") or "-",
        )
        if err:
            text += "\n" + self._msg("status_error", error=err[:400])
        self._reply(chat_id, text)

    def _cmd_cancel(self, chat_id: int, args: str) -> None:
        lang = self._lang()
        if not args.isdigit():
            self._reply(chat_id, self._msg("cancel_usage"))
            return
        job_id = int(args)
        try:
            job = self.store.cancel_job(job_id)
        except ValueError as exc:
            self._reply(
                chat_id,
                self._msg("op_failed", detail=error_label(str(exc), lang)),
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("backend_unavailable", detail=error_label(str(exc), lang)),
            )
            return
        with self._lock:
            self._job_chats[job_id] = chat_id
        self._reply(
            chat_id,
            self._msg(
                "cancel_ok",
                id=job_id,
                status=status_label(str(job.get("status") or ""), lang),
            ),
        )

    def _cmd_url(self, chat_id: int, urls: list[str]) -> None:
        lang = self._lang()
        try:
            normalized = self._parse_urls(urls)  # type: ignore[misc]
            job = self.store.create_job(
                0,
                mode="url",
                input_payload={"urls": normalized},
                source_label="url",
                source_chat="",
            )
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("op_failed", detail=error_label(str(exc), lang)),
            )
            return
        job_id = int(job["id"])
        with self._lock:
            self._job_chats[job_id] = chat_id
        self._reply(chat_id, self._msg("url_queued", id=job_id, n=len(normalized)))

    def _cmd_message_id(self, chat_id: int, message_id: int) -> None:
        lang = self._lang()
        try:
            sources = [
                s
                for s in self.config_store.list_sources()
                if s.get("enabled", True)
            ]
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("backend_unavailable", detail=error_label(str(exc), lang)),
            )
            return
        if not sources:
            self._reply(chat_id, self._msg("no_sources"))
            return
        if len(sources) == 1:
            self._create_message_id_job(chat_id, message_id, str(sources[0]["id"]))
            return
        pending_id = self._new_pending_id()
        with self._lock:
            self._pending[pending_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "created_at": self._clock(),
            }
        rows = []
        for source in sources:
            rows.append(
                [
                    {
                        "text": str(source.get("label") or source["id"])[:40],
                        "callback_data": f"src:{source['id']}:{pending_id}"[:64],
                    }
                ]
            )
        markup = {"inline_keyboard": rows}
        self._reply(
            chat_id,
            self._msg("pick_source", id=message_id),
            reply_markup=markup,
        )

    def _create_message_id_job(self, chat_id: int, message_id: int, source_id: str) -> None:
        lang = self._lang()
        try:
            job = self.store.create_job(
                message_id,
                source_id=source_id,
                mode="message_id",
            )
        except Exception as exc:  # noqa: BLE001
            self._reply(
                chat_id,
                self._msg("op_failed", detail=error_label(str(exc), lang)),
            )
            return
        job_id = int(job["id"])
        with self._lock:
            self._job_chats[job_id] = chat_id
        self._reply(
            chat_id,
            self._msg(
                "msg_queued",
                id=job_id,
                message_id=message_id,
                source=job.get("source_label") or source_id,
            ),
        )

    def _handle_callback(self, query: dict[str, Any], cfg: dict[str, Any]) -> None:
        if not isinstance(query, dict):
            return
        data = str(query.get("data") or "")
        cq_id = str(query.get("id") or "")
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message, dict) else {}
        if not is_private_chat(chat if isinstance(chat, dict) else None):
            return
        chat_id = int(chat["id"])
        self._remember_notify_chat(chat_id)
        match = re.fullmatch(r"src:([^:]+):(.+)", data)
        if not match:
            self._answer_callback(cq_id, self._msg("unknown_action"))
            return
        source_id, pending_id = match.group(1), match.group(2)
        with self._lock:
            pending = self._pending.pop(pending_id, None)
        if not pending:
            self._answer_callback(cq_id, self._msg("pick_expired"))
            self._reply(chat_id, self._msg("pick_expired"))
            return
        if self._clock() - float(pending.get("created_at") or 0) > PENDING_SOURCE_TTL_SECONDS:
            self._answer_callback(cq_id, self._msg("pick_expired"))
            self._reply(chat_id, self._msg("pick_expired"))
            return
        self._answer_callback(cq_id, self._msg("picked"))
        self._create_message_id_job(chat_id, int(pending["message_id"]), source_id)

    def _new_pending_id(self) -> str:
        with self._lock:
            self._pending_seq += 1
            return f"p{self._pending_seq}"

    def _reply(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        api = self._api
        if api is None:
            cfg = normalize_bot_config(self.config_store.get_bot_config())
            if not cfg["token"]:
                return
            api = self._make_api(cfg["token"])
            with self._lock:
                self._api = api
                self._api_proxy_url = self._proxy_url()
        try:
            api.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = str(exc)[:500]

    def _answer_callback(self, callback_query_id: str, text: str = "") -> None:
        if not callback_query_id or self._api is None:
            return
        try:
            self._api.answer_callback_query(callback_query_id, text)
        except Exception:  # noqa: BLE001
            pass

    def _send_best_effort(self, chat_id: int, text: str) -> None:
        cfg = normalize_bot_config(self.config_store.get_bot_config())
        token = cfg.get("token") or ""
        if not token:
            return
        try:
            api = (
                self._api
                if self._api and self._api.token == token
                else self._make_api(token)
            )
            api.send_message(chat_id, text)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = str(exc)[:500]

    def _send_with_timeout(
        self,
        token: str,
        chat_id: int,
        text: str,
        *,
        timeout: float,
    ) -> None:
        done = threading.Event()

        def worker() -> None:
            try:
                self._make_api(token, timeout=timeout).send_message(chat_id, text)
            except Exception:  # noqa: BLE001
                pass
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        done.wait(timeout=timeout + 0.5)
