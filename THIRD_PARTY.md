# Third-Party Notices

## iyear/tdl

This project integrates with `iyear/tdl` as an external Telegram downloader
runtime.

- Project: https://github.com/iyear/tdl
- Version: 0.20.3
- Source tag: https://github.com/iyear/tdl/tree/v0.20.3
- License: GNU Affero General Public License v3.0
- Use in this project: invoked as the `tdl` command-line program

The Docker image and `tg-downloader-ui-full_0.1.0_x86_64.ipk` install an unmodified `tdl 0.20.3` release binary from the upstream GitHub release archive. The archive is verified with a pinned SHA-256 before extraction. The full IPK installs the upstream AGPL-3.0 license and a version/source notice under `/usr/share/licenses/tg-downloader-ui-full`.

The generic `tg-downloader-ui_0.1.0_all.ipk` and the Python package do not bundle `tdl`; users provide it separately.

Anyone publishing a prebuilt image containing `tdl` must satisfy the
corresponding AGPL-3.0 source-code availability and notice obligations.

## Python Dependencies

- Telethon 1.44.0 — MIT
- qrcode 8.2 — BSD
- rsa 4.9.1 — Apache-2.0
- pyasn1 0.6.1 — BSD-2-Clause
- pyaes 1.6.1 — MIT

The OpenWRT IPK includes these packages and installs their license texts under
`/usr/share/licenses/tg-downloader-ui`.

This project is not affiliated with Telegram and is not an official `tdl`
project.
