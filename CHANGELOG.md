# Changelog

## Unreleased

- Support three download job modes: message ID (existing), Telegram URL (`tdl download -u`), and export JSON (`tdl download -f`) with upload/whitelist path under `exports/`.
- Install `python-socks` and `PySocks` with the Python package/Docker image so Telethon SOCKS proxies work.
- Document the recommended bot → private channel → message-ID download workflow in README (EN/ZH).
- Add Web UI Chinese/English language switch (`localStorage` + browser language default).

## 0.1.0 - 2026-07-13

- Add the authenticated Web UI, job history, source/path configuration, and safe first-run setup.
- Add Docker Compose deployment with persistent configuration, tdl state, and download paths.
- Add optional Telethon forwarding with code/QR authorization and authenticated proxy support.
- Add live-process pause and continue using `SIGSTOP`/`SIGCONT` without restarting partial downloads.
- Add OpenWrt/iStoreOS IPK packaging, LuCI entry, procd service, offline Python dependencies, and iStore metadata package.
- Add Docker, OpenWrt 23.05/24.10, and iStoreOS verification coverage.
- Document `iyear/tdl` as an AGPL-3.0 third-party runtime dependency while keeping project code under MIT.
