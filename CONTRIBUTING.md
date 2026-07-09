# Contributing

Use small, focused pull requests.

Before submitting changes, run:

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python -m build
```

Do not commit real Telegram credentials, session files, private bot names,
private channel IDs, or machine-specific paths.
