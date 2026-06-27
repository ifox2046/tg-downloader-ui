# tg-downloader-ui

Lightweight OpenWRT web UI for managing Telegram downloads with `tdl`.

## Defaults

- Web UI: `http://192.168.31.157:9910`
- Auth: `admin / admin123`
- Chat: `Youxiu_bot`
- Download dir: `/mnt/sata1-5/telegram_downloads`
- State dir: `/mnt/sata1-5/tg-downloader-ui`
- tdl storage: `type=bolt,path=/root/.tdl/data`
- Proxy: `socks5://127.0.0.1:7891`

## Test

```powershell
python -m unittest tests.test_tg_downloader_ui -v
python -m compileall tg_downloader_ui tests
```

## Deploy

```powershell
scp .\tg_downloader_ui\app.py root@192.168.31.157:/opt/tg-downloader-ui/app.py
scp .\tg-downloader-ui.init root@192.168.31.157:/etc/init.d/tg-downloader-ui
ssh root@192.168.31.157 "chmod +x /opt/tg-downloader-ui/app.py /etc/init.d/tg-downloader-ui && python3 -m py_compile /opt/tg-downloader-ui/app.py && python3 /opt/tg-downloader-ui/app.py --check && /etc/init.d/tg-downloader-ui restart"
```

## API

Submit a message ID:

```powershell
$auth = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('admin:admin123'))
Invoke-RestMethod -Uri 'http://192.168.31.157:9910/api/jobs' `
  -Method Post `
  -Headers @{Authorization=$auth} `
  -ContentType 'application/json' `
  -Body '{"message_ids":[23311]}'
```
