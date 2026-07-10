# Remove the First-Run Setup Token

## Decision

Remove the one-time setup token completely. An uninitialized installation lets
the first browser session create the administrator account using a username, a
password of at least eight characters, and a download directory.

This matches the project's local-first deployment model: Python and Docker
publish on loopback by default. Operators must complete setup before explicitly
publishing the service to a LAN address.

## Preserved Security Boundaries

- Python continues to bind to `127.0.0.1` by default.
- Docker Compose continues to publish to `127.0.0.1` by default.
- New passwords still require at least eight characters.
- Login throttling, CSRF validation, request-size limits, security headers,
  private file modes, and proxy-log redaction remain unchanged.
- `TGDL_AUTH_USER` and `TGDL_AUTH_PASSWORD` continue to support unattended
  administrator provisioning without the setup page.

## Code and Configuration Changes

- Remove setup-token generation, server state, request-header validation, and
  the setup form token field.
- Remove `TGDL_SETUP_TOKEN` from Docker Compose, environment examples, OpenWRT
  procd environment propagation, README, and tests.
- Keep `POST /api/setup` available only while `ConfigStore.requires_setup()` is
  true. After successful initialization it continues to reject further setup
  attempts.

## Testing

- HTTP tests prove setup succeeds without `X-TGDL-Setup-Token` and still rejects
  a second initialization attempt.
- Template/configuration tests prove no setup-token field or deployment setting
  remains.
- Run the full unit, compile, package, release-safety, OpenWRT, and Docker gates.
- Rebuild `tgdl-test-1` and `tgdl-test-2` from the verified image, delete their
  obsolete token files, and verify both remain uninitialized and loopback-only.

## Accepted Trade-off

If an operator deliberately exposes an uninitialized instance beyond loopback,
the first visitor can claim the administrator account. This is documented as an
operator responsibility rather than adding a second first-run secret.
