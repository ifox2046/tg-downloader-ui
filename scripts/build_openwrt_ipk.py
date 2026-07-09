#!/usr/bin/env python3
"""Build an OpenWRT .ipk package without requiring the OpenWRT SDK."""

from __future__ import annotations

import argparse
import gzip
import io
import re
import tarfile
from pathlib import Path


PACKAGE_NAME = "tg-downloader-ui"
ARCHITECTURE = "all"
DEPENDS = ["python3", "python3-sqlite3", "python3-pip", "ca-bundle", "curl", "tar"]


DATA_FILES = [
    ("tg_downloader_ui/app.py", "./opt/tg-downloader-ui/app.py", 0o755),
    ("tg_downloader_ui/forwarder.py", "./opt/tg-downloader-ui/forwarder.py", 0o755),
    ("tg_downloader_ui/sources.py", "./opt/tg-downloader-ui/sources.py", 0o644),
    ("tg-downloader-ui.init", "./etc/init.d/tg-downloader-ui", 0o755),
    ("openwrt/tg-downloader-ui.env.example", "./etc/tg-downloader-ui.env.example", 0o644),
    (
        "openwrt/usr/share/luci/menu.d/luci-app-tg-downloader-ui.json",
        "./usr/share/luci/menu.d/luci-app-tg-downloader-ui.json",
        0o644,
    ),
    (
        "openwrt/usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json",
        "./usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json",
        0o644,
    ),
    (
        "openwrt/www/luci-static/resources/view/tg-downloader-ui/link.js",
        "./www/luci-static/resources/view/tg-downloader-ui/link.js",
        0o644,
    ),
]

DATA_DIRS = [
    "./opt",
    "./opt/tg-downloader-ui",
    "./etc",
    "./etc/init.d",
    "./usr",
    "./usr/share",
    "./usr/share/luci",
    "./usr/share/luci/menu.d",
    "./usr/share/rpcd",
    "./usr/share/rpcd/acl.d",
    "./www",
    "./www/luci-static",
    "./www/luci-static/resources",
    "./www/luci-static/resources/view",
    "./www/luci-static/resources/view/tg-downloader-ui",
]


POSTINST = """#!/bin/sh
set -e

mkdir -p /etc/tg-downloader-ui /etc/tg-downloader-ui/tdl

if [ ! -f /etc/tg-downloader-ui.env ]; then
\tcp /etc/tg-downloader-ui.env.example /etc/tg-downloader-ui.env
\tchmod 600 /etc/tg-downloader-ui.env
fi

chmod +x /etc/init.d/tg-downloader-ui
/etc/init.d/tg-downloader-ui enable >/dev/null 2>&1 || true
python3 -m pip install --no-cache-dir 'telethon>=1.35' 'qrcode>=7.4' >/dev/null 2>&1 || true

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd restart >/dev/null 2>&1 || true
/etc/init.d/uhttpd restart >/dev/null 2>&1 || true

exit 0
"""


PRERM = """#!/bin/sh
set -e

if [ "$1" = "remove" ]; then
\t/etc/init.d/tg-downloader-ui stop >/dev/null 2>&1 || true
\t/etc/init.d/tg-downloader-ui disable >/dev/null 2>&1 || true
fi

exit 0
"""


POSTRM = """#!/bin/sh
set -e

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd restart >/dev/null 2>&1 || true
/etc/init.d/uhttpd restart >/dev/null 2>&1 || true

exit 0
"""


def project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise ValueError("project version not found in pyproject.toml")
    return match.group(1)


def control_text(version: str) -> str:
    return (
        f"Package: {PACKAGE_NAME}\n"
        f"Version: {version}\n"
        f"Architecture: {ARCHITECTURE}\n"
        f"Maintainer: tg-downloader-ui contributors\n"
        f"License: MIT\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Section: net\n"
        f"Priority: optional\n"
        f"Description: Web UI and automation layer for Telegram downloads with tdl.\n"
    )


def gzip_bytes(payload: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(payload)
    return out.getvalue()


def tar_gz(entries: list[tuple[str, bytes | None, int]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            if content is None:
                info.type = tarfile.DIRTYPE
                info.size = 0
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return gzip_bytes(raw.getvalue())


def control_tar(version: str) -> bytes:
    return tar_gz(
        [
            ("./control", control_text(version).encode("utf-8"), 0o644),
            ("./postinst", POSTINST.encode("utf-8"), 0o755),
            ("./prerm", PRERM.encode("utf-8"), 0o755),
            ("./postrm", POSTRM.encode("utf-8"), 0o755),
        ]
    )


def data_tar(root: Path) -> bytes:
    entries: list[tuple[str, bytes | None, int]] = [
        (directory, None, 0o755) for directory in DATA_DIRS
    ]
    for source, target, mode in DATA_FILES:
        path = root / source
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append((target, path.read_bytes(), mode))
    return tar_gz(entries)


def write_outer_tar(path: Path, members: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [(f"./{name}", data, 0o644) for name, data in members]
    path.write_bytes(tar_gz(entries))


def build_ipk(
    root: Path,
    output_dir: Path,
    version: str | None = None,
    architecture: str = ARCHITECTURE,
) -> Path:
    if architecture != ARCHITECTURE:
        raise ValueError("only architecture 'all' is supported")
    resolved_version = version or project_version(root)
    output_path = output_dir / f"{PACKAGE_NAME}_{resolved_version}_{architecture}.ipk"
    write_outer_tar(
        output_path,
        [
            ("debian-binary", b"2.0\n"),
            ("data.tar.gz", data_tar(root)),
            ("control.tar.gz", control_tar(resolved_version)),
        ],
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build OpenWRT .ipk package")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist/openwrt"),
        help="directory for the generated .ipk",
    )
    parser.add_argument("--version", default=None, help="override package version")
    args = parser.parse_args()

    ipk = build_ipk(args.root.resolve(), args.output_dir, version=args.version)
    print(ipk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
