import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


def extract_shell_function(script: str, name: str) -> str:
    declaration = f"{name}() {{"
    lines = script.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == declaration:
            break
    else:
        raise ValueError(f"shell function not found: {name}")

    depth = 0
    function_lines = []
    for line in lines[start:]:
        function_lines.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "\n".join(function_lines)
    raise ValueError(f"shell function is not closed: {name}")


def load_builder():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "build_openwrt_ipk.py"
    spec = importlib.util.spec_from_file_location("build_openwrt_ipk", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def read_outer_tar_members(path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                members[member.name.lstrip("./")] = handle.read()
    return members


def make_wheel(package_name: str, license_member: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w") as archive:
        archive.writestr(f"{package_name}/__init__.py", "VERSION = 'test'\n")
        archive.writestr(license_member, f"license for {package_name}\n")
    return out.getvalue()


def make_sdist(package_name: str, version: str, license_member: str) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        files = {
            f"{package_name}-{version}/{package_name}/__init__.py": b"VERSION = 'test'\n",
            license_member: f"license for {package_name}\n".encode(),
        }
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def make_tdl_archive(
    binary: bytes = b"fake tdl 0.20.3 payload\n",
    license_text: bytes = b"GNU AFFERO GENERAL PUBLIC LICENSE Version 3\n",
) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        files = {
            "LICENSE": license_text,
            "README.md": b"tdl upstream readme\n",
            "README_zh.md": b"tdl upstream Chinese readme\n",
            "tdl": binary,
        }
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def fake_vendor_lock_and_payloads():
    definitions = [
        ("telethon", "1.44.0", "wheel", "telethon/", "", "telethon-1.44.0.dist-info/licenses/LICENSE"),
        ("qrcode", "8.2", "wheel", "qrcode/", "", "qrcode-8.2.dist-info/LICENSE"),
        ("rsa", "4.9.1", "wheel", "rsa/", "", "rsa-4.9.1.dist-info/LICENSE"),
        ("pyasn1", "0.6.1", "wheel", "pyasn1/", "", "pyasn1-0.6.1.dist-info/LICENSE.rst"),
        ("pyaes", "1.6.1", "tar.gz", "pyaes-1.6.1/pyaes/", "pyaes-1.6.1/", "pyaes-1.6.1/LICENSE.txt"),
        ("python_socks", "2.8.2", "wheel", "python_socks/", "", "python_socks-2.8.2.dist-info/licenses/LICENSE.txt"),
        ("async_timeout", "5.0.1", "wheel", "async_timeout/", "", "async_timeout-5.0.1.dist-info/LICENSE"),
        ("socks", "1.7.1", "wheel", "socks/", "", "socks-1.7.1.dist-info/LICENSE"),
    ]
    packages = []
    payloads = {}
    for name, version, archive_type, prefix, strip_prefix, license_member in definitions:
        payload = (
            make_wheel(name, license_member)
            if archive_type == "wheel"
            else make_sdist(name, version, license_member)
        )
        url = f"https://example.invalid/{name}-{version}"
        payloads[url] = payload
        packages.append(
            {
                "name": name,
                "version": version,
                "archive": archive_type,
                "url": url,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "package_prefix": prefix,
                "strip_prefix": strip_prefix,
                "license_member": license_member,
            }
        )
    return {"packages": packages}, payloads


def fake_vendor_entries():
    entries = []
    for package in [
        "telethon",
        "qrcode",
        "rsa",
        "pyasn1",
        "pyaes",
        "python_socks",
        "async_timeout",
        "socks",
    ]:
        entries.extend(
            [
                (
                    f"./usr/lib/tg-downloader-ui/vendor/{package}/__init__.py",
                    b"VERSION = 'test'\n",
                    0o644,
                ),
                (
                    f"./usr/share/licenses/tg-downloader-ui/{package}-LICENSE",
                    f"license for {package}\n".encode(),
                    0o644,
                ),
            ]
        )
    # Nested vendor path used to regress missing tar directory members.
    entries.append(
        (
            "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber/decoder.py",
            b"decoder = object()\n",
            0o644,
        )
    )
    return entries


def patch_full_arch_sha(builder, architecture: str, sha256: str):
    """Return a patched FULL_ARCH_PROFILES with one arch sha256 replaced."""
    profiles = {
        key: dict(value) for key, value in builder.FULL_ARCH_PROFILES.items()
    }
    openwrt_arch = builder.resolve_full_arch_profile(architecture)["openwrt_arch"]
    profiles[openwrt_arch] = dict(profiles[openwrt_arch])
    profiles[openwrt_arch]["tdl_sha256"] = sha256
    return mock.patch.object(builder, "FULL_ARCH_PROFILES", profiles)


class OpenWrtIpkBuilderTests(unittest.TestCase):
    def test_tdl_entries_reject_sha_mismatch(self):
        builder = load_builder()
        payload = make_tdl_archive()

        with patch_full_arch_sha(builder, "x86_64", "0" * 64):
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                builder.tdl_entries(fetcher=lambda _url: payload)

    def test_tdl_entries_reject_sha_mismatch_for_aarch64(self):
        builder = load_builder()
        payload = make_tdl_archive()

        with patch_full_arch_sha(builder, "aarch64_generic", "0" * 64):
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                builder.tdl_entries(
                    architecture="aarch64_generic",
                    fetcher=lambda _url: payload,
                )

    def test_resolve_full_arch_profile_aliases(self):
        builder = load_builder()
        for token in ("aarch64", "aarch64_generic", "arm64"):
            profile = builder.resolve_full_arch_profile(token)
            self.assertEqual(profile["openwrt_arch"], "aarch64_generic")
            self.assertEqual(profile["tdl_asset"], "tdl_Linux_arm64.tar.gz")
        x86 = builder.resolve_full_arch_profile("x86_64")
        self.assertEqual(x86["openwrt_arch"], "x86_64")
        self.assertEqual(x86["tdl_asset"], "tdl_Linux_64bit.tar.gz")
        with self.assertRaisesRegex(ValueError, "unsupported full package architecture"):
            builder.resolve_full_arch_profile("mips")

    def test_normalize_full_arch_list(self):
        builder = load_builder()
        self.assertEqual(builder.normalize_full_arch_list(None), ["x86_64"])
        self.assertEqual(builder.normalize_full_arch_list([]), ["x86_64"])
        self.assertEqual(
            builder.normalize_full_arch_list(["aarch64"]),
            ["aarch64_generic"],
        )
        self.assertEqual(
            builder.normalize_full_arch_list(["x86_64", "aarch64", "x86_64"]),
            ["x86_64", "aarch64_generic"],
        )
        self.assertEqual(
            builder.normalize_full_arch_list(["all"]),
            ["x86_64", "aarch64_generic"],
        )

    def test_build_full_ipk_bundles_tdl_license_notice_and_launcher(self):
        root = Path(__file__).resolve().parents[1]
        builder = load_builder()
        tdl_binary = b"fake tdl 0.20.3 payload\n"
        tdl_license = b"GNU AFFERO GENERAL PUBLIC LICENSE Version 3\n"
        tdl_archive = make_tdl_archive(tdl_binary, tdl_license)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                builder, "vendor_entries", return_value=fake_vendor_entries()
            ),
            patch_full_arch_sha(
                builder, "x86_64", hashlib.sha256(tdl_archive).hexdigest()
            ),
        ):
            ipk_path = builder.build_full_ipk(
                root,
                Path(tmp),
                fetcher=lambda _url: tdl_archive,
            )

            self.assertEqual(
                ipk_path.name,
                "tg-downloader-ui-full_0.1.4_x86_64.ipk",
            )
            members = read_outer_tar_members(ipk_path)
            self.assertEqual(
                set(members),
                {"debian-binary", "control.tar.gz", "data.tar.gz"},
            )

            control_tar = tarfile.open(
                fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz"
            )
            control_handle = control_tar.extractfile("./control")
            self.assertIsNotNone(control_handle)
            control = control_handle.read().decode("utf-8")
            self.assertIn("Package: tg-downloader-ui-full", control)
            self.assertIn("Version: 0.1.4", control)
            self.assertIn("Architecture: x86_64", control)
            self.assertIn("Conflicts: tg-downloader-ui", control)
            self.assertIn("Provides: tg-downloader-ui", control)
            self.assertIn("License: MIT AND AGPL-3.0-only", control)

            data_tar = tarfile.open(
                fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz"
            )
            names = set(data_tar.getnames())
            tdl_path = "./usr/bin/tdl"
            cli_path = "./usr/bin/tg-downloader-ui"
            license_path = (
                "./usr/share/licenses/tg-downloader-ui-full/tdl-AGPL-3.0.txt"
            )
            notice_path = (
                "./usr/share/licenses/tg-downloader-ui-full/tdl-NOTICE.txt"
            )
            self.assertTrue(
                {tdl_path, cli_path, license_path, notice_path}.issubset(names)
            )
            self.assertEqual(data_tar.getmember(tdl_path).mode, 0o755)
            self.assertEqual(data_tar.getmember(cli_path).mode, 0o755)
            self.assertEqual(data_tar.extractfile(tdl_path).read(), tdl_binary)
            self.assertEqual(data_tar.extractfile(license_path).read(), tdl_license)
            launcher = data_tar.extractfile(cli_path).read().decode("utf-8")
            self.assertEqual(
                launcher,
                "#!/bin/sh\n"
                "exec /usr/bin/python3 /usr/lib/tg-downloader-ui/app.py \"$@\"\n",
            )
            notice = data_tar.extractfile(notice_path).read().decode("utf-8")
            self.assertIn("Version: 0.20.3", notice)
            self.assertIn("https://github.com/iyear/tdl/tree/v0.20.3", notice)
            self.assertIn("tdl_Linux_64bit.tar.gz", notice)
            self.assertIn("unmodified", notice)

    def test_build_full_ipk_aarch64_generic_bundles_arm64_tdl(self):
        root = Path(__file__).resolve().parents[1]
        builder = load_builder()
        tdl_binary = b"fake tdl 0.20.3 arm64 payload\n"
        tdl_license = b"GNU AFFERO GENERAL PUBLIC LICENSE Version 3\n"
        tdl_archive = make_tdl_archive(tdl_binary, tdl_license)
        fetched_urls: list[str] = []

        def fetcher(url: str) -> bytes:
            fetched_urls.append(url)
            return tdl_archive

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                builder, "vendor_entries", return_value=fake_vendor_entries()
            ),
            patch_full_arch_sha(
                builder,
                "aarch64_generic",
                hashlib.sha256(tdl_archive).hexdigest(),
            ),
        ):
            ipk_path = builder.build_full_ipk(
                root,
                Path(tmp),
                architecture="aarch64",
                fetcher=fetcher,
            )

            self.assertEqual(
                ipk_path.name,
                "tg-downloader-ui-full_0.1.4_aarch64_generic.ipk",
            )
            members = read_outer_tar_members(ipk_path)
            control_tar = tarfile.open(
                fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz"
            )
            control = control_tar.extractfile("./control").read().decode("utf-8")
            self.assertIn("Package: tg-downloader-ui-full", control)
            self.assertIn("Architecture: aarch64_generic", control)
            self.assertIn("Conflicts: tg-downloader-ui", control)
            self.assertIn("Provides: tg-downloader-ui", control)
            self.assertIn("Complete aarch64 Telegram download Web UI", control)
            self.assertIn("License: MIT AND AGPL-3.0-only", control)

            data_tar = tarfile.open(
                fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz"
            )
            tdl_path = "./usr/bin/tdl"
            notice_path = (
                "./usr/share/licenses/tg-downloader-ui-full/tdl-NOTICE.txt"
            )
            self.assertEqual(data_tar.extractfile(tdl_path).read(), tdl_binary)
            notice = data_tar.extractfile(notice_path).read().decode("utf-8")
            self.assertIn("tdl_Linux_arm64.tar.gz", notice)
            self.assertIn("Version: 0.20.3", notice)
            self.assertTrue(
                any("tdl_Linux_arm64.tar.gz" in url for url in fetched_urls)
            )
            self.assertFalse(
                any("tdl_Linux_64bit.tar.gz" in url for url in fetched_urls)
            )

    def test_vendor_entries_map_packages_and_licenses(self):
        builder = load_builder()
        lock, payloads = fake_vendor_lock_and_payloads()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "openwrt").mkdir()
            (root / "openwrt/vendor-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            entries = builder.vendor_entries(root, fetcher=payloads.__getitem__)

        names = {name for name, _, _ in entries}
        for package in [
            "telethon",
            "qrcode",
            "rsa",
            "pyasn1",
            "pyaes",
            "python_socks",
            "async_timeout",
            "socks",
        ]:
            self.assertIn(
                f"./usr/lib/tg-downloader-ui/vendor/{package}/__init__.py", names
            )
            self.assertIn(
                f"./usr/share/licenses/tg-downloader-ui/{package}-LICENSE", names
            )

    def test_vendor_entries_reject_sha_mismatch(self):
        builder = load_builder()
        lock, payloads = fake_vendor_lock_and_payloads()
        lock["packages"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "openwrt").mkdir()
            (root / "openwrt/vendor-lock.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                builder.vendor_entries(root, fetcher=payloads.__getitem__)

    def test_build_openwrt_ipk_contains_control_scripts_and_runtime_files(self):
        root = Path(__file__).resolve().parents[1]
        builder = load_builder()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder, "vendor_entries", return_value=fake_vendor_entries()
        ):
            ipk_path = builder.build_ipk(root, Path(tmp))

            self.assertEqual(ipk_path.name, "tg-downloader-ui_0.1.4_all.ipk")
            members = read_outer_tar_members(ipk_path)
            self.assertEqual(set(members), {"debian-binary", "control.tar.gz", "data.tar.gz"})
            self.assertEqual(members["debian-binary"], b"2.0\n")

            control_tar = tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz")
            control_names = set(control_tar.getnames())
            self.assertIn("./control", control_names)
            self.assertIn("./postinst", control_names)
            self.assertIn("./prerm", control_names)
            control = control_tar.extractfile("./control").read().decode("utf-8")
            postinst = control_tar.extractfile("./postinst").read().decode("utf-8")

            self.assertIn("Package: tg-downloader-ui", control)
            self.assertIn("Version: 0.1.4", control)
            self.assertIn("Architecture: all", control)
            self.assertIn("python3", control)
            self.assertIn("python3-sqlite3", control)
            self.assertNotIn("python3-pip", control)
            self.assertNotIn("curl", control)
            self.assertIn("[ ! -f /etc/tg-downloader-ui.env ]", postinst)
            self.assertNotIn("pip install", postinst)
            self.assertIn("/etc/init.d/tg-downloader-ui enable", postinst)
            self.assertIn("/usr/lib/tg-downloader-ui", postinst)
            self.assertIn("/opt/tg-downloader-ui", postinst)
            self.assertIn("ln -s", postinst)

            data_tar = tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz")
            data_names = set(data_tar.getnames())
            expected = {
                "./usr/lib/tg-downloader-ui/app.py",
                "./usr/lib/tg-downloader-ui/bot.py",
                "./usr/lib/tg-downloader-ui/forwarder.py",
                "./usr/lib/tg-downloader-ui/sources.py",
                "./etc/init.d/tg-downloader-ui",
                "./etc/tg-downloader-ui.env.example",
                "./usr/share/luci/menu.d/luci-app-tg-downloader-ui.json",
                "./usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json",
                "./www/luci-static/resources/view/tg-downloader-ui/link.js",
            }
            for package in [
                "telethon",
                "qrcode",
                "rsa",
                "pyasn1",
                "pyaes",
                "python_socks",
                "async_timeout",
                "socks",
            ]:
                expected.add(
                    f"./usr/lib/tg-downloader-ui/vendor/{package}/__init__.py"
                )
                expected.add(
                    f"./usr/share/licenses/tg-downloader-ui/{package}-LICENSE"
                )
            self.assertTrue(expected.issubset(data_names))
            nested_dirs = {
                "./usr/lib/tg-downloader-ui/vendor/pyasn1",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber",
            }
            self.assertTrue(nested_dirs.issubset(data_names))
            for directory in nested_dirs:
                member = data_tar.getmember(directory)
                self.assertTrue(member.isdir())
                self.assertEqual(member.mode, 0o755)
            self.assertIn(
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber/decoder.py",
                data_names,
            )
            self.assertEqual(data_tar.getmember("./etc/init.d/tg-downloader-ui").mode, 0o755)
            self.assertEqual(data_tar.getmember("./usr/lib/tg-downloader-ui/app.py").mode, 0o755)
            forwarder_payload = data_tar.extractfile(
                "./usr/lib/tg-downloader-ui/forwarder.py"
            ).read().decode("utf-8")
            self.assertIn("def is_video_document", forwarder_payload)
            self.assertIn("def video_document_from_message", forwarder_payload)

    def test_with_parent_directories_adds_missing_nested_dirs(self):
        builder = load_builder()
        entries = [
            ("./usr/lib/tg-downloader-ui/vendor", None, 0o755),
            (
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber/decoder.py",
                b"decoder = object()\n",
                0o644,
            ),
        ]
        expanded = builder.with_parent_directories(entries)
        names = [name for name, _, _ in expanded]
        self.assertEqual(
            names,
            [
                "./usr",
                "./usr/lib",
                "./usr/lib/tg-downloader-ui",
                "./usr/lib/tg-downloader-ui/vendor",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber",
                "./usr/lib/tg-downloader-ui/vendor/pyasn1/codec/ber/decoder.py",
            ],
        )
        self.assertIsNone(expanded[0][1])
        self.assertEqual(expanded[-1][1], b"decoder = object()\n")

    def test_build_meta_ipk_registers_istore_installed_metadata(self):
        root = Path(__file__).resolve().parents[1]
        builder = load_builder()

        with tempfile.TemporaryDirectory() as tmp:
            ipk_path = builder.build_meta_ipk(root, Path(tmp), version="0.1.4")

            self.assertEqual(
                ipk_path.name, "app-meta-tg-downloader-ui_0.1.4-r1_all.ipk"
            )
            members = read_outer_tar_members(ipk_path)
            self.assertEqual(
                set(members), {"debian-binary", "control.tar.gz", "data.tar.gz"}
            )

            control_tar = tarfile.open(
                fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz"
            )
            control = control_tar.extractfile("./control").read().decode("utf-8")
            self.assertIn("Package: app-meta-tg-downloader-ui", control)
            self.assertIn("Version: 0.1.4-r1", control)
            self.assertIn("Depends: tg-downloader-ui", control)
            self.assertIn("Section: meta", control)
            self.assertNotIn("luci-app-store", control)
            self.assertNotIn("python3", control)

            data_tar = tarfile.open(
                fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz"
            )
            meta_path = "./usr/lib/opkg/meta/tg-downloader-ui.json"
            self.assertIn(meta_path, set(data_tar.getnames()))
            meta = json.loads(data_tar.extractfile(meta_path).read().decode("utf-8"))
            self.assertEqual(meta["name"], "tg-downloader-ui")
            self.assertEqual(meta["title"], "Telegram Downloads")
            self.assertEqual(
                meta["entry"], "/cgi-bin/luci/admin/services/tg-downloader-ui"
            )
            self.assertEqual(meta["depends"], ["tg-downloader-ui"])
            self.assertEqual(meta["version"], "0.1.4")
            self.assertEqual(meta["release"], 1)
            self.assertIn("net", meta["tags"])

    def test_luci_page_controls_openwrt_service(self):
        root = Path(__file__).resolve().parents[1]
        view = (
            root
            / "openwrt"
            / "www"
            / "luci-static"
            / "resources"
            / "view"
            / "tg-downloader-ui"
            / "link.js"
        ).read_text(encoding="utf-8")
        acl = json.loads(
            (
                root
                / "openwrt"
                / "usr"
                / "share"
                / "rpcd"
                / "acl.d"
                / "luci-app-tg-downloader-ui.json"
            ).read_text(encoding="utf-8")
        )

        self.assertIn("object: 'rc'", view)
        self.assertIn("method: 'list'", view)
        self.assertIn("method: 'init'", view)
        self.assertIn("method: 'setInitAction'", view)
        self.assertIn("isReadonlyView", view)
        self.assertNotIn("var isReadonlyView = !L.hasViewPermission();", view)
        self.assertIn("tg-downloader-ui", view)
        self.assertIn("'start'", view)
        self.assertIn("'stop'", view)
        self.assertIn("'restart'", view)

        permissions = acl["luci-app-tg-downloader-ui"]
        self.assertEqual(permissions["read"]["ubus"]["rc"], ["list"])
        self.assertEqual(permissions["write"]["ubus"]["rc"], ["init"])
        self.assertEqual(permissions["write"]["ubus"]["luci"], ["setInitAction"])

    def test_init_script_exports_env_for_app_and_forwarder(self):
        root = Path(__file__).resolve().parents[1]
        init_script = (root / "tg-downloader-ui.init").read_text(encoding="utf-8")
        env_example = (root / "openwrt" / "tg-downloader-ui.env.example").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            init_script.count("set -a; [ -f /etc/tg-downloader-ui.env ]"),
            1,
        )
        self.assertIn("app_home() {", init_script)
        self.assertIn("/usr/lib/tg-downloader-ui/app.py", init_script)
        self.assertIn("/opt/tg-downloader-ui/app.py", init_script)
        self.assertIn('PYTHONPATH="${TGDL_APP_HOME}/vendor', init_script)
        self.assertEqual(init_script.count("procd_set_param env"), 1)
        self.assertEqual(init_script.count("\tset_runtime_env\n"), 2)
        self.assertIn('TGDL_STATE_DIR="${TGDL_STATE_DIR:-/etc/tg-downloader-ui}"', init_script)
        self.assertIn('TGDL_API_HASH="${TGDL_API_HASH:-}"', init_script)
        self.assertNotIn("TGDL_SETUP_TOKEN", init_script)
        self.assertIn('TGDL_FORWARDER_ENABLED="${TGDL_FORWARDER_ENABLED-1}"', init_script)
        self.assertNotIn(
            'TGDL_FORWARDER_ENABLED="${TGDL_FORWARDER_ENABLED:-1}"', init_script
        )
        self.assertIn("forwarder_enabled() {", init_script)
        self.assertIn("${TGDL_FORWARDER_ENABLED-1}", init_script)
        self.assertIn(
            "sed 's/^[[:space:]]*//; s/[[:space:]]*$//'",
            init_script,
        )
        self.assertIn("tr '[:upper:]' '[:lower:]'", init_script)
        self.assertIn('case "$forwarder_flag" in', init_script)
        self.assertIn("1|true|yes|on) return 0 ;;", init_script)
        self.assertIn("*) return 1 ;;", init_script)
        self.assertIn("if forwarder_enabled; then", init_script)
        self.assertNotIn('[ "${TGDL_FORWARDER_ENABLED:-1}" = "1" ]', init_script)
        self.assertIn("TGDL_FORWARDER_ENABLED=1", env_example)
        self.assertIn(
            'procd_set_param command /usr/bin/python3 "${TGDL_APP_HOME}/app.py"',
            init_script,
        )
        self.assertIn(
            'procd_set_param command /usr/bin/python3 "${TGDL_APP_HOME}/forwarder.py"',
            init_script,
        )

    def test_init_script_forwarder_helper_parses_supported_values(self):
        root = Path(__file__).resolve().parents[1]
        init_script = (root / "tg-downloader-ui.init").read_text(encoding="utf-8")
        function = extract_shell_function(init_script, "forwarder_enabled")
        cases = (
            (None, True),
            ("1", True),
            ("TRUE", True),
            ("Yes", True),
            ("oN", True),
            (" true ", True),
            (" YES ", True),
            ("", False),
            ("   ", False),
            ("0", False),
            ("false", False),
            ("garbage", False),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                env = os.environ.copy()
                if value is None:
                    env.pop("TGDL_FORWARDER_ENABLED", None)
                else:
                    env["TGDL_FORWARDER_ENABLED"] = value
                result = subprocess.run(
                    ["sh", "-c", f"{function}\nforwarder_enabled"],
                    env=env,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0 if expected else 1)

    def test_extract_shell_function_uses_exact_declaration_and_matching_brace(self):
        script = """not_forwarder_enabled() {
  return 1
}
forwarder_enabled() {
  if true; then
    {
      return 0
    }
  fi
}
exit 99
"""

        function = extract_shell_function(script, "forwarder_enabled")
        result = subprocess.run(
            ["sh", "-c", f"{function}\nforwarder_enabled"],
            capture_output=True,
            check=False,
        )

        self.assertEqual(function.splitlines()[0], "forwarder_enabled() {")
        self.assertNotIn("not_forwarder_enabled", function)
        self.assertNotIn("exit 99", function)
        self.assertEqual(result.returncode, 0)

    def test_main_builds_generic_full_and_meta_packages(self):
        builder = load_builder()
        generic = Path("dist/openwrt/tg-downloader-ui_0.1.4_all.ipk")
        full = Path("dist/openwrt/tg-downloader-ui-full_0.1.4_x86_64.ipk")
        meta = Path("dist/openwrt/app-meta-tg-downloader-ui_0.1.4-r1_all.ipk")

        with (
            mock.patch("sys.argv", ["build_openwrt_ipk.py"]),
            mock.patch.object(builder, "build_ipk", return_value=generic) as build_generic,
            mock.patch.object(builder, "build_full_ipk", return_value=full) as build_full,
            mock.patch.object(builder, "build_meta_ipk", return_value=meta) as build_meta,
        ):
            self.assertEqual(builder.main(), 0)

        build_generic.assert_called_once()
        build_full.assert_called_once()
        # Default path must request x86_64 full only.
        self.assertEqual(
            build_full.call_args.kwargs.get("architecture", "x86_64"), "x86_64"
        )
        build_meta.assert_called_once()

    def test_main_builds_selected_full_arches(self):
        builder = load_builder()
        generic = Path("dist/openwrt/tg-downloader-ui_0.1.4_all.ipk")
        full_x86 = Path("dist/openwrt/tg-downloader-ui-full_0.1.4_x86_64.ipk")
        full_arm = Path(
            "dist/openwrt/tg-downloader-ui-full_0.1.4_aarch64_generic.ipk"
        )
        meta = Path("dist/openwrt/app-meta-tg-downloader-ui_0.1.4-r1_all.ipk")
        full_paths = {
            "x86_64": full_x86,
            "aarch64_generic": full_arm,
        }

        def build_full_side_effect(root, output_dir, version=None, architecture="x86_64", fetcher=None):
            return full_paths[architecture]

        with (
            mock.patch(
                "sys.argv",
                ["build_openwrt_ipk.py", "--full-arch", "aarch64", "--full-arch", "x86_64"],
            ),
            mock.patch.object(builder, "build_ipk", return_value=generic),
            mock.patch.object(
                builder, "build_full_ipk", side_effect=build_full_side_effect
            ) as build_full,
            mock.patch.object(builder, "build_meta_ipk", return_value=meta),
        ):
            self.assertEqual(builder.main(), 0)

        arches = [
            call.kwargs.get("architecture", call.args[3] if len(call.args) > 3 else None)
            for call in build_full.call_args_list
        ]
        # Order follows normalize_full_arch_list input order after alias expand.
        self.assertEqual(arches, ["aarch64_generic", "x86_64"])

    def test_main_full_arch_all_builds_every_profile(self):
        builder = load_builder()
        generic = Path("dist/openwrt/tg-downloader-ui_0.1.4_all.ipk")
        meta = Path("dist/openwrt/app-meta-tg-downloader-ui_0.1.4-r1_all.ipk")

        def build_full_side_effect(root, output_dir, version=None, architecture="x86_64", fetcher=None):
            return output_dir / f"tg-downloader-ui-full_0.1.4_{architecture}.ipk"

        with (
            mock.patch("sys.argv", ["build_openwrt_ipk.py", "--full-arch", "all"]),
            mock.patch.object(builder, "build_ipk", return_value=generic),
            mock.patch.object(
                builder, "build_full_ipk", side_effect=build_full_side_effect
            ) as build_full,
            mock.patch.object(builder, "build_meta_ipk", return_value=meta),
        ):
            self.assertEqual(builder.main(), 0)

        arches = [
            call.kwargs.get("architecture") for call in build_full.call_args_list
        ]
        self.assertEqual(arches, ["x86_64", "aarch64_generic"])


if __name__ == "__main__":
    unittest.main()
