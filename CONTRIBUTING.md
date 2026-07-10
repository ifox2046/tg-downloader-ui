# Contributing

Use small, focused pull requests.

Before submitting changes, run:

```sh
python -m unittest discover tests -v
python tests/test_release_safety.py -v
python -m compileall tg_downloader_ui tests
python -m build
python scripts/build_openwrt_ipk.py --output-dir dist/openwrt
docker build --build-arg TDL_VERSION=0.20.3 -t tg-downloader-ui:test .
docker run --rm tg-downloader-ui:test tdl version
```

The wheel/sdist and IPK must build successfully. The IPK vendor directory must
import `telethon`, `qrcode`, `rsa`, `pyasn1`, and `pyaes` without network
access, and the Docker smoke check must report the expected `tdl` version.

You may place private regression literals in `.env.release-safety.local` using
`.env.release-safety.example` as the template. That local file is optional,
ignored by Git, and must never be committed or attached to an issue.

Do not commit real Telegram credentials, session files, private bot names,
private channel IDs, or machine-specific paths.
