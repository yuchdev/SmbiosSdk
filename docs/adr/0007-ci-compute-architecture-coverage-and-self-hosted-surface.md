# 0007 - CI compute, architecture coverage and the self-hosted surface

> **Status:** Proposed
>
> **Date:** 2026-11-16
>
> **Supersedes:** _(none)_
>
> **Superseded by:** _(none)_

## Context

[ADR 0005](0005-unified-build-package-publish-pipeline.md) decided the
pipeline's *shape* - stage order, the leg contract, the artifact manifest,
the destination plug-in interface - and pinned three runner images
(`ubuntu-24.04`, `macos-15`, `windows-2022`) in `scripts/ci/pins.json`.
[ADR 0006](0006-end-user-distribution-and-windows-installer.md) decided
*which channels* this project publishes to.

Neither decided two dimensions that packaging now forces:

1. **Which architectures** this project builds, tests and ships for.
   `scripts/ci/legs.json` already names its packaging legs `linux-x64`,
   `windows-x64` and `macos-arm64`, so the `<os>-<arch>` shape exists -
   but exactly one architecture per OS is covered, and nothing records
   why, or what the other architectures would be for.
2. **Whose compute**, and consequently **what the maintainer must host
   personally**. ADR 0005's DD5 (platform portability) assumed a CI
   provider without choosing one; ADR 0006 left code signing as an
   unbudgeted open question; milestone 0012's `status.md` still carries
   "registry hosting (H2)" as open.

Two facts narrow the problem sharply:

- **This repository is public and MIT-licensed**
  (`github.com/yuchdev/SmbiosSdk`; `LICENSE` landed after ADR 0006 D7.1).
  GitHub's standard hosted runners are therefore free and unlimited here,
  **including arm64 for both Linux and Windows**, which reached general
  availability for public repositories in August 2025.
- **SMBIOS is not universal across architectures.** Generic "build for
  every popular architecture" advice is wrong for this library
  specifically, and following it would spend effort on platforms where
  the library cannot function. See D1.

### Decision drivers

- **DD1 - Architecture coverage must follow purpose, not popularity.**
  An architecture earns a leg because it either (a) carries real users,
  (b) proves a portability property the project already claims in
  writing, or (c) is built for free by someone else. Nothing else.
- **DD2 - Free-for-OSS first, and prefer services that already host what
  they build.** A hosted package repository that the service publishes
  itself is strictly better than a bucket this project signs and serves.
- **DD3 - Minimise the surface that must be hosted by the maintainer,
  and name what remains.** An unnamed self-hosted dependency is an
  availability risk disguised as a habit.
- **DD4 - A public repository must never host a self-hosted runner.**
  Fork pull requests execute arbitrary code; this would undercut ADR
  0005's threat model, which confines credentials by canonical path and
  forbids `secrets.` outside publish jobs.
- **DD5 - Reuse ADR 0005's existing seams.** Architectures are legs in
  `legs.json`; runner images are rows in `pins.json`; publishing targets
  are D9 destination plug-ins. This ADR adds data, not mechanism.

## Decision

### D1 - Architecture coverage is tiered by purpose

SMBIOS availability, not market share, decides the tier:

| Arch | SMBIOS available? | Role |
|---|---|---|
| **x86_64** | Yes, universally | Tier A - primary target, all three OSes |
| **aarch64 / arm64** | Yes - SystemReady/SBBR servers, Apple Silicon (`AppleSMBIOS` via IOKit), Windows on ARM (`GetSystemFirmwareTable()`) | Tier A - real users, all three OSes |
| **s390x** | **No** | **Tier B** - the only readily available big-endian target; see D3 |
| **i686 / armv7** | i686 yes (legacy BIOS); armv7 effectively no | Tier B - 32-bit `size_t`/pointer and bounds-arithmetic check |
| **ppc64le** | No (OPAL / device tree) | Tier C only - built by distro services as a side effect |
| **riscv64** | Specified for RISC-V server platforms, rare in practice | Tier B, optional - forward portability |

- **Tier A - ship legs.** Build, test and package. Real users.
- **Tier B - portability-proof legs.** Build and run `tests/unit/` only.
  No packaging, no live-table test, no GUI.
- **Tier C - distro-driven.** Built for free by OBS/COPR/Launchpad as a
  side effect of packaging; this project runs no CI for them.

### D2 - Tier A is six legs, all on GitHub-hosted runners, at no cost

| Leg | Runner label | Status |
|---|---|---|
| `linux-x64` | `ubuntu-24.04` | exists (pending milestone 0012) |
| `linux-arm64` | `ubuntu-24.04-arm` | **new** |
| `windows-x64` | `windows-2022` | exists (pending milestone 0012) |
| `windows-arm64` | `windows-11-arm` | **new** |
| `macos-arm64` | `macos-15` | exists (pending milestone 0012) |
| `macos-x64` | `macos-15-intel` | **new, with a sunset date** - see Consequences |

`pins.json`'s `runners` map gains the three new labels. The existing
three rows are unchanged.

### D3 - Tier B runs emulated, on unit tests only, and does not use vcpkg

Tier B legs run under QEMU (a `setup-qemu-action` + container, or an
equivalent run-on-arch action) on the same free x64 runners, on **nightly
and tag events only** - never on pull requests.

- **`linux-s390x` is the one Tier B leg with a specific, named
  justification.** Milestone 0003 replaced
  `reinterpret_cast`-onto-packed-struct parsing precisely because it "is
  not portable to a big-endian host", and **that claim has never been
  executed anywhere** - every test since has run little-endian. This leg
  turns a written claim into a tested one. It does not assert that anyone
  runs this library on s390x; it asserts the parser is byte-order-correct.
- `tests/unit/` is the suite that makes this possible: hermetic by
  construction, feeding bytes through the `SmbiosTableWalkTestAccess`
  seam and bypassing every `ISmbiosSource`, so it needs no firmware, no
  hardware and no privileges.
- **No vcpkg on Tier B.** Building Boost from source under emulation
  would dominate the cost of this entire matrix. The root
  `CMakeLists.txt` uses a plain
  `find_package(Boost ${BOOST_MIN_VERSION} REQUIRED)`, so the container's
  own `libboost-dev` satisfies it. Tier B legs configure CMake **without**
  the vcpkg toolchain file. This divergence must be recorded in
  `legs.json`, or it will be "fixed" back to a preset.

### D4 - GitHub Actions is the only CI provider, and no self-hosted runner attaches to this repository

No second CI provider is adopted. The alternatives are surveyed below;
none adds an architecture GitHub does not already cover free, and each
would add a second adapter to maintain against ADR 0005's portability
layer.

**No self-hosted runner is ever attached to this repository** (DD4). If
the firmware lab is ever automated, it runs in a **separate private
repository** that publishes a signed result artifact this repository
consumes.

### D5 - Packaging is per-architecture, with three channel-specific shapes

| Channel | x86_64 | arm64 | Shape |
|---|---|---|---|
| GitHub Release archive | all 3 OSes | all 3 OSes | one archive per `<os>-<arch>` leg - six archives |
| **Windows MSI** | `windows-x64` | `windows-arm64` | MSI is per-architecture: **two MSIs** |
| **WinGet** | both | both | **one manifest**, multiple installer entries keyed by `Architecture` |
| **Chocolatey** | both | both | one package; the install script selects by processor architecture, with **both** checksums from `manifest.json` |
| Homebrew tap | source build | source build | arch-agnostic (bottles are Wave 3) |
| AUR | source build | source build | one `PKGBUILD`, `arch=('x86_64' 'aarch64')` |

This amends milestone 0015 Stories 02.0 and 03.0, which assume a single
MSI and an arch-unaware Chocolatey install script.

### D6 - Package repositories are never self-hosted

OBS, Fedora COPR and Launchpad each **build and publish** their
repository; Cloudsmith's open-source plan hosts apt/yum/nuget
repositories for public, OSI-licensed projects. **This closes ADR 0006
open question 3 and milestone 0012's open question H2**: no bucket, no
GPG-signed flat repo, no hosting bill. S3 staging (ADR 0005 D9's worked
example) remains available and disabled for disposable copies only.

### D7 - The self-hosted surface is exactly four nodes

Free services cover compute. They cannot cover **real firmware**, **a
real display**, or **a signing identity**:

| Node | Runs | Why no free service covers it |
|---|---|---|
| **S1 - Firmware lab** | `tests/integration/`, `tests/perf/`, the legacy `/dev/mem` scan, on real OEM boards | Every hosted Linux and Windows runner is a **virtual machine**: `/sys/firmware/dmi/tables` reports the hypervisor's own strings, not real vendor firmware, entry-point variety or OEM structure types |
| **S2 - GUI local tier** | `ctest -L gui-local`, the manual checklist | Needs a real display, GL context, OS clipboard and live table. `SMBIOS_GUI_LOCAL_TESTS` is **already forced off** when `CI` is set - self-hosted by design |
| **S3 - Signing identity** | Authenticode for the MSI; Apple Developer ID for macOS binaries | A certificate is an identity, not compute; CA/Browser Forum rules keep the private key on FIPS-140 hardware or a cloud HSM |
| **S4 - Secret custody** | Chocolatey API key, WinGet fork token, tap push token, AUR SSH key | The compute is hosted (ADR 0005 D6's `release` environment); the keys are a standing maintainer duty |

**The self-hosted surface is exactly the source layer and the display
layer** - which is not a coincidence. Milestone 0006 already drew this
line inside the test suite when it separated hermetic parser tests from
real-hardware suites. Everything above `ISmbiosSource` is free and hosted
forever; only the sources and the pixels need a machine the maintainer
owns.

### D8 - S1 shrinks through a firmware corpus; S3 shrinks through free OSS signing

- **S1: collect raw SMBIOS dumps instead of machines.** Scrubbed real
  firmware tables, committed as fixtures and replayed through the
  `SmbiosTableWalkTestAccess` seam, convert a hardware-ownership problem
  into a data problem that runs free on every runner and every
  architecture - including s390x, where a real vendor table replayed
  byte-for-byte is a far stronger endian test than any synthetic one.
  This **does not eliminate S1**: a corpus covers table *content*, never
  source *acquisition*, so S1 shrinks from one machine per vendor to
  **one machine per OS**. Owned by
  [milestone 0016](/docs/roadmap/0016-firmware-table-corpus/plan.md).
  - **Scrub before soliciting.** Raw tables carry serial numbers, UUIDs
    and asset tags (Type 1 serial and UUID, Type 2 serial, Type 3 asset
    tag, Type 4 serial, among others). The scrubbing tool and its tests
    must exist **before** this project asks anyone for a dump.
- **S3: apply to the SignPath Foundation's free OSS code-signing
  programme.** Its criteria - an OSI-approved licence, a public
  repository, free downloads, **releases built by CI from the public
  source**, real releases, 2FA - are what ADR 0005's pipeline and ADR
  0006's Wave 1 already produce. **Finishing the pipeline is the
  application.** macOS needs no equivalent in Wave 1, because ADR 0006
  already chose a source-built Homebrew tap, which needs no Developer ID;
  the `.pkg`/`.dmg`/cask rows that would are Wave 2+.

### D9 - Community outreach is gated on a tested pipeline

Outreach is the mechanism ADR 0004 and ADR 0006 already depend on: both
gate their Wave 3 promotions (central vcpkg, Homebrew core, MacPorts,
Debian/Fedora official) on **a named real external consumer**. It is also
how the D8 corpus gets contributors.

It starts only when the readiness bar in Validation / Rollout is met,
because: a demo of a release pipeline is a demo of it *working*; inbound
dumps and bug reports outpace a single maintainer unless the hermetic
suite already catches regressions; and SignPath's own criteria require
real CI-built releases first.

## Alternatives Considered

| Alternative | Pros | Cons | Reason rejected |
|-------------|------|------|-----------------|
| **Build every "popular" architecture at Tier A** | Simple rule; maximal matrix | Spends packaging and live-table effort on ppc64le and s390x, where SMBIOS does not exist at all; six more artifacts nobody can run | Rejected per DD1 - the tiering exists precisely to avoid this |
| **Skip exotic architectures entirely** | Cheapest; no emulation | Leaves milestone 0003's endian-safety claim permanently untested, on a codebase that rewrote its parser specifically for that property | Rejected - D3's s390x leg is the cheapest possible evidence for an existing written claim |
| **Add a second CI provider** (AppVeyor, GitLab CI, Travis) | Redundancy if GitHub has an outage | Adds no architecture GitHub does not cover free; a second adapter to maintain against ADR 0005's portability layer; more credential surface | Rejected per DD5 |
| **Cirrus CI for BSD and arm64** | Was the standard free OSS answer for exactly this | **Shut down 1 June 2026** | Rejected as non-existent - recorded explicitly because it remains the most-recommended answer in older material |
| **Self-hosted runners for the firmware lab** | Real hardware in CI; no emulation | A public repository runs fork PR code on that machine; directly contradicts ADR 0005's threat model | Rejected per DD4; D4 gives the separate-private-repository route instead |
| **Self-hosted apt/yum repository** (bucket + GPG-signed flat repo) | Full control of layout and retention | Hosting bill, key custody, mirror and availability duty - all to duplicate what OBS/COPR/Launchpad/Cloudsmith give free | Rejected per DD2; D6 |
| **Buy real aarch64/riscv64 hardware or a SourceHut builds plan** | Fast native builds; no emulation flakiness | GitHub already covers aarch64 free; riscv64 has no users for this library | Rejected now; revisit only if emulated Tier B proves unusably slow |
| **Accept unsigned Windows installers permanently** (ADR 0006's current stance) | Zero effort | SmartScreen warnings on every install, for a tool users run against their own firmware - exactly the context where a warning costs trust | Rejected - D8's SignPath route makes this unnecessary at no cost |

## Consequences

### Positive

- **The full architecture matrix costs $0.** Tier A is six free legs,
  Tier B is free emulated nightly time, Tier C is free compute from
  distribution services. The only real budgets are nightly wall-clock and
  maintenance.
- **A written claim becomes a tested one.** D3's `linux-s390x` leg gives
  milestone 0003's endian-safe parser its first big-endian execution.
- **Two long-open questions close.** D6 answers ADR 0006 open question 3
  and milestone 0012's H2 (registry hosting); D8 answers ADR 0006 open
  question 2 (code-signing budget) for Windows.
- **The self-hosted surface is named and bounded** (D7) - four nodes,
  each with a reason, rather than an unexamined habit.
- **Everything lands in existing seams**: rows in `legs.json` and
  `pins.json`, destination modules under `scripts/ci/destinations/`. No
  new mechanism, no new workflow family.

### Negative

- **macOS x86_64 has a hard deadline.** `macos-13` retired in December
  2025; `macos-15-intel` is the last Intel image and is scheduled to
  remain available only until **August 2027**. Any macOS x64 artifact
  needs a plan that outlives it - a `lipo`-merged universal binary built
  on an arm64 runner is the obvious candidate, and is an open question.
- **Windows packaging work roughly doubles** (D5): two MSIs and an
  arch-aware Chocolatey install script, amending milestone 0015 Stories
  02.0/03.0 before they start.
- **Tier B will occasionally be slow and flaky.** Emulated builds are
  several times slower and must stay off pull requests.
- **Tier C is free compute and non-free maintenance.** Enabling
  `ppc64le` on OBS costs nothing to run and everything to debug the first
  time it breaks on a platform nobody here can reproduce.
- **The corpus creates a privacy obligation** this project did not
  previously have (D8). Soliciting firmware dumps before the scrubber
  exists would publish contributors' machine identifiers permanently.
- **Single-provider concentration.** D4 accepts that a prolonged GitHub
  outage stops all CI and publishing. The mitigation is ADR 0005's
  portability layer, not a second live provider.

## Validation / Rollout

**Sequencing.**

```
pins.json + legs.json rows (D2, D3)
  -> Tier A six legs green on PR and tag
  -> Tier B linux-s390x green            (milestone 0003's claim proven)
  -> milestone 0016 corpus               (scrubber first, then replay, then solicit)
  -> SignPath application                (D8; needs a real tagged release)
  -> community track                     (D9; only past the bar below)
```

**Readiness bar for D9.** Outreach starts only when all of these hold:

- [ ] All six Tier A legs green on pull request **and** on a release tag.
- [ ] The `linux-s390x` Tier B leg green.
- [ ] One tagged release published end to end (`package` → `verify` →
      `publish`) with no manual intervention.
- [ ] `check_published` verified on at least one real channel.
- [ ] Clean-room consumer tests passing against **published** artifacts.
- [ ] The GUI manual checklist executed once per OS on the S2 node, and
      the packaging checklist
      ([docs/test/manual-packaging-verification.md](/docs/test/manual-packaging-verification.md))
      executed on all three x64 platforms.
- [ ] The corpus scrubber exists and is tested - **before** any dump is
      solicited.

**Per-decision validation.**

- D2/D3: each new leg must fail loudly on its own first run rather than
  silently skipping; a Tier B leg that cannot find distro Boost must say
  so by name.
- D5: `winget validate` on the multi-installer manifest; a Chocolatey
  install on an arm64 Windows machine resolving the arm64 MSI.
- D6: a clean-room `apt`/`dnf` install from the chosen service's
  published repository.
- D7/D8: the corpus milestone's own exit gates
  ([milestone 0016](/docs/roadmap/0016-firmware-table-corpus/plan.md)).

**Rollback.** Every element is a data row: a leg is removed from
`legs.json`, a destination is `enabled: false` in
`publish_destinations.json`, a runner label reverts in `pins.json`. No
code change is required to retreat from any part of this ADR.

## Open questions

1. **How many Tier B legs?** `linux-s390x` is recommended with a specific
   justification; `linux-i686` and `linux-riscv64` are defensible but
   optional.
2. **How many Tier C architectures to enable** on OBS/COPR/Launchpad,
   given each is free to run and not free to debug.
3. **macOS: two archives, or one universal binary?** The latter survives
   the August 2027 Intel-runner sunset.
4. **Which Linux packaging service**, given OBS, COPR and Launchpad
   overlap heavily and each builds *and* hosts. OBS covers the most
   distributions and architectures in one place; COPR is the least
   friction for Fedora alone.
5. **Does `windows-arm64` get its own MSI in Wave 1**, or does Wave 1
   ship x64 only? This decides whether milestone 0015 Story 02.0 is
   amended before it starts.
6. **What S1 hardware actually exists?** D7 asserts the lab shrinks to
   "one machine per OS"; the real inventory - and whether any of it is an
   ARM server or a Windows-on-ARM device - is the maintainer's to state,
   and it bounds how much of the `ISmbiosSource` layer is testable at all.
7. **When to apply to SignPath Foundation?** Probably at the first green
   tagged release, but it has a lead time that should not be discovered
   on release day.

## Links

- **Pipeline this extends:** [docs/adr/0005-unified-build-package-publish-pipeline.md](0005-unified-build-package-publish-pipeline.md) (D3 pins, D4 legs, D6 gating, D7 tiering, D9 destinations)
- **Channels this serves:** [docs/adr/0006-end-user-distribution-and-windows-installer.md](0006-end-user-distribution-and-windows-installer.md) (D5 amends its MSI assumption; D6 closes its open question 3; D8 closes its open question 2)
- **Milestone owning `legs.json`/`pins.json`:** [docs/roadmap/0014-ci-rework-from-scratch/plan.md](/docs/roadmap/0014-ci-rework-from-scratch/plan.md)
- **Milestone this ADR creates:** [docs/roadmap/0016-firmware-table-corpus/plan.md](/docs/roadmap/0016-firmware-table-corpus/plan.md)
- **Milestone whose MSI stories D5 amends:** [docs/roadmap/0015-end-user-distribution-and-windows-installer/plan.md](/docs/roadmap/0015-end-user-distribution-and-windows-installer/plan.md)
- **Milestone gating the version source:** [docs/roadmap/0012-package-manager-distribution/plan.md](/docs/roadmap/0012-package-manager-distribution/plan.md) (Story 04.0 Task 01)
- **The endian-safety work D3 finally tests:** [docs/roadmap/0003-portable-byte-level-smbios-parsing/plan.md](/docs/roadmap/0003-portable-byte-level-smbios-parsing/plan.md)
- **Local pre-flight checklist for the three x64 platforms:** [docs/test/manual-packaging-verification.md](/docs/test/manual-packaging-verification.md)
