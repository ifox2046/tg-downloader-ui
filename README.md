# tg-downloader-ui

Lightweight web UI and automation layer for Telegram downloads with
[`iyear/tdl`](https://github.com/iyear/tdl).

`tdl` is the downloader runtime. This project provides a small web console,
job history, path/source settings, and an optional Telegram forwarder. It is
not affiliated with Telegram and is not an official `tdl` project.

## Modes

- Basic download mode: install and log in to `tdl`, configure a download
  directory and source chats, then submit Telegram message IDs in the web UI.
- Optional forwarder mode: additionally configure your own Telegram API
  credentials, a Telethon session, source users/bots, and a target channel.

The forwarder is only needed if you want this service to listen for messages
and forward summaries into your own channel.

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
- admin password
- absolute download directory, for Docker usually `/downloads`

Persistent Docker paths:

- `./data/config` -> `/config`
- `./data/tdl` -> `/tdl`
- `./downloads` -> `/downloads`

The Docker container starts the Web UI and the optional forwarder together.
The forwarder restart button restarts the in-container forwarder process; it
does not need the Docker socket.

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

## Configuration

Configuration priority:

1. command-line flags, where available
2. environment variables
3. `config.json`
4. safe defaults

`config.json` is stored under `TGDL_STATE_DIR`. In Docker this is `/config`.

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `TGDL_HOST` | no | `0.0.0.0` | Web UI bind host. |
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
tg-downloader-ui --host 0.0.0.0 --port 9910
```

Forwarder dependencies are optional:

```sh
python -m pip install ".[forwarder]"
tg-downloader-forwarder
```

## OpenWRT

Build the OpenWRT `.ipk` package on a normal development machine:

```sh
python scripts/build_openwrt_ipk.py
```

Install it on OpenWRT:

```sh
opkg install tg-downloader-ui_0.1.0_all.ipk
```

The package installs the web app, procd init script, environment template, and
LuCI menu link. It does not bundle `tdl`; install the correct upstream `tdl`
binary for your router separately and keep it at `/usr/bin/tdl` or set
`TGDL_TDL_BIN`.

The package depends on `python3-pip` and its post-install script makes a
best-effort install of `telethon>=1.35` and `qrcode>=7.4`. If the router is
offline during install, run this after network is available:

```sh
python3 -m pip install --no-cache-dir 'telethon>=1.35' 'qrcode>=7.4'
```

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

## Development

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
python scripts/build_openwrt_ipk.py
```

## License

This project's own code is MIT licensed. `iyear/tdl` is AGPL-3.0 and is used
as a third-party downloader runtime. See [THIRD_PARTY.md](THIRD_PARTY.md).
