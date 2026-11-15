# 01 - Chocolatey package sources and destination module

**Status:** ⬜ Not started
**Depends on:** Story 02.0 (the MSI); milestone 0012 Story 04.0 Task 05 (the destination plug-in framework)

## Objective
Package Story 02.0's MSI for Chocolatey and publish it as a contract C3 destination module.

## Files
- **Create** `packaging/chocolatey/libbios.nuspec.in`: `id` `libbios`, version and `licenseUrl` substituted from the single version source and the repository's MIT `LICENSE`, plus `projectUrl`, `packageSourceUrl`, `tags`, `summary`, `description`, and `requireLicenseAcceptance` set false (MIT needs no acceptance click).
- **Create** `packaging/chocolatey/tools/chocolateyInstall.ps1`: installs the MSI silently, passing the feature selection Chocolatey package parameters allow (so `choco install libbios --params "/Features:CLI"` is possible), and **verifies the MSI checksum against the value substituted from `manifest.json` before executing it**.
- **Create** `packaging/chocolatey/tools/chocolateyUninstall.ps1`: the matching silent uninstall by product code.
- **Create** `packaging/chocolatey/tools/VERIFICATION.txt.in`: names the upstream GitHub Release URL and the manifest's sha256, as Chocolatey's embedded-binary policy requires.
- **Create** `scripts/ci/destinations/chocolatey.py`: `NAME = "chocolatey"`; `REQUIRED_ENV` = `CI_PUBLISH_CHOCO_API_KEY` (+ the neutral version/artifact names); `validate` checks the MSI entry exists in the manifest, the templates substitute cleanly, and the API key is present without printing it; `publish` builds the `.nupkg` and pushes it; `check_published` queries the published package version and compares its embedded checksum with the manifest.
- **Modify** `scripts/ci/publish_destinations.json`: `{ "name": "chocolatey", "enabled": true, "tier": "release", "permission_class": "token-only", "environment": "release" }`.
- **Modify** `scripts/ci/check_versions.py`: the `nuspec` version as a version site.

## Constraints
- **The checksum comes from `manifest.json`**, substituted into both `VERIFICATION.txt` and the install script. Never recompute it in the destination, and never let the install script run an unverified MSI.
- **Embedding vs downloading:** choose one explicitly and record the reason in the task's implementation note. Embedding keeps the package self-contained but is subject to Chocolatey's package size limits; downloading keeps the package small but makes the install depend on the Release URL staying reachable. Either way the checksum gate above is mandatory.
- **A pending moderation is not a failure.** `publish` succeeds once the push is accepted; `check_published` must distinguish "awaiting moderation" from "published with a wrong digest" and only fail on the latter.
- **The API key reaches the process only through the `release` environment** and is passed to the child process by name (ADR 0005 D6; milestone 0014 Story 02.0 Task 04). It is never echoed, never in a log line, never in an error message.
- **Feature parameters must degrade safely:** an unrecognized `--params` value installs the default feature set rather than failing or installing nothing.
- Stdlib only in the destination module. The `.nupkg` build and push go through `ci_step` as argv, never a shell string.

## Success criteria
- [ ] `stage.py publish --destination chocolatey --dry-run` lists the exact `.nupkg` it would build and the exact package id/version it would push, with no network access and no secret read.
- [ ] A manifest whose MSI digest disagrees with the substituted `VERIFICATION.txt` value fails `validate`.
- [ ] `choco install libbios` on a clean Windows runner installs the MSI and `smbios --version` resolves; `choco uninstall libbios` removes it.
- [ ] `choco install libbios --params "/Features:CLI"` installs the CLI without the GUI.
- [ ] `check_published` fails on a digest mismatch and passes on a package awaiting moderation.
- [ ] No log line, including every failure path, contains the API key.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
