# tg-downloader-ui

[简体中文](README_ZH.md) | English

Lightweight web UI and automation layer for Telegram downloads with
[`iyear/tdl`](https://github.com/iyear/tdl).

`tdl` is the downloader runtime. This project provides a small web console,
job history, path/source settings, and an optional Telegram forwarder. It is
not affiliated with Telegram and is not an official `tdl` project.

This is a local-first service. The Python application binds to loopback by
default, and Docker Compose publishes only on `127.0.0.1` by default. Do not
expose the service directly to the public Internet; use a trusted HTTPS reverse
proxy or VPN when remote access is required.

## Modes

- Basic download mode: install and log in to `tdl`, configure a download
  directory and source chats, then submit Telegram message IDs in the web UI.
- Optional forwarder mode: additionally configure your own Telegram API
  credentials, a Telethon session, source users/bots, and a target channel.

The forwarder is only needed if you want this service to listen for messages
and forward summaries into your own channel.

### Download input modes

The Web UI download form supports three job input modes:

| Mode | Input | `tdl` behavior | Output naming |
| --- | --- | --- | --- |
| Message ID | Source chat + message ID(s) | `chat export` by id, then `download -f` | Existing Movies/TV rename pipeline |
| URL | One or more `https://t.me/...` links (max 50) | `download -u` (repeated) | Native `tdl` filenames under the download directory |
| Export file | Upload and/or path under `TGDL_STATE_DIR/exports` (max 32 MiB upload) | `download -f` | Native `tdl` filenames under the download directory |

URL mode does not use the source selector (the link embeds the chat). Export
paths are restricted to the exports whitelist root; path traversal is rejected.

## Language

The Web UI supports **Chinese** and **English**. Use the **中文 | EN** control
in the header (also on the login and first-run setup pages). The choice is
stored in browser `localStorage` (`tgdl_lang`). With no saved preference, the
UI follows `navigator.languages` (`zh*` / `en*`) and otherwise defaults to
Chinese. Server API error text stays English; the client shows those messages
as returned. Forwarder channel summary labels (`File` / `Size` / `Message ID`)
are fixed English and are not re-localized by the UI switch.

## Recommended workflow (bot → private channel → message ID download)

This is the intended end-to-end path for most operators:

```text
source bot(s)
    │  (Telethon forwarder listens)
    ▼
your private channel  ── shows file summary + 消息ID ──► copy ID
    │
    ▼
Web UI: pick source + paste message ID(s)
    │  (tdl downloads from the original chat)
    ▼
download directory (Docker: /downloads)
```

### 1. Deploy and first-run setup

1. Start Docker / package install and open the Web UI.
2. Create the admin account and set an absolute download directory
   (`/downloads` in Docker).
3. Bind-mount the host download folder to `/downloads` if you want files on a
   NAS share (for example `/vol1/.../telegram_downloads:/downloads`).

### 2. Configure download sources

In **来源设置** (sources), add each bot or chat you download from:

| Field | Meaning |
| --- | --- |
| Label | Display name in the UI |
| Chat | `tdl` chat identity used for download (usually the bot username without `@`) |
| Forward source | Telethon `from_users` filter (usually `@BotUsername`) |

Enable only the sources you use. The same list drives both manual downloads and
the forwarder.

### 3. Log in `tdl` (required for downloads)

`tdl` is the real downloader. Complete **tdl 下载登录** (QR or code) with the
Telegram account that can see the source bots/chats. Without this login,
message-ID downloads will fail even if the Web UI is up.

### 4. Optional but recommended: private channel + forwarder

Use this when you do not want to dig message IDs out of bot chats by hand.

1. Create a **private channel** you control (receive-only inbox).
2. Get `api_id` / `api_hash` from https://my.telegram.org.
3. On **Telegram 授权**, save API credentials, proxy if needed, session path
   (Docker default `/tdl/session.txt`), and the channel numeric ID.
4. Authorize Telethon (SMS/code or QR). This session is **independent** of the
   `tdl` login.
5. Ensure `TGDL_FORWARDER_ENABLED=1` (Docker default). The forwarder listens to
   enabled sources and posts media summaries into your channel.
6. Optionally open **Telegram 授权 → 转发过滤** (Forwarder filters) to change
   media types, caption requirement, size bounds, and keywords. Defaults match
   historical behavior: **video only** and **require caption/text**. Saving
   filters writes `config.json` and **restarts the forwarder** (same path as
   the restart button).

Forwarded text includes the original caption plus fixed English technical
lines:

```text
File: example.mp4
Size: 1.2 GB
Message ID: 26933
```

Copy the **Message ID** value into the Web UI download form (with the matching
source selected). The service then runs `tdl` against the configured source
chat and that message ID.

#### Forwarder filters (`config.json`)

Stored under `forwarder_filters` (missing key → defaults):

| Field | Default | Meaning |
| --- | --- | --- |
| `media_video` | `true` | Forward video documents |
| `media_photo` | `false` | Forward photos |
| `media_document` | `false` | Forward non-video documents |
| `require_text` | `true` | Skip when caption/text is empty |
| `min_size_bytes` / `max_size_bytes` | `0` | Size bounds (`0` = no bound); UI uses MiB |
| `include_keywords` | `[]` | If non-empty, caption/text must match any (case-insensitive) |
| `exclude_keywords` | `[]` | Skip when any keyword matches (case-insensitive) |

Skips are logged as `SKIP: <reason>` in the forwarder log.

### 5. Download

1. Open the main page, select the source bot/chat.
2. Paste one or more message IDs from the channel summary (or from Telegram
   directly).
3. Submit the job and watch history / progress.
4. Finished files land under the configured download directory. In-progress
   files may appear as `*.tmp` until `tdl` finishes renaming them.

### What you need vs what is optional

| Capability | Required pieces |
| --- | --- |
| Manual message-ID download only | Web UI admin + `tdl` login + source chat + download dir |
| Bot watch → channel summary → copy ID download | Above + Telethon session + private channel ID + forwarder enabled |

You do **not** need the forwarder if you already know the message IDs. You do
**not** need Telegram API credentials for pure `tdl` downloads.

## Pause and Recovery Semantics

Pause and Continue are live-process controls on Linux: the service sends
`SIGSTOP` and `SIGCONT` to the same running `tdl` process, preserving its PID,
open partial file, and current byte offset. Cancel or service shutdown first
continues a stopped child and then terminates it cleanly.

This is separate from recovery after an application or container restart.
`tdl 0.20.3 --continue` can skip fully completed items in a multi-item export,
but it does not resume byte ranges inside one partially downloaded file. A
restart can therefore restart the current single file from zero.

## Docker Quick Start

The Docker image installs an unmodified `tdl` release binary inside the image.
See [THIRD_PARTY.md](THIRD_PARTY.md) for the AGPL-3.0 notice.

```sh
cp .env.example .env
docker compose up --build
```

Open:

```text
http://localhost:9910
```

On first launch, the setup page requires:

- admin username
- admin password with at least eight characters (no composition rules)
- absolute download directory, for Docker usually `/downloads`

The first browser to complete this form creates the administrator account.
Complete the first-run administrator setup before changing the bind or publish
address from loopback to a LAN address.

For unattended provisioning, set both `TGDL_AUTH_USER` and
`TGDL_AUTH_PASSWORD` before the first start. Leave `TGDL_AUTH_PASSWORD` unset
to use the browser setup flow, and keep real passwords in an untracked local
environment file.

Persistent Docker paths:

- `./data/config` -> `/config`
- `./data/tdl` -> `/tdl`
- `./downloads` -> `/downloads`

These host directories must be writable by UID/GID `1000`. The container runs
the application as that non-root user after preparing the mount roots.

The published Docker image is multi-architecture (`linux/amd64` and
`linux/arm64`) under one name on Docker Hub:

```text
ifox2046/tg-downloader-ui:0.1.2
ifox2046/tg-downloader-ui:latest
```

Each platform installs a checksum-verified, unmodified upstream `tdl` `0.20.3`
binary for that architecture (`tdl_Linux_64bit.tar.gz` on amd64,
`tdl_Linux_arm64.tar.gz` on arm64). Local `docker compose build` / plain
`docker build` on an amd64 host still works without Buildx.

```sh
docker pull ifox2046/tg-downloader-ui:0.1.2
```

The container starts the Web UI and optional forwarder by default; set
`TGDL_FORWARDER_ENABLED=0` to opt out of the forwarder. The forwarder restart
button restarts the in-container forwarder process; it does not need the Docker
socket.

Multi-arch images are built and pushed from GitHub Actions only on version tags
or releases (workflow `Docker Publish`). PR/main CI builds both platforms to
verify the Dockerfile without pushing. Repository secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are required for publish; they are
never stored in this repository.

## tdl Login

Basic download mode requires a working `tdl` login. In Docker, use the Web UI
first:

1. Log in to `http://localhost:9910`.
2. Open the Telegram authorization area.
3. In `tdl 下载登录`, start QR login and scan the terminal QR output with your
   own Telegram account.

You can still run the equivalent command directly when you need a shell-only
check:

```sh
docker compose run --rm web tdl login --storage type=bolt,path=/tdl/data
```

For non-Docker installs, install `tdl` from its upstream documentation and run
the equivalent login command with the same storage path you configure in
`TGDL_TDL_STORAGE`.

## Telegram API Credentials

Only the optional forwarder requires Telegram API credentials.

1. Sign in to https://my.telegram.org with your own Telegram account.
2. Create an app and copy its `api_id` and `api_hash`.
3. Create your own Telegram channel for forwarded messages.
4. Add your account to that channel and get its numeric channel ID.
5. Set `TGDL_API_ID`, `TGDL_API_HASH`, `TGDL_SESSION_FILE`, and
   `TGDL_FORWARD_CHANNEL_ID`.

After initial setup, the Web UI has a `Telegram 授权` page. Save the API
ID/hash, session file path, target channel ID, and optional proxy there, then
authorize the Telethon account by SMS/code or QR scan. The UI writes a Telethon
`StringSession` to `TGDL_SESSION_FILE`. The Docker forwarder service can use
that same `/config/config.json` and `/tdl/session.txt` state.

`tdl` login and Telethon authorization are separate. Basic downloads need the
`tdl` login; the forwarder needs the Telethon session.

Do not publish `api_hash`, session strings, or channel IDs from private
accounts.

Treat the entire state directory, Telegram sessions, proxy credentials, job
logs, and downloader logs as sensitive data. Keep them out of commits, issue
attachments, and support logs.

## Configuration

Configuration priority:

1. command-line flags, where available
2. environment variables
3. `config.json`
4. safe defaults

`config.json` is stored under `TGDL_STATE_DIR`. In Docker this is `/config`.

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `TGDL_HOST` | no | `127.0.0.1` | Web UI bind host. Docker overrides the container bind address while Compose limits publication to loopback. |
| `TGDL_PORT` | no | `9910` | Web UI port. |
| `TGDL_STATE_DIR` | no | user state dir | Config, database, logs, forwarder status. |
| `TGDL_DOWNLOAD_DIR` | setup | user downloads dir | Default download directory. |
| `TGDL_TDL_BIN` | no | `tdl` | Path to the `tdl` binary. |
| `TGDL_TDL_STORAGE` | no | state-local bolt DB | `tdl --storage` value. |
| `TGDL_TDL_LOG` | no | state-local log | Path tailed for `tdl` diagnostic details. |
| `TGDL_PROXY` | no | empty | Global proxy fallback. |
| `TGDL_TDL_PROXY` | no | `TGDL_PROXY` | Proxy for `tdl` download/export commands. Empty disables proxy. |
| `TGDL_TELEGRAM_PROXY` | no | `TGDL_PROXY` | Proxy for forwarder/Telethon. Empty disables proxy. |
| `TGDL_SESSION_MAX_AGE` | no | `604800` | Login cookie lifetime in seconds. |
| `TGDL_AUTH_USER` | unattended setup | `admin` | Administrator username used when `TGDL_AUTH_PASSWORD` is set before first start. |
| `TGDL_AUTH_PASSWORD` | unattended setup | empty | Optional administrator password for first-start provisioning; otherwise use the browser setup page. Keep this value out of Git. |
| `TGDL_COOKIE_SECURE` | no | `0` | Set to `1` when the browser reaches the service through HTTPS. |
| `TGDL_PUBLISH_HOST` | Docker | `127.0.0.1` | Host address used by Docker Compose port publication. |
| `TGDL_PUBLISH_PORT` | Docker | `9910` | Host port used by Docker Compose. |
| `TGDL_FORWARDER_ENABLED` | no | `1` for packaged deployments | Docker and OpenWRT start the optional forwarder by default. Set to `0` to opt out. |
| `TGDL_API_ID` | forwarder | empty | Telegram API ID from `my.telegram.org`. |
| `TGDL_API_HASH` | forwarder | empty | Telegram API hash from `my.telegram.org`. |
| `TGDL_SESSION_FILE` | forwarder | state-local session path | Telethon string session file. |
| `TGDL_FORWARD_SOURCE` | fallback | empty | Source user/bot if no source config exists. |
| `TGDL_FORWARD_CHANNEL_ID` | forwarder | empty | Target channel ID for forwarded messages. |
| `TGDL_FORWARDER_LOG` | no | state-local log | Forwarder log path. |
| `TGDL_FORWARDER_STATUS` | no | state-local JSON | Forwarder status JSON path. |
| `TGDL_FORWARDER_RESTART_CMD` | no | OpenWRT auto-detect; Docker sets local restart script | Custom command used by the Web UI forwarder restart button. Parsed as argv and never run through a shell. |

Proxy values use URL form, for example:

```text
socks5://127.0.0.1:1080
http://127.0.0.1:8080
```

## Python Package

```sh
python -m pip install .
tg-downloader-ui
```

The package includes Telethon, qrcode, and SOCKS proxy libraries
(`python-socks`, `PySocks`) required for Telegram authorization through a
proxy. The forwarder remains opt-in:

```sh
tg-downloader-forwarder
```

## OpenWRT

Build the OpenWRT `.ipk` package on a normal development machine:

```sh
python scripts/build_openwrt_ipk.py
```

The default builder run produces three packages:

- `tg-downloader-ui_0.1.0_all.ipk`: architecture-independent application package. Install the correct upstream `tdl` binary separately.
- `tg-downloader-ui-full_0.1.0_x86_64.ipk`: complete x86_64 package containing the application and the unmodified upstream `tdl 0.20.3` binary (`tdl_Linux_64bit.tar.gz`).
- `app-meta-tg-downloader-ui_0.1.0-r1_all.ipk`: iStore installed-app metadata.

Build a separate aarch64 full package (OpenWrt `Architecture: aarch64_generic`, upstream `tdl_Linux_arm64.tar.gz`) with:

```sh
python scripts/build_openwrt_ipk.py --full-arch aarch64
# or both full arches:
python scripts/build_openwrt_ipk.py --full-arch all
```

That emits `tg-downloader-ui-full_0.1.0_aarch64_generic.ipk` in addition to the packages above when using `--full-arch all`. Full packages for different CPU arches are separate IPK files that share the same package name (`tg-downloader-ui-full`) and Conflicts/Provides `tg-downloader-ui`.

Install only one application package:

```sh
opkg install tg-downloader-ui_0.1.0_all.ipk
# or, on x86_64:
opkg install tg-downloader-ui-full_0.1.0_x86_64.ipk
# or, on aarch64 OpenWrt:
opkg install tg-downloader-ui-full_0.1.0_aarch64_generic.ipk
```

The generic and full packages conflict because they own the same runtime files. The full package removes the separate `tdl` installation step, but it does not remove first-run administrator setup or Telegram authentication. Log in to your own Telegram account with the Web UI QR flow or with `tdl login` using the configured storage path.

The package installs the web app, procd init script, environment template, and
LuCI menu link. Telethon, qrcode, rsa, pyasn1, and pyaes are verified and
prebundled in the IPK, so installing the package on the router does not run
`pip` and can be completed offline.

For the full manual testing checklist, see [docs/TESTING.md](docs/TESTING.md).
For an OpenWRT-specific real-device checklist, see
[docs/OPENWRT_TESTING.md](docs/OPENWRT_TESTING.md).

Use the environment template:

```sh
cp openwrt/tg-downloader-ui.env.example /etc/tg-downloader-ui.env
chmod 600 /etc/tg-downloader-ui.env
```

Edit `/etc/tg-downloader-ui.env`, then restart:

```sh
/etc/init.d/tg-downloader-ui restart
```

Docker and OpenWRT packaged deployments start the optional forwarder by default.
Set `TGDL_FORWARDER_ENABLED=0` in that file to opt out on OpenWRT.

## Development

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
python scripts/build_openwrt_ipk.py
```

## License

This project's own code is MIT licensed. The `tdl` binary bundled in multi-arch Docker images (`linux/amd64` and `linux/arm64`) and the full OpenWrt IPKs (`tg-downloader-ui-full_0.1.0_x86_64.ipk` and `tg-downloader-ui-full_0.1.0_aarch64_generic.ipk`) is an unmodified upstream `tdl 0.20.3` binary licensed under AGPL-3.0. Each full IPK includes the upstream license and source/version notice under `/usr/share/licenses/tg-downloader-ui-full`. See [THIRD_PARTY.md](THIRD_PARTY.md).
