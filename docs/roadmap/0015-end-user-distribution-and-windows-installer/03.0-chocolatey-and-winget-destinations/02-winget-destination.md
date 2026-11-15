# 02 - WinGet manifest and destination module

**Status:** ⬜ Not started
**Depends on:** Story 02.0 (the MSI); milestone 0012 Story 04.0 Tasks 02-05 (the GitHub Release destination publishing the MSI at a stable public URL, and the plug-in framework)

## Objective
Submit a WinGet manifest referencing the MSI's public GitHub Release URL and digest, as a contract C3 destination module. This is the cheapest Wave 1 channel: it hosts nothing, referencing the Release asset milestone 0012 already publishes.

## Files
- **Create** `packaging/winget/` with the three manifest templates WinGet's schema requires - version, installer, and default-locale - using the package identifier convention `<Publisher>.libbios`, `InstallerType: wix`, the `Scope: machine` the MSI implements, and `License: MIT` with `LicenseUrl` pointing at the repository `LICENSE`.
- **Create** `scripts/ci/destinations/winget.py`: `NAME = "winget"`; `REQUIRED_ENV` = the fork-push token plus the neutral version/artifact names; `validate` asserts the manifest's `InstallerUrl` matches the Release asset URL for this exact version and its `InstallerSha256` equals the manifest's digest, and that the schema version is the pinned one; `publish` renders the manifests, pushes a branch to the project's `winget-pkgs` fork and opens the PR; `check_published` re-reads the merged manifest and compares `InstallerSha256` with `manifest.json`.
- **Modify** `scripts/ci/publish_destinations.json`: `{ "name": "winget", "enabled": true, "tier": "release", "permission_class": "repo-write", "environment": "release" }`.
- **Modify** `scripts/ci/check_versions.py`: the WinGet `PackageVersion` as a version site.
- **Modify** `docs/cli-usage.md`: `winget install <Publisher>.libbios` in the "Installing on Windows" section.

## Constraints
- **`InstallerSha256` comes from `manifest.json`**, never recomputed. A WinGet manifest whose digest drifts from the Release asset makes every `winget install` fail a hash check - a user-visible breakage that no amount of local testing catches, because the local MSI matches.
- **`publish` must run after the GitHub Release asset is actually public**, since the manifest references its URL. This is an ordering constraint on the publish stage's destination sequence, not something `winget.py` can fix itself: `validate` must therefore fetch the asset URL and fail if it is not yet reachable.
- **A PR opened with `GITHUB_TOKEN` does not trigger workflows**, so any validation this project controls runs *inside* the publish run before the PR is opened - the same rule ADR 0005 D6 states for the vcpkg registry PR.
- **Merging is a human action.** `publish` succeeds when the PR is open; `check_published` distinguishes "PR open, not yet merged" from "merged with a wrong digest" and fails only on the latter.
- **The WinGet manifest schema version is pinned** and recorded with the other tool pins, so a schema change is a deliberate, reviewed update rather than a surprise validation failure on release day.
- The fork-push credential comes only through the `release` environment, passed by name, never logged.

## Success criteria
- [ ] `stage.py publish --destination winget --dry-run` prints the exact manifest files, the identifier, the version, the installer URL and the digest it would submit, with no network write and no secret read.
- [ ] `validate` fails when the installer URL is unreachable, when the digest differs from `manifest.json`, or when the schema version differs from the pin.
- [ ] `winget validate` (the official validator) passes on the rendered manifests.
- [ ] `winget install` from the merged manifest installs on a clean Windows runner and `smbios --version` resolves.
- [ ] `check_published` passes on an open-but-unmerged PR and fails on a merged manifest whose digest disagrees with the manifest.
- [ ] No log line contains the token.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
