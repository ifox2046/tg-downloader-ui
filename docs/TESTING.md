# Manual Testing Guide

This guide covers full manual testing for `tg-downloader-ui` across Docker,
Python package installs, OpenWRT, and optional Telegram forwarder mode.

Use your own Telegram account and your own test channel. Do not paste real
`api_hash`, session strings, cookies, private channel IDs, or private paths into
issues, screenshots, or public logs.

## 1. What CI Already Covers

CI and local automated tests cover:

- metadata parsing
- job creation, pause, resume, retry, delete
- auth session behavior
- setup-required behavior
- download directory validation
- source configuration
- proxy precedence
- forwarder config validation
- release-safety scans for private defaults
- Python package build
- Docker image build and `tdl version` smoke test

Manual testing should focus on real runtime behavior:

- first-run setup in a browser
- real `tdl` QR login
- real Telegram message download
- pause/resume from a real partial download
- volume persistence
- optional forwarder with real Telegram API credentials
- platform-specific deployment, especially OpenWRT init/LuCI

## 2. Test Data You Need

Prepare before testing:

- A Telegram account you control.
- A source chat/channel/bot that contains at least one downloadable file.
- A known message ID from that source.
- Enough disk space for the test file.
- Optional: a Telegram channel you own for forwarder tests.
- Optional: `api_id` and `api_hash` from https://my.telegram.org for forwarder.

Recommended test file:

- Small enough to finish quickly.
- Large enough to observe progress if you want to test cancel/retry.
- Non-sensitive, because filenames and message text may appear in logs.

## 3. Docker Test

Docker is the recommended general server/staging path. The image installs an
unmodified `iyear/tdl` release binary. See `THIRD_PARTY.md` for the AGPL-3.0
notice.

### 3.1 Start from a Clean Docker State

From the repository root:

```sh
docker compose down --remove-orphans
rm -rf data downloads
cp .env.example .env
```

If your network needs a proxy, edit `.env`:

```sh
TGDL_PROXY=socks5://127.0.0.1:1080
TGDL_TDL_PROXY=
TGDL_TELEGRAM_PROXY=
```

Start the web service:

```sh
docker compose up --build
```

Expected:

- Image builds successfully.
- Container starts without crashing.
- Port `9910` is exposed.

Open:

```text
http://localhost:9910
```

Expected:

- First visit opens `/setup`.
- `/login` redirects to `/setup` before initialization.
- There is no usable default admin password.

### 3.2 First-Run Setup

Fill setup:

- Admin username: any test username.
- Admin password: any strong test password.
- Download directory: `/downloads`.
- Forwarder fields: leave empty for basic download testing.

Submit.

Expected:

- Browser moves to `/login`.
- Login succeeds with the new admin account.
- `./data/config/config.json` exists.
- `config.json` does not contain the plaintext password.

Check:

```sh
grep -n "password" ./data/config/config.json
```

Expected: password hash/salt fields may exist; plaintext password must not.

### 3.3 tdl Login in Docker

Use the Web UI first:

1. Log in to the app.
2. Open the Telegram authorization area.
3. In `tdl 下载登录`, start QR login.
4. Scan the displayed terminal QR output with your own Telegram account.

Expected:

- The QR output appears in one fixed area and is replaced on a new attempt.
- Login eventually reports success.
- No session string is printed into project files.

Shell-only fallback:

```sh
docker compose run --rm web tdl login --storage type=bolt,path=/tdl/data
```

Smoke-check `tdl`:

```sh
docker compose run --rm web tdl --storage type=bolt,path=/tdl/data chat ls
```

Expected:

- Your accessible chats are listed.
- No session string is printed into project files.

### 3.4 Configure Sources

In Web UI:

1. Open the sources page.
2. Add a source your `tdl` account can access.
3. Fill:
   - label: any human-readable name
   - `tdl` chat: source chat username or identifier used by `tdl`
   - forward source: optional `@username`
   - enabled: checked
   - default: selected
4. Save.

Expected:

- Reloading the page keeps the source.
- Download page source dropdown shows the source.

### 3.5 Basic Download

Submit the known message ID from your configured source.

Expected job flow:

```text
queued -> exporting -> downloading -> done
```

Check files:

```sh
find ./downloads -type f | head
```

Check logs:

```sh
find ./data/config/logs -type f -maxdepth 1 -print
tail -n 80 ./data/config/logs/*.log
```

Pass criteria:

- The file appears under `./downloads`.
- Job status is `done`, or `skipped` if the final file already existed.
- Job log includes `tdl` export/download output.
- Web UI does not expose `api_hash` or session strings.

### 3.6 Pause, Resume, Retry, Delete

Pause:

1. Submit a larger test file.
2. Click pause while active.

Expected:

- Active job records a pause request.
- Final stored status may be `canceled`, but the UI presents the operator
  action as paused/resumable.

Resume:

1. Resume the paused job.
2. Inspect the job log.

Expected:

- Attempts count increases only as expected for the resumed run.
- Existing progress/downloaded-size fields are preserved at resume time.
- The resumed command includes both `tdl download --continue` and
  `-f <export.json>`.

Retry:

1. Retry a failed job that is not resumable.

Expected:

- Attempts count increases.
- Job returns to `queued` and runs through the normal download flow.

Delete:

1. Delete a finished job.

Expected:

- Job row disappears.
- Job export/log side files are removed.

### 3.7 Docker Persistence

Restart:

```sh
docker compose down
docker compose up
```

Expected:

- Login still works.
- Sources persist.
- Download directory persists.
- Job history persists.
- Existing downloaded files remain in `./downloads`.

### 3.8 Docker Optional Forwarder

Skip this if you only need basic download mode.

You can either save Telegram API settings and authorize the Telethon session in
the Web UI, or prepare `.env` before starting the forwarder:

```sh
TGDL_API_ID=your_api_id
TGDL_API_HASH=your_api_hash
TGDL_SESSION_FILE=/tdl/session.txt
TGDL_FORWARD_CHANNEL_ID=-100your_channel_id
TGDL_PROXY=
TGDL_TELEGRAM_PROXY=
```

Create or provide the session file expected by the forwarder. The Web UI can
write the Telethon `StringSession` to `/tdl/session.txt`; if env vars are empty,
the forwarder falls back to `/config/config.json`.

Start Docker:

```sh
docker compose up
```

Expected:

- Forwarder service does not exit with a config error.
- Web UI forwarder status changes from missing/stale to running.
- Sending a test message from the configured source results in a forwarded
  summary in your target channel.
- After changing source configuration, restart the forwarder before testing
  newly added sources. With the bundled Docker image, the Web UI restart button
  restarts the in-container `tg-downloader-forwarder` process.

If it fails:

```sh
docker compose logs web
cat ./data/config/forwarder_status.json
tail -n 100 ./data/config/forwarder.log
```

Common expected errors:

- `TGDL_API_ID is required`
- `TGDL_API_HASH is required`
- `TGDL_FORWARD_CHANNEL_ID is required`
- `session file not found`
- proxy connection failures

### 3.9 Docker Cleanup

Stop:

```sh
docker compose down --remove-orphans
```

Remove runtime data only if you no longer need it:

```sh
rm -rf data downloads
```

## 4. Python Package Test

Use this path to verify non-Docker server installs.

Create a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip build
python -m build
python -m pip install dist/*.whl
```

Smoke-check commands:

```sh
tg-downloader-ui --check
tg-downloader-forwarder
```

Expected:

- `tg-downloader-ui --check` prints `ok`.
- `tg-downloader-forwarder` exits with a clear config error if forwarder
  credentials are not configured.

Run Web UI:

```sh
TGDL_STATE_DIR=$(pwd)/data/config \
TGDL_DOWNLOAD_DIR=$(pwd)/downloads \
TGDL_TDL_STORAGE=type=bolt,path=$(pwd)/data/tdl/data \
tg-downloader-ui --host 0.0.0.0 --port 9910
```

Then repeat the Docker browser, `tdl` QR login, source, download, pause/resume,
and persistence tests using local paths instead of Docker volumes.

## 5. OpenWRT Test

OpenWRT has its own full real-device checklist:

[OPENWRT_TESTING.md](OPENWRT_TESTING.md)

Use it to verify:

- native init script
- `/etc/tg-downloader-ui.env`
- LuCI menu link
- router storage paths
- OpenWRT reboot behavior
- optional forwarder on router hardware

## 6. Automated Verification Before Release

Run on a normal development machine or staging host:

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
python scripts/build_openwrt_ipk.py
docker build --build-arg TDL_VERSION=0.20.3 -t tg-downloader-ui:test .
docker run --rm tg-downloader-ui:test tdl version
docker run --rm tg-downloader-ui:test tg-downloader-ui --check
```

Expected:

- All unit tests pass.
- Compileall succeeds.
- Python package builds sdist and wheel.
- OpenWRT `.ipk` package is generated under `dist/openwrt/`.
- Docker image builds.
- Container reports `tdl` version.
- Container app check prints `ok`.

## 7. Final Acceptance Checklist

- [ ] Docker starts from a clean `data/` and `downloads/` directory.
- [ ] First-run setup is required.
- [ ] No default admin password works before setup.
- [ ] Admin login works after setup.
- [ ] `tdl` QR login completed with your own Telegram account.
- [ ] Source configuration persists.
- [ ] A known message ID downloads successfully.
- [ ] Pause, resume, retry, and delete behave correctly.
- [ ] Restart preserves config, jobs, and downloaded files.
- [ ] Optional forwarder works, if enabled.
- [ ] OpenWRT checklist passes, if OpenWRT is a target for the release.
- [ ] Logs and screenshots do not expose Telegram API hash or session strings.
