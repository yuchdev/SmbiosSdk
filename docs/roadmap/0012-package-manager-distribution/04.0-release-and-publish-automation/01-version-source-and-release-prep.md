# 01 - Version sites and release preparation

**Status:** ✅ Complete (2026-11-17) - the root `project()` VERSION is authoritative and
`prepare_release.py` exists; the licence and `.pc` items were already met by Stories 02.0 and
03.0. Threat-model requirement 1 (the RP-3 `1.0.0` collision ruling) is the maintainer's and
must be recorded before Task 03 starts - see As built

## Objective

Implement [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) D5 for this milestone's version sites, and resolve the blocking decisions (licence, `.pc` prefix). The consistency *check* itself (`scripts/ci/check_versions.py`, run in `lint` and on tags) is owned by milestone 0014 (SC3). This task only adds rows and derivations.

## Files

- **Modify** root `CMakeLists.txt`. Add `project(... VERSION X.Y.Z)` as the one authoritative value, and derive `SMBIOS_C_VERSION` (`smbios_c/CMakeLists.txt`) and `LIBSMBIOS_VERSION` (Story 01.0) from `PROJECT_VERSION`. This is a product-build change handed to `cpp-expert`.
- **Modify** `scripts/ci/version_sites.json` (owned by 0014). Add `equal` rows for `ports/smbios-c/vcpkg.json` `"version"` and the registry `versions/baseline.json` entry, and `derived` rows for the two CMake variables.
- **Create** `scripts/release/prepare_release.py`. It bumps the authoritative value and every `equal` site from one argument, then runs `check_versions.py`. The registry update itself is Task 03.
- **Create** `LICENSE` (the maintainer's chosen licence), per review finding H3.
- **Modify** `smbios_c/smbios_c.pc.in` (or the install step) so that the installed `.pc` is relocatable, per finding H1. This needs an explicit exception to plan.md's "pc unchanged" constraint, recorded in `status.md`.

## Success criteria

- [x] `check_versions.py` (0014) passes on the tree, and fails with a discrepancy line when any one site is edited alone.
      *`ok: versions: 1.0.0 (root project() VERSION), 4 sites`; each of the five sites edited
      alone fails (`SingleSiteEditTests`), and was shown by hand on the real tree.*
- [x] An extracted archive's `pkg-config --cflags --libs smbios_c` works from any extraction directory with no configure-time prefix present.
      *Met before this task, by `c25a928` (Story 03.0 Task 03, H1); unchanged here.*
- [x] `LICENSE` exists and the port's copyright step resolves it.
      *Met before this task, by Story 02.0 (MIT; registry `vcpkg install` on `x64-osx`).*

## As built (2026-11-17)

**What this task changed.** Three coupled CMake edits, which have to land together because
`check_versions.py` rejects a literal in a `derived` site once the authoritative value exists:

- root `CMakeLists.txt`: `project(smbios_sdk VERSION 1.0.0 LANGUAGES CXX)`;
- `smbios_c/CMakeLists.txt`: `set(SMBIOS_C_VERSION ${PROJECT_VERSION})`;
- `libsmbios/CMakeLists.txt`: `set(LIBSMBIOS_VERSION ${PROJECT_VERSION})`.

`1.0.0` is not a new number: both CMake variables, the port, the registry baseline and the
published `libbios-1.0.0-*` archives already said it. The lint line moved from
`1.0.0 (SMBIOS_C_VERSION, interim)` to `1.0.0 (root project() VERSION)`. `PROJECT_VERSION`
is used, not `CMAKE_PROJECT_VERSION`, so the value stays this project's own when the tree is
consumed through `add_subdirectory`. Neither `smbios_c/` nor `libsmbios/` calls `project()`;
the test subdirectories do, but they read neither variable.

**Derivation proven by configure, not assumed.** Re-configuring `build/macos-arm64` left the
generated `smbios_c.pc` and `libsmbiosConfigVersion.cmake` byte-identical (`Version: 1.0.0`,
`PACKAGE_VERSION "1.0.0"`, `prefix=${pcfiledir}/../..`). A scratch worktree bumped to `2.3.4`
then configured to `Version: 2.3.4` and `PACKAGE_VERSION "2.3.4"`, with the relocatable prefix
intact.

**`scripts/release/prepare_release.py`** (stdlib only). It takes `X.Y.Z` and sets the
authoritative site plus every `equal` site of `version_sites.json` (today the port's
`"version"` and `versions/baseline.json`). It never touches `derived` sites. Then it runs
`check_versions.check()`. Rules:

- **Edits are planned before any write.** An uneditable site (missing, a `${...}` in an
  `equal` site, JSON that would not re-serialise byte-identically) aborts with nothing
  written. Commented-out `project()`/`set()` calls are masked with the checker's own comment
  rules. Line endings and file modes are kept, and each write is atomic.
- **Idempotent.** If every site already holds the version, nothing is written and the dirty
  check is skipped. A re-run, or a resumed interrupted run, converges.
- **Dirty files are refused.** A file it would write that has staged, unstaged or untracked
  changes is refused (one read-only `git status` per file). It also refuses outside a git
  work tree, and refuses a version lower than the current one.
- **It never** commits, tags, pushes, runs `x-add-version`, or edits
  `versions/s-/smbios-c.json`, `port-version` or the portfile `REF` (threat-model Task 01
  requirement 3). An AST test pins that its only child process is `git ... status`.

**Pre-release decision (threat model RP-5; Task 01 requirement 2): refused.** A
`-<pre>` argument exits 2 rather than being truncated to `X.Y.Z`. There are three reasons:

1. CMake rejects it (`VERSION "1.0.0-rc.1" format invalid`, checked), and `equal` sites must
   equal the authoritative value.
2. Writing the base instead would register the final version from a candidate (constraint 8).
3. A pre-release is a property of the **tag** only: prepare `X.Y.Z`, then tag `vX.Y.Z-rc.N`,
   which `check_versions.py --tag` already accepts.

So the D4 `<version>` never carries the suffix, and no ADR 0005 D4/D5 amendment is needed.
RP-5's consequence is accepted as stated there: an RC and its final release have archives with
equal names and different digests.

**Consequence for the registry procedure (for Task 03).** Bumping on a real checkout
(`1.0.0 -> 1.1.0`) leaves `check_versions` green, but the lint `registry` step fails with
`versions/s-/smbios-c.json: newest version differs from the port version` until
`x-add-version` adds the entry. Because D5 now requires the port version and the baseline to
equal the root VERSION in every commit, the bump travels with the source commit. The registry
procedure's step 1 ("merge the source change to `master`") would therefore leave `master` red.
That step is recorded in `docs/dev/vcpkg-registry.md`; Task 03 owns the fix.

**Already satisfied elsewhere, not changed here:**

- `LICENSE` (Story 02.0).
- The relocatable `.pc` (`c25a928`; verified on a locally packaged archive in Story 03.0
  Task 03. The CI archives on record, run 44074901424, predate it). That ruling is also the
  explicit exception to plan.md's "`smbios_c.pc` path must keep working unchanged" constraint,
  now noted in `status.md`. The port is unaffected, because `vcpkg_fixup_pkgconfig()` rewrites
  `prefix=` itself.
- All four `version_sites.json` rows, which were already present. The file is unchanged. Its
  `interim_authoritative` key is now inert: the root VERSION exists, and a derived stand-in
  could not supply a literal anyway.

**Tests:** `scripts/ci/tests/test_prepare_release.py`, 23 cases on temporary copies of the
real site files and sites file. They cover a round-trip bump, idempotency and resume, refused
bad, hostile, pre-release and downgrade versions (nothing written), dirty targets (stubbed,
and real `git`, staged and unstaged), success criterion 1 for each of the five sites, the
never-tags AST check, the real repository being a no-op, and the CLI.

**Docs:** `docs/dev/release-artifacts.md` (version source, pre-release rule) and
`docs/dev/vcpkg-registry.md` step 2 (the stale "keep it equal to `SMBIOS_C_VERSION`"). Also
the `scripts/ci/stage.py` package docstring.
