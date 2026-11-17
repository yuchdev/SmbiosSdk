# Milestone 0015 - End-User Distribution and the Windows Installer - Status

Tracks progress against [plan.md](plan.md). Updated as each story lands.

## Current status

| Story | Name                                                        | Status         | Tests |
|-------|-------------------------------------------------------------|----------------|-------|
| 01.0  | Licence, and end-user `install()` rules for the CLI and GUI  | ⬜ Not started | - |
| 02.0  | Windows WiX MSI packaging leg - blocked on 0012 Stories 01.0/04.0 and 0014 | ⬜ Not started | - |
| 03.0  | Chocolatey and WinGet publish destinations - blocked on Story 02.0 | ⬜ Not started | - |
| 04.0  | Homebrew tap and AUR publish destinations - blocked on 0012 Story 04.0 Task 01 and 0014 | ⬜ Not started | - |

**Legend:** ✅ Complete · 🔶 In progress / partial · ⬜ Not started

## Notes & decisions

- 2026-11-15 Milestone created from [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)
  (Proposed), which is itself the "own future ADR" that
  [ADR 0004](/docs/adr/0004-package-manager-distribution.md)'s
  Alternative 4 deferred the end-user axis to. Scope is ADR 0006's
  **Wave 1 only**: Chocolatey, WinGet, a Homebrew tap and AUR, plus the
  WiX MSI they wrap. Wave 2/3 and the rejected sandboxed channels
  (Snap, Flatpak, MSIX) are Non-goals.
- 2026-11-15 **Licence decided: MIT** (maintainer ruling). This resolves
  ADR 0006 open question 1 and milestone 0012's open question H3, and
  unblocks every row of ADR 0006's channel matrix - MIT is OSI-approved,
  so even the Wave 3 rows (Homebrew core, Debian/Fedora official) stay
  reachable. ADR 0006 D7.1 is updated accordingly; the file itself is
  Story 01.0 Task 01. The GPL-2.0-or-later `dmidecode` sources under
  `c-versions/` are reference-only (never compiled, linked or
  `#include`d, per `CLAUDE.md` and milestone 0008's isolation guard) and
  do not constrain this choice - but that guard must stay intact and
  demonstrably so if a distro licence audit ever happens (Wave 3).
- 2026-11-15 **New prerequisite recorded, not previously tracked by any
  milestone:** `smbios` (CLI) and `smbios_gui` have **no `install()` rule
  anywhere in the repository** - they land in build-tree `smbios/bin/`
  and `smbios_gui/bin/`. Since ADR 0005 D4 makes the tested
  `cmake --install` tree the `package` stage's only input, the CLI and the
  GUI **cannot be packaged by any channel today**. Milestone 0012 Story
  01.0 covers `libsmbios`'s library export only. This is Story 01.0 Task
  02 here, and it also unblocks milestone 0012 Story 03.0's claim to ship
  a complete tarball.
- 2026-11-15 **No new workflow file and no new entry-workflow job.** Per
  ADR 0006 D5, the MSI is a `package`-stage leg and the four channels are
  ADR 0005 D9 destination plug-ins, so ADR 0005 D8's workflow family and
  0014's policy allowlist P9 are untouched by this milestone.

## Still open for the maintainer

These are ADR 0006's own open questions, carried here because they gate
work in this milestone rather than in the ADR:

- **Code-signing budget** (ADR 0006 open question 2, DD5). Without an
  Authenticode certificate, Story 02.0's MSI ships with SmartScreen
  warnings - a named, accepted consequence, but the maintainer should
  confirm it rather than discover it. An Apple Developer ID is not needed
  for Wave 1 (the Homebrew tap builds from source).
- **Shipped build configuration** (ADR 0006 open question 4; milestone
  0012's own note). 0014 C06 tests `-C Debug` today. An MSI shipping
  Debug binaries with a debug-CRT dependency would be a user-visible
  defect, so the packaged configuration must be named before Story 02.0
  Task 02 lands.
- **Windows static triplet** (ADR 0006 open question 6; milestone 0012's
  M4). `/MT` vs `/MD` for the MSI's `SDK_Static` feature determines
  whether a consumer's own CRT choice can link it.
- **Registry/tap hosting** (ADR 0006 open question 3; milestone 0012's
  H2). The Homebrew tap repository, the AUR package name and 0012's vcpkg
  registry are the same hosting question and should get one answer.
- **Is the GUI in Wave 1's MSI?** (ADR 0006 open question 5.) The GUI is
  the artifact most exposed to SmartScreen; a CLI-only first MSI is a
  defensible alternative reading of ADR 0006 D3. Story 02.0's feature
  tree assumes the GUI is included.
- 2026-11-16 **Amended by [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
  D5 (architecture coverage).** Windows is now two architectures, so
  **Story 02.0 produces two MSIs** (x64 and arm64, same `UpgradeCode`
  family) and **Story 03.0's Chocolatey install script becomes
  arch-aware**, carrying both checksums from `manifest.json`; the WinGet
  manifest gains a second installer entry keyed by `Architecture` rather
  than becoming a second manifest. This is an amendment, not a rewrite -
  contract C2's feature tree and upgrade-identity rules are unchanged per
  MSI. ADR 0007 open question 5 asks whether arm64 ships in Wave 1 or
  waits; answer it before Story 02.0 starts. ADR 0007 D6 and D8 also
  close two of this milestone's inherited open questions (registry
  hosting, and Windows code-signing budget).
