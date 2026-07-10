#!/usr/bin/env python3
"""Build an OpenWRT .ipk package without requiring the OpenWRT SDK."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import io
import json
import re
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


PACKAGE_NAME = "tg-downloader-ui"
ARCHITECTURE = "all"
DEPENDS = ["python3", "python3-sqlite3", "ca-bundle"]


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
    "./opt/tg-downloader-ui/vendor",
    "./etc",
    "./etc/init.d",
    "./usr",
    "./usr/share",
    "./usr/share/licenses",
    "./usr/share/licenses/tg-downloader-ui",
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


def download_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def verify_sha256(payload: bytes, expected: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError(
            f"vendor artifact sha256 mismatch: expected {expected}, got {actual}"
        )


def archive_files(payload: bytes, archive_type: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if archive_type == "wheel":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe vendor member: {info.filename}")
                files[path.as_posix()] = archive.read(info)
        return files
    if archive_type == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe vendor member: {member.name}")
                handle = archive.extractfile(member)
                if handle is not None:
                    files[path.as_posix()] = handle.read()
        return files
    raise ValueError(f"unsupported vendor archive: {archive_type}")


def vendor_entries(
    root: Path, fetcher=download_bytes
) -> list[tuple[str, bytes, int]]:
    lock = json.loads(
        (root / "openwrt/vendor-lock.json").read_text(encoding="utf-8")
    )
    entries: list[tuple[str, bytes, int]] = []
    for package in lock["packages"]:
        payload = fetcher(package["url"])
        verify_sha256(payload, package["sha256"])
        files = archive_files(payload, package["archive"])
        package_prefix = package["package_prefix"]
        strip_prefix = package["strip_prefix"]
        copied = 0
        for name, content in files.items():
            if not name.startswith(package_prefix):
                continue
            relative = name[len(strip_prefix) :] if strip_prefix else name
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe vendor target: {relative}")
            entries.append(
                (f"./opt/tg-downloader-ui/vendor/{path.as_posix()}", content, 0o644)
            )
            copied += 1
        if not copied:
            raise ValueError(f"vendor package files not found: {package['name']}")
        license_member = package["license_member"]
        if license_member not in files:
            raise ValueError(f"vendor license not found: {package['name']}")
        entries.append(
            (
                f"./usr/share/licenses/tg-downloader-ui/{package['name']}-LICENSE",
                files[license_member],
                0o644,
            )
        )
    return entries


def data_tar(root: Path) -> bytes:
    entries: list[tuple[str, bytes | None, int]] = [
        (directory, None, 0o755) for directory in DATA_DIRS
    ]
    for source, target, mode in DATA_FILES:
        path = root / source
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append((target, path.read_bytes(), mode))
    entries.extend(vendor_entries(root))
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
