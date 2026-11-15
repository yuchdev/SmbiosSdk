# Milestone 0012 - Package Manager Distribution for `libsmbios` and `smbios_c` - Status

Tracks progress against [plan.md](plan.md). Updated as each story lands.

## Current status

| Story | Name                                                        | Status         | Tests |
|-------|------------------------------------------------------------------|----------------|-------|
| 01.0  | `libsmbios` install rules and whole-archive-safe package export | 🔶 In progress (3/3 tasks; story-close gates pending) | - |
| 02.0  | Self-hosted vcpkg overlay/registry port for `smbios_c`           | ⬜ Not started | - |
| 03.0  | GitHub Releases prebuilt artifacts - pipeline `package` stage (ADR 0005); blocked on 0014 | ⬜ Not started | - |
| 04.0  | Release and publish automation - pipeline `verify`/`publish` stages (ADR 0005); blocked on 0014 | ⬜ Not started | - |

**Legend:** ✅ Complete · 🔶 In progress / partial · ⬜ Not started

## Notes & decisions

- 2026-11-12 Story 04.0 added after the [packaging-automation review](/docs/reviews/milestone-0012-packaging-automation-2026-11-12.md) (H1-H3, M1-M6).
- 2026-11-12 **Amended by [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md)** (Proposed). This follows the maintainer's ruling: "CI, packaging and publishing should be designed as unified subsystem."

  **What changed in this milestone:**
  - **Story 03.0** is now the pipeline's `package` stage. It archives the build stage's tested `cmake --install` tree and does not rebuild. The standalone `release.yml` is withdrawn, because its sketch broke 0014 policy rules P3/P6/P7/P13 and shipped an untested build.
  - **Story 04.0** is now the `verify` and `publish` stages. Its duplicated workflow conventions (permissions, pinning, report contract) were removed in favour of pointers to ADR 0005 and milestone 0014.
  - **Version source decided (D5):** the root CMake `project()` VERSION, checked against the port, the registry and the tag by 0014's `check_versions.py`. This reverses Story 03.0 Task 02's "drift is acceptable".
  - **Artifact name** is now `libbios-<version>-<os>-<arch>.<ext>`, with a `manifest.json` of digests.

  **Dependency fixed:** this milestone's workflow work (Stories 03.0 and 04.0) depends on milestone 0014 Stories 01.0-03.0 plus Story 04.0 Task 02. Stories 01.0 and 02.0 do not.

  **New requirement:** Story 03.0 must name the shipped build configuration, because 0014 C06 tests `-C Debug` today.

  **Still open for the maintainer:** licence (H3), `.pc` relocatability (H1), CLI in the tarball (I2), registry hosting (H2), Windows static triplet (M4).
- 2026-11-15 The end-user distribution axis this milestone's Non-goals defer (Homebrew, apt, distro packaging of the `smbios` CLI - ADR 0004's Alternative 4) is now decided by [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md) (Proposed), which adds a Windows WiX MSI and a wave-ordered channel matrix. It changes nothing in this milestone's scope, but it **depends** on Story 01.0 (install rules + whole-archive-safe package export) and Story 04.0 Task 01 (the D5 version source), and it escalates open question **H3 (licence) from deferred to blocking** - no public registry row in its matrix can ship without a `LICENSE` file. It also records a prerequisite neither milestone covers: `smbios` (CLI) and `smbios_gui` have no `install()` rules at all, so the `package` stage cannot archive them.

- **2026-11-15 Story 01.0 Task 02 (as built, macOS-verified).** `libsmbiosConfigVersion.cmake` uses `COMPATIBILITY SameMajorVersion` on `LIBSMBIOS_VERSION` (1.0.0, separate from `SMBIOS_C_VERSION`; both are rows in `scripts/ci/version_sites.json`): the C++ surface may grow within a major version, and no ABI/semver policy exists yet (plan Key constraints), so a major bump is the only boundary a consumer should be protected from. Revisit when a versioning policy is adopted. Measured: CMake does **not** rewrite a bare `$<TARGET_FILE:libsmbios>` on export, so the whole-archive link option is `$<INSTALL_INTERFACE:...$<TARGET_FILE:libsmbios::libsmbios>>` (installed form only; in-repo consumers keep their own whole-archive). macOS frameworks export as `-Wl,-framework,X` (a spaced `-framework X` is split into `-framework -lX`). The package config calls `find_dependency(Boost COMPONENTS iostreams)`. Windows (`/WHOLEARCHIVE:` on an imported target path) and Linux (`--whole-archive`) forms are unverified until Task 03's CI consumer gate.

- **2026-11-15 Story 01.0 Task 03 (as built).** The clean-room consumer is `tests/packaging/libsmbios_consumer/` (no whole-archive line of its own), driven by `scripts/ci/consumer_smoke.py` and wired as the `consumer` step of the `test` stage in the `build` job (all three OSes; `stage.py test --only consumer`), not as a new workflow or `ci.yml` job (ADR 0005 / plan Key constraints). It prints `ok: consumer smoke: formatters=<n>, type17=<name>, fields=<n>`. **One-time negative check (macOS):** with `INTERFACE_LINK_OPTIONS` deleted from the installed `libsmbiosTargets.cmake`, the same fixture printed `formatters=0` and exited 1 (`factory`/formatter registries empty), so it does catch the DD1 regression. The fixture builds a synthetic type 17 structure through `SMBiosEntryFactory` (a type it never names), so CI needs no hardware or privilege. Windows (`/WHOLEARCHIVE:` on an imported target path, VS multi-config `--config Debug`) and Linux (`--whole-archive`) are exercised for the first time by the next PR run.
