# Remove Setup Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the first-run setup token while preserving loopback defaults, password validation, unattended administrator provisioning, and the existing HTTP security boundary.

**Architecture:** Keep `ConfigStore.requires_setup()` as the sole setup-state gate. Delete token generation and transport from the HTTP server, browser template, Docker/OpenWRT configuration, and documentation; tests define that the first valid setup request succeeds and every later setup request fails.

**Tech Stack:** Python 3.10+, stdlib `http.server`, `unittest`, Docker Compose, POSIX shell, OpenWRT procd.

---

### Task 1: Define Token-Free Setup Behavior

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`

- [ ] **Step 1: Replace setup-token template assertions**

Replace `test_setup_form_collects_one_time_token` with:

```python
def test_setup_form_does_not_collect_setup_token(self):
    self.assertNotIn('id="setupToken"', app.SETUP_HTML)
    self.assertNotIn("X-TGDL-Setup-Token", app.SETUP_HTML)
```

- [ ] **Step 2: Change the setup HTTP test to require no token and reject reinitialization**

Construct `DownloadServer` without `setup_token`. The first `POST /api/setup`
uses this payload and must return `201`:

```python
{"username": "owner", "password": "strong-password", "download_dir": ""}
```

Send the same request again and assert `400` with the configuration still owned
by `owner`.

- [ ] **Step 3: Run tests to verify RED**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.IndexTemplateTests.test_setup_form_does_not_collect_setup_token `
  tests.test_tg_downloader_ui.AuthHttpTests.test_setup_api_returns_default_download_dir_and_accepts_blank_download_dir -v
```

Expected: failures because the template still contains the token and the server
still requires the header.

---

### Task 2: Remove Setup Token from the Application

**Files:**
- Modify: `tg_downloader_ui/app.py`
- Modify: `tests/test_tg_downloader_ui.py`

- [ ] **Step 1: Delete token generation and server state**

Delete `new_setup_token`, `RequestHandler.setup_token`, the `setup_token`
parameter and attribute from `DownloadServer`, and token creation/logging in
`run_server`.

- [ ] **Step 2: Remove header validation from setup**

Make the setup route begin directly with JSON parsing:

```python
if parsed.path == "/api/setup":
    if not self.config_store.requires_setup():
        return self.send_error_text(HTTPStatus.BAD_REQUEST, "setup already completed")
    try:
        payload = self.read_json()
        self.config_store.initialize(
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
            download_dir=str(payload.get("download_dir") or ""),
            telegram=dict(payload.get("telegram") or {}),
        )
        return self.send_json({"ok": True}, status=HTTPStatus.CREATED)
    except Exception as exc:
        return self.send_error_text(HTTPStatus.BAD_REQUEST, str(exc))
```

- [ ] **Step 3: Remove the token field and header from `SETUP_HTML`**

Delete the `setupToken` input and use:

```javascript
const res = await fetch('/api/setup', {
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify(payload)
});
```

- [ ] **Step 4: Simplify HTTP test helpers**

Remove `setup_token` from `AuthHttpTests.running_server` and every direct
`DownloadServer` construction.

- [ ] **Step 5: Run GREEN tests**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.ConfigAuthTests `
  tests.test_tg_downloader_ui.IndexTemplateTests `
  tests.test_tg_downloader_ui.AuthHttpTests -v
```

Expected: all pass.

- [ ] **Step 6: Commit application behavior**

```powershell
git add tg_downloader_ui/app.py tests/test_tg_downloader_ui.py
git commit -m "refactor: remove first-run setup token"
```

---

### Task 3: Remove Deployment Token Configuration

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tests/test_openwrt_ipk_builder.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `openwrt/tg-downloader-ui.env.example`
- Modify: `tg-downloader-ui.init`

- [ ] **Step 1: Write failing deployment assertions**

In `DockerComposeTests`, replace the positive setup-token assertion with:

```python
self.assertNotIn("TGDL_SETUP_TOKEN", compose)
```

In the OpenWRT init test add:

```python
self.assertNotIn("TGDL_SETUP_TOKEN", init_script)
```

- [ ] **Step 2: Run deployment tests to verify RED**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.DockerComposeTests `
  tests.test_openwrt_ipk_builder.OpenWrtIpkBuilderTests.test_init_script_exports_env_for_app_and_forwarder -v
```

Expected: failures because Docker Compose and procd still propagate the token.

- [ ] **Step 3: Remove deployment settings**

Delete `TGDL_SETUP_TOKEN` from `docker-compose.yml`, `.env.example`,
`openwrt/tg-downloader-ui.env.example`, and the `set_runtime_env` arguments in
`tg-downloader-ui.init`.

- [ ] **Step 4: Run deployment tests and shell syntax checks**

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.DockerComposeTests `
  tests.test_openwrt_ipk_builder -v
sh -n docker/entrypoint.sh
sh -n docker/restart-forwarder.sh
sh -n tg-downloader-ui.init
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit deployment cleanup**

```powershell
git add .env.example docker-compose.yml openwrt/tg-downloader-ui.env.example `
  tg-downloader-ui.init tests/test_tg_downloader_ui.py tests/test_openwrt_ipk_builder.py
git commit -m "refactor: remove setup token deployment settings"
```

---

### Task 4: Update Public Documentation

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`

- [ ] **Step 1: Remove token instructions**

Delete setup-token references and `TGDL_SETUP_TOKEN` from README. State:

```markdown
Complete the first-run administrator setup before changing the bind or publish
address from loopback to a LAN address.
```

Keep the eight-character password rule and document optional unattended setup
with `TGDL_AUTH_USER` and `TGDL_AUTH_PASSWORD`.

- [ ] **Step 2: Check tracked documentation**

Run:

```powershell
git grep -n "TGDL_SETUP_TOKEN\|setup token\|设置令牌" -- `
  README.md CONTRIBUTING.md SECURITY.md THIRD_PARTY.md .env.example `
  openwrt/tg-downloader-ui.env.example docker-compose.yml tg-downloader-ui.init
```

Expected: no output.

- [ ] **Step 3: Commit documentation**

```powershell
git add README.md CONTRIBUTING.md
git commit -m "docs: simplify local first-run setup"
```

---

### Task 5: Full Verification and Test Container Rebuild

**Files:**
- No tracked changes expected.

- [ ] **Step 1: Run the full local gate**

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest discover tests -v
& 'C:\ProgramData\anaconda3\python.exe' -m compileall tg_downloader_ui tests scripts
& 'C:\ProgramData\anaconda3\python.exe' tests/test_release_safety.py -v
& 'C:\ProgramData\anaconda3\python.exe' scripts/build_openwrt_ipk.py --output-dir dist/openwrt
git diff --check
git status --short --branch
```

Expected: tests pass, compile/build commands exit `0`, and the worktree is clean.

- [ ] **Step 2: Rebuild the shared remote image from `git archive HEAD`**

Use only committed files:

```powershell
$target = $env:TGDL_DOCKER_TEST_SSH
if (-not $target) { throw 'TGDL_DOCKER_TEST_SSH is required' }
$id = [guid]::NewGuid().ToString('N').Substring(0, 12)
$archive = Join-Path $env:TEMP "tgdl-tokenless-$id.tar"
git archive --format=tar --output=$archive HEAD
scp $archive ("${target}:/tmp/tgdl-tokenless-$id.tar")
```

On the remote host, validate both paths begin with `/tmp/tgdl-tokenless-`,
extract the archive, then build:

```sh
proxy_value="$(docker info --format '{{.HTTPSProxy}}')"
[ -n "$proxy_value" ] || proxy_value="$(docker info --format '{{.HTTPProxy}}')"
docker build --network=host \
  --build-arg HTTPS_PROXY="$proxy_value" \
  -t tg-downloader-ui:test-current .
```

Remove the remote source directory/archive and the local archive after the
image build. Never upload `.env.release-safety.local` or any ignored file.

- [ ] **Step 3: Recreate both test containers without token env files**

Recreate `tgdl-test-1` and `tgdl-test-2` on `127.0.0.1:19910` and
`127.0.0.1:19911` using their existing isolated named volumes after clearing
the old uninitialized volumes. Their env files contain only:

```dotenv
TGDL_FORWARDER_ENABLED=0
TGDL_COOKIE_SECURE=0
```

Use these exact container mappings:

```sh
docker rm -f tgdl-test-1 tgdl-test-2 >/dev/null 2>&1 || true
docker volume rm \
  tgdl-test-1-config tgdl-test-1-tdl tgdl-test-1-downloads \
  tgdl-test-2-config tgdl-test-2-tdl tgdl-test-2-downloads \
  >/dev/null 2>&1 || true

docker run -d --name tgdl-test-1 --restart unless-stopped \
  --env-file "$HOME/.config/tg-downloader-ui-tests/test-1.env" \
  -p 127.0.0.1:19910:9910 \
  -v tgdl-test-1-config:/config \
  -v tgdl-test-1-tdl:/tdl \
  -v tgdl-test-1-downloads:/downloads \
  tg-downloader-ui:test-current

docker run -d --name tgdl-test-2 --restart unless-stopped \
  --env-file "$HOME/.config/tg-downloader-ui-tests/test-2.env" \
  -p 127.0.0.1:19911:9910 \
  -v tgdl-test-2-config:/config \
  -v tgdl-test-2-tdl:/tdl \
  -v tgdl-test-2-downloads:/downloads \
  tg-downloader-ui:test-current
```

- [ ] **Step 4: Verify token-free runtime behavior**

For both containers verify:

- `GET /api/setup` reports `required: true`;
- the first valid `POST /api/setup` succeeds without a token header;
- PID 1 is UID `1000`;
- publication is loopback-only;
- forwarder is absent;
- private directories are `0700` and files are `0600`.

Use temporary test credentials, then clear and recreate the instance volumes so
both containers are left uninitialized for the user.

- [ ] **Step 5: Final hygiene check**

Confirm no temporary archive/script remains locally or remotely and run:

```powershell
git status --short --branch
git log -5 --format='%h %an <%ae> %s'
```

Expected: clean feature branch and commit identity
`ifox2046 <2927211+ifox2046@users.noreply.github.com>`.
