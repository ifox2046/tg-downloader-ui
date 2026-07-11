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
    start = script.index(f"{name}() {{")
    end = script.index("\n}", start) + 2
    return script[start:end]


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


def fake_vendor_lock_and_payloads():
    definitions = [
        ("telethon", "1.44.0", "wheel", "telethon/", "", "telethon-1.44.0.dist-info/licenses/LICENSE"),
        ("qrcode", "8.2", "wheel", "qrcode/", "", "qrcode-8.2.dist-info/LICENSE"),
        ("rsa", "4.9.1", "wheel", "rsa/", "", "rsa-4.9.1.dist-info/LICENSE"),
        ("pyasn1", "0.6.1", "wheel", "pyasn1/", "", "pyasn1-0.6.1.dist-info/LICENSE.rst"),
        ("pyaes", "1.6.1", "tar.gz", "pyaes-1.6.1/pyaes/", "pyaes-1.6.1/", "pyaes-1.6.1/LICENSE.txt"),
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
    for package in ["telethon", "qrcode", "rsa", "pyasn1", "pyaes"]:
        entries.extend(
            [
                (
                    f"./opt/tg-downloader-ui/vendor/{package}/__init__.py",
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
    return entries


class OpenWrtIpkBuilderTests(unittest.TestCase):
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
        for package in ["telethon", "qrcode", "rsa", "pyasn1", "pyaes"]:
            self.assertIn(
                f"./opt/tg-downloader-ui/vendor/{package}/__init__.py", names
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

            self.assertEqual(ipk_path.name, "tg-downloader-ui_0.1.0_all.ipk")
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
            self.assertIn("Version: 0.1.0", control)
            self.assertIn("Architecture: all", control)
            self.assertIn("python3", control)
            self.assertIn("python3-sqlite3", control)
            self.assertNotIn("python3-pip", control)
            self.assertNotIn("curl", control)
            self.assertIn("[ ! -f /etc/tg-downloader-ui.env ]", postinst)
            self.assertNotIn("pip install", postinst)
            self.assertIn("/etc/init.d/tg-downloader-ui enable", postinst)

            data_tar = tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz")
            data_names = set(data_tar.getnames())
            expected = {
                "./opt/tg-downloader-ui/app.py",
                "./opt/tg-downloader-ui/forwarder.py",
                "./opt/tg-downloader-ui/sources.py",
                "./etc/init.d/tg-downloader-ui",
                "./etc/tg-downloader-ui.env.example",
                "./usr/share/luci/menu.d/luci-app-tg-downloader-ui.json",
                "./usr/share/rpcd/acl.d/luci-app-tg-downloader-ui.json",
                "./www/luci-static/resources/view/tg-downloader-ui/link.js",
            }
            for package in ["telethon", "qrcode", "rsa", "pyasn1", "pyaes"]:
                expected.add(
                    f"./opt/tg-downloader-ui/vendor/{package}/__init__.py"
                )
                expected.add(
                    f"./usr/share/licenses/tg-downloader-ui/{package}-LICENSE"
                )
            self.assertTrue(expected.issubset(data_names))
            self.assertEqual(data_tar.getmember("./etc/init.d/tg-downloader-ui").mode, 0o755)
            self.assertEqual(data_tar.getmember("./opt/tg-downloader-ui/app.py").mode, 0o755)
            forwarder_payload = data_tar.extractfile(
                "./opt/tg-downloader-ui/forwarder.py"
            ).read().decode("utf-8")
            self.assertIn("def is_video_document", forwarder_payload)
            self.assertIn("def video_document_from_message", forwarder_payload)

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
        self.assertIn("tg-downloader-ui", view)
        self.assertIn("'start'", view)
        self.assertIn("'stop'", view)
        self.assertIn("'restart'", view)

        permissions = acl["luci-app-tg-downloader-ui"]
        self.assertEqual(permissions["read"]["ubus"]["rc"], ["list"])
        self.assertEqual(permissions["write"]["ubus"]["rc"], ["init"])

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
        self.assertEqual(
            init_script.count('PYTHONPATH="/opt/tg-downloader-ui/vendor'), 1
        )
        self.assertEqual(init_script.count("procd_set_param env"), 1)
        self.assertEqual(init_script.count("\tset_runtime_env\n"), 2)
        self.assertIn('TGDL_STATE_DIR="${TGDL_STATE_DIR:-/etc/tg-downloader-ui}"', init_script)
        self.assertIn('TGDL_API_HASH="${TGDL_API_HASH:-}"', init_script)
        self.assertNotIn("TGDL_SETUP_TOKEN", init_script)
        self.assertIn('TGDL_FORWARDER_ENABLED="${TGDL_FORWARDER_ENABLED:-1}"', init_script)
        self.assertIn("forwarder_enabled() {", init_script)
        self.assertIn("tr '[:upper:]' '[:lower:]'", init_script)
        self.assertIn('case "$forwarder_flag" in', init_script)
        self.assertIn("1|true|yes|on) return 0 ;;", init_script)
        self.assertIn("*) return 1 ;;", init_script)
        self.assertIn("if forwarder_enabled; then", init_script)
        self.assertNotIn('[ "${TGDL_FORWARDER_ENABLED:-1}" = "1" ]', init_script)
        self.assertIn("TGDL_FORWARDER_ENABLED=1", env_example)
        self.assertIn(
            "procd_set_param command /usr/bin/python3 /opt/tg-downloader-ui/app.py",
            init_script,
        )
        self.assertIn(
            "procd_set_param command /usr/bin/python3 /opt/tg-downloader-ui/forwarder.py",
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


if __name__ == "__main__":
    unittest.main()
