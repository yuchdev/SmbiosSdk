# 03 - Registry update in the publish stage

**Status:** ⬜ Not started

## Objective

Automate the circular steps of review finding H2 as the publish stage's `registry-pr` job ([ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) D6).

## Design

This is `python3 scripts/ci/stage.py publish --only registry-pr`, run after `release-promote`. The job:

- checks out the registry branch/repo;
- sets the port `REF` to the released tag's commit;
- runs `vcpkg x-add-version smbios-c`, which computes `git-tree` and updates `versions/baseline.json` and `versions/s-/smbios-c.json`;
- **validates the result in the same run**, with the Story 02.0 Task 03 consumer test against a `filesystem` registry pointing at that checkout;
- only then opens a PR through `scripts/ci/open_registry_pr.py` (REST, `CI_PUBLISH_TOKEN`).

The validation must run before the PR because a PR opened with `GITHUB_TOKEN` triggers no workflow, so no PR-side check would ever run (ADR 0005 D6).

Two items carry over from the review and stay in this task:

- Registry hosting (this repo's `master` vs a dedicated registry repo) is an open maintainer decision (ADR 0005 Open questions 4), recorded before implementation.
- The portfile fixes from review finding M4 (`vcpkg_from_git` arguments, the real repo URL, `vcpkg_check_linkage`), and a decision on M5 (root CMake options for the CLI and tests).

## Success criteria

- [ ] Publishing a scratch release produces a registry PR with a consistent `git-tree`, which passes `vcpkg x-add-version --verbose` without edits.
- [ ] No manual step remains in the "bump baseline" procedure doc. That doc becomes a description of the automation.

## Carried forward from Story 02.0 (2026-11-16, named items, not optional)

Story 02.0 delivered the port, `versions/`, `check_vcpkg_registry.py` and a clean-room
driver, but deliberately not these; this task or Task 04 owns them:

- **Wire `scripts/ci/vcpkg_registry_smoke.py`** as a `verify`-stage step on Linux, macOS and
  Windows (it was verified on macOS only; a cold Boost build exceeds the build job's 8-minute
  test-step budget, so it belongs in `stage-verify.yml` with its own budget).
- **Windows** (`x64-windows`, the static triplet question M4): the `.dll` symbol check is
  skipped today (`symbols=skipped`); use `dumpbin /exports` or `llvm-nm`.
- **`REF` reachability lint**: fail when the portfile `REF` is not an ancestor of `master`
  (needs full history in the lint checkout), since a squash merge orphans it.
- **`git-tree` overwrite lint**: fail when a `(version, port-version)` already on `master`
  changes its `git-tree` (guards `x-add-version --overwrite-version`).
- **Installed-file allowlist**: assert the exact file set under `vcpkg_installed/<triplet>`
  so a new root install rule cannot ship silently (the portfile prunes a fixed list).
