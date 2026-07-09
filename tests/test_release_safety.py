import pathlib
import re
import unittest


class ReleaseSafetyTests(unittest.TestCase):
    def test_tracked_files_do_not_contain_private_defaults(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        patterns = [
            # Private admin/router defaults from earlier internal testing.
            r"192\.168\.31\." + "157",
            r"admin" + "123",
            r"26375" + "241",
            r"26592" + "314",
            r"-100449" + "6489706",
            r"/mnt/sata" + "1-5",
            r"/root/\." + "tdl",
            r"You" + "xiu",
            r"you" + "you0",
            # Internal test-lab host, SSH user, and container names. These must
            # never reappear in public files; they live only in local,
            # git-ignored working notes.
            r"192\.168\.101\." + "128",
            r"fox" + "@192.168",
            r"tgdl-openwrt" + "-mgmt",
            r"tgdl-ui-docker" + "-latest",
        ]
        combined = re.compile("|".join(patterns), re.IGNORECASE)
        skipped_dirs = {".git", ".codegraph", "__pycache__", ".pytest_cache"}
        skipped_suffixes = {".pyc", ".db", ".sqlite"}

        matches = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if skipped_dirs.intersection(path.parts):
                continue
            if path.suffix in skipped_suffixes:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if combined.search(text):
                matches.append(str(path.relative_to(root)))

        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
