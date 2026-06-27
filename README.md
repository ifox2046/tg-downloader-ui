# tg-downloader-ui

Lightweight OpenWRT web UI for managing Telegram downloads with `tdl`.

## Defaults

- Web UI: `http://192.168.31.157:9910`
- Initial admin: `admin / admin123`
- Chat: `Youxiu_bot`
- Download dir: `/mnt/sata1-5/telegram_downloads`
- State dir: `/mnt/sata1-5/tg-downloader-ui`
- tdl storage: `type=bolt,path=/root/.tdl/data`
- Proxy: `socks5://127.0.0.1:7891`

The first startup stores the admin password hash and runtime settings in:

```text
/mnt/sata1-5/tg-downloader-ui/config.json
```

## Test

```powershell
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
```

## Deploy

Create or update the OpenWRT environment file for the forwarder:

```sh
cat >/etc/tg-downloader-ui.env <<'EOF'
TGDL_API_ID=26375241
TGDL_API_HASH=your_telegram_api_hash
TGDL_SESSION_FILE=/opt/tg_session.txt
TGDL_PROXY=socks5://127.0.0.1:7891
TGDL_FORWARD_SOURCE=@Youxiu_bot
TGDL_FORWARD_CHANNEL_ID=-1004496489706
EOF
chmod 600 /etc/tg-downloader-ui.env
```

Sync and restart:

```powershell
scp .\tg_downloader_ui\app.py root@192.168.31.157:/opt/tg-downloader-ui/app.py
scp .\tg_downloader_ui\forwarder.py root@192.168.31.157:/opt/tg-downloader-ui/forwarder.py
scp .\tg-downloader-ui.init root@192.168.31.157:/etc/init.d/tg-downloader-ui
ssh root@192.168.31.157 "chmod +x /opt/tg-downloader-ui/app.py /opt/tg-downloader-ui/forwarder.py /etc/init.d/tg-downloader-ui && python3 -m py_compile /opt/tg-downloader-ui/app.py /opt/tg-downloader-ui/forwarder.py && python3 /opt/tg-downloader-ui/app.py --check && /etc/init.d/tg-downloader-ui restart"
```

## API

Login and keep the session cookie:

```powershell
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-RestMethod -Uri 'http://192.168.31.157:9910/api/auth/login' `
  -Method Post `
  -WebSession $session `
  -ContentType 'application/json' `
  -Body '{"username":"admin","password":"admin123"}'
```

Submit a message ID:

```powershell
Invoke-RestMethod -Uri 'http://192.168.31.157:9910/api/jobs' `
  -Method Post `
  -WebSession $session `
  -ContentType 'application/json' `
  -Body '{"message_ids":[23311]}'
```

Update download directory:

```powershell
Invoke-RestMethod -Uri 'http://192.168.31.157:9910/api/config' `
  -Method Put `
  -WebSession $session `
  -ContentType 'application/json' `
  -Body '{"download_dir":"/mnt/sata1-5/telegram_downloads"}'
```
