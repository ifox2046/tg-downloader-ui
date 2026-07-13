# Full x86_64 IPK and Contributor History Rewrite Design

## Goal

Publish one architecture-specific OpenWrt/iStoreOS package that includes the application and `tdl`, while rewriting the Git history so GitHub attributes project commits only to the owner's `ifox2046` account and no AI co-author trailers remain.

## Scope

This change has two linked deliverables:

1. `tg-downloader-ui-full_0.1.0_x86_64.ipk`, installable without separately downloading `tdl`.
2. A rewritten `main` and `v0.1.0` history whose author identity is consistently `ifox2046 <2927211+ifox2046@users.noreply.github.com>` and whose commit messages contain no AI `Co-authored-by` trailers.

The existing generic `tg-downloader-ui_0.1.0_all.ipk`, iStore metadata package, Docker images, and GitHub Release remain available.

## Full IPK Architecture

The full package will be a new package variant rather than replacing the generic package:

- Package: `tg-downloader-ui-full`
- Version: `0.1.0`
- Architecture: `x86_64`
- Output: `tg-downloader-ui-full_0.1.0_x86_64.ipk`
- Conflicts: `tg-downloader-ui`
- Provides: `tg-downloader-ui`

The package will reuse the existing application data and control scripts, then add:

- `/usr/bin/tdl`, sourced from the upstream `tdl 0.20.3` Linux 64-bit release.
- A bundled copy of the upstream AGPL-3.0 license and source/version notice.
- Package metadata that clearly states this is the x86_64 complete variant.

The builder will download the upstream archive through the existing injectable fetch pattern, verify the pinned SHA-256 checksum before extraction, locate the `tdl` executable, and write it with mode `0755`. No new Python dependency or external build tool will be added.

## Installation Semantics

Installing the full package removes the separate `tdl` installation step. It does not bypass application setup or Telegram authentication:

- The operator must still complete first-run administrator setup.
- The operator must still authenticate their own Telegram account with `tdl login` or the Web UI QR flow.
- Existing configuration, session, and download directories retain their current behavior.

The full and generic application packages must not be installed together because they own the same runtime files. The package conflict declaration makes this explicit.

## Testing and Verification

TDD coverage will verify:

- The full package filename and `Architecture: x86_64` control field.
- `Conflicts: tg-downloader-ui` and `Provides: tg-downloader-ui` metadata.
- The upstream archive checksum is mandatory and rejected on mismatch.
- `/usr/bin/tdl` exists in `data.tar.gz`, contains the fetched payload, and has mode `0755`.
- The upstream license/source notice is present.
- The existing generic and iStore metadata packages remain unchanged.

After local tests pass, the package will be installed on the existing x86_64 iStoreOS test VM. Verification requires:

- `opkg install` succeeds on a clean package state.
- `tdl version` reports `0.20.3`.
- `tg-downloader-ui --check` succeeds.
- The procd service starts and its Web UI responds.

Only after those checks pass will the package be attached to the existing `v0.1.0` GitHub Release.

## Licensing

The project remains MIT licensed. The bundled `tdl` executable remains AGPL-3.0. The full package will include the upstream license, version, source tag URL, and an explicit statement that the binary is unmodified. Release notes and README documentation will distinguish the two licenses.

## Git History Rewrite

Before rewriting, create recoverable local backup references and a Git bundle containing all refs. The rewrite will then process every commit reachable from `main`:

- Set author and committer name to `ifox2046`.
- Set author and committer email to `2927211+ifox2046@users.noreply.github.com`.
- Remove `Co-authored-by` trailers for Claude, Sisyphus, ChatGPT, and other AI agents.
- Preserve the remaining subject and body text.

After rewriting:

- Verify every commit author/committer identity.
- Verify no AI co-author trailers remain.
- Run the full test suite on the rewritten tree.
- Force-push `main` using `--force-with-lease`.
- Recreate and force-update the annotated `v0.1.0` tag at the rewritten release commit.
- Confirm the existing GitHub Release remains published and points to the updated tag.

The Release assets do not need rebuilding solely because commit hashes changed. The newly built full IPK will be uploaded after its own verification.

## Failure Recovery

If rewriting, testing, pushing, package installation, or Release updating fails:

- Do not delete backup refs or the Git bundle.
- Restore local refs from the backup.
- Use the old remote commit recorded before the rewrite as the `--force-with-lease` safety value.
- Do not alter the published Release assets until the replacement package passes verification.

## Success Criteria

- GitHub `main` and `v0.1.0` use the rewritten owner-only history.
- Git history contains no AI `Co-authored-by` trailers and all commits use the owner's verified noreply identity.
- Local tests pass after the rewrite and package changes.
- `tg-downloader-ui-full_0.1.0_x86_64.ipk` installs on the x86_64 iStoreOS test VM and includes a working `tdl 0.20.3`.
- The full package is attached to the existing public `v0.1.0` Release with accurate licensing notes.
