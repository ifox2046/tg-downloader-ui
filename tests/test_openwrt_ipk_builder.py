import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path


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


class OpenWrtIpkBuilderTests(unittest.TestCase):
    def test_build_openwrt_ipk_contains_control_scripts_and_runtime_files(self):
        root = Path(__file__).resolve().parents[1]
        builder = load_builder()

        with tempfile.TemporaryDirectory() as tmp:
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
            self.assertIn("python3-pip", control)
            self.assertIn("curl", control)
            self.assertIn("[ ! -f /etc/tg-downloader-ui.env ]", postinst)
            self.assertIn("pip install --no-cache-dir", postinst)
            self.assertIn("telethon>=1.35", postinst)
            self.assertIn("qrcode>=7.4", postinst)
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

        self.assertEqual(
            init_script.count("set -a; [ -f /etc/tg-downloader-ui.env ]"),
            2,
        )
        self.assertIn("exec /usr/bin/python3 /opt/tg-downloader-ui/app.py", init_script)
        self.assertIn("exec /usr/bin/python3 /opt/tg-downloader-ui/forwarder.py", init_script)


if __name__ == "__main__":
    unittest.main()
