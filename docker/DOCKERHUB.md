# Docker Hub repository text

Source of truth for the Hub **short description** and **full Overview**.

CI updates Hub automatically:

- On image publish (workflow `Docker Publish`) after multi-arch push
- On push to `main` that touches this file (workflow `Docker Hub Overview`)
- Manual: Actions → **Docker Hub Overview** → Run workflow

Local / one-off (needs Hub credentials):

```sh
export DOCKERHUB_USERNAME=...
export DOCKERHUB_TOKEN=...
python scripts/update_dockerhub_overview.py
```

Keep this file in sync with `CHANGELOG.md` and the README Docker section when
shipping a new version.

---

## Short description (≤100 characters)

```
Local-first Telegram download Web UI on tdl — optional forwarder + control bot.
```

---

## Full description / Overview (paste as Markdown on Hub)

# tg-downloader-ui

Lightweight **local-first** Web UI and automation for Telegram downloads, built
on [iyear/tdl](https://github.com/iyear/tdl).

- **Image:** `ifox2046/tg-downloader-ui`
- **Architectures:** `linux/amd64`, `linux/arm64`
- **Current tags:** `0.1.4`, `latest`

## Features (0.1.4)

- Web console: job history, sources, paths, concurrency
- Download modes: message ID, `t.me` URL (export-first), export JSON
- Optional **Telethon forwarder** → private channel summaries (message IDs)
- Optional **control bot** (Bot API): private-DM enqueue / `/jobs` / `/status` /
  `/cancel`; terminal + lifecycle notifies; multi-source inline pick
- Open-job **dedupe** (same URL or message id + source while still active)
- Bundled unmodified upstream **tdl 0.20.3** (checksum-verified per arch)

## Quick start

    docker pull ifox2046/tg-downloader-ui:0.1.4

Default publish is loopback-friendly; do **not** expose the UI to the public
Internet without HTTPS reverse proxy / VPN.

## Control bot (optional)

1. Create a bot with BotFather.
2. Log in to the Web UI → **Control bot** → paste token → enable → save.
3. Private-chat the bot: `/help`, paste a `t.me` link or message id.

Uses the same Telegram proxy env as tdl when `api.telegram.org` is blocked:
`TGDL_TELEGRAM_PROXY` / `TGDL_PROXY` / `TGDL_TDL_PROXY` (or UI `telegram.proxy`).

**Do not** run the same bot token on two containers/hosts at once (Telegram
`getUpdates` 409 Conflict).

## Docs & source

- GitHub: https://github.com/ifox2046/tg-downloader-ui
- README (EN/ZH), CHANGELOG, OpenWrt IPKs on Releases
- License: project MIT; bundled `tdl` is AGPL-3.0 (unmodified upstream binary)

---

## 中文短描述（可选）

```
本地优先的 Telegram 下载 Web UI（基于 tdl），含可选转发器与控制 Bot。
```

## 中文 Overview 要点（可选）

- 镜像：`ifox2046/tg-downloader-ui:0.1.4` / `:latest`（amd64 + arm64）
- Web 下载：消息 ID / t.me 链接 / 导出 JSON
- 可选 Telethon 转发器 → 私有频道摘要
- 可选控制 Bot：私聊入队、查询、取消；终态与生命周期通知
- 活跃任务去重；内置 tdl 0.20.3
- 文档与源码：GitHub `ifox2046/tg-downloader-ui`
