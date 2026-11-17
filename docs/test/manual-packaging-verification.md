# Manual Pre-Flight Plan - CI, packaging and installation on three x64 platforms

Run this **on your own machines, before trusting CI**, on Windows x64,
Linux x64 and macOS x64. Its purpose is narrow: confirm that every
pipeline stage, every packaging artifact and every installation path works
locally, so a red CI run means "CI is wrong" rather than "we never checked".

This complements [manual-testing.md](manual-testing.md), which covers the
GUI's pixels and window behaviour. This plan covers **stages, artifacts and
installs**. Neither replaces the automated suites - run those first:

```bash
ctest --test-dir build/<preset> --output-on-failure
```

> **Why x64 specifically.** [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
> D2 makes x86_64 and arm64 both Tier A, but x64 is the architecture where
> all three OSes are available on ordinary hardware, and it is the one
> whose macOS runner has a sunset date (August 2027). Verifying x64 by hand
> and letting CI cover arm64 is the cheapest split.

## Recording results

Report **only discrepancies**, in the short form `expected -> actual`, one
per line. Say nothing for checks that pass. A clean run is one line:
`<platform>: all checks OK`.

**Never paste real serial numbers, UUIDs or asset tags into a report** -
counts and structure types only. The same rule as
[manual-testing.md](manual-testing.md).

```
P-07 install tree: bin/smbios present -> missing (GUI-off build)
P-12 MSI uninstall: no residue -> PATH entry remains
```

## Readiness - which checks you can run today

Several checks depend on work that has not landed. Run what you can now;
the rest are listed so the plan is complete rather than quietly partial.

| Group | Runnable today? | Gated on |
|-------|-----------------|----------|
| A - Stage parity (P-01..P-04) | **Yes** | - |
| B - Build and test (P-05..P-06) | **Yes** | - |
| C - Install tree (P-07..P-09) | **No** | milestone 0015 Story 01.0 (CLI/GUI install rules); milestone 0012 Story 01.0 (`libsmbios` export) |
| D - Packaging artifacts (P-10..P-11) | **No** | milestone 0012 Story 03.0; milestone 0015 Story 02.0 (MSI) |
| E - Install and uninstall (P-12..P-14) | **No** | milestone 0015 Story 02.0 |
| F - Clean-room consumers (P-15..P-17) | **Partly** - P-17 (`smbios_c` via pkg-config) works today | milestone 0012 Story 01.0 for P-15/P-16 |
| G - Channel dry runs (P-18..P-20) | **No** | milestone 0015 Stories 03.0/04.0 |
| H - Safety and privilege (P-21..P-24) | **Partly** - P-23/P-24 work today | the rest follow group C |

## Prerequisites

Per platform, before starting:

- A clean checkout and `VCPKG_ROOT` set.
- `python3` on `PATH` (every `scripts/ci/` entry point is stdlib-only
  Python).
- **Windows:** Visual Studio 2022 toolchain; an **elevated** shell for MSI
  install/uninstall checks (installing per-machine needs admin; *running*
  the installed programs does not).
- **Linux:** the GUI's X11/OpenGL packages if testing GUI legs
  (`scripts/ci/apt-packages-gui.txt`); `sudo` for live-table checks.
- **macOS:** no privileges needed for any check in this plan.
- A scratch directory outside the repository for install prefixes. Never
  install into the source or build tree - several checks exist precisely to
  prove the artifacts do not depend on it.

## Platform matrix

One column per machine. `-` = not run. Fill in only discrepancies; a
passing check needs no annotation.

| Check | Windows x64 | Linux x64 | macOS x64 |
|-------|-------------|-----------|-----------|
| P-01 stage list runs locally | | | |
| P-02 lint stage | | | |
| P-03 build stage | | | |
| P-04 test stage | | | |
| P-05 preset build | | | |
| P-06 ctest full suite | | | |
| P-07 install tree completeness | | | |
| P-08 install tree, GUI-on | | | |
| P-09 installed CLI runs standalone | | | |
| P-10 package stage artifacts | | | |
| P-11 manifest digests | | | |
| P-12 MSI install / uninstall | | n/a | n/a |
| P-13 MSI feature selection | | n/a | n/a |
| P-14 MSI major upgrade | | n/a | n/a |
| P-15 `find_package(libsmbios)` consumer | | | |
| P-16 consumer **parses** a table | | | |
| P-17 `smbios_c` via pkg-config | | | |
| P-18 Chocolatey dry run | | n/a | n/a |
| P-19 WinGet manifest validate | | n/a | n/a |
| P-20 Homebrew / AUR dry run | n/a | AUR | brew |
| P-21 no setuid / setcap | n/a | | |
| P-22 no test binaries shipped | | | |
| P-23 unprivileged behaviour | | | |
| P-24 no secrets in logs | | | |

## Checks

### Group A - Stage parity (does the pipeline run on your machine?)

ADR 0005 DD5 promises every stage command runs identically on a developer
machine. These four checks are what make that promise real - and they are
the highest-value checks in this plan, because a stage that only works on
a runner cannot be debugged.

#### P-01 Stage commands exist and refuse unknown legs

```bash
python3 scripts/ci/stage.py --help
python3 scripts/ci/stage.py build --leg not-a-real-leg
```

Expected: help lists `lint|build|test|package|verify|publish`; the bogus
leg exits non-zero naming the leg and pointing at `legs.json`. A silent
success or a Python traceback is a discrepancy.

#### P-02 Lint stage

```bash
python3 scripts/ci/stage.py lint --leg linux --reporter plain
```

Expected: completes with one line per passing step. On Windows and macOS
use the leg for that OS. Lint is Linux-only in `legs.json` today, so on
the other two platforms record `n/a` rather than forcing it.

#### P-03 Build stage

```bash
python3 scripts/ci/stage.py build --leg <linux|windows|macos> --reporter plain
```

Expected: configures and builds through the pinned preset; one line per
step; no megabyte log dump. Compare the toolchain versions it prints
against `scripts/ci/pins.json` - a mismatch is the single most common
cause of "works locally, fails on CI" and this is where you catch it.

#### P-04 Test stage

```bash
python3 scripts/ci/stage.py test --leg <linux|windows|macos> --reporter plain
```

Expected: the leg's declared test sets run and pass. On Linux the
live-table set runs under `sudo` per `legs.json`'s `live_table_sudo`;
confirm it actually elevated rather than silently skipping.

### Group B - Build and test the ordinary way

#### P-05 Preset build

```bash
cmake --preset <windows|linux|macos>
cmake --build --preset <windows|linux|macos>
```

Expected: a clean build. Binaries at `build/<preset>/smbios/bin/smbios`
(and `libsmbios/bin/` for test binaries).

#### P-06 Full CTest suite

```bash
ctest --test-dir build/<preset> --output-on-failure
```

Expected: unit tests pass everywhere. Integration and perf suites need
root/admin for the live table - on Linux without `sudo` they must **fail
cleanly, not crash** (that is itself the check).

### Group C - Install tree *(gated: milestone 0015 Story 01.0)*

#### P-07 Install tree completeness, default (GUI-off) build

```bash
cmake --install build/<preset> --prefix /tmp/smbios-prefix
```

Expected, per milestone 0015's contract C1: `bin/smbios[.exe]`,
`share/doc/libbios/LICENSE`, and `smbios_c`'s existing payload
(`lib/`/`bin/` library, `include/smbios/smbios.h`,
`lib/pkgconfig/smbios_c.pc`). The command must **succeed** on a GUI-off
build - no error about a missing GUI target.

#### P-08 Install tree, GUI-on build

Repeat P-07 against a `-DSMBIOS_BUILD_GUI=ON` build. Expected: additionally
`bin/smbios_gui[.exe]`. Expected **absent**: `smbios_gui_autoexit` and any
`*_test` binary.

#### P-09 Installed CLI runs standalone

Run `/tmp/smbios-prefix/bin/smbios --format json` from **outside** the
repository, with the build tree not on `PATH` or any library path.

Expected: parsable JSON (under `sudo` on Linux). If it only works from the
build tree, the install rules are incomplete - which is exactly what the
packaging stage would then ship.

### Group D - Packaging artifacts *(gated: milestones 0012/0015)*

#### P-10 Package stage produces artifacts

```bash
python3 scripts/ci/stage.py package --leg <linux-x64|windows-x64|macos-x64> --reporter plain
```

**Today this exits 2** - the packaging legs are marked `pending` in
`legs.json` until milestone 0012 names the shipped configuration. That
refusal *is* the expected result right now, and worth confirming: a leg
that silently produced an artifact while marked pending would be a bug.

Once unblocked, expected: an archive plus `.sha256`, and on Windows an MSI,
all recorded in `manifest.json`.

#### P-11 Manifest digests match

Recompute the digest of every file `manifest.json` names and compare.

Expected: identical. This is the invariant every publish destination
depends on - they read digests from the manifest and never recompute, so a
drift here propagates to Chocolatey, WinGet and the Homebrew formula
simultaneously.

### Group E - Install and uninstall, Windows *(gated: milestone 0015 Story 02.0)*

#### P-12 MSI install and uninstall

On a machine that does **not** already have the product: install silently,
confirm files land at their declared paths, `smbios --version` resolves
through `PATH` in a **new** shell, and the Start Menu shortcut exists for
the GUI feature. Then uninstall.

Expected after uninstall: no files, no `PATH` entry, no Start Menu entry,
no registry residue.

#### P-13 Feature selection

Install with only the `CLI` feature. Expected: no `smbios_gui.exe`, no
Start Menu shortcut, no SDK files. Repeat selecting `SDK_Static` and
confirm headers and `libsmbiosConfig.cmake` appear.

#### P-14 Major upgrade

Install the previous version, then the current one. Expected: exactly
**one** product registered (not two side by side), files replaced rather
than accumulated, and the previous feature selection carried forward. Two
entries in the installed-programs list means the `UpgradeCode` changed -
a defect that is permanent once released.

### Group F - Clean-room consumers

The point of these is that they must **not** see your build tree. Build
each in a scratch directory with no reference to this repository.

#### P-15 `find_package(libsmbios)` consumer *(gated: milestone 0012 Story 01.0)*

A minimal external CMake project:

```cmake
find_package(libsmbios CONFIG REQUIRED)
target_link_libraries(my_app PRIVATE libsmbios::libsmbios)
```

Expected: configures and links against the installed prefix alone.

#### P-16 The consumer actually **parses** a table *(gated: same)*

Extend P-15's consumer to walk a small synthetic table and assert a
non-null entry for a known structure type.

Expected: a correct parse. **A successful link is not sufficient** - it
passes even when every `REGISTER_SMBIOS_ENTRY` initializer was dropped,
which is precisely the silent `SMBiosEntryFactory::create() == nullptr`
failure the exported target's whole-archive link option exists to prevent.
This is the single most important check in group F.

#### P-17 `smbios_c` via pkg-config *(runnable today)*

```bash
PKG_CONFIG_PATH=/tmp/smbios-prefix/lib/pkgconfig pkg-config --cflags --libs smbios_c
```

Compile a trivial C program against those flags and call the C ABI.
Expected: builds and runs. This path has existed since milestone 0007, so a
failure here is a regression, not a gap.

### Group G - Channel dry runs *(gated: milestone 0015 Stories 03.0/04.0)*

Every destination implements `--dry-run`, which must perform **no network
access and read no secret**. Confirm that, not just that it exits zero.

#### P-18 Chocolatey dry run

```bash
python3 scripts/ci/stage.py publish --destination chocolatey --dry-run
```

Expected: prints the `.nupkg` it would build and the package id/version it
would push. Run it with no API key present - it must still succeed.

#### P-19 WinGet manifest validation

Render the manifests and run the official `winget validate`. Expected:
valid, with `InstallerSha256` equal to the manifest's digest for that MSI.

#### P-20 Homebrew and AUR dry runs

`homebrew-tap` on macOS, `aur` on Linux. Expected: the rendered formula /
`PKGBUILD`, with `sha256` taken from `manifest.json` and no network write.

### Group H - Safety and privilege

#### P-21 No setuid or setcap *(Linux, macOS; gated with group C)*

Inspect every file in the install prefix (and, on Linux, in a built
package). Expected: no setuid bit, no file capabilities. `libsmbios` reads
raw firmware memory; a privileged binary in a package would be a security
defect, and the documented answer is `sudo smbios`.

#### P-22 No test binaries shipped *(gated with group C)*

Expected absent from every install tree, archive and MSI: `*_test`,
`smbios_gui_autoexit`, `read_smbios_table_fuzzer`. The autoexit twin is
compiled with `SMBIOS_GUI_TEST_HOOKS` and must never reach a user.

#### P-23 Unprivileged behaviour *(runnable today)*

Run the CLI **without** elevation on each platform.

- macOS and Windows: expected to work normally - neither needs privileges.
- Linux: expected to fall through the whole source chain and exit
  **cleanly with a diagnostic**, never crash. Confirm the message explains
  the privilege requirement; a user who installed from a package manager
  will read a bare empty result as "broken".

#### P-24 No secrets in output *(runnable today)*

Re-read the output of every stage command you ran. Expected: no token, key
or credential value anywhere, including on failure paths. Also confirm no
real serial number, UUID or asset tag ended up in anything you plan to
paste into a report.

## When to run

- **Before trusting a new or changed leg** - groups A and B.
- **Before the first tagged release** - the whole plan, on all three
  platforms. This is the executed-checklist evidence ADR 0007 D9's
  readiness bar asks for.
- **After any change to install rules, the MSI, or a destination module** -
  the affected group, on at least the platform it touches.
- **Not routinely otherwise.** Everything here that can be automated is
  being automated by milestones 0012, 0015 and 0016; this plan is for the
  parts that need a person and a real machine.
