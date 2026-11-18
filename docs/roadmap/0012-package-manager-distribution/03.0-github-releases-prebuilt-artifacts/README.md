# Story 03.0 - GitHub Releases Prebuilt Artifacts (Windows/Linux/macOS)

**Parent milestone:** [plan.md](../plan.md)
**Status:** 🔶 In progress (Tasks 01-02 complete; Task 03 partial - success criterion 1 on all
three OSes is wired by Story 04.0 Task 04's `verify` stage, 2026-11-18, and closes on its first
green CI run)
**Depends on:** Story 01.0 (`libsmbios` must be `install()`-able for the
tarball to be complete - see `plan.md`'s Dependency graph note); milestone
0014 Stories 01.0-03.0 (pipeline foundation, [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md))

> **Amended 2026-11-12 (ADR 0005).** This story is the pipeline's
> **`package` stage**. It is not a standalone release workflow. Its input is
> the build stage's tested `cmake --install` tree at
> `$CI_ARTIFACT_DIR/install/<leg>/` (0014 SC2). Its output is the D4
> `dist/` layout: `libbios-<version>-<leg>.{tar.gz,zip}`, `.sha256` files and
> `manifest.json`. Uploading to a GitHub Release is the `publish` stage
> (Story 04.0). Workflow conventions (pins, permissions, step shape,
> reporting) are defined by ADR 0005 and 0014, not here. **This story must
> also name the shipped build configuration**, which the build stage's
> packaging legs then build and test (0014 SC2).

## Scope

Implement ADR 0004's adopted **parallel baseline** (the ADR's own
Alternative 3, "GitHub Releases of prebuilt tarballs/zips + `cmake
--install`"): a CI job that, on a tagged release, builds and installs
this project on all three platforms and attaches a per-platform
tarball/zip to the corresponding GitHub Release - serving any consumer
unwilling to add a vcpkg registry at all.

## Tasks

| #  | Document                                                             | Status         | Blocks |
|----|----------------------------------------------------------------------|----------------|--------|
| 01 | [Package stage: archive the tested install tree](01-release-workflow-and-packaging.md) | ✅ Complete (validated on CI run 44074901424) | 02     |
| 02 | [Artifact naming, checksums, and versioning](02-artifact-naming-checksums-and-versioning.md) | ✅ Complete | 03     |
| 03 | [Tests](03-tests.md)                                                  | 🔶 Partial (verifier + 57 tests; H1 fixed, arch guard added; all five checks pass on macOS x64 with the literal `pkg-config` command, layout/digest/`pkg-config` on all three CI artifacts; SC1 wired on all three OSes by Story 04.0 Task 04 (`verify`, `build=True`, plus an export-allowlist check 6), closes on the first green CI `verify` run) | -      |

## Key constraints

- **Reuses `cmake --install` output only - no second packaging
  implementation.** Per ADR 0004's own framing of this option, the
  release job is a thin wrapper around the exact same `cmake --install`
  path Story 01.0 (`libsmbios`) and milestone 0007 (`smbios_c`) already
  produce and verify; it does not hand-assemble a tarball from
  individually-copied files.
- **Not a substitute for the vcpkg port (Story 02.0).** Per the ADR's own
  Consequences, this baseline gives no dependency-graph integration (a
  consumer's build system does not learn about `smbios_c`'s own
  dependencies automatically) - it exists alongside Story 02.0, not
  instead of it.
- **No semantic-versioning policy is introduced.** Per the milestone's
  Key constraints, this task needs just enough of a naming/tagging
  convention to produce a reproducible, collision-free filename per
  release - not a full semver policy for either `libsmbios` or
  `smbios_c`.
- **The per-platform artifact matrix (Windows/Linux/macOS x this
  project's own compiler) is this project's own CI burden**, per the
  ADR's own named Negative consequence. Which events run it is now ADR
  0005 D7's tier table: PRs to `master`, nightly and tags, never a branch
  push. The archives are made from the tested build, so packaging adds no
  rebuild.

## Files or directories created or modified by this story

- `scripts/ci/stage.py`'s `package` stage, plus `scripts/ci/package_archive.py`
  (deterministic archive, stdlib SHA-256, `manifest.json`) and its tests.
- `.github/workflows/stage-package.yml` (reusable, one `stage.py package`
  step per packaging leg), called from the entry workflow on the tiers
  ADR 0005 D7 lists. **No `release.yml`** (superseded by ADR 0005 D8).
- `scripts/ci/verify_release_artifact.py` (Task 03): clean-room
  verification of one release archive (sidecar + manifest digests,
  extraction, layout and `install_sha256`, `pkg-config` + C consumer,
  `find_package(libsmbios)` consumer, and since Story 04.0 Task 04 the export
  allowlist), called by Story 04.0's `verify` stage; tests in
  `scripts/ci/tests/test_verify_release_artifact.py`.
- Documentation (exact location decided during implementation, e.g. a
  new `docs/dev/release-artifacts.md` or a section in
  `docs/cli-usage.md`) describing the artifact naming convention and how
  to consume a release tarball without any package manager.
