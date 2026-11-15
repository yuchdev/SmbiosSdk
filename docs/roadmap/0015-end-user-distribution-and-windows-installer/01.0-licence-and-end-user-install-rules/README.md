# Story 01.0 - Licence, and End-User `install()` Rules for the CLI and GUI

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** nothing outside this milestone - this story touches no CI and no other milestone's files, so it can start immediately

## Scope

Clear the two prerequisites [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)
D7 names as blocking and this milestone can fix itself:

1. **An MIT `LICENSE` at the repository root** with an SPDX identifier
   (D7.1). The repository has **no licence file of any kind** today, and
   every row of ADR 0006's channel matrix requires one as package
   metadata: Chocolatey's `licenseUrl` and `VERIFICATION.txt`, WinGet's
   `License` field, the MSI's displayed licence, `brew audit`, and an AUR
   `PKGBUILD`'s `license=()` array. MIT is the maintainer's ruling
   (2026-11-15), recorded in [status.md](../status.md).
2. **`install()` rules for `smbios` and `smbios_gui`** (D7.3), which have
   none - they land in build-tree `smbios/bin/` and `smbios_gui/bin/`.
   Because ADR 0005 D4 makes the already-tested `cmake --install` tree the
   `package` stage's only input, **the CLI and the GUI cannot be packaged
   by any channel at all** until this lands. This story establishes the
   milestone's contract C1 install layout.

The third D7 prerequisite - the root `project(... VERSION)` single version
source - is **milestone 0012 Story 04.0 Task 01's**, not this story's. It
is not duplicated here.

## Tasks

| #  | Document                                                        | Status         | Blocks |
|----|-----------------------------------------------------------------|----------------|--------|
| 01 | [MIT licence file and SPDX identification](01-mit-licence-and-spdx.md) | ⬜ Not started | 02, 03 |
| 02 | [End-user install rules for the CLI and GUI](02-cli-and-gui-install-rules.md) | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                            | ⬜ Not started | -      |

## Key constraints

- **The licence applies to this repository's own code only.** The
  GPL-2.0-or-later `dmidecode` sources under `c-versions/` are
  reference-only material - git-ignored, never compiled, linked or
  `#include`d, enforced by milestone 0008's build-isolation guard. Task 01
  must not add any licence text, notice or attribution that implies those
  sources are distributed, and must not weaken that guard.
- **No `install()` rule may install a setuid or setcap binary**
  (ADR 0006 D6/DD3). `libsmbios` reads raw firmware memory; the correct
  answer for Linux is that the user runs `sudo smbios`. Task 03 asserts
  the absence of those bits on every installed file.
- **`smbios_gui`'s install rule is guarded by `SMBIOS_BUILD_GUI`**
  (contract C1). The GUI is opt-in and OFF by default, so a non-GUI build
  must still produce a complete, installable tree - one simply without
  `bin/smbios_gui`. Guarding this wrong turns every default build's
  `cmake --install` into a configure- or install-time error.
- **`GNUInstallDirs`, not hand-written paths.** Contract C1's layout is
  expressed with `${CMAKE_INSTALL_BINDIR}` and friends so the same rules
  produce a correct tree under a vcpkg prefix, a Homebrew cellar, an AUR
  `pkgdir`, and the MSI's staging prefix without per-channel patching.
- **No change to `smbios_c/CMakeLists.txt`'s existing `install()` rules
  or `smbios_c.pc.in`** (milestone 0007's work), and no overlap with
  milestone 0012 Story 01.0's `libsmbios` library export. This story is
  additive and covers the two **executables** plus the licence only.
- **No new CMake minimum version.** The root `CMakeLists.txt` pins
  `cmake_minimum_required(VERSION 3.13)`; `GNUInstallDirs` and
  `install(TARGETS ... RUNTIME DESTINATION ...)` are available well below
  that floor and must stay the mechanism used.

## Files or directories created or modified by this story

- `LICENSE` (new) - MIT, repository root, the filename every registry and
  forge looks for.
- `README.md` - a licence section naming MIT and the SPDX identifier.
- `smbios/src/CMakeLists.txt` - first `install()` rule for the `smbios`
  CLI target.
- `smbios_gui/CMakeLists.txt` - first `install()` rule for the
  `smbios_gui` target, guarded by `SMBIOS_BUILD_GUI`.
- Root `CMakeLists.txt` - `include(GNUInstallDirs)` and the
  `share/doc/libbios/LICENSE` install rule (contract C1's last row).
- `docs/cli-usage.md` - a short "Installing" pointer, so the CLI's own
  documentation names the install layout rather than only the build tree.
- **No change** to `smbios_c/`, to any `libsmbios*` `CMakeLists.txt`, or
  to `.github/workflows/`.
