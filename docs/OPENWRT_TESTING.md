# OpenWRT Manual Test Guide

This guide verifies `tg-downloader-ui` on a real OpenWRT device. It covers the
native init script, LuCI link, first-run setup, `tdl` login, basic downloads,
optional forwarder mode, persistence, and rollback.

Use your own Telegram account and your own test channel. Do not paste real
`api_hash`, session strings, cookies, private channel IDs, or private paths into
issues or public logs.

## 1. Test Scope

Run these tests on OpenWRT:

- Web UI starts under `/etc/init.d/tg-downloader-ui`.
- First launch requires setup and does not create a default password.
- Configuration is stored under `TGDL_STATE_DIR`.
- `tdl` can log in with your Telegram account and download a known message.
- LuCI shows `Services -> Telegram Downloads`.
- Optional forwarder mode starts only after Telegram API credentials are set.
- Restarting the service preserves config, sessions, and job history.

Do not use public CI for real Telegram downloads. These tests require private
credentials and network access.

## 2. Device Prerequisites

SSH into the OpenWRT device as `root`.

Check basic facts:

```sh
uname -a
df -h
opkg update
```

Install runtime packages:

```sh
opkg install python3 python3-sqlite3 ca-bundle curl tar
```

If you plan to use the optional forwarder, also make sure Python can install or
run `telethon`. On small routers, prefer testing forwarder on a larger OpenWRT
target or use Docker/server deployment instead.

## 3. Install tdl (generic-package path)

When testing the generic `tg-downloader-ui_0.1.0_all.ipk` package, install
`tdl` separately from the upstream project:

```sh
TDL_VERSION=0.20.3
cd /tmp
curl -fL "https://github.com/iyear/tdl/releases/download/v${TDL_VERSION}/tdl_Linux_64bit.tar.gz" -o tdl.tar.gz
tar -xzf tdl.tar.gz
find /tmp -type f -name tdl -exec install -m 0755 {} /usr/bin/tdl \;
tdl version
```

Expected:

```text
Version: 0.20.3
```

Use the correct upstream asset for your router CPU. The command above is for
64-bit Linux targets.

## 4. Install Application

Preferred path: build and install the `.ipk` package.

On your development machine:

```sh
python scripts/build_openwrt_ipk.py
```

For x86_64 iStoreOS/OpenWrt, the full package includes `tdl 0.20.3`; skip the separate `tdl` installation section when testing it.

```powershell
$target = "$env:OPENWRT_TEST_USER@$env:OPENWRT_TEST_HOST"
scp .\dist\openwrt\tg-downloader-ui-full_0.1.0_x86_64.ipk "${target}:/tmp/"
ssh $target "opkg install /tmp/tg-downloader-ui-full_0.1.0_x86_64.ipk"
```

On aarch64 OpenWrt, build and install the separate arm64 full package instead:

```sh
python scripts/build_openwrt_ipk.py --full-arch aarch64
# artifact: dist/openwrt/tg-downloader-ui-full_0.1.0_aarch64_generic.ipk
# Architecture: aarch64_generic ; tdl asset: tdl_Linux_arm64.tar.gz
```

```powershell
$target = "$env:OPENWRT_TEST_USER@$env:OPENWRT_TEST_HOST"
scp .\dist\openwrt\tg-downloader-ui-full_0.1.0_aarch64_generic.ipk "${target}:/tmp/"
ssh $target "opkg install /tmp/tg-downloader-ui-full_0.1.0_aarch64_generic.ipk"
```

The full package still requires first-run administrator setup and Telegram authentication.

Expected full-package checks:

```sh
opkg status tg-downloader-ui-full
tdl version
tg-downloader-ui --check
test -f /usr/share/licenses/tg-downloader-ui-full/tdl-AGPL-3.0.txt
test -f /usr/share/licenses/tg-downloader-ui-full/tdl-NOTICE.txt
```

Generic-package path: upload and install the architecture-independent package
(after completing the separate `tdl` installation section above):

```powershell
$target = "$env:OPENWRT_TEST_USER@$env:OPENWRT_TEST_HOST"
scp .\dist\openwrt\tg-downloader-ui_0.1.0_all.ipk "${target}:/tmp/"
ssh $target "opkg install /tmp/tg-downloader-ui_0.1.0_all.ipk"
```

Expected:

- `/opt/tg-downloader-ui/app.py` exists.
- `/etc/init.d/tg-downloader-ui` exists and is executable.
- `/etc/tg-downloader-ui.env.example` exists.
- If `/etc/tg-downloader-ui.env` did not already exist, package post-install
  created it from the example with mode `600`.
- LuCI menu cache is cleared.

Manual fallback: copy files directly.

From your development machine:

```powershell
ssh root@OPENWRT_IP "mkdir -p /opt/tg-downloader-ui"
scp .\tg_downloader_ui\app.py root@OPENWRT_IP:/opt/tg-downloader-ui/app.py
scp .\tg_downloader_ui\forwarder.py root@OPENWRT_IP:/opt/tg-downloader-ui/forwarder.py
scp .\tg_downloader_ui\sources.py root@OPENWRT_IP:/opt/tg-downloader-ui/sources.py
scp .\tg-downloader-ui.init root@OPENWRT_IP:/etc/init.d/tg-downloader-ui
scp .\openwrt\tg-downloader-ui.env.example root@OPENWRT_IP:/etc/tg-downloader-ui.env
```

On OpenWRT:

```sh
chmod +x /opt/tg-downloader-ui/app.py /opt/tg-downloader-ui/forwarder.py
chmod +x /etc/init.d/tg-downloader-ui
chmod 600 /etc/tg-downloader-ui.env
python3 -m py_compile /opt/tg-downloader-ui/app.py /opt/tg-downloader-ui/forwarder.py /opt/tg-downloader-ui/sources.py
```

Expected: no output from `py_compile`.

## 5. Configure Environment

Edit:

```sh
vi /etc/tg-downloader-ui.env
```

Minimum basic-download configuration:

```sh
TGDL_HOST=0.0.0.0
TGDL_PORT=9910
TGDL_STATE_DIR=/etc/tg-downloader-ui
TGDL_DOWNLOAD_DIR=/root/telegram-downloads
TGDL_TDL_BIN=/usr/bin/tdl
TGDL_TDL_STORAGE=type=bolt,path=/etc/tg-downloader-ui/tdl/data
TGDL_TDL_LOG=/etc/tg-downloader-ui/tdl.log
TGDL_PROXY=
TGDL_TDL_PROXY=
TGDL_TELEGRAM_PROXY=
TGDL_SESSION_MAX_AGE=604800
```

If your network requires a proxy:

```sh
TGDL_PROXY=socks5://127.0.0.1:1080
TGDL_TDL_PROXY=
TGDL_TELEGRAM_PROXY=
```

`TGDL_TDL_PROXY` overrides `TGDL_PROXY` only for `tdl`.
`TGDL_TELEGRAM_PROXY` overrides `TGDL_PROXY` only for forwarder/Telethon.

Create directories:

```sh
mkdir -p /etc/tg-downloader-ui/tdl /root/telegram-downloads
chmod 700 /etc/tg-downloader-ui
```

Note: on many OpenWrt/iStoreOS images `/mnt` is only a small tmpfs until you
mount a USB/HDD. Prefer `/root/telegram-downloads` for the package default, and
change `TGDL_DOWNLOAD_DIR` to the real mount (for example
`/mnt/sda1/telegram-downloads`) when external storage is available.

## 6. Start Web UI

Start the service:

```sh
/etc/init.d/tg-downloader-ui enable
/etc/init.d/tg-downloader-ui restart
logread -e tg-downloader-ui
```

Check the port:

```sh
netstat -lntp | grep 9910 || ss -lntp | grep 9910
```

Open:

```text
http://OPENWRT_IP:9910
```

Expected:

- First visit redirects to `/setup`.
- Login page is not usable before setup.
- API requests before setup return `setup required`.

## 7. First-Run Setup

In the setup page, fill:

- Admin username: your choice.
- Admin password: your choice.
- Download directory: the exact absolute path from `TGDL_DOWNLOAD_DIR`, for
  example `/root/telegram-downloads` (or an external mount such as
  `/mnt/sda1/telegram-downloads`).

Forwarder fields can stay empty for basic download testing.

Submit setup.

Expected:

- Browser moves to `/login`.
- Login succeeds with the new admin credentials.
- `/etc/tg-downloader-ui/config.json` exists.
- `config.json` does not contain the plaintext admin password.

Check on OpenWRT:

```sh
ls -l /etc/tg-downloader-ui/config.json
grep -n "password" /etc/tg-downloader-ui/config.json
```

Expected: password hash/salt fields may exist, plaintext password must not.

## 8. Login tdl

Run `tdl login` using the same storage configured for the app:

```sh
tdl --storage type=bolt,path=/etc/tg-downloader-ui/tdl/data login
```

Follow the prompts and log in with your own Telegram account.

Smoke-check `tdl` can access a source chat:

```sh
tdl --storage type=bolt,path=/etc/tg-downloader-ui/tdl/data chat ls
```

Expected: your accessible chats are listed.

## 9. Configure Sources

In Web UI:

1. Open the sources page.
2. Add a source that your `tdl` account can access.
3. Set:
   - label: human-readable name
   - `tdl` chat: chat username or identifier used by `tdl`
   - forward source: optional `@username`, used by forwarder mode
   - enabled: checked
   - default: selected
4. Save.

Expected:

- Reloading the page keeps the source.
- Download page source dropdown includes the source.
- If optional forwarder mode is already running, click the Web UI forwarder
  restart button after saving sources. The forwarder reads source configuration
  on startup, so newly added forward sources are used after restart.

## 10. Basic Download Test

Pick a Telegram message ID from the configured source that contains a file.

Submit it in the Web UI.

Expected job flow:

```text
queued -> exporting -> downloading -> done
```

Check files:

```sh
find /root/telegram-downloads -type f | head
```

Check logs:

```sh
ls -R /etc/tg-downloader-ui/logs
tail -n 80 /etc/tg-downloader-ui/logs/*.log
```

Pass criteria:

- Downloaded file exists under `TGDL_DOWNLOAD_DIR`.
- Job status is `done`, or `skipped` if the exact final file already existed.
- Job log shows the `tdl` export/download commands.
- No secrets are printed in the Web UI.

## 11. Cancel, Retry, and Delete

Use a larger test file if possible.

Cancel:

1. Submit a download.
2. Click cancel while it is active.

Expected:

- Status becomes `canceled` or cancel-requested while active.
- Partial temporary files are cleaned when the worker handles cancellation.

Retry:

1. Retry a canceled or failed job.

Expected:

- Attempts count increases.
- Job returns to `queued` then runs again.

Delete:

1. Delete a finished job.

Expected:

- Job row disappears.
- Export/log side files for that job are removed.

## 12. Persistence Test

Restart:

```sh
/etc/init.d/tg-downloader-ui restart
```

Refresh the Web UI.

Expected:

- Login still works with the initialized admin account.
- Download directory and sources persist.
- Job history persists.
- Active jobs from before restart are marked failed with a restart message.

Reboot test:

```sh
reboot
```

After the router returns:

```sh
/etc/init.d/tg-downloader-ui status
logread -e tg-downloader-ui
```

Expected: service starts automatically if enabled.

## 13. LuCI Link Test

Copy the OpenWRT LuCI files from your development machine:

```powershell
scp -r .\openwrt root@OPENWRT_IP:/tmp/tg-downloader-ui-openwrt
```

On OpenWRT:

```sh
chmod +x /tmp/tg-downloader-ui-openwrt/install-luci-link.sh
/tmp/tg-downloader-ui-openwrt/install-luci-link.sh
```

Open LuCI and verify:

```text
Services -> Telegram Downloads
```

Expected: clicking the menu entry opens `http://OPENWRT_IP:9910`.

## 14. Optional Forwarder Test

Skip this section if you only need basic download mode.

Prepare:

1. Create a Telegram app at https://my.telegram.org.
2. Copy your own `api_id` and `api_hash`.
3. Create your own target Telegram channel.
4. Get the numeric channel ID.
5. Use the Web UI `Telegram 授权` page to save the Telegram config and generate
   a Telethon string session by SMS/code or QR scan.

The OpenWRT package depends on `python3-pip` and tries to install Telethon and
QR generation support during post-install. If the install happened while the
router had no working network, run:

```sh
python3 -m pip install --no-cache-dir 'telethon>=1.35' 'qrcode>=7.4'
```

If you prefer env-file configuration, edit `/etc/tg-downloader-ui.env`:

```sh
TGDL_API_ID=your_api_id
TGDL_API_HASH=your_api_hash
TGDL_SESSION_FILE=/etc/tg-downloader-ui/session.txt
TGDL_FORWARD_SOURCE=@your_source_username
TGDL_FORWARD_CHANNEL_ID=-100your_channel_id
TGDL_TELEGRAM_PROXY=
```

Restart:

```sh
/etc/init.d/tg-downloader-ui restart
logread -e tg-downloader-ui
```

Expected:

- Forwarder status is not `failed`.
- Web UI forwarder status updates.
- Sending a test message from the configured source results in a forwarded
  summary in your channel.
- Clicking the Web UI forwarder restart button schedules an init-service
  restart. This also recovers the case where the `forwarder` procd instance
  did not exist yet.

If it fails, check:

```sh
cat /etc/tg-downloader-ui/forwarder_status.json
tail -n 100 /etc/tg-downloader-ui/forwarder.log
```

Common failures:

- `TGDL_API_ID is required`: env file did not set API ID.
- `TGDL_API_HASH is required`: env file did not set API hash.
- `TGDL_FORWARD_CHANNEL_ID is required`: target channel ID is empty.
- `session file not found`: `TGDL_SESSION_FILE` path is wrong.
- proxy errors: set `TGDL_TELEGRAM_PROXY` or `TGDL_PROXY`.

## 15. Upgrade Test

Deploy new files over an existing configured install:

```powershell
scp .\tg_downloader_ui\app.py root@OPENWRT_IP:/opt/tg-downloader-ui/app.py
scp .\tg_downloader_ui\forwarder.py root@OPENWRT_IP:/opt/tg-downloader-ui/forwarder.py
scp .\tg_downloader_ui\sources.py root@OPENWRT_IP:/opt/tg-downloader-ui/sources.py
scp .\tg-downloader-ui.init root@OPENWRT_IP:/etc/init.d/tg-downloader-ui
```

On OpenWRT:

```sh
python3 -m py_compile /opt/tg-downloader-ui/app.py /opt/tg-downloader-ui/forwarder.py /opt/tg-downloader-ui/sources.py
/etc/init.d/tg-downloader-ui restart
```

Expected:

- Existing `config.json` is preserved.
- Existing database is preserved.
- Web UI does not return to first-run setup.
- Existing sources and download path still load.

## 16. Rollback and Cleanup

Stop service:

```sh
/etc/init.d/tg-downloader-ui stop
/etc/init.d/tg-downloader-ui disable
```

Remove app files:

```sh
rm -rf /opt/tg-downloader-ui
rm -f /etc/init.d/tg-downloader-ui
```

Remove LuCI link:

```sh
rm -f /usr/share/luci/menu.d/luci-app-tg-downloader-ui.json
rm -f /usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json
rm -rf /www/luci-static/resources/view/tg-downloader-ui
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd restart
/etc/init.d/uhttpd restart
```

Remove runtime data only if you no longer need it:

```sh
rm -rf /etc/tg-downloader-ui
rm -rf /root/telegram-downloads
```

## 17. aarch64 full IPK lab (macvlan only, no host network)

Use this when verifying `tg-downloader-ui-full_*_aarch64_generic.ipk` on an
**amd64 Ubuntu Docker lab** (not a native ARM board). Hard rules:

- Never use Docker `--network host`.
- Never change the host primary address or default route.
- Use a **disposable macvlan** on the host LAN parent NIC with a free IP.
- Tear down the guest container and macvlan after the test.

Outline (adjust parent NIC, subnet, gateway, free IP to your lab):

```sh
# On the lab host (example parent ens33, free IP not the host address):
# 1) Snapshot host LAN first: ip -br addr; ip route
# 2) Install qemu-user-static + binfmt-support (minimal packages only)
# 3) docker network create -d macvlan \
#      --subnet=192.168.101.0/24 --gateway=192.168.101.2 \
#      -o parent=ens33 tgdl-macvlan-test
# 4) Run OpenWrt aarch64 rootfs with --platform linux/arm64
#      --network tgdl-macvlan-test --ip <free-ip>
# 5) Inside guest: opkg install the aarch64_generic full IPK;
#      tdl version; tg-downloader-ui --check; license files present
# 6) Destroy container + docker network rm tgdl-macvlan-test
# 7) Confirm host address/route unchanged
```

Do not commit lab host credentials, sudo passwords, or private channel IDs.

## 18. Final Acceptance Checklist

- [ ] Web UI starts from OpenWRT init script.
- [ ] First-run setup is required.
- [ ] No default admin password works before setup.
- [ ] Admin login works after setup.
- [ ] `tdl version` works.
- [ ] `tdl login` completed with your own Telegram account.
- [ ] Source config persists.
- [ ] A known message ID downloads successfully.
- [ ] Restart preserves config and job history.
- [ ] LuCI menu entry opens the Web UI.
- [ ] Optional forwarder works, if enabled.
- [ ] Logs do not expose Telegram API hash or session strings.
- [ ] aarch64 full IPK (if tested) used macvlan only, not host network.
