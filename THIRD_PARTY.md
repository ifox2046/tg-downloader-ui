# Third-Party Notices

## iyear/tdl

This project integrates with `iyear/tdl` as an external Telegram downloader
runtime.

- Project: https://github.com/iyear/tdl
- Version: 0.20.3
- Source tag: https://github.com/iyear/tdl/tree/v0.20.3
- License: GNU Affero General Public License v3.0
- Use in this project: invoked as the `tdl` command-line program

The multi-arch Docker image (`ifox2046/tg-downloader-ui`, platforms
`linux/amd64` and `linux/arm64`) and full OpenWrt IPKs install an unmodified
`tdl 0.20.3` release binary from the upstream GitHub release archive. Each
archive is verified with a pinned SHA-256 before extraction:

| Consumer | Platform / package arch | Upstream asset | SHA-256 |
| --- | --- | --- | --- |
| Docker (BuildKit `TARGETARCH=amd64`) | `linux/amd64` | `tdl_Linux_64bit.tar.gz` | `f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718` |
| Docker (BuildKit `TARGETARCH=arm64`) | `linux/arm64` | `tdl_Linux_arm64.tar.gz` | `8398784d5b9390d26450e3e3528e2ffd0e9fe75d374f63273d0247e7ab0378b7` |
| OpenWrt full IPK | `x86_64` | `tdl_Linux_64bit.tar.gz` | same as amd64 above |
| OpenWrt full IPK | `aarch64_generic` | `tdl_Linux_arm64.tar.gz` | same as arm64 above |

Each full IPK installs the upstream AGPL-3.0 license and a version/source notice under `/usr/share/licenses/tg-downloader-ui-full`.

The generic `tg-downloader-ui_0.1.4_all.ipk` and the Python package do not bundle `tdl`; users provide it separately.

Anyone publishing a prebuilt image containing `tdl` must satisfy the
corresponding AGPL-3.0 source-code availability and notice obligations.

## Python Dependencies

- Telethon 1.44.0 — MIT
- qrcode 8.2 — BSD
- python-socks 2.8.2 — Apache-2.0 (Telethon proxy; Docker/`pip install .`)
- PySocks 1.7.1 — BSD (optional `socks` module; aligned with OpenWrt vendor set)
- rsa 4.9.1 — Apache-2.0
- pyasn1 0.6.1 — BSD-2-Clause
- pyaes 1.6.1 — MIT
- async-timeout 5.0.1 — Apache-2.0 (pulled for older Python / OpenWrt vendor set)

The OpenWRT IPK includes these packages and installs their license texts under
`/usr/share/licenses/tg-downloader-ui`.

This project is not affiliated with Telegram and is not an official `tdl`
project.
