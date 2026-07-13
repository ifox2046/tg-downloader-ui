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
META_PACKAGE_NAME = "app-meta-tg-downloader-ui"
META_APP_NAME = "tg-downloader-ui"
ARCHITECTURE = "all"
DEPENDS = ["python3", "python3-sqlite3", "ca-bundle"]
META_RELEASE = 1


# Ship under /usr/lib so opkg extraction always lands on a normal overlay path.
# /opt is still preferred as a compatibility symlink when that directory works
# (plain OpenWrt / healthy iStoreOS). Broken /opt overlays (seen with some
# iStoreOS Docker data roots) must not prevent installation.
APP_INSTALL_ROOT = "./usr/lib/tg-downloader-ui"
APP_RUNTIME_HOME = "/usr/lib/tg-downloader-ui"
APP_COMPAT_LINK = "/opt/tg-downloader-ui"

DATA_FILES = [
    ("tg_downloader_ui/app.py", f"{APP_INSTALL_ROOT}/app.py", 0o755),
    ("tg_downloader_ui/forwarder.py", f"{APP_INSTALL_ROOT}/forwarder.py", 0o755),
    ("tg_downloader_ui/sources.py", f"{APP_INSTALL_ROOT}/sources.py", 0o644),
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
    "./etc",
    "./etc/init.d",
    "./usr",
    "./usr/lib",
    "./usr/lib/tg-downloader-ui",
    "./usr/lib/tg-downloader-ui/vendor",
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


POSTINST = f"""#!/bin/sh
set -e

APP_HOME="{APP_RUNTIME_HOME}"
COMPAT_LINK="{APP_COMPAT_LINK}"

mkdir -p /etc/tg-downloader-ui /etc/tg-downloader-ui/tdl

if [ ! -f /etc/tg-downloader-ui.env ]; then
\tcp /etc/tg-downloader-ui.env.example /etc/tg-downloader-ui.env
\tchmod 600 /etc/tg-downloader-ui.env
fi

# Best-effort /opt compatibility symlink. Never fail install if /opt is broken
# (common on some iStoreOS Docker layouts).
if [ -d "$APP_HOME" ]; then
\tif [ -L "$COMPAT_LINK" ]; then
\t\tln -sfn "$APP_HOME" "$COMPAT_LINK" 2>/dev/null || true
\telif [ ! -e "$COMPAT_LINK" ]; then
\t\tmkdir -p /opt 2>/dev/null || true
\t\tln -s "$APP_HOME" "$COMPAT_LINK" 2>/dev/null || true
\tfi
fi

chmod +x /etc/init.d/tg-downloader-ui
/etc/init.d/tg-downloader-ui enable >/dev/null 2>&1 || true

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd restart >/dev/null 2>&1 || true
/etc/init.d/uhttpd restart >/dev/null 2>&1 || true

exit 0
"""


PRERM = f"""#!/bin/sh
set -e

APP_HOME="{APP_RUNTIME_HOME}"
COMPAT_LINK="{APP_COMPAT_LINK}"

if [ "$1" = "remove" ]; then
\t/etc/init.d/tg-downloader-ui stop >/dev/null 2>&1 || true
\t/etc/init.d/tg-downloader-ui disable >/dev/null 2>&1 || true
\tif [ -L "$COMPAT_LINK" ]; then
\t\tlink_target="$(readlink "$COMPAT_LINK" 2>/dev/null || true)"
\t\tif [ "$link_target" = "$APP_HOME" ]; then
\t\t\trm -f "$COMPAT_LINK" 2>/dev/null || true
\t\tfi
\tfi
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


def meta_version(version: str, release: int = META_RELEASE) -> str:
    return f"{version}-r{release}"


def istore_meta_json(version: str, release: int = META_RELEASE) -> dict[str, object]:
    """iStore installed-app metadata consumed from /usr/lib/opkg/meta/*.json."""
    return {
        "name": META_APP_NAME,
        "title": "Telegram Downloads",
        "entry": "/cgi-bin/luci/admin/services/tg-downloader-ui",
        "author": "tg-downloader-ui contributors",
        "website": "https://github.com/iyear/tdl",
        "version": version,
        "release": release,
        "arch": ["all"],
        "description": "基于 tdl 的 Telegram 下载 Web 控制台与任务管理。",
        "description_en": (
            "Web UI and automation layer for Telegram downloads with tdl."
        ),
        "tags": ["net", "tool"],
        # Only the main app package. Shared runtime deps stay on the main package
        # so iStore uninstall does not remove packages used by other software.
        "depends": [PACKAGE_NAME],
    }


def meta_control_text(version: str, release: int = META_RELEASE) -> str:
    return (
        f"Package: {META_PACKAGE_NAME}\n"
        f"Version: {meta_version(version, release)}\n"
        f"Architecture: {ARCHITECTURE}\n"
        f"Maintainer: tg-downloader-ui contributors\n"
        f"License: MIT\n"
        f"Depends: {PACKAGE_NAME}\n"
        f"Provides: {META_PACKAGE_NAME}-any\n"
        f"Section: meta\n"
        f"Priority: optional\n"
        f"Description: iStore metadata for {PACKAGE_NAME}.\n"
    )


def meta_control_tar(version: str, release: int = META_RELEASE) -> bytes:
    # Keep scripts no-op friendly on plain OpenWrt without OpenWrt package helpers.
    postinst = "#!/bin/sh\nexit 0\n"
    prerm = "#!/bin/sh\nexit 0\n"
    return tar_gz(
        [
            ("./control", meta_control_text(version, release).encode("utf-8"), 0o644),
            ("./postinst", postinst.encode("utf-8"), 0o755),
            ("./prerm", prerm.encode("utf-8"), 0o755),
        ]
    )


def meta_data_tar(version: str, release: int = META_RELEASE) -> bytes:
    payload = (
        json.dumps(istore_meta_json(version, release), ensure_ascii=False, indent=2)
        + "\n"
    ).encode("utf-8")
    entries: list[tuple[str, bytes | None, int]] = [
        ("./usr", None, 0o755),
        ("./usr/lib", None, 0o755),
        ("./usr/lib/opkg", None, 0o755),
        ("./usr/lib/opkg/meta", None, 0o755),
        (
            f"./usr/lib/opkg/meta/{META_APP_NAME}.json",
            payload,
            0o644,
        ),
    ]
    return tar_gz(with_parent_directories(entries))


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


def with_parent_directories(
    entries: list[tuple[str, bytes | None, int]],
) -> list[tuple[str, bytes | None, int]]:
    """Ensure every file path has explicit directory members.

    Older OpenWrt/iStoreOS opkg extractors do not auto-create missing parents
    when unpacking data.tar.gz, so nested vendor trees fail with wfopen errors.
    """
    dir_modes: dict[str, int] = {}
    file_entries: list[tuple[str, bytes, int]] = []
    for name, content, mode in entries:
        normalized = name.rstrip("/")
        if content is None:
            dir_modes.setdefault(normalized, mode)
            continue
        file_entries.append((normalized, content, mode))
        leading_dot_slash = normalized.startswith("./")
        for parent in PurePosixPath(normalized).parents:
            text = parent.as_posix()
            if text in (".", ""):
                continue
            if leading_dot_slash and not text.startswith("./"):
                text = f"./{text}"
            dir_modes.setdefault(text, 0o755)
    ordered_dirs = sorted(dir_modes.items(), key=lambda item: (item[0].count("/"), item[0]))
    return [(name, None, mode) for name, mode in ordered_dirs] + file_entries


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
                (
                    f"{APP_INSTALL_ROOT}/vendor/{path.as_posix()}",
                    content,
                    0o644,
                )
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
    return tar_gz(with_parent_directories(entries))


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


def build_meta_ipk(
    root: Path,
    output_dir: Path,
    version: str | None = None,
    architecture: str = ARCHITECTURE,
    release: int = META_RELEASE,
) -> Path:
    """Build the iStore app-meta package that powers the Installed apps list."""
    if architecture != ARCHITECTURE:
        raise ValueError("only architecture 'all' is supported")
    resolved_version = version or project_version(root)
    meta_ver = meta_version(resolved_version, release)
    output_path = (
        output_dir / f"{META_PACKAGE_NAME}_{meta_ver}_{architecture}.ipk"
    )
    write_outer_tar(
        output_path,
        [
            ("debian-binary", b"2.0\n"),
            ("data.tar.gz", meta_data_tar(resolved_version, release)),
            ("control.tar.gz", meta_control_tar(resolved_version, release)),
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
    parser.add_argument(
        "--skip-meta",
        action="store_true",
        help="do not build the iStore app-meta package",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    ipk = build_ipk(root, args.output_dir, version=args.version)
    print(ipk)
    if not args.skip_meta:
        meta_ipk = build_meta_ipk(root, args.output_dir, version=args.version)
        print(meta_ipk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
