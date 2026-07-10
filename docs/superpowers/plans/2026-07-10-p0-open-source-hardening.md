# P0 Open-Source Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tg-downloader-ui` safe to publish as a local-first `0.1.0` release while keeping the stdlib HTTP architecture and providing an offline-installable OpenWRT IPK with Telegram dependencies included.

**Architecture:** Keep security state in the existing `ConfigStore`, `AuthManager`, `DownloadServer`, and `RequestHandler` boundaries. Add small stdlib helpers for setup tokens, login throttling, CSRF, private file modes, and command redaction. Docker and OpenWRT remain thin packaging layers; OpenWRT dependencies are fetched and hash-verified on the build machine, then placed in an isolated vendor directory inside the IPK.

**Tech Stack:** Python 3.10+, `unittest`, stdlib `http.server`, SQLite, POSIX shell, Docker Compose, OpenWRT IPK tar archives.

---

## File Map

- Modify `tg_downloader_ui/app.py`: HTTP/authentication protections, setup token, private modes, command redaction.
- Modify `tg_downloader_ui/forwarder.py`: private forwarder log/status modes.
- Modify `tests/test_tg_downloader_ui.py`: TDD coverage for app security and Docker configuration.
- Modify `tests/test_forwarder.py`: TDD coverage for forwarder file modes.
- Replace `tests/test_release_safety.py`: generic public scanner plus optional local denylist.
- Modify `tests/test_openwrt_ipk_builder.py`: vendoring, post-install, and procd tests.
- Create `.env.release-safety.example`: non-secret local denylist format.
- Modify `.gitignore` and `.dockerignore`: exclude local security data and Docker context secrets.
- Modify `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`: verified tdl artifact, localhost publication, non-root runtime, opt-in forwarder.
- Create `openwrt/vendor-lock.json`: exact OpenWRT dependency artifacts and hashes.
- Modify `scripts/build_openwrt_ipk.py`: download, verify, and extract vendored pure-Python packages.
- Modify `tg-downloader-ui.init` and `openwrt/tg-downloader-ui.env.example`: vendor path and opt-in forwarder.
- Modify `.env.example`, `README.md`, `SECURITY.md`, `THIRD_PARTY.md`, `CONTRIBUTING.md`, `.github/workflows/ci.yml`, and `pyproject.toml`: secure deployment and reproducible dependency documentation.

---

### Task 1: Replace Private Release-Safety Literals

**Files:**
- Create locally, never commit: `.env.release-safety.local`
- Create: `.env.release-safety.example`
- Modify: `.gitignore`
- Replace: `tests/test_release_safety.py`

- [ ] **Step 1: Preserve the existing private regression values locally**

Create `.env.release-safety.local` before deleting the current literals. Store the reconstructed values from the current `patterns` list as a JSON array:

```dotenv
TGDL_RELEASE_SAFETY_DENYLIST=["first-local-value","second-local-value"]
```

Do not paste the real values into this plan, shell output, commits, or review messages. Verify Git ignores the file:

```sh
git check-ignore -v .env.release-safety.local
```

Expected: `.gitignore` identifies the file after Step 2.

- [ ] **Step 2: Add the ignored local file and a safe example**

Add to `.gitignore`:

```gitignore
.env.release-safety.local
```

Create `.env.release-safety.example`:

```dotenv
# JSON array of local-only literal values that must never appear in tracked files.
TGDL_RELEASE_SAFETY_DENYLIST=["sample-local-value"]
```

- [ ] **Step 3: Write the new public scanner tests**

Replace `tests/test_release_safety.py` with stdlib-only helpers and tests shaped as follows:

```python
import json
import os
import pathlib
import re
import subprocess
import unittest
from unittest import mock


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
                raise ValueError("TGDL_RELEASE_SAFETY_DENYLIST must be a JSON string array")
            return values
    return []


class ReleaseSafetyTests(unittest.TestCase):
    def test_local_denylist_parser_reads_json_array(self):
        with mock.patch.object(pathlib.Path, "exists", return_value=True):
            with mock.patch.object(
                pathlib.Path,
                "read_text",
                return_value=(
                    'TGDL_RELEASE_SAFETY_DENYLIST='
                    '["local-deny-value-one","local-deny-value-two"]\n'
                ),
            ):
                self.assertEqual(
                    load_local_denylist(pathlib.Path(".")),
                    ["local-deny-value-one", "local-deny-value-two"],
                )

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
```

The test bodies use only safe sample values and never embed previous private defaults.

- [ ] **Step 4: Run the release-safety test**

Run:

```sh
python tests/test_release_safety.py -v
```

Expected: all three tests pass, and `.env.release-safety.local` remains ignored.

- [ ] **Step 5: Confirm the removed values are absent from the tracked tree**

Run the new scanner and inspect the staged diff without printing local denylist contents:

```sh
git diff -- tests/test_release_safety.py .gitignore .env.release-safety.example
git status --short --ignored
```

Expected: the local file is shown as ignored and is not staged.

- [ ] **Step 6: Commit**

```sh
git add .gitignore .env.release-safety.example tests/test_release_safety.py
git commit -m "security: remove private release-safety literals"
```

---

### Task 2: Protect Sensitive Files and Redact Command Logs

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tests/test_forwarder.py`
- Modify: `tg_downloader_ui/app.py`
- Modify: `tg_downloader_ui/forwarder.py`

- [ ] **Step 1: Write failing app tests**

Add imports `stat` and `from unittest import mock` to `tests/test_tg_downloader_ui.py`. Add tests:

```python
class SecurityHelperTests(unittest.TestCase):
    def test_redact_command_args_hides_proxy_credentials(self):
        args = app.redact_command_args(
            ["tdl", "--proxy", "socks5://user:password@127.0.0.1:1080", "download"]
        )
        self.assertEqual(args, ["tdl", "--proxy", "<redacted>", "download"])

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_config_database_and_job_log_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = app.ConfigStore(
                root / "state",
                default_download_dir=root / "downloads",
                default_password="test-password",
            )
            config.init()
            store = JobStore(root / "state", config)
            store.init()
            job = store.create_job(23311)
            store.append_log(job["id"], "hello\n")
            self.assertEqual(stat.S_IMODE(config.state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.db_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(job["log_path"]).stat().st_mode), 0o600)
```

Extend `ForwarderStatusApiTests.test_restart_forwarder_runs_configured_command_without_shell` with a command containing `--proxy` and assert `result["args"]` is redacted.

- [ ] **Step 2: Write failing forwarder tests**

Add `os` and `stat` imports to `tests/test_forwarder.py` and add:

```python
@unittest.skipIf(os.name == "nt", "POSIX mode assertion")
def test_forwarder_log_and_status_files_are_private(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_path = root / "forwarder.log"
        status_path = root / "forwarder_status.json"
        forwarder.log_line("hello", log_path=log_path)
        forwarder.write_status_file(status_path, {"state": "running"})
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)
```

- [ ] **Step 3: Run the new tests to verify RED**

Run:

```sh
python -m unittest \
  tests.test_tg_downloader_ui.SecurityHelperTests \
  tests.test_tg_downloader_ui.ForwarderStatusApiTests.test_restart_forwarder_runs_configured_command_without_shell \
  tests.test_forwarder.ForwarderFormattingTests.test_forwarder_log_and_status_files_are_private -v
```

Expected: fail because the helpers and private modes do not exist.

- [ ] **Step 4: Implement minimal private-mode helpers in `app.py`**

Add:

```python
def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def redact_command_args(args: list[str]) -> list[str]:
    redacted = list(args)
    for index, value in enumerate(redacted):
        if index and redacted[index - 1] == "--proxy":
            redacted[index] = "<redacted>"
        elif value.startswith("--proxy="):
            redacted[index] = "--proxy=<redacted>"
    return redacted
```

Use `ensure_private_dir` for the state, logs, and exports directories in `ConfigStore.init`, `ConfigStore.save`, and `JobStore.init`. Do not change the configured download-directory mode. Apply `ensure_private_file` after writing/replacing `config.json`, after `sqlite3.connect` creates `state.db`, after job log creation/appends, after Telegram code state is written, and after the existing StringSession write. Change `DownloadWorker.run_command` to log `redact_command_args(cmd)` rather than raw `cmd`. Return redacted args from `restart_forwarder`.

- [ ] **Step 5: Implement minimal private-mode helpers in `forwarder.py`**

Add local `ensure_private_dir`, `ensure_private_file`, and a reusable status writer:

```python
def write_status_file(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ensure_private_file(tmp_path)
    tmp_path.replace(path)
    ensure_private_file(path)
```

Make `write_status` call `write_status_file`. Make `log_line` secure the parent directory and log after opening it.

- [ ] **Step 6: Verify GREEN**

Run the command from Step 3, then:

```sh
python -m unittest tests.test_forwarder tests.test_tg_downloader_ui.CommandConstructionTests -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```sh
git add tg_downloader_ui/app.py tg_downloader_ui/forwarder.py tests/test_tg_downloader_ui.py tests/test_forwarder.py
git commit -m "security: protect state files and redact proxy logs"
```

---

### Task 3: Require a Setup Token and Eight-Character New Passwords

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tg_downloader_ui/app.py`

- [ ] **Step 1: Write failing tests**

Add to `ConfigAuthTests`:

```python
def test_new_passwords_require_eight_characters(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_password="",
        )
        config.init()
        with self.assertRaisesRegex(ValueError, "at least 8"):
            config.initialize("owner", "short", root / "downloads")
```

Update `AuthHttpTests.test_setup_api_returns_default_download_dir_and_accepts_blank_download_dir` to construct:

```python
server = app.DownloadServer(
    ("127.0.0.1", 0),
    app.RequestHandler,
    store,
    config,
    auth,
    setup_token="one-time-setup-token",
)
```

Then assert missing and incorrect `X-TGDL-Setup-Token` return 403 before asserting the correct token returns 201. Add to `IndexTemplateTests`:

```python
def test_setup_form_collects_one_time_token(self):
    self.assertIn('id="setupToken"', app.SETUP_HTML)
    self.assertIn("X-TGDL-Setup-Token", app.SETUP_HTML)

def test_default_host_is_loopback(self):
    self.assertEqual(app.DEFAULT_HOST, "127.0.0.1")
```

- [ ] **Step 2: Verify RED**

Run:

```sh
python -m unittest \
  tests.test_tg_downloader_ui.ConfigAuthTests.test_new_passwords_require_eight_characters \
  tests.test_tg_downloader_ui.AuthHttpTests.test_setup_api_returns_default_download_dir_and_accepts_blank_download_dir \
  tests.test_tg_downloader_ui.IndexTemplateTests.test_setup_form_collects_one_time_token \
  tests.test_tg_downloader_ui.IndexTemplateTests.test_default_host_is_loopback -v
```

Expected: failures for missing validation, token handling, UI field, and loopback default.

- [ ] **Step 3: Implement password and setup-token behavior**

In `app.py`:

```python
DEFAULT_HOST = os.environ.get("TGDL_HOST", "127.0.0.1")
MIN_PASSWORD_LENGTH = 8


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must contain at least {MIN_PASSWORD_LENGTH} characters")


def new_setup_token() -> str:
    return os.environ.get("TGDL_SETUP_TOKEN", "").strip() or secrets.token_urlsafe(24)
```

Call `validate_new_password` from `ConfigStore.set_password`; do not call it while loading an existing stored hash or bootstrapping a legacy `TGDL_AUTH_PASSWORD`.

Add `setup_token: str = ""` to `DownloadServer.__init__`, store it on the server, and add a `RequestHandler.setup_token` property. Before `ConfigStore.initialize`, compare the `X-TGDL-Setup-Token` header using `hmac.compare_digest`; return 403 when absent or incorrect.

In `run_server`, create the token only when setup is required, print it once with `print(f"setup token: {setup_token}", flush=True)`, and pass it to `DownloadServer`.

Add the setup token password input to `SETUP_HTML`, send it as `X-TGDL-Setup-Token`, and do not include it in the JSON body or logs.

- [ ] **Step 4: Verify GREEN and compatibility**

Run the Step 2 command, then:

```sh
python -m unittest tests.test_tg_downloader_ui.ConfigAuthTests tests.test_tg_downloader_ui.IndexTemplateTests -v
```

Expected: all selected tests pass; existing `test-password` credentials remain valid.

- [ ] **Step 5: Commit**

```sh
git add tg_downloader_ui/app.py tests/test_tg_downloader_ui.py
git commit -m "security: protect first-run setup"
```

---

### Task 4: Add In-Memory Login Throttling

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tg_downloader_ui/app.py`

- [ ] **Step 1: Write failing unit and HTTP tests**

Add to `ConfigAuthTests`:

```python
def test_login_failures_block_and_success_clears_key(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_password="test-password",
        )
        config.init()
        auth = app.AuthManager(config)
        key = "127.0.0.1:admin"
        for _ in range(5):
            auth.record_login_failure(key, now=1000)
        self.assertEqual(auth.login_retry_after(key, now=1000), 900)
        auth.clear_login_failures(key)
        self.assertEqual(auth.login_retry_after(key, now=1000), 0)
```

Add to `AuthHttpTests`:

```python
def test_login_rate_limit_returns_429(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = app.ConfigStore(
            root / "state",
            default_download_dir=root / "downloads",
            default_password="test-password",
        )
        config.init()
        store = JobStore(root / "state", config)
        store.init()
        auth = app.AuthManager(config)
        server = app.DownloadServer(
            ("127.0.0.1", 0), app.RequestHandler, store, config, auth
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for _ in range(5):
                status, _, _ = self.request(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin", "password": "wrong-password"},
                )
                self.assertEqual(status, 401)
            status, headers, _ = self.request(
                port,
                "POST",
                "/api/auth/login",
                {"username": "admin", "password": "test-password"},
            )
            self.assertEqual(status, 429)
            self.assertGreater(int(headers["Retry-After"]), 0)
        finally:
            server.shutdown()
            server.server_close()
```

- [ ] **Step 2: Verify RED**

Run:

```sh
python -m unittest \
  tests.test_tg_downloader_ui.ConfigAuthTests.test_login_failures_block_and_success_clears_key \
  tests.test_tg_downloader_ui.AuthHttpTests.test_login_rate_limit_returns_429 -v
```

Expected: fail because throttling methods and HTTP response do not exist.

- [ ] **Step 3: Implement the minimal throttle**

Add constants and state to `AuthManager`:

```python
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_BLOCK_SECONDS = 15 * 60
```

Store `login_failures: dict[str, list[float]]` and `login_blocked_until: dict[str, float]`. Implement `record_login_failure`, `login_retry_after`, and `clear_login_failures` under the existing lock. Prune timestamps outside the five-minute window. When the fifth failure occurs, set `blocked_until = now + 900`.

Use this implementation shape:

```python
def record_login_failure(self, key: str, now: float | None = None) -> None:
    current = time.time() if now is None else now
    cutoff = current - LOGIN_FAILURE_WINDOW_SECONDS
    with self.lock:
        failures = [value for value in self.login_failures.get(key, []) if value >= cutoff]
        failures.append(current)
        self.login_failures[key] = failures
        if len(failures) >= LOGIN_FAILURE_LIMIT:
            self.login_blocked_until[key] = current + LOGIN_BLOCK_SECONDS


def login_retry_after(self, key: str, now: float | None = None) -> int:
    current = time.time() if now is None else now
    with self.lock:
        blocked_until = self.login_blocked_until.get(key, 0)
        if blocked_until <= current:
            self.login_blocked_until.pop(key, None)
            return 0
        return max(1, int(blocked_until - current + 0.999))


def clear_login_failures(self, key: str) -> None:
    with self.lock:
        self.login_failures.pop(key, None)
        self.login_blocked_until.pop(key, None)
```

In `/api/auth/login`, derive the key from `self.client_address[0]` and normalized username. Check `login_retry_after` before password verification; return 429 with an integer `Retry-After`. Record invalid attempts and clear the key after successful verification. Extend `send_text` and `send_error_text` with an optional `headers` dictionary so the 429 response can send `Retry-After` without duplicating response-writing code.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command and the full `AuthHttpTests` class.

- [ ] **Step 5: Commit**

```sh
git add tg_downloader_ui/app.py tests/test_tg_downloader_ui.py
git commit -m "security: throttle repeated login failures"
```

---

### Task 5: Add CSRF, Body Limits, Secure Cookies, and Security Headers

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tg_downloader_ui/app.py`

- [ ] **Step 1: Write failing HTTP boundary tests**

Add to `AuthHttpTests`:

```python
@contextlib.contextmanager
def running_server(
    self,
    root,
    default_password="test-password",
    setup_token="",
    cookie_secure=False,
):
    config = app.ConfigStore(
        root / "state",
        default_download_dir=root / "downloads",
        default_user="admin",
        default_password=default_password,
    )
    config.init()
    store = JobStore(root / "state", config)
    store.init()
    auth = app.AuthManager(config, session_max_age_seconds=604800)
    server = app.DownloadServer(
        ("127.0.0.1", 0),
        app.RequestHandler,
        store,
        config,
        auth,
        setup_token=setup_token,
        cookie_secure=cookie_secure,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], config
    finally:
        server.shutdown()
        server.server_close()


def login_headers(self, port, password="test-password"):
    status, headers, _ = self.request(
        port,
        "POST",
        "/api/auth/login",
        {"username": "admin", "password": password},
    )
    self.assertEqual(status, 200)
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, _, payload = self.request(
        port, "GET", "/api/auth/me", headers={"Cookie": cookie}
    )
    self.assertEqual(status, 200)
    csrf_token = json.loads(payload)["csrf_token"]
    return {"Cookie": cookie, "X-CSRF-Token": csrf_token}
```

Add tests:

```python
def test_authenticated_mutation_requires_csrf_token(self):
    with tempfile.TemporaryDirectory() as tmp:
        with self.running_server(Path(tmp)) as (port, _):
            status, headers, _ = self.request(
                port,
                "POST",
                "/api/auth/login",
                {"username": "admin", "password": "test-password"},
            )
            self.assertEqual(status, 200)
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            status, _, _ = self.request(
                port,
                "POST",
                "/api/auth/logout",
                {},
                headers={"Cookie": cookie},
            )
            self.assertEqual(status, 403)
            status, _, payload = self.request(
                port, "GET", "/api/auth/me", headers={"Cookie": cookie}
            )
            csrf_token = json.loads(payload)["csrf_token"]
            status, _, _ = self.request(
                port,
                "POST",
                "/api/auth/logout",
                {},
                headers={"Cookie": cookie, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(status, 200)

def test_oversized_json_request_returns_413(self):
    with tempfile.TemporaryDirectory() as tmp:
        with self.running_server(Path(tmp)) as (port, _):
            status, _, _ = self.request(
                port,
                "POST",
                "/api/auth/login",
                headers={"Content-Length": str(app.MAX_JSON_BODY_BYTES + 1)},
            )
            self.assertEqual(status, 413)

def test_security_headers_are_present(self):
    with tempfile.TemporaryDirectory() as tmp:
        with self.running_server(Path(tmp)) as (port, _):
            status, headers, _ = self.request(port, "GET", "/login")
            self.assertEqual(status, 200)
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")

def test_secure_cookie_can_be_enabled_per_server(self):
    with tempfile.TemporaryDirectory() as tmp:
        with self.running_server(Path(tmp), cookie_secure=True) as (port, _):
            status, headers, _ = self.request(
                port,
                "POST",
                "/api/auth/login",
                {"username": "admin", "password": "test-password"},
            )
            self.assertEqual(status, 200)
            self.assertIn("; Secure", headers["Set-Cookie"])
```

- [ ] **Step 2: Verify RED**

Run the four new tests. Expected: failures because sessions have no CSRF token, body size is unlimited, headers are absent, and cookie security is not configurable.

- [ ] **Step 3: Implement session CSRF and browser propagation**

Add a random `csrf_token` to `AuthManager.create_session`. Return it from `/api/auth/me`.

Add to `RequestHandler`:

```python
def require_csrf(self) -> bool:
    session = self.auth.get_session(self.get_session_token())
    supplied = self.headers.get("X-CSRF-Token", "")
    expected = str((session or {}).get("csrf_token") or "")
    if not expected or not hmac.compare_digest(supplied, expected):
        self.send_error_text(HTTPStatus.FORBIDDEN, "invalid csrf token")
        return False
    return True
```

Call it after authentication and before every authenticated POST, PUT, and DELETE route. Login and setup remain exempt.

In `INDEX_HTML`, define `let csrfToken = '';`, make `loadMe` store `data.csrf_token`, and make `api()` add `X-CSRF-Token` automatically for non-GET/HEAD methods.

- [ ] **Step 4: Implement request limits, cookie option, and headers**

Add:

```python
MAX_JSON_BODY_BYTES = 1024 * 1024
COOKIE_SECURE = os.environ.get("TGDL_COOKIE_SECURE", "").strip().lower() in {
    "1", "true", "yes", "on"
}
```

Add `cookie_secure: bool = COOKIE_SECURE` to `DownloadServer`. Add a `reject_oversized_request()` helper that validates `Content-Length`, returns 400 for invalid/negative values, and sends 413 when it exceeds the limit. Call it at the start of `do_POST` and `do_PUT`.

Append `; Secure` in `build_session_cookie` when `self.server.cookie_secure` is true.

Override `end_headers`:

```python
def end_headers(self) -> None:
    self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    self.send_header("X-Frame-Options", "DENY")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("Cache-Control", "no-store")
    super().end_headers()
```

- [ ] **Step 5: Update existing authenticated mutation tests**

Use `login_headers()` instead of cookie-only headers in these tests:

- `test_login_cookie_allows_api_access_and_logout_revokes_it`
- `test_job_pause_resume_api_requires_auth_and_updates_status`
- `test_tdl_qr_login_api_requires_auth_and_returns_status`
- `test_tdl_code_login_api_requires_auth_and_accepts_input`
- `test_sources_api_requires_auth_and_updates_sources`
- `ForwarderStatusApiTests.test_forwarder_restart_api_requires_auth_and_runs_restart`

GET-only tests may continue to use a Cookie alone.

- [ ] **Step 6: Verify GREEN**

Run:

```sh
python -m unittest tests.test_tg_downloader_ui.AuthHttpTests tests.test_tg_downloader_ui.ForwarderStatusApiTests -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```sh
git add tg_downloader_ui/app.py tests/test_tg_downloader_ui.py
git commit -m "security: harden authenticated HTTP requests"
```

---

### Task 6: Harden Docker Defaults

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `.dockerignore`
- Modify: `.env.example`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `docker/entrypoint.sh`

- [ ] **Step 1: Write failing Docker configuration tests**

Replace/extend `DockerComposeTests` with assertions that:

```python
def test_compose_publishes_only_to_loopback_and_disables_forwarder(self):
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    self.assertIn('${TGDL_PUBLISH_HOST:-127.0.0.1}:${TGDL_PUBLISH_PORT:-9910}:9910', compose)
    self.assertIn('TGDL_FORWARDER_ENABLED: "${TGDL_FORWARDER_ENABLED:-0}"', compose)

def test_dockerfile_verifies_tdl_and_drops_runtime_privileges(self):
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    self.assertIn("f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718", dockerfile)
    self.assertIn("sha256sum -c -", dockerfile)
    self.assertIn("useradd", dockerfile)
    self.assertIn("setpriv", dockerfile)

def test_docker_context_excludes_local_secrets(self):
    ignored = Path(".dockerignore").read_text(encoding="utf-8")
    for value in [".env", ".env.release-safety.local", ".agents", ".codex", ".claude", "dist/"]:
        self.assertIn(value, ignored)
```

Update the entrypoint test to assert `${TGDL_FORWARDER_ENABLED:-0}` rather than the old default-on expression.

- [ ] **Step 2: Verify RED**

Run:

```sh
python -m unittest tests.test_tg_downloader_ui.DockerComposeTests -v
```

Expected: failures for port binding, checksum, privilege dropping, context exclusions, and default forwarder state.

- [ ] **Step 3: Implement Dockerfile verification and runtime user**

Use these pinned values:

```dockerfile
ARG TDL_VERSION=0.20.3
ARG TDL_ASSET=tdl_Linux_64bit.tar.gz
ARG TDL_SHA256=f69fe06c17f74c30a3b894b5be05c57a1b082f56b346c994025a2301b269a718
ARG TGDL_UID=1000
ARG TGDL_GID=1000
```

Install `util-linux` for `setpriv`, verify the archive before extraction, create group/user `tgdl`, and create `/home/tgdl`, `/config`, `/downloads`, and `/tdl` with UID/GID ownership. Set `HOME=/home/tgdl` and `TGDL_FORWARDER_ENABLED=0`.

Keep the entrypoint as root only long enough to ensure `/config`, `/tdl`, and the `/downloads` root are writable, then re-exec itself:

```sh
if [ "$(id -u)" = "0" ]; then
  mkdir -p /config /downloads /tdl
  chown -R tgdl:tgdl /config /tdl
  chown tgdl:tgdl /downloads
  exec setpriv --reuid=tgdl --regid=tgdl --init-groups "$0" "$@"
fi
```

Do not recursively chown the potentially large downloads tree.

- [ ] **Step 4: Implement Compose and context defaults**

Use:

```yaml
ports:
  - "${TGDL_PUBLISH_HOST:-127.0.0.1}:${TGDL_PUBLISH_PORT:-9910}:9910"
environment:
  TGDL_FORWARDER_ENABLED: "${TGDL_FORWARDER_ENABLED:-0}"
```

Add `TGDL_FORWARDER_ENABLED=0`, `TGDL_SETUP_TOKEN=`, `TGDL_COOKIE_SECURE=0`, `TGDL_PUBLISH_HOST=127.0.0.1`, and `TGDL_PUBLISH_PORT=9910` to `.env.example`.

Add all tested exclusions to `.dockerignore`.

- [ ] **Step 5: Verify GREEN**

Run the Step 2 command and `git diff --check`.

- [ ] **Step 6: Commit**

```sh
git add .dockerignore .env.example Dockerfile docker-compose.yml docker/entrypoint.sh tests/test_tg_downloader_ui.py
git commit -m "security: harden Docker defaults"
```

---

### Task 7: Vendor Telegram Dependencies into the OpenWRT IPK

**Files:**
- Create: `openwrt/vendor-lock.json`
- Modify: `scripts/build_openwrt_ipk.py`
- Modify: `tests/test_openwrt_ipk_builder.py`
- Modify: `tg-downloader-ui.init`
- Modify: `openwrt/tg-downloader-ui.env.example`
- Modify: `pyproject.toml`

- [ ] **Step 1: Create the exact vendor lock**

Create `openwrt/vendor-lock.json` with these five entries:

```json
{
  "packages": [
    {
      "name": "telethon",
      "version": "1.44.0",
      "archive": "wheel",
      "url": "https://files.pythonhosted.org/packages/82/fd/4ad621d7a4b8655dfc964ee1c1267407496ec6fd10c91901a64ea29c16c8/telethon-1.44.0-py3-none-any.whl",
      "sha256": "52fc49efb67a4916c2aedcb295ad286f4afa2aba9bf15d83ed2acdc64af0c718",
      "package_prefix": "telethon/",
      "strip_prefix": "",
      "license_member": "telethon-1.44.0.dist-info/licenses/LICENSE"
    },
    {
      "name": "qrcode",
      "version": "8.2",
      "archive": "wheel",
      "url": "https://files.pythonhosted.org/packages/dd/b8/d2d6d731733f51684bbf76bf34dab3b70a9148e8f2cef2bb544fccec681a/qrcode-8.2-py3-none-any.whl",
      "sha256": "16e64e0716c14960108e85d853062c9e8bba5ca8252c0b4d0231b9df4060ff4f",
      "package_prefix": "qrcode/",
      "strip_prefix": "",
      "license_member": "qrcode-8.2.dist-info/LICENSE"
    },
    {
      "name": "rsa",
      "version": "4.9.1",
      "archive": "wheel",
      "url": "https://files.pythonhosted.org/packages/64/8d/0133e4eb4beed9e425d9a98ed6e081a55d195481b7632472be1af08d2f6b/rsa-4.9.1-py3-none-any.whl",
      "sha256": "68635866661c6836b8d39430f97a996acbd61bfa49406748ea243539fe239762",
      "package_prefix": "rsa/",
      "strip_prefix": "",
      "license_member": "rsa-4.9.1.dist-info/LICENSE"
    },
    {
      "name": "pyasn1",
      "version": "0.6.1",
      "archive": "wheel",
      "url": "https://files.pythonhosted.org/packages/c8/f1/d6a797abb14f6283c0ddff96bbdd46937f64122b8c925cab503dd37f8214/pyasn1-0.6.1-py3-none-any.whl",
      "sha256": "0d632f46f2ba09143da3a8afe9e33fb6f92fa2320ab7e886e2d0f7672af84629",
      "package_prefix": "pyasn1/",
      "strip_prefix": "",
      "license_member": "pyasn1-0.6.1.dist-info/LICENSE.rst"
    },
    {
      "name": "pyaes",
      "version": "1.6.1",
      "archive": "tar.gz",
      "url": "https://files.pythonhosted.org/packages/44/66/2c17bae31c906613795711fc78045c285048168919ace2220daa372c7d72/pyaes-1.6.1.tar.gz",
      "sha256": "02c1b1405c38d3c370b085fb952dd8bea3fadcee6411ad99f312cc129c536d8f",
      "package_prefix": "pyaes-1.6.1/pyaes/",
      "strip_prefix": "pyaes-1.6.1/",
      "license_member": "pyaes-1.6.1/LICENSE.txt"
    }
  ]
}
```

- [ ] **Step 2: Write failing builder tests**

In `tests/test_openwrt_ipk_builder.py`, add helpers that create a minimal wheel ZIP and a minimal `tar.gz` sdist in memory. Add tests for:

1. `vendor_entries` rejects a SHA mismatch.
2. It safely maps package files under `./opt/tg-downloader-ui/vendor/`.
3. It copies each license to `./usr/share/licenses/tg-downloader-ui/{name}-LICENSE`.
4. The built IPK contains importable `telethon`, `qrcode`, `rsa`, `pyasn1`, and `pyaes` package paths.
5. `postinst` contains no `pip install` and no swallowed dependency installation.
6. The init script exports `PYTHONPATH=/opt/tg-downloader-ui/vendor` and conditionally creates the forwarder procd instance only for `TGDL_FORWARDER_ENABLED=1`.

Use an injected `fetcher(url) -> bytes` backed by the in-memory archives so unit tests never access the network.

Add these concrete helpers and tests, importing `hashlib`, `json`, `zipfile`, and `mock`:

```python
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


def test_vendor_entries_map_packages_and_licenses(self):
    builder = load_builder()
    lock, payloads = fake_vendor_lock_and_payloads()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "openwrt").mkdir()
        (root / "openwrt/vendor-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        entries = builder.vendor_entries(root, fetcher=payloads.__getitem__)
    names = {name for name, _, _ in entries}
    for package in ["telethon", "qrcode", "rsa", "pyasn1", "pyaes"]:
        self.assertIn(f"./opt/tg-downloader-ui/vendor/{package}/__init__.py", names)
        self.assertIn(f"./usr/share/licenses/tg-downloader-ui/{package}-LICENSE", names)


def test_vendor_entries_reject_sha_mismatch(self):
    builder = load_builder()
    lock, payloads = fake_vendor_lock_and_payloads()
    lock["packages"][0]["sha256"] = "0" * 64
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "openwrt").mkdir()
        (root / "openwrt/vendor-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            builder.vendor_entries(root, fetcher=payloads.__getitem__)
```

Update every existing test that calls `builder.build_ipk` to patch `builder.vendor_entries` with deterministic fake entries. Add package entries for all five `__init__.py` files and five licenses, then assert they are present in `data.tar.gz`. This keeps the unit suite offline while the real build in Step 7 verifies the published URLs and hashes.

- [ ] **Step 3: Verify RED**

Run:

```sh
python -m unittest tests.test_openwrt_ipk_builder -v
```

Expected: new vendoring and conditional-forwarder assertions fail.

- [ ] **Step 4: Implement verified artifact loading**

Add `hashlib`, `hmac`, `json`, `urllib.request`, `zipfile`, and `PurePosixPath` imports to the builder. Implement:

```python
def download_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def verify_sha256(payload: bytes, expected: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError(f"vendor artifact sha256 mismatch: expected {expected}, got {actual}")


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


def vendor_entries(root: Path, fetcher=download_bytes) -> list[tuple[str, bytes, int]]:
    lock = json.loads((root / "openwrt/vendor-lock.json").read_text(encoding="utf-8"))
    entries = []
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
            relative = name[len(strip_prefix):] if strip_prefix else name
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
```

The implementation must reject members containing `..`, absolute paths, or paths escaping the configured prefix. `data_tar` appends `vendor_entries` to the existing application entries. Add `./opt/tg-downloader-ui/vendor`, `./usr/share/licenses`, and `./usr/share/licenses/tg-downloader-ui` to `DATA_DIRS`.

- [ ] **Step 5: Remove device-time pip and enable vendored imports**

Remove `python3-pip`, `curl`, and `tar` from `DEPENDS`. Remove the pip command from `POSTINST`.

In both procd command strings, export:

```sh
PYTHONPATH=/opt/tg-downloader-ui/vendor${PYTHONPATH:+:$PYTHONPATH}
```

Source `/etc/tg-downloader-ui.env` once at the start of `start_service`; create the forwarder instance only inside:

```sh
if [ "${TGDL_FORWARDER_ENABLED:-0}" = "1" ]; then
    # existing forwarder procd instance
fi
```

Add `TGDL_FORWARDER_ENABLED=0` to the OpenWRT environment example.

- [ ] **Step 6: Align Python package versions**

In `pyproject.toml`, pin the application dependencies used by Docker and OpenWRT:

```toml
dependencies = [
  "qrcode==8.2",
  "telethon==1.44.0",
]
```

Remove the redundant `forwarder` optional dependency group because the release intentionally preinstalls Telegram authorization support.

- [ ] **Step 7: Verify GREEN and perform a real IPK build**

Run unit tests, then:

```sh
python scripts/build_openwrt_ipk.py --output-dir dist/openwrt
```

Inspect the resulting IPK data archive and run an offline import smoke test by extracting its vendor directory to a temporary directory and executing:

```sh
vendor_root="$(mktemp -d)"
trap 'rm -rf "$vendor_root"' EXIT
tar -xOf dist/openwrt/tg-downloader-ui_0.1.0_all.ipk ./data.tar.gz | tar -xz -C "$vendor_root"
PYTHONPATH="$vendor_root/opt/tg-downloader-ui/vendor" python -c "import telethon, qrcode, rsa, pyasn1, pyaes"
```

The temporary directory is outside the repository and is removed after the check.

- [ ] **Step 8: Commit**

```sh
git add openwrt/vendor-lock.json scripts/build_openwrt_ipk.py tests/test_openwrt_ipk_builder.py tg-downloader-ui.init openwrt/tg-downloader-ui.env.example pyproject.toml
git commit -m "feat: vendor Telegram dependencies in OpenWRT package"
```

---

### Task 8: Update Public Documentation and CI Gates

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `THIRD_PARTY.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Update README deployment guidance**

Document all of these exact behaviors:

- local-first service; default Python host and Compose publication are loopback only;
- setup token comes from `TGDL_SETUP_TOKEN` or startup logs;
- new passwords require at least eight characters, with no composition rules;
- `TGDL_COOKIE_SECURE=1` is required behind HTTPS;
- `TGDL_FORWARDER_ENABLED=1` explicitly enables the optional forwarder;
- Docker is Linux x86-64 for `0.1.0` and verifies tdl `0.20.3`;
- host bind directories must be writable by UID/GID 1000;
- OpenWRT includes Telethon/qrcode and dependencies in the IPK and installs offline;
- state files, Telegram sessions, proxy credentials, and logs are sensitive;
- the service must not be exposed directly to the public Internet.

- [ ] **Step 2: Update security and third-party notices**

`SECURITY.md` must direct reports to GitHub private vulnerability reporting, list `0.1.x` as supported, and forbid secrets in public issues.

`THIRD_PARTY.md` must list:

- `iyear/tdl` 0.20.3, AGPL-3.0, exact source tag URL;
- Telethon 1.44.0, MIT;
- qrcode 8.2, BSD;
- rsa 4.9.1, Apache-2.0;
- pyasn1 0.6.1, BSD-2-Clause;
- pyaes 1.6.1, MIT.

State that anyone publishing a prebuilt image containing tdl must satisfy the corresponding AGPL source and notice obligations.

- [ ] **Step 3: Update contributor verification commands**

Add release-safety, wheel, IPK, and Docker smoke expectations to `CONTRIBUTING.md`. Explain that `.env.release-safety.local` is optional, private, and must never be attached to an issue.

- [ ] **Step 4: Strengthen CI without adding new tooling**

At the workflow top level add:

```yaml
permissions:
  contents: read
```

Keep the existing test matrix. Add an OpenWRT package job with these commands, and keep the existing Docker build/version check:

```yaml
- run: python scripts/build_openwrt_ipk.py --output-dir dist/openwrt
- run: |
    vendor_root="$(mktemp -d)"
    trap 'rm -rf "$vendor_root"' EXIT
    tar -xOf dist/openwrt/tg-downloader-ui_0.1.0_all.ipk ./data.tar.gz | tar -xz -C "$vendor_root"
    PYTHONPATH="$vendor_root/opt/tg-downloader-ui/vendor" python -c "import telethon, qrcode, rsa, pyasn1, pyaes"
```

- [ ] **Step 5: Run documentation and safety checks**

Run:

```sh
python tests/test_release_safety.py -v
python -m unittest tests.test_openwrt_ipk_builder tests.test_tg_downloader_ui.DockerComposeTests -v
git diff --check
```

Expected: all pass.

- [ ] **Step 6: Commit**

```sh
git add README.md SECURITY.md THIRD_PARTY.md CONTRIBUTING.md .github/workflows/ci.yml
git commit -m "docs: document secure local-first deployment"
```

---

### Task 9: Full Local Verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Run the full unit suite**

```sh
python -m unittest discover tests -v
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Compile all Python sources**

```sh
python -m compileall tg_downloader_ui tests scripts
```

Expected: exit code 0.

- [ ] **Step 3: Build wheel and sdist**

```sh
python -m pip install build
python -m build
```

Expected: `tg_downloader_ui-0.1.0-py3-none-any.whl` and source archive are created.

- [ ] **Step 4: Build and inspect the OpenWRT package**

```sh
python scripts/build_openwrt_ipk.py --output-dir dist/openwrt
python -m unittest tests.test_openwrt_ipk_builder -v
```

Expected: IPK build succeeds and vendor/archive assertions pass.

- [ ] **Step 5: Re-run release and Git hygiene checks**

```sh
python tests/test_release_safety.py -v
git diff --check
git status --short --branch
```

Expected: only intentional tracked changes or a clean tree after commits; `.env.release-safety.local` remains ignored.

---

### Task 10: Isolated Docker Smoke Test on the Private Host

**Files:**
- No tracked source changes expected unless the smoke test exposes a defect; defects return to the relevant TDD task.

- [ ] **Step 1: Set the private SSH target outside the repository**

Use the user-provided target only in the current shell:

```powershell
$target = $env:TGDL_DOCKER_TEST_SSH
if (-not $target) { throw 'TGDL_DOCKER_TEST_SSH is required' }
```

Do not write the target to `.env`, scripts, documentation, or Git config.

- [ ] **Step 2: Create and upload a committed-source archive**

Generate a unique identifier, archive `HEAD`, and upload it to `/tmp` with `scp`. Use only files included by `git archive`, so `.env.release-safety.local` and other ignored files cannot be transferred:

```powershell
$id = [guid]::NewGuid().ToString('N').Substring(0, 12)
$archive = Join-Path $env:TEMP ("tgdl-p0-$id.tar")
git archive --format=tar --output=$archive HEAD
scp $archive ("${target}:/tmp/tgdl-p0-$id.tar")
```

- [ ] **Step 3: Build and start an isolated Compose project**

On the remote host, set `remote_dir=/tmp/tgdl-p0-$id`, `project=tgdl-p0-$id`, extract the uploaded archive there, and create an untracked `.env` containing:

```dotenv
TGDL_SETUP_TOKEN=docker-smoke-setup-token
TGDL_PUBLISH_HOST=127.0.0.1
TGDL_PUBLISH_PORT=0
TGDL_FORWARDER_ENABLED=0
```

Run:

```sh
docker compose -p "$project" up -d --build
```

Docker selects an unused host port because the requested publish port is `0`.

- [ ] **Step 4: Verify runtime security behavior**

Check:

1. `docker compose -p "$project" port web 9910` reports `127.0.0.1:` followed by a numeric port.
2. `/proc/1/status` inside the container reports a nonzero UID after the entrypoint drops privileges.
3. No `tg-downloader-forwarder` process exists by default.
4. Setup without `X-TGDL-Setup-Token` returns 403.
5. Setup with the token and password `docker-test-password` returns 201.
6. Login returns a session cookie.
7. `/api/auth/me` returns a CSRF token.
8. Authenticated POST without CSRF returns 403; with CSRF it succeeds.
9. Response headers include CSP, frame denial, no-sniff, and no-referrer.
10. `/config/config.json`, `/config/state.db`, and generated logs are not group/world readable.

Use the container's Python stdlib client for the HTTP sequence so the host needs only Docker and SSH.

- [ ] **Step 5: Always clean up the remote project**

In a `finally`/trap path, run:

```sh
case "$remote_dir" in /tmp/tgdl-p0-*) ;; *) exit 1 ;; esac
docker compose -p "$project" down -v --remove-orphans
rm -rf "$remote_dir" "/tmp/tgdl-p0-$id.tar"
```

Resolve and verify both remote paths begin with `/tmp/tgdl-p0-` before recursive deletion.

- [ ] **Step 6: Record only non-sensitive results**

Report build status, HTTP status codes, container UID, port binding, forwarder state, and file modes. Do not report setup tokens, cookies, CSRF tokens, Telegram configuration, or host inventory.

---

### Task 11: Final Review and Release-History Handoff

**Files:**
- No source changes expected.

- [ ] **Step 1: Review the complete diff against the approved design**

Confirm every section in `docs/superpowers/specs/2026-07-10-p0-open-source-hardening-design.md` has an implementation and verification result.

- [ ] **Step 2: Verify commit identity**

```sh
git log -5 --format='%h %an <%ae> %s'
```

Expected: new commits use `ifox2046 <2927211+ifox2046@users.noreply.github.com>`.

- [ ] **Step 3: Request code review**

Invoke `superpowers:requesting-code-review`, inspect its findings, and fix any P0 correctness or security issue through a new failing test.

- [ ] **Step 4: Run the full verification gate again**

Repeat Task 9 after any review fix. Do not claim completion from an earlier test run.

- [ ] **Step 5: Stop before destructive history rewriting**

Report that the source is ready for history cleanup. Ask for explicit approval before squashing/rebasing/filtering the existing branch or rotating external credentials. The recommended public-history operation is a clean single initial commit after preserving a private backup branch or bundle.
