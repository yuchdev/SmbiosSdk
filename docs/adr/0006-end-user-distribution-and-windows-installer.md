# 0006 - End-user distribution channels and the Windows installer

> **Status:** Proposed
>
> **Date:** 2026-11-15
>
> **Supersedes:** _(none)_
>
> **Superseded by:** _(none)_

## Context

[ADR 0004](0004-package-manager-distribution.md) decides how this project's
**libraries** reach **downstream code**: `smbios_c` first, through a
self-hosted vcpkg registry, with GitHub Releases in parallel. Its
Alternative 4 names the other half of the problem and deliberately leaves
it open:

> **4 - System-package route (Homebrew formula / apt / distro packaging)
> for the `smbios` CLI** ... Solves a different problem than this ADR's
> scope (library *distribution* for downstream code, not CLI
> *installation* for end users) ... **Out of scope for this ADR - worth its
> own future ADR if/when the `smbios` CLI (not `libsmbios`/`smbios_c`) is
> the thing being distributed to end users, not developers.**

Milestone 0012's own Non-goals repeat the deferral verbatim. This ADR is
that revisit, and it widens the question in one way ADR 0004 could not
have: milestone 0011 shipped `smbios_gui`, so the project now has a
**desktop application**, not only a `dmidecode`-shaped command. A GUI
raises distribution questions a CLI never does (Start Menu entries, `.app`
bundles, Gatekeeper, Flathub, desktop files).

### Two distribution axes

Keeping these apart is the single most load-bearing distinction in this
ADR, because they want different artifacts, different channels, different
versioning strictness and different review burdens.

| | **Developer axis** (ADR 0004) | **End-user axis** (this ADR) |
|---|---|---|
| Consumer wants | to *link* code | to *run* a program |
| Artifacts | `smbios_c` shared lib + headers, `libsmbios` static lib + headers + CMake package config | `smbios` CLI, `smbios_gui` application |
| Channels | vcpkg, `pkg-config`, (later Conan, NuGet) | Chocolatey, WinGet, Homebrew, apt/dnf/AUR, AppImage |
| Breakage cost | a consumer's build fails loudly | a user's install is silently stale or broken |
| Owned by | ADR 0004, milestone 0012 | **this ADR** |

They are not disjoint channels, though: an **installer and a tarball can
carry both axes at once**, which is exactly what the Windows installer
below does. The axes separate *audiences*, not *files*.

### What exists today

- **Install rules:** only `smbios_c/CMakeLists.txt` calls `install()`
  (library, public header, generated `smbios_c.pc`). `libsmbios` has none;
  milestone 0012 Story 01.0 owns adding them plus a whole-archive-safe
  `libsmbiosConfig.cmake`. **Neither the CLI nor the GUI has an
  `install()` rule at all** - `smbios` lands in `smbios/bin/` and
  `smbios_gui` in `smbios_gui/bin/` inside the build tree, and nothing
  installs them anywhere.
- **Pipeline:** [ADR 0005](0005-unified-build-package-publish-pipeline.md)
  defines one `lint -> build/test -> package -> verify -> publish`
  pipeline. Its D4 says the `package` stage **archives the build stage's
  already-tested `cmake --install` tree and never rebuilds**; its D9 makes
  every publishing target a **plug-in destination** (a
  `scripts/ci/destinations/<name>.py` module plus one row in
  `scripts/ci/publish_destinations.json`), so adding a destination inside
  an existing permission class needs **no workflow edit**.
- **No `LICENSE` file exists anywhere in the repository** (`git ls-files`
  finds none; `README.md` states no licence either). Milestone 0012's
  `status.md` already carries this as open question H3.
- **No version exists.** The root `CMakeLists.txt` is
  `project(smbios_sdk CXX)` with no `VERSION`. ADR 0005 D5 makes that
  value the single source of truth and milestone 0012 Story 04.0 Task 01
  owns adding it; until then there is no version to put in any manifest.
- **No installer and no CPack configuration** of any kind.
- **Linkage reality, relevant to "static and dynamic versions":**
  `libsmbios` is `add_library(${TARGET} ${SOURCES})` with no explicit
  type, so it is static (`BUILD_SHARED_LIBS` defaults `OFF`), and
  **nothing in the tree exports its C++ surface from a DLL** - no
  `dllexport`, no `GenerateExportHeader`, no `WINDOWS_EXPORT_ALL_SYMBOLS`
  outside `smbios_c`. `smbios_c` is hard-coded `SHARED`, and its own
  comments explain why a `STATIC` one cannot hold its contract ("a STATIC
  `smbios_c` only records a usage requirement on `libsmbios`, it does not
  merge its objects, so the whole-archive problem would simply reappear at
  every downstream consumer's own link step").

### Privilege model, per platform (decides which channels are even viable)

From `docs/architecture/overview.md` and the source chains:

| Platform | Primary source | Privilege to **run** |
|---|---|---|
| Windows | `GetSystemFirmwareTable()` | none |
| macOS | `AppleSMBIOS` IORegistry via IOKit | none |
| Linux | `/sys/firmware/dmi/tables` (mode 0400), then `/dev/mem` | **root** |

This is not trivia. It is the reason sandboxed channels are treated as
hostile below, and the reason the Linux story is the hardest of the three.

### Decision drivers

- **DD1 - A channel is only worth owning if it can stay current.** Each
  one is a recurring obligation on every release, not a one-off. Channels
  whose update is a data file in this repository (a Scoop bucket, a
  Homebrew tap, an AUR `PKGBUILD`) cost far less per release than ones
  gated behind someone else's review queue (Chocolatey moderation,
  `winget-pkgs` validation, Debian sponsorship).
- **DD2 - Reuse ADR 0005's machinery; add no second pipeline.** Every
  artifact below must be a `package`-stage leg over the tested install
  tree, and every channel a D9 destination. If a channel cannot be
  expressed that way, that is an argument against the channel.
- **DD3 - Never ship a privilege escalation.** `libsmbios` reads raw
  firmware memory. No packaged artifact may be setuid, setcap, or install
  a privileged helper. A Linux user runs `sudo smbios`; that is the
  correct, boring answer.
- **DD4 - Sandboxed formats fight this program's purpose.** Snap strict
  confinement, Flatpak and MSIX all exist to deny exactly the resources
  this project must read (`/sys/firmware/dmi`, `/dev/mem`, IOKit). A
  channel that needs a confinement exception needs a reviewer to grant it,
  on every submission.
- **DD5 - Signing is a budget, not a flag.** Unsigned MSIs raise
  SmartScreen warnings; unsigned macOS `.pkg`/`.dmg`/`.app` bundles are
  refused by Gatekeeper and need Apple notarization (a paid Developer ID).
  Source-building channels (Homebrew formula, AUR) sidestep signing
  entirely. Channel ordering should follow that asymmetry.
- **DD6 - One artifact, many features, beats many artifacts.** The
  Windows ask ("CLI and GUI, static and dynamic library") is a
  *feature-selection* problem, which MSI models natively, not a reason for
  four separate downloads.

## Decision

### D1 - This ADR owns the end-user axis; ADR 0004 keeps the developer axis

ADR 0004 and milestone 0012 remain the authority on vcpkg, `pkg-config`
and the `libsmbios` CMake package export. This ADR adds no library
channel and changes no decision in ADR 0004. It decides which **end-user**
channels this project enters, in what order, and what the Windows
installer is.

The one place the axes meet is the Windows installer and the release
tarballs, which carry developer payloads (headers, import lib, static
archives, CMake package config) as **optional features** alongside the
CLI and GUI. That is a packaging convenience, not a change of scope.

### D2 - The channel matrix

Four waves. **Wave 1** is what this project commits to; later waves are
pre-triaged, not promised. Each row's "Update cost" is the recurring
per-release obligation DD1 weighs.

#### Windows

| Channel | Ships | Audience | Gate | Update cost | Verdict |
|---|---|---|---|---|---|
| **GitHub Releases** (`.zip` + MSI + `.sha256` + `manifest.json`) | everything | both | none (own repo) | automated, ADR 0005 D6 | **Wave 1** - already milestone 0012 Story 03.0/04.0 |
| **Chocolatey** (community repo) | `nuspec` wrapping the MSI | end user | moderation review; embedded binaries need `VERIFICATION.txt`; `licenseUrl` required | push per release, occasional moderator round-trip | **Wave 1** (named in the request) |
| **WinGet** (`microsoft/winget-pkgs`) | YAML manifest pointing at the Release MSI URL + sha256 | end user | PR + automated validation; `License` required | one PR per release, scriptable (`wingetcreate`) | **Wave 1** - lowest marginal cost once the MSI exists, since it only references the Release asset |
| **Scoop** (self-hosted bucket) | portable `.zip` manifest | end user, portable/no-admin | none (own git repo) | one JSON edit, same run | **Wave 2** - cheapest row in the whole matrix; deferred only because it needs no MSI and so adds no new information |
| **vcpkg** (self-hosted registry) | `smbios_c`, `libsmbios` | developer | none | - | **ADR 0004** - not this ADR |
| **NuGet** (native runtime package) | `smbios_c.dll` | developer (.NET P/Invoke) | account | per release | **Wave 3** - belongs with milestone 0013's C# binding, not here |
| **MSIX / Microsoft Store** | app package | end user | Store identity, signing, API restrictions (DD4) | review per submission | **Rejected** - sandbox fights the purpose; Store identity and signing cost exceed the reach gained |

#### macOS

| Channel | Ships | Audience | Gate | Update cost | Verdict |
|---|---|---|---|---|---|
| **GitHub Releases** (`.tar.gz`) | everything | both | none | automated | **Wave 1** - milestone 0012 |
| **Homebrew tap** (own `homebrew-libbios` repo) | formula: CLI + libs, built from source | both | none (own repo) | one formula edit (version + sha256) | **Wave 1** - best effort/reach ratio on macOS: no signing needed (DD5), no review queue (DD1), and `brew install` is the idiom this audience expects |
| **Homebrew core** | same formula, with bottles | both | notability + stability review; requires an OSI-approved licence | upstream handles builds | **Wave 3** - revisit only once the tap has real users; same promotion-trigger logic ADR 0004 applies to central vcpkg |
| **Homebrew cask** | `smbios_gui` as a `.app` bundle | end user (GUI) | none for own tap | rebuild bundle per release | **Wave 2** - requires first *making* a `.app` bundle: `smbios_gui` is a plain Mach-O executable today, not a bundle, and a cask of an unsigned binary still hits Gatekeeper (DD5) |
| **`.pkg` / `.dmg` installer** | CLI + GUI + dev files | end user | Developer ID + notarization (paid) | signing per release | **Deferred** - the macOS analogue of the Windows MSI, blocked on DD5 budget; `brew` covers this audience without it |
| **MacPorts** | Portfile (Tcl), source-built | both | PR review in `macports/macports-ports`. **No licence constraint**: MacPorts is BSD-3-Clause and ports software under any licence; its `license` field only gates whether MacPorts serves a *prebuilt* archive, and MIT permits that | one PR per release | **Wave 3** - no technical barrier, and no signing need (source-built, like the Homebrew tap). Deferred purely on DD1: unlike Homebrew there is no self-hosted-tap equivalent, so **every** release goes through an upstream review queue, for an audience that largely overlaps Homebrew's. Promote alongside Homebrew core |

#### Linux

| Channel | Ships | Audience | Gate | Update cost | Verdict |
|---|---|---|---|---|---|
| **GitHub Releases** (`.tar.gz`) | everything | both | none | automated | **Wave 1** - milestone 0012 |
| **AUR** (`PKGBUILD`, source + `-bin`) | CLI, GUI, libs | both | none (user-submitted) | one `PKGBUILD` bump | **Wave 1** - the only zero-gate, zero-signing native-package channel on Linux (DD1, DD5) |
| **Fedora COPR** (`.spec` -> `.rpm`) | `smbios`, `smbios-gui`, `smbios-devel` | both | COPR account only, not Fedora review | rebuild per release, COPR builds it | **Wave 2** - gives real `dnf install` semantics with no distro review |
| **Self-hosted apt repo** (`.deb`, GPG-signed flat repo) | `smbios`, `smbios-gui`, `libsmbios-dev` | both | none (own hosting) | one repo regeneration | **Wave 2** - the honest `apt` answer at this scale; needs a signing key and a hosting location |
| **AppImage** | `smbios_gui` (and CLI) single file | end user | none | built in the `package` stage | **Wave 2** - near-free once install rules exist, and the one Linux artifact that needs no package manager at all |
| **Debian / Ubuntu official**, **Fedora official** | same packages | both | **sponsor / Debian Developer**; full package review; DFSG/licensing audit | upstream-driven, slow | **Wave 3** - correct long-term home, wrong first move for a pre-v1, unlicensed, single-maintainer project (DD1) |
| **Ubuntu PPA** (Launchpad) | `.deb` | both | Launchpad account | `dput` per release | **Wave 3** - overlaps the self-hosted apt repo; pick one |
| **Nixpkgs** | derivation | both | PR review | upstream | **Wave 3** |
| **openSUSE OBS** | `.deb` + `.rpm` for many distros | both | account | per release | **Wave 3** - attractive multi-distro shortcut; revisit if the apt/COPR pair proves too costly |
| **Snap** | CLI + GUI | end user | **classic confinement needs manual Snap Store approval**, because strict confinement blocks `/sys/firmware/dmi` and `/dev/mem` (DD4) | review per confinement change | **Rejected for now** - a channel whose core mechanism must be excepted on every submission |
| **Flatpak / Flathub** | `smbios_gui` | end user (GUI) | Flathub review; needs `--device=all` / `--filesystem=/sys/firmware` justification (DD4) | review per release | **Rejected for now** - same reason; AppImage serves the "portable GUI" need without arguing with a sandbox |

#### Cross-platform language ecosystems

PyPI wheels, crates.io, NuGet and npm all bundle `smbios_c`. They are
**milestone 0013's** (language bindings) business, listed here only so the
matrix is complete and so nobody adds them to this ADR's scope.

### D3 - The Windows installer is a WiX MSI built *over the install tree*

**Technology:** WiX Toolset, with hand-authored `.wxs` sources under
`packaging/windows/`, invoked from a `package`-stage step.

**Not CPack's `WIX` generator**, for a reason specific to this project:
ADR 0005 D4 fixes the `package` stage's input as the build stage's
**already-tested `cmake --install` tree**, and forbids rebuilding. CPack
runs from the build tree as a build-system step, which would either
re-derive the payload independently of the tested install tree or move
packaging logic back into CMake, where ADR 0005's DD5 (platform-neutral
in-repo commands, thin adapter) does not want it. Building the MSI from
the install tree keeps one source of truth for "what ships" - the same
tree the tarballs archive and the same tree `verify` smoke-tests.

**Feature tree** (DD6 - one download, user selects):

| Feature | Payload | Default | Notes |
|---|---|---|---|
| `CLI` | `smbios.exe` | on | adds its directory to the **per-machine** `PATH` |
| `GUI` | `smbios_gui.exe` | on | Start Menu shortcut; no `PATH` entry |
| `SDK_Shared` | `smbios_c.dll`, `smbios_c.lib` (import lib), `smbios/smbios.h` | off | the **dynamic** library, as it exists today |
| `SDK_Static` | `libsmbios.lib` + the per-OS/legacy archives, `libsmbios/include/**`, `libsmbiosConfig.cmake` | off | the **static** library; **requires milestone 0012 Story 01.0** |

**Fixed properties:** per-machine install (`Program Files`), a permanent
`UpgradeCode` (never changes, for the life of the product), `Version`
derived from ADR 0005 D5's single source, stable component GUIDs so major
upgrades replace rather than accumulate, and a `LICENSE` displayed in the
UI (hence D7).

The MSI needs **no elevation to run** the installed programs - Windows'
`GetSystemFirmwareTable()` needs no admin - only to install per-machine.

### D4 - What "static and dynamic versions of the library" resolves to

The request maps onto the two libraries that already exist, which is
**not** the same as building each library both ways:

- **Dynamic = `smbios_c`** (the C ABI shim). Already `SHARED`, already
  self-contained, already `install()`-able. Ships as `SDK_Shared`.
- **Static = `libsmbios`** (the C++ core). Already static. Ships as
  `SDK_Static`, once milestone 0012 Story 01.0 gives it install rules and
  the whole-archive-safe `libsmbiosConfig.cmake` - without that export,
  shipping the raw `.lib` would hand every consumer the silent
  `SMBiosEntryFactory::create() == nullptr` footgun ADR 0004's DD1 exists
  to prevent.

**A shared C++ `libsmbios.dll` is explicitly rejected**, and this is the
one place this ADR declines part of the literal request:

1. Nothing in the tree exports `libsmbios`'s C++ surface - no `dllexport`,
   no `GenerateExportHeader`, no `WINDOWS_EXPORT_ALL_SYMBOLS` outside
   `smbios_c`. `-DBUILD_SHARED_LIBS=ON` on Windows yields a DLL with no
   usable exports.
2. A C++ DLL boundary is an ABI contract across MSVC toolset versions,
   `/MD` vs `/MT`, iterator-debug levels and exception types - for a
   project that has deliberately deferred any ABI-stability policy (ADR
   0004 DD2, milestone 0007 Non-goals).
3. It would break `smbios_c`, whose whole-archive step requires
   `libsmbios` to be a static **archive** to merge objects from.
4. `smbios_c` already *is* the supported dynamic surface. That is why it
   exists.

A consumer wanting a DLL gets `smbios_c.dll`; a consumer wanting the full
C++ API links the static `libsmbios` through its exported CMake target.
If a shared C++ surface is ever genuinely needed, it is its own ADR with
an ABI policy attached, not an installer feature.

### D5 - Channels are ADR 0005 D9 destinations; installers are packaging legs

No new pipeline, no new workflow files beyond those ADR 0005 D8 already
allows (DD2).

- **New `package`-stage legs** (D4 manifest entries, over the install
  tree): `windows-msi`, `linux-appimage`, and later `linux-deb`,
  `linux-rpm`. Each emits its artifact plus `.sha256` into the same
  `manifest.json`, so `verify` clean-room-tests them and `publish`
  destinations consume them without recomputing digests.
- **New `publish` destinations** (`scripts/ci/destinations/`):
  `chocolatey.py`, `winget.py`, `homebrew-tap.py`, `aur.py`, each
  implementing D9's `NAME` / `REQUIRED_ENV` / `validate` / `publish` /
  `check_published`. `chocolatey.py` is `token-only` (an API key from the
  `release` environment); `winget.py`, `homebrew-tap.py` and `aur.py` are
  `repo-write`-shaped (they open a PR or push to a repository this project
  owns). All are `release` tier: they run only on a `v*` tag, only after
  `verify` passed in the same run (D6), and - per D9's CR-2 rule - refuse
  any manifest leg whose `binary_cache` is `restored`.
- `check_published` is not optional ceremony for these channels. A
  Chocolatey package that passes moderation but installs a wrong-digest
  MSI, or a WinGet manifest whose `InstallerSha256` drifted from the
  Release asset, is exactly the "silently stale or broken install" failure
  the end-user axis is more exposed to than the developer axis.

### D6 - Privilege and sandbox rules, binding on every artifact

- **No artifact is setuid, setcap, or installs a privileged helper**
  (DD3). The Linux packages document `sudo smbios`; the GUI keeps the root
  warning milestone 0011 already shows.
- **No packaged artifact relies on confinement exceptions.** This is what
  makes Snap, Flatpak and MSIX "rejected for now" rather than "wave 3":
  they would require arguing the exception with a reviewer on every
  submission, forever (DD4).
- **Linux package metadata must state the privilege requirement** in its
  description, because an unprivileged `smbios` run on Linux falls through
  the whole source chain and reports nothing useful - an outcome a user
  who typed `apt install` will read as "broken", not as "needs root".

### D7 - Prerequisites that gate every row in D2

None of this can ship without three things that do not exist today. They
are listed as decisions because each is a precondition, not a task
detail:

1. **A `LICENSE` file at the repository root, with an SPDX identifier.**
   Chocolatey requires `licenseUrl` (and `VERIFICATION.txt` for embedded
   binaries); WinGet requires `License`; Homebrew core requires an
   OSI-approved licence; Debian requires a DFSG-free one; the MSI UI
   displays one. This is milestone 0012's open question H3 and it is now
   **blocking**, not deferrable. **Decided 2026-11-15: MIT** (maintainer
   ruling) - OSI-approved and DFSG-free, so every row of D2 stays
   reachable, including the Wave 3 rows that impose a licence test of
   their own (Homebrew core, MacPorts' prebuilt-archive policy,
   Debian/Fedora official). The file itself is
   [milestone 0015](/docs/roadmap/0015-end-user-distribution-and-windows-installer/plan.md)
   Story 01.0 Task 01.
2. **A real version: `project(smbios_sdk VERSION <x.y.z> LANGUAGES CXX)`**, per
   ADR 0005 D5, owned by milestone 0012 Story 04.0 Task 01. Every manifest
   in D2 keys off it, and `check_versions.py` gains a version site per
   channel (`nuspec`, WinGet manifest, formula, `PKGBUILD`).
3. **`install()` rules for `smbios` and `smbios_gui`.** Neither has one;
   both land in build-tree `bin/` directories today. The `package` stage
   archives the install tree (ADR 0005 D4), so **a CLI or GUI that is not
   installed cannot be packaged at all.** This is a genuinely new
   prerequisite that milestone 0012 Story 01.0 does not cover - that story
   scopes itself to `libsmbios`'s library export.

## Alternatives Considered

| Alternative | Pros | Cons | Reason rejected |
|-------------|------|------|-----------------|
| **Do nothing; GitHub Releases only** | Zero recurring cost; milestone 0012 already delivers it | A `dmidecode`-shaped tool and a desktop GUI distributed only as a tarball reach almost no end users; "download, unzip, add to PATH" is not how this audience installs anything | Rejected - the Release baseline is necessary (it is every channel's upstream source) but not sufficient |
| **Enter every channel in D2 at once** | Maximum reach immediately | Multiplies DD1's recurring obligation by ~12, on a project with no licence, no version and no release cadence; the first failed moderation round-trip stalls the release | Rejected - hence the wave structure: each wave must prove it stays current before the next starts |
| **CPack (`WIX`, `NSIS`, `productbuild`, `DEB`/`RPM`) for everything** | One CMake-native mechanism; least new code; generators already exist | Runs from the build tree, so it either bypasses or re-derives the tested install tree ADR 0005 D4 fixes as the `package` stage's only input; moves packaging logic into CMake, against ADR 0005 DD5; CPack's WiX support is tied to specific WiX generations, adding a pin this project would have to track | Rejected as the *mechanism*; CPack remains a reasonable fallback for `DEB`/`RPM` specifically, where its output is simple and the install tree maps directly - revisit at Wave 2 |
| **NSIS or Inno Setup instead of WiX** | Simpler scripts; no MSI schema to learn; Inno has a pleasant UI out of the box | Not MSI, so no native feature tree (DD6), no Group Policy/Intune deployment, no standard major-upgrade semantics; Chocolatey and WinGet both handle MSI best, and enterprise deployment is a plausible audience for a hardware-inventory tool | Rejected - MSI's feature selection *is* the requested behaviour |
| **Four separate Windows downloads (CLI, GUI, static SDK, shared SDK)** | Each download is minimal | Four artifacts to version, sign, checksum, verify and publish per release, for a total payload of a few megabytes; four Chocolatey packages to moderate | Rejected per DD6 |
| **Build `libsmbios` both static and shared and ship both** | Literal reading of the request | Needs a C++ DLL export surface that does not exist, an ABI policy this project has deferred twice, and would break `smbios_c`'s whole-archive contract (D4) | Rejected - `smbios_c` is the dynamic surface by design |
| **Snap and Flatpak as the Linux answer** | Single artifact across distros; app-store discovery; automatic updates | Both sandbox precisely the firmware paths this project must read; Snap needs manually-approved classic confinement, Flathub needs reviewer-justified device access - per submission (DD4) | Rejected for now; AppImage gives the same "works everywhere, no distro packaging" benefit with no confinement to argue about |
| **Debian/Fedora official packages first** | The genuinely correct long-term home; distro-maintained forever | Requires a sponsor/DD, full package review, and a licence audit - against a repository that currently has no licence at all | Rejected as a *first* move; Wave 3, after AUR/COPR/apt prove the packaging itself is sound |

## Consequences

### Positive

- **Each channel's cost is now visible before it is paid.** The waves
  encode DD1 explicitly rather than discovering per-release maintenance
  cost after committing.
- **No new pipeline.** Every row lands as an ADR 0005 D4 packaging leg or
  a D9 destination, so the installer and the channels inherit the existing
  gating (tag-only, post-`verify`, same-run), digest discipline, tier
  rules and `check_published` verification for free (DD2).
- **The MSI and the Release tarballs share one payload definition** - the
  tested `cmake --install` tree - so "what shipped" has a single answer
  per release across every channel.
- **The sandbox and privilege questions are settled once, in writing**
  (D6), instead of being rediscovered per channel by whoever writes the
  next manifest.
- **Three latent blockers are surfaced as blockers** (D7), including one
  nobody had recorded: the CLI and GUI have no `install()` rules, and
  therefore cannot be packaged at all today.

### Negative

- **Chocolatey and WinGet both put a third party in the release path.**
  Moderation or validation can delay a release after the GitHub Release is
  already public. Mitigation: the Release is the source of truth; these
  channels are allowed to lag it.
- **Three new per-release obligations in Wave 1** (choco push, WinGet PR,
  formula bump), automated but not free. A broken channel is a visible
  user-facing failure, unlike a broken developer channel.
- **No Linux `apt`/`dnf` answer in Wave 1.** AUR covers Arch users only.
  Debian/Ubuntu and Fedora users get a tarball until Wave 2.
- **macOS has no GUI story in Wave 1** - a Homebrew cask needs a `.app`
  bundle that does not exist yet, and Gatekeeper still objects without
  paid signing (DD5).
- **Code signing stays unsolved**, so Windows users see SmartScreen
  warnings on the MSI and macOS binary distribution is capped at `brew`'s
  source build.
- **This ADR declines part of the literal request** (a shared C++
  `libsmbios`), redirecting it to `smbios_c`. If that redirection is
  wrong, D4 is the point to argue.

## Validation / Rollout

**Sequencing.** Nothing here starts before its prerequisites:

```
D7 (licence, version, CLI/GUI install rules)
  -> milestone 0012 Story 01.0 (libsmbios install + package export)
  -> milestone 0014 (pipeline foundation)
     -> Wave 1: windows-msi packaging leg
                -> chocolatey + winget destinations
                -> homebrew-tap + aur destinations
```

**Per-channel validation**, reusing milestone 0007's and 0012's own
clean-room pattern (a scratch consumer with no reference to this source
tree):

- **MSI:** install on a clean Windows runner, assert each feature's files
  land where declared, `smbios --version` resolves through `PATH`, the GUI
  shortcut exists, a major upgrade from the previous version replaces
  rather than duplicates, and uninstall leaves nothing behind. Assert
  `SDK_Static` + `find_package(libsmbios CONFIG)` builds and *correctly
  parses* a synthetic table from the installed tree - the whole-archive
  regression check, not just a link check.
- **Chocolatey / WinGet:** `choco install` and `winget install` from the
  real published package on a clean runner; `check_published` re-reads the
  published digest and compares it with `manifest.json`.
- **Homebrew tap:** `brew install --build-from-source` on a clean runner,
  plus `brew audit --strict`.
- **AUR:** `makepkg` in a clean container.
- **Every channel:** `check_versions.py` gains a version site, so a
  manifest that drifts from the root `project()` version fails the
  pipeline rather than shipping.

**Promotion triggers** (mirroring ADR 0004's own rollout logic): a Wave 2
channel starts only after Wave 1 has survived **two consecutive releases
with no manual intervention**. Homebrew core and Debian/Fedora official
submission wait for a named, real external user asking for them -
identical to ADR 0004's central-vcpkg trigger.

**Rollback.** Each destination is independently disable-able by flipping
`enabled` to `false` in `publish_destinations.json` (ADR 0005 D9), with no
workflow change. A channel that becomes a maintenance burden is switched
off, not worked around.

## Open questions

1. ~~**Licence (blocking, maintainer decision).**~~ **Resolved
   2026-11-15: MIT.** Every row of D2 stays reachable. The constraint
   recorded in `CLAUDE.md` did not bind the choice and still holds:
   `c-versions/` holds GPL-2.0-or-later `dmidecode` sources as *reference
   only*, never compiled, linked or `#include`d - but milestone 0008's
   isolation guard must stay intact, and demonstrably so, if a distro
   licence audit ever happens (Wave 3).
2. **Code-signing budget (DD5).** An Authenticode certificate and an
   Apple Developer ID gate the unsigned-binary warnings, the macOS
   `.pkg`/`.dmg` row and the Homebrew cask row.
   **Largely answered 2026-11-16 by [ADR 0007](0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
   D8:** the SignPath Foundation's free OSS programme covers Windows
   signing at no cost, and its criteria are what this project's own
   pipeline already produces - so the "Wave 1 ships with SmartScreen
   warnings" consequence above is likely pessimistic. macOS is unchanged
   and needs nothing in Wave 1, because D2's Wave 1 macOS row is a
   source-built Homebrew tap.
3. ~~**Registry hosting for the Homebrew tap and the apt repo.**~~
   **Resolved 2026-11-16 by [ADR 0007](0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
   D6:** nothing is self-hosted. OBS, Fedora COPR and Launchpad each
   *build and publish* their repository, and Cloudsmith's open-source
   plan hosts apt/yum/nuget repositories for public OSI-licensed
   projects. This also closes milestone 0012's H2.
4. **Shipped build configuration.** Milestone 0012's status note already
   flags that the packaged configuration must be named (0014 C06 tests
   `-C Debug` today). An MSI shipping Debug binaries with a debug CRT
   dependency would be a user-visible defect, so this must be settled
   before the `windows-msi` leg lands.
5. **Does the GUI belong in Wave 1's MSI, given no signing?** The GUI is
   the artifact most exposed to SmartScreen. Shipping CLI-only first is a
   defensible alternative reading of D3.
6. **Windows static triplet** (milestone 0012's M4) - `/MT` vs `/MD` for
   the shipped `SDK_Static` feature, which determines whether a consumer's
   own CRT choice can link it.

## Links

- **Prior decision this ADR extends:** [docs/adr/0004-package-manager-distribution.md](0004-package-manager-distribution.md)
  (Alternative 4 defers the end-user axis to "its own future ADR"; this is it)
- **Pipeline this ADR builds on:** [docs/adr/0005-unified-build-package-publish-pipeline.md](0005-unified-build-package-publish-pipeline.md)
  (D4 packaging legs, D6 publish gating, D9 destination plug-ins)
- **Roadmap milestone implementing Wave 1 of this ADR:** [docs/roadmap/0015-end-user-distribution-and-windows-installer/plan.md](/docs/roadmap/0015-end-user-distribution-and-windows-installer/plan.md)
  (licence + CLI/GUI install rules, the WiX MSI leg, and the Chocolatey /
  WinGet / Homebrew-tap / AUR destinations)
- **Milestones this ADR depends on:** [0012 package manager distribution](/docs/roadmap/0012-package-manager-distribution/plan.md)
  (Story 01.0 install rules and package export; Story 04.0 Task 01 version
  source), [0014 CI rework](/docs/roadmap/0014-ci-rework-from-scratch/plan.md)
  (pipeline foundation)
- **Milestone that owns the language-ecosystem column of D2:** [0013 language bindings](/docs/roadmap/0013-language-bindings/plan.md)
- **GUI artifact this ADR newly has to distribute:** [0011 GUI visualization tool](/docs/roadmap/0011-gui-visualization-tool/plan.md)
- **Destination-authoring guide the new destinations follow:** `docs/dev/adding-a-publish-destination.md` (created by milestone 0012 Story 04.0 Task 05)
