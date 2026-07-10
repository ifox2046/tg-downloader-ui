# P0 Open-Source Hardening Design

## Goal

Make the repository safe to publish as a local-first `0.1.0` release without
adding a web framework or new Python runtime dependencies. The release remains
an administrator-operated downloader, not an Internet-facing multi-user
service.

## Scope

This change covers repository secret regression checks, first-run ownership,
authentication boundaries, sensitive file permissions, command-log redaction,
Docker defaults, OpenWRT installation behavior, and deployment documentation.

Rewriting existing Git history and rotating credentials are release operations,
not source changes. They remain mandatory after this implementation, but will
only be performed with explicit approval because they are destructive or affect
external systems.

## Repository Safety Checks

`tests/test_release_safety.py` will no longer contain any real internal value,
even split across string literals. Its public checks will use generic patterns
for private keys, common access tokens, populated Telegram API hashes, bot
tokens, long session strings, and accidentally tracked secret-bearing files.

Private regression values will live in an untracked
`.env.release-safety.local` file. The file contains a JSON array assigned to
`TGDL_RELEASE_SAFETY_DENYLIST`. The test reads this file when present and adds
the literal values to its scan. CI remains reproducible when the local file is
absent and still runs the generic checks. A committed example documents the
format without containing real values, and `.gitignore` excludes the local
file.

## First-Run Ownership

When setup is required, the server has a one-time setup token. It uses
`TGDL_SETUP_TOKEN` when supplied; otherwise it generates a random token and
prints it once to the process log. The setup form asks for this token and
`POST /api/setup` rejects missing or incorrect tokens. Completing setup makes
the endpoint unavailable as it is today.

The Python application default host changes to `127.0.0.1`. Docker may continue
to listen on `0.0.0.0` inside the container, but Compose publishes it as
`127.0.0.1:9910:9910`. Documentation explains how to opt into LAN access and
requires a trusted reverse proxy and TLS for any broader exposure.

## Authentication and HTTP Boundary

New passwords must contain at least 8 characters. Existing stored hashes
remain valid so upgrades do not lock out current users.

Login failures are tracked in memory by client address and username. Five
failures within five minutes cause a fifteen-minute block and return HTTP 429.
A successful login clears the failure record. Proxy forwarding headers are not
trusted because the stdlib server has no trusted-proxy configuration.

Each authenticated session receives a CSRF token. `/api/auth/me` returns it,
the browser sends it in `X-CSRF-Token` for POST, PUT, and DELETE requests, and
the server rejects missing or mismatched tokens. Login uses password
authentication and setup uses the setup token, so neither needs an existing
CSRF token.

JSON request bodies are capped at 1 MiB. Responses add a minimal set of
security headers: CSP compatible with the current inline UI, frame denial,
content-type sniffing protection, and a no-referrer policy. Session cookies
remain `HttpOnly` and `SameSite=Lax`; `TGDL_COOKIE_SECURE=1` adds `Secure` for
TLS deployments.

## Sensitive State and Logs

State directories are created as mode `0700` where supported. Configuration,
SQLite databases, Telegram authorization state, session files, status files,
and logs are set to `0600` after creation or replacement. Permission failures
on platforms without POSIX modes are tolerated, matching the existing session
file behavior.

Commands continue to execute as argv lists without a shell. Before commands
are written to job logs or returned by restart APIs, proxy argument values are
replaced with `<redacted>` so proxy credentials cannot leak through the UI.

## Docker

The Docker build verifies the exact SHA-256 checksum of the pinned `tdl`
archive. `.dockerignore` excludes `.env`, local agent/tooling state, build
artifacts, and local release-safety data.

The image creates an unprivileged application account. Its root entrypoint only
prepares the three writable mount roots, then re-executes itself as the
application user before starting the UI or forwarder. The forwarder is disabled
by default and starts only when `TGDL_FORWARDER_ENABLED=1`.

The documented image remains Linux x86-64 for `0.1.0`; the limitation is stated
explicitly instead of implying multi-architecture support.

## OpenWRT

The package post-install script no longer runs `pip` or performs optional
network installation as root. Instead, the development-machine IPK build pins,
downloads, hash-verifies, and vendors the pure-Python Telethon and qrcode
dependency set under `/opt/tg-downloader-ui/vendor`. Pillow is not included
because the application uses qrcode's SVG output. The IPK includes the
corresponding third-party license notices and configures the packaged runtime
to import from the vendor directory, so Telegram authorization and the
forwarder work immediately after an offline device installation.

The procd forwarder instance is created only when
`TGDL_FORWARDER_ENABLED=1`. The environment example defaults it to `0`.
Device package dependencies that were needed only by the removed post-install
download are removed from the package metadata.

## Documentation and Third-Party Notice

README and SECURITY documentation will state that the application is
local-first, describe the setup token and TLS cookie setting, document file and
log sensitivity, explain how to enable the optional forwarder, and warn against
publishing Telegram sessions or proxy credentials.

The third-party notice will identify the exact bundled `tdl` version and source
URL and state that prebuilt image publishers must satisfy the corresponding
AGPL source and notice obligations. It will also list the Python runtime
dependencies used by optional Telegram authorization features.

## Verification

Every behavior change starts with a failing unittest. The final local gate is:

1. `python -m unittest discover tests -v`
2. `python -m compileall tg_downloader_ui tests scripts`
3. wheel build with no runtime dependency download
4. OpenWRT IPK build and offline import smoke test for vendored dependencies
5. repository secret scan and clean Git status

Docker verification runs in a unique temporary directory on the private Docker
test host supplied for this task. It builds the image, confirms the application
process is non-root, verifies the port is bound only to localhost, exercises
setup-token rejection and acceptance, checks authenticated CSRF behavior, and
confirms the forwarder is absent by default. The temporary compose project and
files are removed after the smoke test. The private SSH target is never written
to tracked files.

## Non-Goals

- Replacing the stdlib HTTP server with a framework.
- Multi-user accounts, password recovery, OAuth, or public Internet hosting.
- Multi-architecture Docker images in `0.1.0`.
- Automatically rewriting Git history or rotating external credentials.
