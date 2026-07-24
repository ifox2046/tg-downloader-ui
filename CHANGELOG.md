# Changelog

## Unreleased

## 0.1.4 - 2026-07-24

- Add optional Telegram **control bot** (Bot API): Web UI token/enable, private-DM URL and message-ID enqueue (multi-source inline pick), `/jobs` `/status` `/cancel`, job terminal notify, start/graceful-stop and Telegram API recover notifies, backend health probe (5 min / 2 fails). Complements Telethon forwarder; does not replace it.
- Control bot uses the same Telegram proxy chain as tdl/Telethon (`TGDL_TELEGRAM_PROXY` → `TGDL_PROXY` → `TGDL_TDL_PROXY` → `telegram.proxy`) so `api.telegram.org` works on restricted networks.
- Reject enqueue of duplicate **open** jobs (queued/exporting/downloading/renaming/paused) for the same URL canonical key or same message_id+source; terminal jobs may be re-queued.
- Fix OpenWrt/iStoreOS LuCI service page: pack `bot.py` in IPKs; service Start/Restart/Stop/Enable no longer permanently greyed out (`disabled: null` like System → Startup; prefer `luci.setInitAction`).

## 0.1.3 - 2026-07-21

- Pin URL/message-ID export JSON to the requested message id before `tdl download -f` so adjacent channel messages are not downloaded (tdl 0.20.3 `-i` is a single id; multi-message exports are filtered in-app).
- Show a dedicated Cancel button on queued/active/paused jobs that calls `/api/jobs/{id}/cancel` (separate from Pause); Cancel and Delete ask for browser confirmation.
- Fix multi-arch Docker builds so BuildKit `TARGETARCH` is not overridden by a hardcoded amd64 default (arm64 images install `tdl_Linux_arm64`).
- Isolate concurrent download workers onto cloned tdl bolt storage slots so parallel jobs no longer fail with "database is used by another process".
- Add configurable concurrent download jobs (`max_concurrent_jobs` in `config.json` / Web UI paths page, optional `TGDL_MAX_CONCURRENT_JOBS` when the key is missing). Default 1, max 8; worker pool resizes without full process restart. Paused jobs count toward the concurrency limit.
- Fix download-page URL mode submit-band layout alignment; fix login/setup top-right language switch button sizing.
- Support three download job modes: message ID (existing), Telegram URL (export-first: parse link → `chat export` → `download -f`), and export JSON (`tdl download -f`) with upload/whitelist path under `exports/`.
- URL mode resolves title/filename via export metadata (same Movies/TV rename path as message ID); multi-URL jobs process each link sequentially.
- Move forwarder filters UI to a dedicated sidebar page (`转发过滤` / Forwarder Filters); Telegram auth page no longer hosts the filters block.
- Install `python-socks` and `PySocks` with the Python package/Docker image so Telethon SOCKS proxies work.
- Document the recommended bot → private channel → message-ID download workflow in README (EN/ZH).
- Add Web UI Chinese/English language switch (`localStorage` + browser language default).
- Add configurable forwarder filters (`forwarder_filters` in `config.json`): media type toggles, require caption/text, size bounds, include/exclude keywords; Web UI + API with auto restart; channel summary labels fixed as `File` / `Size` / `Message ID`.

## 0.1.0 - 2026-07-13

- Add the authenticated Web UI, job history, source/path configuration, and safe first-run setup.
- Add Docker Compose deployment with persistent configuration, tdl state, and download paths.
- Add optional Telethon forwarding with code/QR authorization and authenticated proxy support.
- Add live-process pause and continue using `SIGSTOP`/`SIGCONT` without restarting partial downloads.
- Add OpenWrt/iStoreOS IPK packaging, LuCI entry, procd service, offline Python dependencies, and iStore metadata package.
- Add Docker, OpenWrt 23.05/24.10, and iStoreOS verification coverage.
- Document `iyear/tdl` as an AGPL-3.0 third-party runtime dependency while keeping project code under MIT.
