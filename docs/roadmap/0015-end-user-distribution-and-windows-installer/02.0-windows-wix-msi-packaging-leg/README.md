# Story 02.0 - Windows WiX MSI Packaging Leg

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Story 01.0 (the CLI/GUI install rules and the licence the MSI UI displays); milestone 0012 Story 01.0 (`libsmbios` install rules + `libsmbiosConfig.cmake`, for the `SDK_Static` feature); milestone 0012 Story 04.0 Task 01 (the root `project(... VERSION)` the MSI `Version` derives from); milestone 0014 (`stage.py`, the `package` stage, the report contract)

## Scope

Build the Windows installer of [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)
D3: a WiX MSI with the four-feature tree of this milestone's contract C2
(`CLI`, `GUI`, `SDK_Shared`, `SDK_Static`), built **from the already-tested
`cmake --install` tree** as a new `windows-msi` leg of the existing
`package` stage, landing in the same `manifest.json` as the tarballs so
`verify` and every publish destination treat it like any other artifact.

This story produces the MSI. **Publishing it is Story 03.0's job** - the
MSI is uploaded to the GitHub Release by milestone 0012's existing
`github-release` destination, and Chocolatey and WinGet consume it from
there.

## Tasks

| #  | Document                                                                 | Status         | Blocks |
|----|--------------------------------------------------------------------------|----------------|--------|
| 01 | [WiX sources, feature tree and upgrade identity](01-wix-sources-and-feature-tree.md) | ⬜ Not started | 02     |
| 02 | [The `windows-msi` package-stage leg and manifest entry](02-package-stage-leg-and-manifest.md) | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                                     | ⬜ Not started | -      |

## Key constraints

- **Built from the install tree, never by CPack** (ADR 0006 D3). ADR 0005
  D4 fixes the `package` stage's only input as the build stage's
  already-tested `cmake --install` tree and forbids rebuilding; CPack runs
  from the build tree, so it would either bypass or re-derive that tree
  and would move packaging logic into CMake, against ADR 0005 DD5. No task
  here adds `include(CPack)`.
- **`UpgradeCode` is permanent and component GUIDs are stable**
  (contract C2). A changed `UpgradeCode` turns every future upgrade into a
  side-by-side second installation; auto-generated component GUIDs make a
  major upgrade accumulate files instead of replacing them. Both are
  recorded with an explicit "never change this" comment.
- **`Version` comes from ADR 0005 D5's single source** and is injected at
  package time. The `.wxs` carries a placeholder, never a literal, and
  `check_versions.py` gains the MSI as a version site.
- **The `GUI` feature requires a GUI-enabled packaging configuration.**
  `SMBIOS_BUILD_GUI` is OFF by default, so Task 02 must state explicitly
  which configuration the `windows-msi` leg packages rather than assuming
  `bin/smbios_gui.exe` is present - and must fail loudly if a feature's
  payload is missing from the install tree, never silently ship a feature
  with no files.
- **The `SDK_Static` feature must ship `libsmbiosConfig.cmake`, not just
  the `.lib`.** Shipping the raw archive without the whole-archive-safe
  exported target hands every consumer the silent
  `SMBiosEntryFactory::create() == nullptr` footgun ADR 0004's DD1 exists
  to prevent. If milestone 0012 Story 01.0 has not landed, this feature is
  omitted entirely rather than shipped incomplete.
- **No test target, and no `smbios_gui_autoexit`, in any feature.** The
  autoexit twin is compiled with `SMBIOS_GUI_TEST_HOOKS`, which the
  shipped GUI must never contain.
- **Per-machine install; no elevation needed to run.** The installed
  programs need no admin (Windows' `GetSystemFirmwareTable()` does not),
  so the MSI must not request elevation for the applications themselves,
  nor install any service, driver or scheduled task.
- **Unsigned, knowingly.** ADR 0006 open question 2 leaves code signing
  unbudgeted, so the MSI triggers SmartScreen warnings. The signing step
  must be a clearly-marked, currently-disabled hook rather than an
  afterthought to retrofit.

## Files or directories created or modified by this story

- `packaging/windows/libbios.wxs` (new) - product, feature tree,
  directory layout, `PATH` environment entry, Start Menu shortcut.
- `packaging/windows/components.wxs` (new) - the file components with
  their stable, explicitly-authored GUIDs.
- `packaging/windows/identity.md` (new) - the permanent `UpgradeCode` and
  the component-GUID policy, with the "never change" rationale.
- `packaging/windows/license.rtf` (new) - the MIT text in the RTF form
  WiX's UI requires, generated from `LICENSE` rather than retyped.
- `scripts/ci/package_windows_msi.py` (new) - stdlib-only driver: reads
  the install tree and the version, invokes WiX through `ci_step`, emits
  the MSI and its `.sha256`.
- `scripts/ci/legs.json`, `scripts/ci/stage.py` - the new `windows-msi`
  packaging leg.
- `scripts/ci/pins.*` (milestone 0014's pins file) - the WiX toolset
  version, so it is a declared tool requirement printed in the RC4 header
  and a TeamCity agent requirement on migration.
- `scripts/ci/check_versions.py` - the MSI version site.
- `docs/dev/windows-installer.md` (new) - what the MSI contains, how to
  build it locally, and the upgrade-identity rules.
- **No change** to any `CMakeLists.txt` (this story consumes Story 01.0's
  and milestone 0012's install rules; it adds none) and **no new workflow
  file**.
