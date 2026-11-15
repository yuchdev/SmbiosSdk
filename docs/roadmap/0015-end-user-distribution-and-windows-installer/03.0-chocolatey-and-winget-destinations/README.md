# Story 03.0 - Chocolatey and WinGet Publish Destinations

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Story 02.0 (both channels ship the MSI it builds); milestone 0012 Story 04.0 Tasks 02-05 (the GitHub Release destination whose public asset URL WinGet references, and the destination plug-in framework itself); milestone 0014 (`stage.py`, `check_versions.py`, the policy gate)

## Scope

The two Windows end-user channels of [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)
D2's Wave 1, as **ADR 0005 D9 destination plug-ins** (contract C3) -
not as workflow jobs:

- **`chocolatey`** - a `nuspec` + install script wrapping Story 02.0's
  MSI, pushed to the Chocolatey community repository.
- **`winget`** - a multi-file YAML manifest pointing at the MSI's public
  GitHub Release URL and digest, submitted as a PR to
  `microsoft/winget-pkgs`.

Both are `release` tier: they run only on a `v*` tag, only after `verify`
passed in the same run, and they refuse any manifest leg whose
`binary_cache` is `restored` (ADR 0005 D6/D9).

**Neither channel rebuilds or re-digests anything.** `manifest.json` is
their only source of truth.

## Tasks

| #  | Document                                                             | Status         | Blocks |
|----|----------------------------------------------------------------------|----------------|--------|
| 01 | [Chocolatey package sources and destination module](01-chocolatey-destination.md) | ⬜ Not started | 03     |
| 02 | [WinGet manifest and destination module](02-winget-destination.md)   | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                                 | ⬜ Not started | -      |

## Key constraints

- **`manifest.json` is the only digest source** (ADR 0005 D4/D9). The
  `nuspec`'s `VERIFICATION.txt` and the WinGet manifest's
  `InstallerSha256` both carry the *manifest's* digest for the MSI, never
  one recomputed at publish time. A destination that recomputes a digest
  can publish a package that disagrees with the release it claims to be.
- **`check_published` is load-bearing, not ceremony.** A Chocolatey
  package that passes moderation while installing a wrong-digest MSI, or a
  WinGet manifest whose `InstallerSha256` drifted from the Release asset,
  is exactly the silent end-user breakage ADR 0006's D1 table names as
  this axis's characteristic failure mode. Both modules must re-read the
  published artifact and compare.
- **Both channels put a third party in the release path, and that is
  accepted.** Chocolatey moderation and `winget-pkgs` validation can lag
  the GitHub Release. The Release is the source of truth; these channels
  are permitted to trail it, and a pending moderation must not fail the
  pipeline after a successful submission.
- **Credentials only through the `release` GitHub environment**, never
  from the repository or a PR (ADR 0005 D6). `chocolatey` is
  `token-only` (an API key); `winget` is `repo-write`-shaped (it pushes a
  branch to a fork and opens a PR). No new permission class is introduced,
  so no workflow edit is needed (ADR 0005 D9).
- **`validate` prints nothing secret** and builds every remote location
  from validated parts only - the same confinement discipline ADR 0005 D9
  spells out for `s3_staging`.
- **Chocolatey's embedded-binary rules must be satisfied honestly.** If
  the `nuspec` embeds the MSI, it requires a `VERIFICATION.txt` naming the
  upstream URL and the checksum; if it downloads at install time, the
  install script must verify the checksum before executing. Task 01
  chooses one and states why - it must not do neither.
- **No new workflow file and no new entry-workflow job** (ADR 0006 D5).

## Files or directories created or modified by this story

- `packaging/chocolatey/libbios.nuspec.in` (new), `packaging/chocolatey/tools/chocolateyInstall.ps1`
  (new), `packaging/chocolatey/tools/chocolateyUninstall.ps1` (new),
  `packaging/chocolatey/tools/VERIFICATION.txt.in` (new).
- `packaging/winget/` (new) - the three-part manifest templates
  (version, installer, default-locale).
- `scripts/ci/destinations/chocolatey.py` (new),
  `scripts/ci/destinations/winget.py` (new) - contract C3 modules.
- `scripts/ci/publish_destinations.json` - two new rows.
- `scripts/ci/check_versions.py` - two new version sites.
- `scripts/ci/tests/` - unit tests for both modules.
- `docs/dev/adding-a-publish-destination.md` (created by milestone 0012
  Story 04.0 Task 05) - these two as worked examples.
- `docs/cli-usage.md` - an "Installing on Windows" section naming
  `choco install` and `winget install`.
