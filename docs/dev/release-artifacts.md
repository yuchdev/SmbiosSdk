# Release artifacts: naming, verification and supported baselines

What a release of this project contains, how the files are named, how to
check that a download is intact, and which systems the binaries actually
run on.

Everything below describes the artifacts the pipeline's `package` stage
produces (milestone 0012 Story 03.0). The figures are taken from a real
run, not from the specification: run `44074901424` on commit `6cf73e4`.

## What a release contains

Three archives, one per packaging leg, each with a detached checksum
file, plus one manifest describing all three:

```
libbios-1.0.0-linux-x64.tar.gz        libbios-1.0.0-linux-x64.tar.gz.sha256
libbios-1.0.0-macos-arm64.tar.gz      libbios-1.0.0-macos-arm64.tar.gz.sha256
libbios-1.0.0-windows-x64.zip         libbios-1.0.0-windows-x64.zip.sha256
manifest.json
```

### Naming

```
libbios-<version>-<leg>.<ext>
          │        │      └─ .zip on Windows legs, .tar.gz everywhere else
          │        └───────── <os>-<arch>, the packaging leg name
          └────────────────── the project version (a release tag without its leading "v")
```

`<leg>` is the leg name from `scripts/ci/legs.json`, so the filename and
the pipeline job that produced it always agree.

**`<version>` is one authoritative value**, not a per-component number.
It is checked across every place it appears by
`scripts/ci/check_versions.py`, so the archive name, the port manifest
and the release tag cannot drift apart
([ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md)
D5). It is the root `CMakeLists.txt`'s `project(... VERSION X.Y.Z ...)`
(currently `1.0.0`); `SMBIOS_C_VERSION` and `LIBSMBIOS_VERSION` derive from
it through `${PROJECT_VERSION}`. Change it only with
`python3 scripts/release/prepare_release.py X.Y.Z`, which sets it together
with the port's `"version"` and the registry baseline and then runs the
check. A pre-release (`v1.2.0-rc.1`) is a property of the **tag** only:
`<version>` never carries the suffix, so the release candidate's and the
final release's archives share file names but not digests.

**A version is not a compatibility promise.** This project has not
adopted semantic versioning for either `libsmbios` or `smbios_c`, and
deliberately so - see [ADR 0004](/docs/adr/0004-package-manager-distribution.md)
DD2 and milestone 0007's own Non-goals. Two releases differing in the
minor or patch field make no ABI guarantee to each other. The one
boundary that *is* enforced is `libsmbiosConfigVersion.cmake`'s
`SameMajorVersion` check, which refuses a consumer asking for a different
major version.

## What is in an archive

Libraries, headers and package-discovery files - everything a program
needs to *link against* this project:

| Path | Contents |
|---|---|
| `include/libsmbios/` | the C++ API, including `tables/` |
| `include/smbios/` | the per-OS and legacy segment headers |
| `include/smbios_c/` | the generated `smbios_c_export.h` |
| `lib/` | the static `libsmbios` archives and the `smbios_c` shared library |
| `lib/cmake/libsmbios/` | `libsmbiosConfig.cmake`, `libsmbiosConfigVersion.cmake`, `libsmbiosTargets*.cmake` |
| `lib/pkgconfig/smbios_c.pc` | `pkg-config` metadata for the C ABI |

The library filenames differ per platform, as they must:

| Leg | Shared C ABI | Static C++ core | Per-OS / legacy |
|---|---|---|---|
| `linux-x64` | `lib/libsmbios_c.so` | `lib/libsmbios.a` | `lib/libsmbios_linux.a`, `lib/libsmbios_posix_legacy.a` |
| `macos-arm64` | `lib/libsmbios_c.dylib` | `lib/libsmbios.a` | `lib/libsmbios_macos.a`, `lib/libsmbios_posix_legacy.a` |
| `windows-x64` | `bin/smbios_c.dll` + `lib/smbios_c.lib` | `lib/libsmbios.lib` | `lib/libsmbios_win.lib`, `lib/libsmbios_win_legacy.lib` |

On Windows the runtime DLL lives in `bin/` and its import library in
`lib/`, which is the platform convention; `bin/` on that leg contains
**only** that DLL.

### What is deliberately *not* in an archive

**Neither the `smbios` CLI nor the `smbios_gui` application ships in
these artifacts.** There is no `smbios`/`smbios.exe` and no
`smbios_gui`/`smbios_gui.exe` in any leg, and the `package` stage has a
test that fails if one ever appears.

That is a recorded decision, not an oversight: these are *developer*
artifacts, matching ADR 0004's scope of library distribution for
downstream code. The two executables also have no `install()` rule
anywhere in the tree yet, so they cannot be packaged at all today; adding
those rules is milestone 0015 Story 01.0, and end-user distribution of
the CLI and GUI is [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)'s
subject - Chocolatey, WinGet, a Homebrew tap, AUR and a Windows
installer.

## Verifying a download

Two independent checks are published. Use either; they must agree.

### Against the detached `.sha256`

```bash
# Linux
sha256sum -c libbios-1.0.0-linux-x64.tar.gz.sha256
```

```bash
# macOS
shasum -a 256 -c libbios-1.0.0-macos-arm64.tar.gz.sha256
```

```powershell
# Windows PowerShell
(Get-FileHash libbios-1.0.0-windows-x64.zip -Algorithm SHA256).Hash -eq `
  ((Get-Content libbios-1.0.0-windows-x64.zip.sha256) -split '\s+')[0]
```

### Against `manifest.json`

The manifest carries the same digests, so one file verifies the whole
release. This works identically on all three platforms:

```bash
python3 - <<'EOF'
import hashlib, json, pathlib
m = json.load(open("manifest.json"))
for leg in m["legs"]:
    f = pathlib.Path(leg["file"])
    ok = hashlib.sha256(f.read_bytes()).hexdigest() == leg["sha256"]
    print(f"{leg['leg']:13} {'OK' if ok else 'MISMATCH'}  {leg['file']}")
EOF
```

These digests are computed once, by the `package` stage, and every later
consumer reads them from the manifest rather than recomputing - so a
checksum quoted in an announcement, in a Chocolatey `VERIFICATION.txt` or
in a WinGet manifest traces back to the same single value.

### The manifest's fields

```json
{
  "schema": 1,
  "version": "1.0.0",
  "commit": "6cf73e4b6c8e8dd62d0d3d85760b414dafa28742",
  "legs": [
    {
      "leg": "linux-x64",
      "file": "libbios-1.0.0-linux-x64.tar.gz",
      "bytes": 1466181,
      "files": 78,
      "config": "Release",
      "sha256": "4c73cb7b…",
      "install_sha256": "0ad18bff…",
      "arch": "x64",
      "glibc": "2.38",
      "binary_cache": "none"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `commit` | the exact source commit the artifacts were built from |
| `config` | the CMake build configuration shipped (always `Release`) |
| `sha256` | digest of the archive - the value the `.sha256` file repeats |
| `install_sha256` | digest of the **installed tree before archiving** |
| `arch` | CPU architecture of the shipped binary (`x64`, `arm64`; `+`-joined for a universal Mach-O). It is read from the binary, and the `package` stage refuses an archive whose binary does not match its leg name. Manifests from before this field existed (run `44074901424`) do not have it. |
| `glibc` / `msvc` / `macos_min` | the platform floor, read from the built binary itself |
| `binary_cache` | `none` when the build job built every vcpkg dependency from source (`VCPKG_BINARY_SOURCES=clear` and 0 packages restored in its configure log), else `restored`. It is the build job's own `binary_cache_<leg>` output, and a release publishes only legs that say `none` (threat model RP-4, CR-2). Manifests from before Story 04.0 Task 02 do not have it, and cannot be released. |

`install_sha256` is the integrity link between stages: the `package`
stage refuses to archive a tree whose digest differs from the one the
build stage recorded after running the tests. It is what makes "the bytes
we shipped are the bytes we tested" checkable rather than assumed, and it
is verified on every leg of every run.

Nothing downstream trusts a digest that travels inside an artifact, because
any job of the run can replace an artifact (threat model RP-4). The package
stage's manifest job therefore downloads each leg by artifact ID and checks
its archive, `install_sha256` and `binary_cache` against the jobs that
produced them, then exports `manifest_sha256`. The publish stage checks
`manifest.json` against that job output before it parses it.

## Using an extracted archive

Extract the archive anywhere. Nothing in it needs to be installed into a
system prefix.

**C ABI (`smbios_c`) through `pkg-config`.**

```bash
PKG_CONFIG_PATH=<dir>/lib/pkgconfig pkg-config --cflags --libs smbios_c
```

`smbios_c.pc` is relocatable: its `prefix` is `${pcfiledir}/../..`, the
directory two levels above the `.pc` file. The flags therefore point into
wherever you extracted the archive, and into the real prefix of a system
install. At run time the program must also find the shared library:
`-Wl,-rpath,<dir>/lib` on Linux and macOS, or `<dir>/bin` on `PATH` on
Windows.

Confirmed on real artifacts: CI run `44112333813` (commit `57ca646`) is the
first whose archives carry the relocatable prefix, on all three legs, and
the clean-room verifier reports `pkg-config ok` for each - where the
previous run reported `needs --define-prefix (non-relocatable .pc)`. That
run is also the first to carry the per-leg `arch` field.

Archives of `1.0.0` produced before that run (CI run `44074901424`) still
record the build machine's prefix: `/usr/local`, or
`C:/Program Files (x86)/smbios_sdk` on Windows. For those, add
`--define-prefix` (pkg-config 0.29+ and pkgconf).

**C++ (`libsmbios`) through CMake.**

```bash
cmake -S . -B build -DCMAKE_PREFIX_PATH="<dir>;<boost prefix>"
```

Then use `find_package(libsmbios CONFIG REQUIRED)` and
`target_link_libraries(app PRIVATE libsmbios::libsmbios)`. Do not add a
whole-archive flag; the exported target already carries it. **You must
supply Boost.Iostreams yourself.** The package config calls
`find_dependency(Boost COMPONENTS iostreams)`, and the archive does not
contain Boost.

**Checking all of this in one command.** `scripts/ci/verify_release_artifact.py`
runs the steps above in a clean room against one archive:

- the sha256 against the `.sha256` file and `manifest.json`;
- extraction, the layout, and the `install_sha256` of the extracted tree;
- `pkg-config` plus a compiled C program;
- a `find_package(libsmbios)` C++ program.

It refuses to run if any path into the source checkout or a vcpkg root
leaks into the environment or the build:

```bash
python3 scripts/ci/verify_release_artifact.py dist/libbios-1.0.0-linux-x64.tar.gz
python3 scripts/ci/verify_release_artifact.py --dist dist --layout-only   # any host
```

The compile and run checks need a host of the archive's own platform and
architecture. `--layout-only` runs the rest on any host.

## Supported baselines

Each binary's floor is read from the binary itself at package time, not
assumed from the runner. Observed values, from the run cited above:

| Leg | Architecture | Builder image | Floor |
|---|---|---|---|
| `linux-x64` | x86_64 | `ubuntu-24.04` | **glibc 2.38** |
| `windows-x64` | x86_64 | `windows-2022` | **MSVC toolset 14.44** |
| `macos-arm64` | **arm64 only** | `macos-15` | **macOS 15.0** |

Three consequences worth knowing before depending on these files:

- **Linux: glibc 2.38 or newer.** The build image is pinned
  (`scripts/ci/pins.json`), so this floor moves only when that pin moves -
  but a distribution older than roughly Ubuntu 24.04 / Debian 13 will not
  run the shared library. Build from source there.
- **macOS: arm64 only, and macOS 15.0 or newer.** There is no x86_64
  macOS artifact, and the deployment floor follows the pinned runner's
  SDK, which is newer than a typical local build produces (a local build
  on an older Xcode records 12.7). This is a narrower audience than it
  looks; see [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
  for the architecture-coverage plan, including adding `macos-x64` and
  the August 2027 sunset of GitHub's last Intel macOS image.
- **Windows: x86_64 only**, built with the MSVC toolset above. Consumers
  linking the static `libsmbios.lib` must match the CRT model it was
  built with; that choice is still an open question (milestone 0012's
  M4), so prefer `smbios_c.dll` if you only need the C ABI.

`linux-arm64` and `windows-arm64` artifacts do not exist yet. ADR 0007 D2
adds them, along with `macos-x64`.

## Reproducibility

Archiving the same installed tree twice produces a **byte-identical**
archive: entries are sorted, mode bits normalised and timestamps fixed.
A release therefore has one digest, not one per attempt.

The *installed tree* itself is not bit-identical across different build
directories - on Apple platforms the static archives embed a `ranlib`
timestamp - so digests differ between independent builds of the same
commit. That does not affect a release, which is built once.

## See also

- [CI pipeline](ci.md) - the stages that produce these files, and how to run them locally.
- [vcpkg registry](vcpkg-registry.md) - consuming `smbios_c` through the self-hosted port instead of a tarball.
- [ADR 0004](/docs/adr/0004-package-manager-distribution.md) - why `smbios_c` is packaged first, and why there is no semver policy.
- [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) - D4 (artifact layout and manifest), D5 (one version source).
- [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md) - where the CLI and GUI will be distributed instead.
- [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md) - which architectures are built, and which are planned.
