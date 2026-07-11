# Docker Forwarder Default-On Design

## Decision

Keep the Telegram forwarder optional, but enable it by default for Docker
deployments. Operators can still disable it explicitly with
`TGDL_FORWARDER_ENABLED=0`.

OpenWrt keeps its current default of `0`; this change is limited to Docker so
an OpenWrt package upgrade does not unexpectedly start an optional background
service.

## Docker Defaults

- Set the Docker image default `TGDL_FORWARDER_ENABLED` to `1`.
- Set the Docker Compose fallback to `1`.
- Set the root `.env.example` value to `1`.
- Keep the in-container restart command as
  `/usr/local/bin/tg-downloader-forwarder-restart`.
- Keep `TGDL_FORWARDER_ENABLED=0` as the explicit opt-out mechanism.

## Missing Telegram Configuration

Starting the default-enabled forwarder without Telegram API configuration must
not produce the obsolete instruction to run `docker compose restart
forwarder`.

When the forwarder reports that `TGDL_API_ID` or `TGDL_API_HASH` is missing,
the status response will expose a configuration-required flag and a clear
Chinese hint directing the administrator to the `Telegram 授权` page to enter
the API ID and API Hash. The Web UI will render this hint next to the forwarder
status while retaining the original diagnostic error for troubleshooting.

After configuration is saved, the existing Web UI restart button invokes the
in-container restart script. No Docker socket or host-side Compose command is
required.

If the forwarder is explicitly disabled, the disabled restart button will say
that `TGDL_FORWARDER_ENABLED=1` is required and that the container must be
recreated. If the forwarder is enabled but no restart command exists in a
non-Docker deployment, the hint will only mention
`TGDL_FORWARDER_RESTART_CMD`.

## Tests

- Docker configuration tests assert the image, Compose fallback, and root env
  example default to `1`.
- Status tests assert missing API ID/Hash produces the configuration hint.
- Status tests assert disabled forwarder and missing restart command use the
  new hints and never mention the removed `docker compose restart forwarder`
  command.
- Existing restart API tests continue to prove the command is executed without
  a shell.
- Rebuild both remote Docker test containers with the forwarder enabled and
  verify the restart button is configured while missing API values produce the
  configuration prompt.

## Alternatives Considered

1. Keep Docker default-off and only update the message. This preserves the
   current resource behavior but does not match the desired out-of-box Docker
   experience.
2. Always run the forwarder with no opt-out. This is simpler but removes useful
   operator control and is unnecessary.
3. Enable by default while retaining an explicit opt-out. This is the selected
   approach because it makes the integrated restart button available by
   default without making the forwarder mandatory.
