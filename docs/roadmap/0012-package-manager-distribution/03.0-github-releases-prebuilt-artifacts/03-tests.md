# 03 - Tests

**Parent story:** [README.md](README.md)
**Status:** ✅ Complete (2026-11-18) - verifier and its tests delivered, and Story 04.0 Task 04's
`verify` stage ran them with `build=True` on each leg's own runner, plus a sixth check (the export
allowlist). **Success criterion 1 is met: CI run 44290476729 is green on all three `verify` legs**
(`linux-x64`, `macos-arm64`, `windows-x64`) - see As built and the 2026-11-18 note

## Objective

Confirm the GitHub Releases baseline works end to end from a genuinely
clean machine, closing out Story 03.0 and milestone 0012 - directly
re-running ADR 0004's own Validation/Rollout item: "The GitHub Releases
baseline (option 3) is validated by downloading a tagged release's
artifact on a clean machine with no vcpkg/Conan/this repo's source
present and confirming `pkg-config --cflags --libs smbios_c` plus a
compile/link/run smoke test succeeds from the extracted tarball alone."

This is a **packaging-correctness verification task, not a unit test** -
no Boost.Test case is added; verification happens against a downloaded
artifact in a disposable environment (a fresh CI container/VM, or a
local scratch machine during implementation), never against this
repo's own build tree.

## Files

No new test source beyond a verification script (location decided during
implementation, e.g. `scripts/verify_release_artifact.sh`/`.ps1`) that:
1. Downloads (or accepts a path to) a release tarball/zip.
2. Extracts it into an empty directory.
3. Runs `pkg-config --cflags --libs smbios_c` against the extracted
   `lib/pkgconfig/smbios_c.pc` and compiles/links/runs a trivial C
   program against the result (reusing milestone 0007's own verification
   shape).
4. Runs a second smoke test against the extracted `libsmbios` package
   config (Story 01.0) - a trivial C++ program using
   `find_package(libsmbios CONFIG)` against the extracted
   `lib/cmake/libsmbios/` directory via `CMAKE_PREFIX_PATH`.
5. Verifies the `.sha256` checksum (Task 02) matches the downloaded
   artifact.

## Success criteria

- [x] On each of Windows, Linux, and macOS, a released artifact
      extracted on a clean machine (no vcpkg, no Conan, no checkout of
      this repo's source) passes all four checks above.
      **Met 2026-11-18 by CI run 44290476729**, green on all three `verify` legs, each
      verifying its own leg's archive on a runner of that leg's own OS and architecture:
      `ok: verify linux-x64: 7 files = manifest b7b4235589cb; tree 78 = install; pkg-config ok;
      exports 14 ok; c: no-table; cxx: formatters=4`, the same for `macos-arm64`
      (`exports 13 ok`), and `windows-x64` with `PATH -2 vcpkg; tree 81 = install;
      pkg-config ok; exports 13 ok; c: ok structures=14; cxx: formatters=4`. Three details
      worth recording: the Windows clean room needed **two** vcpkg `PATH` entries removed (the
      image's `C:\vcpkg` and Visual Studio's own bundled one); Linux reports **14** exports
      against 13 elsewhere because ELF counts the `SMBIOS_C_1.0` version tag the linker script
      adds, which was independently confirmed not to be a leaked symbol; and the C check found a
      real table only on Windows (`structures=14`), while Linux and macOS degraded cleanly to
      `no-table` on virtualized firmware - both outcomes are a pass per `smbios_open()`'s
      documented behaviour. Wired by Story 04.0 Task 04 (2026-11-18).
      *Historical note, kept:* `stage.py verify` now runs this verifier with `build=True` and a relocatable `.pc`
      required, on `ubuntu-24.04`, `windows-2022` and `macos-15` (arm64), each verifying its own
      leg's archive from the same run's `dist/`, extracted into an empty directory, with Boost
      from a separate prefix and no vcpkg or checkout reachable from the consumer. Locally the
      stage was run against a real arm64 archive packaged on this x86_64 Mac (layout, digests,
      literal `pkg-config` and the new export check pass; build/run `NOT EXECUTED`: wrong
      architecture). Earlier record: checks 1, 2 and 5
      plus the layout and `pkg-config` resolution pass on all three CI
      artifacts of run 44074901424; the compile/link/run checks ran only
      on this host's leg (macOS x64, a locally packaged archive). No
      Linux or Windows host and no arm64 macOS was available, so those
      were **not executed**, not passed. The CI archives also predate the
      H1 fix: their literal `pkg-config` command still needs
      `--define-prefix`. Archives packaged after the fix resolve with the
      literal command (see As built).
- [x] The `smbios_c` smoke test produces correct output against real
      SMBIOS data (or degrades cleanly to "no table found," matching
      `smbios_open()`'s documented behavior).
      *Real data on macOS x64 (`smbios_open=ok structures=32`); the
      `no-table` degradation is accepted by the verifier and pinned by a
      unit test. Other legs: with criterion 1.*
- [x] The `libsmbios` smoke test links successfully with **no**
      whole-archive flag of its own (re-confirming Story 01.0's Task 03
      result, this time from a release tarball rather than a local
      `cmake --install`).
      *macOS x64 (`formatters=4 type17=Memory Device fields=15`); the
      verifier refuses a fixture that gains a whole-archive line. Other
      legs: with criterion 1.*
- [x] `python scripts/check_doc_links.py docs/` reports zero problems.
- [x] This story's, and milestone 0012's overall, `status.md` are
      updated to reflect every story's final state.

## As built (2026-11-17)

**The verifier is `scripts/ci/verify_release_artifact.py`**, not the
`.sh`/`.ps1` pair this spec suggested: one stdlib-only Python module
works on all three OSes and follows the house pattern of
`consumer_smoke.py`. It is a library first. `run()` returns value-free
summary lines and raises `VerifyError`, whose first line names the
failing check. It prints nothing (P11). A thin `main()` reports through
`ci_step` for manual use. Tests: `scripts/ci/tests/test_verify_release_artifact.py`
(57 cases, synthetic archives built by `package_archive` itself, every
child process mocked).

**No new fixture.** Check 3 compiles
`tests/packaging/smbios_c_vcpkg_consumer/main.c` from the raw
`pkg-config` flags alone (its `CMakeLists.txt` is not used). Check 4
builds `tests/packaging/libsmbios_consumer/` unchanged. Both are copied
into the scratch directory first, so no compile line names a source
path in this repository.

**What each check does:**

1. Takes the archive by path (offline), or resolves it from a `dist/`
   directory through `manifest.json` (`--dist DIR --leg LEG`).
   **Deviation:** there is no download path. Fetching belongs to the
   caller (the `verify` stage downloads by artifact ID), and keeping
   the network out keeps the module offline and testable.
2. Extracts the archive into a freshly created `prefix/`. Only regular
   files and directories are written. Names must be plain relative
   paths, member count and size are bounded, and modes are normalised.
   The extracted tree must then hold `package_archive.REQUIRED` (a
   missing `lib/pkgconfig/smbios_c.pc` or `lib/cmake/libsmbios/` fails
   with that path in the message) and no CLI/GUI. **Its digest must
   equal the manifest's `install_sha256` and its file count the
   manifest's `files`.** This goes beyond the spec: it proves that the
   extracted bytes are the bytes the build stage tested.
3. Runs `pkg-config --cflags --libs smbios_c` with `PKG_CONFIG_LIBDIR`
   set to the extracted `lib/pkgconfig` only. Every `-I`/`-L` must lie
   inside the extracted archive, and only `-I`/`-L`/`-l` are accepted.
   It then compiles, links (with an `-rpath` to the extracted `lib`) and
   runs `main.c`. Either `smbios_open=ok structures=<n>` or
   `smbios_open=no-table` is a pass (criterion 2). Any other output or
   exit status fails.
4. Configures `libsmbios_consumer` with `CMAKE_PREFIX_PATH` = the
   extracted prefix, and with both CMake package registries off. It
   requires `libsmbios_DIR` in `CMakeCache.txt` to be the extracted
   `lib/cmake/libsmbios/`. It builds with `--verbose` and runs the
   result. It also refuses a fixture whose CMake code (comments
   stripped) gains a whole-archive flag. Otherwise check 4 would not
   prove DD1.
5. Recomputes the archive's SHA-256 once and compares it with the
   `.sha256` sidecar **and** with the manifest entry (`file`, `sha256`,
   `bytes`, `version`). **This runs first**, so nothing is extracted
   before its bytes are known to be the published ones. The spec's
   numbering is not an order.

**The clean room is enforced, not assumed.** Child processes get an
allowlisted environment, so `VCPKG_*`, `PKG_CONFIG_*`, `CMAKE_*`,
`CPATH`, `LIBRARY_PATH`, `DYLD_*`/`LD_*`, `*FLAGS` and package hints
(`BOOST_ROOT`, `libsmbios_DIR`, ...) never reach them. The house CR-5
`child_environment` filter is applied on top. Then the run is
**asserted**. It fails, naming the variable or artifact but never the
value, if any of these mentions this repository or a parent-environment
vcpkg root (`VCPKG_ROOT`, `VCPKG_INSTALLATION_ROOT`):

- a surviving environment value (`PATH`, `CC`, `INCLUDE`, ...);
- the scratch directory or a dependency prefix;
- the resolved `pkg-config` output;
- the C compile/link line;
- the CMake configure line or output;
- the verbose build output;
- `CMakeCache.txt`, `build.ninja`, `link.txt`, `flags.make` or any
  `*.vcxproj`.

Exercised for real on this host, with `VCPKG_ROOT=~/.vcpkg` set:

- `CC` pointing into the repo fails;
- a `PATH` entry under `build/` fails;
- `--dependency-prefix` set to the repo's `vcpkg_installed/x64-osx` or
  to `$VCPKG_ROOT/installed/x64-osx` fails;
- a `--work-dir` inside the repo fails, and creates nothing;
- `PKG_CONFIG_PATH`, `CMAKE_PREFIX_PATH`, `CPATH` and `LIBRARY_PATH` all
  pointed into the build tree are scrubbed, and the run still passes
  from the archive alone.

**What ran, on what.** This host is macOS 12.7 on **x86_64**, not arm64
(AppleClang 14, pkg-config 0.29.2, CMake 3.31.12, no Docker/Podman).

| Archive | sha256 + manifest | extract + layout + install digest | `pkg-config` resolution | C compile/link/run | C++ `find_package` build/run |
|---|---|---|---|---|---|
| CI `libbios-1.0.0-macos-arm64.tar.gz` | pass | pass (78 files) | pass with `--define-prefix` only | **not executed** (arm64 and macOS 15.0 floor; x86_64 macOS 12.7 host) | **not executed** |
| CI `libbios-1.0.0-linux-x64.tar.gz` | pass | pass (78 files) | pass with `--define-prefix` only | **not executed** (no Linux host) | **not executed** |
| CI `libbios-1.0.0-windows-x64.zip` | pass | pass (81 files) | pass with `--define-prefix` only | **not executed** (no Windows host) | **not executed** |
| local `libbios-1.0.0-macos-x64.tar.gz` (re-packaged after the H1 fix) | pass | pass (78 files) | **pass with the literal command** (`--require-relocatable-pc`) | **pass**, `smbios_open=ok structures=32` | **pass**, `formatters=4 type17=Memory Device fields=15` |

The CI artifacts are from run 44074901424 on `6cf73e4`. The local
archive was produced by `package_archive` (the package stage's own code)
from a `Release` `cmake --install` of `build/macos-arm64`. Despite its
name, that tree is an x86_64 `x64-osx` build. The archive was named
`macos-x64` because that is what it contains.

The C++ consumer found Boost 1.76 from MacPorts (`/opt/local`), not from
vcpkg. A release consumer must supply Boost itself: `libsmbiosConfig.cmake`
calls `find_dependency(Boost COMPONENTS iostreams)`, which is ADR 0004's
named "no dependency-graph integration" consequence.

As a manual extra, outside the script, the C check was cross-linked
against the **CI arm64** archive (`cc -arch arm64` with the
`--define-prefix` flags). It linked (`Mach-O 64-bit executable arm64`,
with a "built for newer macOS 15.0" linker warning) but cannot run here
(`bad CPU type`).

**Finding 1, H1 - resolved the same day on the maintainer's ruling.**
The ADR's literal command failed from every CI release archive. All
three `smbios_c.pc` files carried the build's configure-time prefix:
`/usr/local` on Linux and macOS, and `C:/Program Files (x86)/smbios_sdk`
on Windows, which contains spaces and does not even split into flags. So
`pkg-config --cflags --libs smbios_c` resolved outside the extracted
tarball.

**Fix:** `smbios_c/smbios_c.pc.in` now says `prefix=${pcfiledir}/../..`.
The existing `configure_file(... @ONLY)` keeps `${pcfiledir}` verbatim.
That `@ONLY` is now commented as load-bearing and pinned by a test,
because without it CMake would write `prefix=/../..`.

After re-packaging, the literal command with only `PKG_CONFIG_LIBDIR`
set printed:

```
-I<x>/prefix/lib/pkgconfig/../../include -L<x>/prefix/lib/pkgconfig/../../lib -lsmbios_c
```

That resolves to the extracted `prefix/include` and `prefix/lib`. The
verifier now expects the literal command. `--define-prefix` is only a
reported fallback (`pkg-config needs --define-prefix (non-relocatable
.pc)`) for archives packaged before the fix, and
`--require-relocatable-pc` (off by default) makes it a failure.

The vcpkg port is unaffected: `vcpkg_fixup_pkgconfig()` replaces any
`prefix=` line with its own `${pcfiledir}`-relative one.

**Finding 2 - this host's `--leg macos-arm64` tree is x86_64. Fixed by
an architecture guard.** `package_archive.platform_meta(src, leg)` now
reads the CPU architecture from the installed `smbios_c` binary:

- ELF `e_machine`;
- Mach-O `cputype` of every slice;
- PE `Machine`.

It refuses a mismatch with the leg, naming both. On the real tree it
printed `architecture mismatch: lib/libsmbios_c.dylib is built for x64,
but the leg macos-arm64 is arm64`. `manifest.json` records the value as
a required per-leg `arch` field, and the verifier checks it when present.

**2026-11-18, Story 04.0 Task 04.** The `verify` stage now exists
([Task 04](../04.0-release-and-publish-automation/04-post-publish-verification.md)). Its
`artifact` step calls `run(archive, work_dir, build=True, require_relocatable_pc=True,
dependency_prefixes=[<Boost prefix>])` on each packaging leg's own runner. A runner of another
leg is a failure on CI, never a layout-only pass. It added **check 6**, the export allowlist of
the extracted `smbios_c` library (`nm`; `dumpbin /exports` on Windows). Boost comes from a
release-only `boost-iostreams` prefix that the pinned vcpkg builds cold outside the checkout and
`VCPKG_ROOT`, and `pkg-config` on Windows from a hash-pinned `pkgconf` wheel. **Criterion 1 is
still unchecked:** nothing has run on a hosted runner, because nothing was pushed. It closes,
and this story with it, when the first CI `verify` run is green on `linux-x64`,
`windows-x64` and `macos-arm64`, with the three `ok: verify <leg>` lines recorded here.

**Not done here, by instruction (2026-11-17):** no `verify` stage, job or workflow.
`stage.py verify` still exits 2. Story 04.0 Task 02 wires the stage by
calling `verify_release_artifact.run(archive, work_dir, ...)` on each
leg's own runner, where `build=True` is meaningful. It must provide
`pkg-config`/`pkgconf` and a non-vcpkg Boost (`--dependency-prefix`
if it is not on CMake's default path), and it must pass a scratch
directory outside the checkout. **Criterion 1 completes there.**
