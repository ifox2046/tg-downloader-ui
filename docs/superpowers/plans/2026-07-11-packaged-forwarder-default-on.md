# Packaged Forwarder Default-On Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the packaged Docker and OpenWrt forwarder default to enabled, replace the obsolete Docker restart hint, and guide administrators to configure Telegram API credentials when they are missing.

**Architecture:** Deployment files set `TGDL_FORWARDER_ENABLED=1` while retaining `0` as an explicit opt-out. The status API derives restart availability from both the enabled flag and restart command, maps known missing-API failures to a configuration hint, and the existing single-page UI links that hint to the Telegram authorization page.

**Tech Stack:** Python 3.10+ stdlib, `unittest`, POSIX shell, Docker Compose, OpenWrt procd.

---

### Task 1: Define Packaged Default-On Behavior

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`
- Modify: `tests/test_openwrt_ipk_builder.py`

- [ ] **Step 1: Write failing Docker default assertions**

In `DockerComposeTests.test_compose_publishes_only_to_loopback_and_disables_forwarder`, rename the test to `test_compose_publishes_only_to_loopback_and_enables_forwarder` and assert:

```python
self.assertIn(
    'TGDL_FORWARDER_ENABLED: "${TGDL_FORWARDER_ENABLED:-1}"',
    compose,
)
dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
env_example = Path(".env.example").read_text(encoding="utf-8")
self.assertIn("TGDL_FORWARDER_ENABLED=1", dockerfile)
self.assertIn("TGDL_FORWARDER_ENABLED=1", env_example)
```

- [ ] **Step 2: Write failing OpenWrt default assertions**

In `OpenWrtIpkBuilderTests.test_init_script_exports_env_for_app_and_forwarder`, add:

```python
env_example = (root / "openwrt" / "tg-downloader-ui.env.example").read_text(
    encoding="utf-8"
)
self.assertIn('TGDL_FORWARDER_ENABLED="${TGDL_FORWARDER_ENABLED:-1}"', init_script)
self.assertIn("TGDL_FORWARDER_ENABLED=1", env_example)
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.DockerComposeTests `
  tests.test_openwrt_ipk_builder.OpenWrtIpkBuilderTests.test_init_script_exports_env_for_app_and_forwarder -v
```

Expected: failures because Docker, Compose, `.env.example`, OpenWrt init, and the OpenWrt env example still default to `0`.

---

### Task 2: Enable the Packaged Forwarder by Default

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `tg-downloader-ui.init`
- Modify: `openwrt/tg-downloader-ui.env.example`
- Modify: `README.md`
- Test: `tests/test_tg_downloader_ui.py`
- Test: `tests/test_openwrt_ipk_builder.py`

- [ ] **Step 1: Change deployment defaults to `1`**

Use these exact values:

```dockerfile
TGDL_FORWARDER_ENABLED=1 \
```

```yaml
TGDL_FORWARDER_ENABLED: "${TGDL_FORWARDER_ENABLED:-1}"
```

```dotenv
TGDL_FORWARDER_ENABLED=1
```

In `tg-downloader-ui.init`, use:

```sh
TGDL_FORWARDER_ENABLED="${TGDL_FORWARDER_ENABLED:-1}" \
```

and start the OpenWrt forwarder with:

```sh
if [ "${TGDL_FORWARDER_ENABLED:-1}" = "1" ]; then
```

- [ ] **Step 2: Document opt-out behavior**

Update README wording to state that Docker and OpenWrt start the optional forwarder by default and operators can set:

```dotenv
TGDL_FORWARDER_ENABLED=0
```

to disable it. Update the configuration table default from `0` to `1` for packaged deployments.

- [ ] **Step 3: Run deployment tests and syntax checks**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.DockerComposeTests `
  tests.test_openwrt_ipk_builder -v
sh -n docker/entrypoint.sh
sh -n docker/restart-forwarder.sh
sh -n tg-downloader-ui.init
```

Expected: all commands exit `0`.

- [ ] **Step 4: Commit packaged defaults**

```powershell
git add Dockerfile docker-compose.yml .env.example tg-downloader-ui.init `
  openwrt/tg-downloader-ui.env.example README.md `
  tests/test_tg_downloader_ui.py tests/test_openwrt_ipk_builder.py
git commit -m "feat: enable packaged forwarder by default"
```

---

### Task 3: Define Configuration and Restart Hints

**Files:**
- Modify: `tests/test_tg_downloader_ui.py`

- [ ] **Step 1: Add a missing-API status test**

Add to `ForwarderStatusApiTests`:

```python
def test_forwarder_status_prompts_for_missing_telegram_api(self):
    original_read = app.read_forwarder_status
    original_restart = app.forwarder_restart_configured
    try:
        app.read_forwarder_status = lambda: {
            "state": "failed",
            "last_error": "TGDL_API_ID is required",
        }
        app.forwarder_restart_configured = lambda: True

        data = app.forwarder_status_response()

        self.assertTrue(data["configuration_required"])
        self.assertIn("Telegram 授权", data["configuration_hint"])
        self.assertEqual(data["last_error"], "TGDL_API_ID is required")
    finally:
        app.read_forwarder_status = original_read
        app.forwarder_restart_configured = original_restart
```

- [ ] **Step 2: Add disabled and unconfigured restart-hint tests**

Add direct tests which temporarily set `TGDL_FORWARDER_ENABLED=0`, then assert:

```python
data = app.forwarder_status_response()
self.assertFalse(data["forwarder_enabled"])
self.assertFalse(data["restart_configured"])
self.assertIn("TGDL_FORWARDER_ENABLED=1", data["restart_hint"])
self.assertNotIn("docker compose restart forwarder", data["restart_hint"])
```

Update the existing missing-command test to assert:

```python
self.assertIn("TGDL_FORWARDER_RESTART_CMD", data["restart_hint"])
self.assertNotIn("docker compose restart forwarder", data["restart_hint"])
```

- [ ] **Step 3: Add a UI navigation assertion**

Add to `IndexTemplateTests`:

```python
def test_forwarder_configuration_hint_links_to_telegram_page(self):
    self.assertIn("status.configuration_hint", app.INDEX_HTML)
    self.assertIn("showPage('telegram')", app.INDEX_HTML)
    self.assertNotIn("docker compose restart forwarder", app.INDEX_HTML)
```

- [ ] **Step 4: Run tests to verify RED**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.ForwarderStatusApiTests `
  tests.test_tg_downloader_ui.IndexTemplateTests -v
```

Expected: failures because the status response has no enabled/configuration fields and the UI has no configuration link.

---

### Task 4: Implement Configuration-Aware Forwarder Status

**Files:**
- Modify: `tg_downloader_ui/app.py`
- Test: `tests/test_tg_downloader_ui.py`

- [ ] **Step 1: Replace the obsolete hint constants**

Define:

```python
FORWARDER_DISABLED_HINT = (
    "forwarder 未启用，请设置 TGDL_FORWARDER_ENABLED=1 并重启部署。"
)
FORWARDER_RESTART_HINT = (
    "forwarder 重启命令未配置，请设置 TGDL_FORWARDER_RESTART_CMD。"
)
FORWARDER_CONFIGURATION_HINT = (
    "尚未配置 Telegram API，请前往“Telegram 授权”填写 API ID 和 API Hash。"
)
FORWARDER_CONFIGURATION_ERRORS = {
    "TGDL_API_ID is required",
    "TGDL_API_HASH is required",
}
```

- [ ] **Step 2: Add enabled-state parsing**

Add:

```python
def forwarder_enabled() -> bool:
    return os.environ.get("TGDL_FORWARDER_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
```

- [ ] **Step 3: Enrich the status response**

Implement:

```python
def forwarder_status_response() -> dict[str, Any]:
    status = read_forwarder_status()
    enabled = forwarder_enabled()
    restart_configured = enabled and forwarder_restart_configured()
    status["forwarder_enabled"] = enabled
    status["restart_configured"] = restart_configured

    if not enabled:
        status["restart_hint"] = FORWARDER_DISABLED_HINT
    elif not restart_configured:
        status["restart_hint"] = FORWARDER_RESTART_HINT

    configuration_required = str(status.get("last_error") or "") in (
        FORWARDER_CONFIGURATION_ERRORS
    )
    status["configuration_required"] = configuration_required
    if configuration_required:
        status["configuration_hint"] = FORWARDER_CONFIGURATION_HINT
    return status
```

- [ ] **Step 4: Render the configuration action**

In `renderForwarder`, append:

```javascript
const configurationAction = status.configuration_hint
  ? `<button class="secondary" type="button" onclick="showPage('telegram')">配置 Telegram API</button><span class="muted">${escapeHtml(status.configuration_hint)}</span>`
  : '';
```

and append `configurationAction` to `forwarderStatus.innerHTML` before the restart controls.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest `
  tests.test_tg_downloader_ui.ForwarderStatusApiTests `
  tests.test_tg_downloader_ui.IndexTemplateTests -v
```

Expected: all pass.

- [ ] **Step 6: Commit status behavior**

```powershell
git add tg_downloader_ui/app.py tests/test_tg_downloader_ui.py
git commit -m "fix: guide forwarder Telegram configuration"
```

---

### Task 5: Full Verification and Remote Container Refresh

**Files:**
- No additional tracked changes expected.

- [ ] **Step 1: Run the complete local gate**

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m unittest discover tests -v
& 'C:\ProgramData\anaconda3\python.exe' -m compileall tg_downloader_ui tests scripts
& 'C:\ProgramData\anaconda3\python.exe' tests/test_release_safety.py -v
& 'C:\ProgramData\anaconda3\python.exe' scripts/build_openwrt_ipk.py --output-dir dist/openwrt
git diff --check
git status --short --branch
```

Expected: 102 or more tests pass with only the two Windows POSIX-mode skips, builds exit `0`, and the worktree is clean.

- [ ] **Step 2: Build the remote image from committed files**

Create and upload only `git archive HEAD`, then build on the existing test host with:

```sh
proxy_value="$(docker info --format '{{.HTTPSProxy}}')"
[ -n "$proxy_value" ] || proxy_value="$(docker info --format '{{.HTTPProxy}}')"
docker build --network=host \
  --build-arg HTTPS_PROXY="$proxy_value" \
  -t tg-downloader-ui:test-current .
```

Never upload `.env.release-safety.local` or other ignored files.

- [ ] **Step 3: Update private test env files without clearing volumes**

Set both remote env files to:

```dotenv
TGDL_FORWARDER_ENABLED=1
TGDL_COOKIE_SECURE=0
```

Keep their mode at `0600`. Remove and recreate `tgdl-test-1` and
`tgdl-test-2` with the existing named volumes; do not delete or clear any
volume.

- [ ] **Step 4: Verify runtime behavior**

For both containers verify:

- publication remains `127.0.0.1:19910` and `127.0.0.1:19911`;
- PID 1 uses UID `1000`;
- `TGDL_FORWARDER_ENABLED=1` and the in-container restart command are present;
- the forwarder supervisor process is running;
- missing API ID/Hash writes a failed status and the packaged Web UI contains
  the `Telegram 授权` configuration hint;
- invoking `/usr/local/bin/tg-downloader-forwarder-restart` returns success;
- existing named volumes and their contents remain present.

- [ ] **Step 5: Final hygiene**

Remove local and remote temporary archives/source directories, then run:

```powershell
git status --short --branch
git log -5 --format='%h %an <%ae> %s'
```

Expected: clean `main`, commits owned by
`ifox2046 <2927211+ifox2046@users.noreply.github.com>`, and both persistent
test containers running the refreshed image.
