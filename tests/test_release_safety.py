import json
import pathlib
import re
import subprocess
import tempfile
import unittest


GENERIC_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "telegram bot token": re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{30,}\b"),
    "telegram api hash": re.compile(
        r"(?i)(?:TGDL_API_HASH|api_hash)\s*[:=]\s*[\"']?[0-9a-f]{32}\b"
    ),
    "telethon session": re.compile(
        r"(?i)(?:StringSession|session_string|TGDL_SESSION)"
        r"[^\r\n]{0,40}[=:]\s*[\"']?[A-Za-z0-9+/=_-]{80,}"
    ),
}

SENSITIVE_TRACKED_PATHS = [
    re.compile(r"(?i)(^|/)(?![^/]*\.example$)\.env(?:$|\.)"),
    re.compile(r"(?i)(session|credentials?|secrets?|cookies?).*\.(json|txt|db|sqlite)$"),
    re.compile(r"(?i)\.(pem|key|p12|pfx)$"),
]


def tracked_paths(root: pathlib.Path) -> list[pathlib.Path]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [root / value.decode("utf-8") for value in output.split(b"\0") if value]


def load_local_denylist(root: pathlib.Path) -> list[str]:
    path = root / ".env.release-safety.local"
    if not path.exists():
        return []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("TGDL_RELEASE_SAFETY_DENYLIST="):
            values = json.loads(line.split("=", 1)[1])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError(
                    "TGDL_RELEASE_SAFETY_DENYLIST must be a JSON string array"
                )
            return values
    return []


class ReleaseSafetyTests(unittest.TestCase):
    def test_local_denylist_parser_reads_json_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".env.release-safety.local").write_text(
                'TGDL_RELEASE_SAFETY_DENYLIST=["local-one","local-two"]\n',
                encoding="utf-8",
            )
            self.assertEqual(load_local_denylist(root), ["local-one", "local-two"])

    def test_tracked_files_do_not_contain_generic_or_local_secrets(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        denylist = load_local_denylist(root)
        matches = []
        for path in tracked_paths(root):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            relative = path.relative_to(root).as_posix()
            for label, pattern in GENERIC_PATTERNS.items():
                if pattern.search(text):
                    matches.append(f"{label}: {relative}")
            if any(value in text for value in denylist):
                matches.append(f"local denylist: {relative}")
        self.assertEqual(matches, [])

    def test_sensitive_runtime_files_are_not_tracked(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        matches = []
        for path in tracked_paths(root):
            relative = path.relative_to(root).as_posix()
            if any(pattern.search(relative) for pattern in SENSITIVE_TRACKED_PATHS):
                matches.append(relative)
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
