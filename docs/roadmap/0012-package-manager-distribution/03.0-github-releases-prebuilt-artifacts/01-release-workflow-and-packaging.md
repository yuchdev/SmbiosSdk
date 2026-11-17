# 01 - Package stage: archive the tested install tree

**Parent story:** [README.md](README.md)
**Status:** ✅ Complete (2026-11-17; wired into `ci.yml`, first CI run pending - see As built)
**Depends on:** milestone 0014 Story 03.0 Task 01 (`stage.py`, the
install-tree export SC2)

> **Amended 2026-11-12 by [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md).**
> The original version of this task specified a standalone tag-triggered
> `release.yml` that rebuilt on `ubuntu-latest`/`macos-latest` and uploaded
> with a marketplace action. That design is withdrawn, for three reasons:
>
> - It broke milestone 0014's policy rules P3 (floating labels), P6/P10
>   (expressions in `run:`), P7 (multi-line shell) and P13 (an action
>   where a script suffices).
> - It shipped a build other than the tested one (ADR 0005 DD1).
> - It raced three uploads into one release (review M2).
>
> Uploading is now the `publish` stage (Story 04.0 Task 02).

## Objective

Implement `python3 scripts/ci/stage.py package --leg <leg>`. For one
packaging leg (`linux-x64`, `windows-x64`, `macos-arm64` - see 0014
`legs.json`), it takes the build stage's tested
`$CI_ARTIFACT_DIR/install/<leg>/` tree and produces the ADR 0005 D4 `dist/`
entries. It never configures or builds.

## Files

- **Create** `scripts/ci/package_archive.py` (stdlib only). It:
  - archives `install/<leg>/` as `.zip` on Windows legs and `.tar.gz`
    elsewhere, with sorted entries, normalised mode bits and a fixed mtime;
  - writes the `.sha256` file;
  - merges its leg into `dist/manifest.json` (fields per ADR 0005 D4,
    including the glibc / macOS minimum / MSVC toolset and the build
    configuration).
- **Modify** `scripts/ci/stage.py`: register the `package` stage. Its steps
  go through `ci_step` (RC1-RC3).
- **Create** `.github/workflows/stage-package.yml`: reusable, one
  `stage.py package` step per leg, plus artifact download/upload of
  `install/<leg>` and `dist/`. Every convention (pins, permissions,
  neutral `CI_*` env, step shape) is defined by ADR 0005 and 0014, and the
  policy gate checks it.

## Functions and Classes

```python
def archive_tree(src: Path, dest: Path, fmt: Literal["zip", "tar.gz"]) -> Path: ...
def sha256_file(path: Path) -> str: ...
def update_manifest(manifest: Path, leg: str, archive: Path, meta: Mapping[str, str]) -> None: ...
```

## Constraints

- **No rebuild and no hand-assembled files**: the archive content is
  exactly the `cmake --install` tree (ADR 0004 option 3, ADR 0005 D1).
- **The shipped build configuration is decided in this story** and
  recorded in `status.md` before implementation. The build stage's
  packaging legs then build and test that configuration (0014 SC2).
- **Whether the `smbios` CLI ships** in the tree is an open maintainer
  question (ADR 0005 Open questions 3). If the answer is yes, the CLI's
  `install()` rule is a scope addition to Story 01.0 Task 01, not part of
  this task.

## Tests

- `scripts/ci/tests/test_package_archive.py`: archiving the same tree twice
  gives byte-identical archives; the manifest records the correct digest;
  a missing `install/<leg>` exits 2 with one RC1 line.
- End-to-end: covered by Story 04.0's `verify` stage.

## Success criteria

- [x] On a PR to `master`, `package` produces three archives, three
      `.sha256` files and one manifest from the tested trees, with no
      configure/build step in the stage. **Wired** (`ci.yml` `package` job;
      three per-leg jobs and a manifest job), proven locally and by the
      merge test, and **observed on a real CI run**: run 44074901424 on
      `6cf73e4` (push to `master`, full tier) was green across all 18 jobs,
      including `build (linux-x64 | windows-x64 | macos-arm64)` and
      `package (linux-x64 | windows-x64 | macos-arm64 | manifest)`. The
      Release-only test bug that blocked it is fixed (`60f8eab`).
- [x] Each archive contains `smbios_c`'s library/headers/`.pc` and
      `libsmbios`'s installed library/headers/package config, confirmed by
      listing the archive contents (macOS locally, and all three legs from
      the CI `dist` artifact); the stage itself refuses a tree without them.
- [x] `stage.py package` runs locally against a local `install/<leg>/`
      with `CI_REPORTER=plain`.

## As built (2026-11-17)

- **Rulings applied** ([status.md](../status.md) 2026-11-16). (1) The
  packaging legs `linux-x64`/`windows-x64`/`macos-arm64` in
  `scripts/ci/legs.json` ship `Release` (no longer `pending`). This **differs
  by design** from the `-C Debug` that 0014 C06 tests on the plain `windows`
  leg; the `legs.json` `about` text says so. Because a Ninja preset ignores
  `--config`, `stage.py build` now configures a packaging leg into its own
  `build/<leg>` with `-DCMAKE_BUILD_TYPE=<config>` and builds and installs it
  with `--config <config>`. Without this, a Linux/macOS tree labelled
  `Release` would have been the Debug tree. (2) **Libraries only.**
  `package_archive.REQUIRED` lists the `libsmbios` and `smbios_c` files.
  `check_contents` refuses a tree with `smbios`/`smbios.exe`/`smbios_gui*`.
  `test_package_archive.py` asserts this, and also asserts that neither
  `smbios/` nor `smbios_gui/` has an `install()` rule. When 0015 Story 01.0
  adds one, both checks fail on purpose.
- **Stage.** `stage.py package --leg <leg>` runs the steps `env`, `unpack`
  and `archive`; `manifest` runs only with `--only manifest` (see CI wiring
  below). `archive` reads only `$CI_ARTIFACT_DIR/install/<leg>/`; if that
  tree is missing, it prints one RC1 line naming the path and exits 2. It
  re-checks the tree with the SC2 digest (no symlinks, no `Testing`). The
  version comes from `check_versions.check()`: today that is the interim
  `SMBIOS_C_VERSION` (1.0.0), so this adds no new version literal and no
  `version_sites.json` row, and a version-site mismatch fails the step. The
  step writes a reproducible `dist/libbios-<version>-<leg>.<tar.gz|zip>`
  (sorted entries, modes `0644`/`0755`, mtime 1980-01-01, owner `0:0`, gzip
  header without a name or mtime) and its `.sha256` in `sha256sum -c` form.
  It then merges the leg into `dist/manifest.json` (D4 fields plus
  `install_sha256` and `files`). The platform minimum is read from the
  installed `smbios_c` binary itself, not from the runner: `glibc` (highest
  `GLIBC_x.y` in the ELF), `macos_min` (Mach-O `LC_BUILD_VERSION`) or `msvc`
  (PE linker version). Entries sit at the archive root (`lib/`, `include/`),
  with no top-level directory, so that Task 03 can extract and point
  `pkg-config`/`CMAKE_PREFIX_PATH` at the extraction directory.
- **CI wiring (2026-11-17, second pass).** The coordinator corrected the
  briefing: the plan's Key constraints allow a job in the entry workflow.
  - *Build stage.* The packaging legs are in `tiers.full` only, never in
    `push_branch`, so they build, test and package on every event except a
    push to a non-`master` branch (D7). They are also in `stages.test`. The
    test stage runs `unit` (`unit_tests: true`) against the Release tree, then
    `cli-formats` (under sudo on `linux-x64`, as on the plain Linux leg), then
    `consumer`. `stage-build.yml` maps the packaging legs to the pinned
    runners. Its Windows guards now cover `windows-x64`, and its new `Install`
    step runs `build --only install`.
  - *Hand-off.* Policy P15 allows only literal file paths, and it is
    unchanged. So `install` also writes `install/<leg>/` as one uncompressed
    `install-<leg>.tar` (deterministic, plumbing only), which is uploaded as
    `install-<leg>` with 1-day retention. The build job exports two unique
    outputs per leg, `install_sha256_<leg>` (set by `stage.py`) and
    `install_artifact_<leg>` (the upload's `artifact-id`). Unique names are the
    documented way to combine matrix outputs without one leg overwriting
    another.
  - *Package stage.* The `package` job in `ci.yml` (`needs: [plan, build]`,
    `if: needs.plan.outputs.package != '[]'`) builds a JSON list of
    `{leg, install_sha256, artifact_id}` from those outputs. `stage-package.yml`
    turns that list into one job per leg, and the digest reaches the stage as
    `CI_INSTALL_SHA256` through `matrix.*` (the only route P10 allows; it is a
    new neutral name in `env_names.py` and the ADR 0005 table). Each per-leg
    job downloads the hand-off **by artifact ID** (CR-8). Its new `unpack`
    step extracts the tar safely and **requires the tree digest to equal the
    build job's**, and a mismatch fails the job. On CI the expected digest is
    required, and an existing tree is never a fallback. The job then runs
    `archive` (the product archive is unchanged) and uploads `dist-<leg>`. A
    final `manifest` job runs `stage.py package --only manifest`. It merges
    the three one-leg parts into the one `dist/manifest.json`, re-checks each
    archive against the digest in its part and copies that digest rather than
    recomputing it, then uploads `dist` (seven literal paths).
    `stage.UNWIRED_STAGES` and the special-case tier helper are gone. The plan
    now schedules `package` with its build legs, and the run report's
    `package` row reads `run: result in its own jobs`, never PASS.
  - *Tests changed by the wiring, and why.*
    - `test_run_report` (4 cases): the packaging legs share OS columns with
      the plain build legs, so a cell became `PASS, PASS`. The packaging legs
      now get their own `build (packaging)` row.
    - `test_parity_check.test_removed_windows_test_step_is_missing`:
      `parity_check` narrowed only a single `matrix.leg` comparison, so with
      the compound Windows guards it counted the POSIX `Test` step as running
      on the Windows leg, which would hide a deleted Windows test step.
      `step_applies` now evaluates pure `&&`/`||` chains of `matrix.leg`
      comparisons, with a new unit test.
    - `test_run_report.test_UndefinedStagesAreReasonRows`: `package` is now
      scheduled on a tag.
    - `test_stage.test_TierTable` and
      `test_classify_changes.TierTableTests` (`FULL_BUILD`, the same-repo PR
      row, `package` now `run`): these hard-code the full tier, which D7
      deliberately extends with the packaging legs.
    - `test_stage.test_WorkflowRunnerLabelsArePinned`: its runs-on parser
      rejected digits in a leg name (`windows-x64`).
    - `test_stage.test_EveryRunLineIsOneStageCommand`: the allowed run lines
      are now `package` and `package --only manifest`.
    - `test_plan` (`test_EveryOutputIsOneJsonLine`,
      `test_SameRepoPrSkipsThePushTierLegs`): same full-tier change.
    - `test_log_budget.test_every_full_tier_leg_has_a_baseline`: every
      full-tier leg needs a `log_baseline.json` entry. The packaging legs map
      to the old `build` job of their OS, whose steps they run plus one
      install line.
- **Blocker found by item 4 (not fixed; milestone 0007's file).** The first
  Release run of the unit set (`stage.py test --leg macos-arm64`) fails:
  `smbios_c_typed_getters_test` aborts with `std::length_error: basic_string`.
  The cause is a test bug. `tests/c_abi/smbios_typed_getters_test.cpp` makes
  calls with side effects inside `assert(...)`:
  `assert(smbios_entry_at(handle, reference_index, &entry) == SMBIOS_OK)`
  (line 94) and `assert(smbios_entry_count(...))` (line 84). Under `NDEBUG`
  those calls are compiled out, so `entry` stays `nullptr`. `fetch()` then
  builds `std::string(buf.data(), needed - 1)` with `needed == 0`. The fix
  belongs to milestone 0007: call outside the `assert` and check the result.
  This is exactly what "ship what you tested" exists to catch. Until it is
  fixed, the packaging legs' `test` step fails on CI, so `package` never
  starts there. The other packaging-leg steps passed locally (`cli formats`;
  `consumer smoke: formatters=4, type17=Memory Device, fields=15`).
- **Not done yet.** The `package` jobs publish no summary artifact (the report
  row only says where the result is), and `binary_cache` (CR-2/D9) has no
  channel into the manifest yet (Story 04.0).
- **Install-tree reproducibility, evidence for H1 (recorded, not changed).**
  The coordinator's run produced install digest `4268076e…` (archive 838070
  bytes) against my `b6c6ecc2…` (838069 bytes) from the same source. My second
  install from the same `build/macos-arm64`, into a different prefix, gave
  `16649a59…` (archive 838071 bytes). `diff -rq` across the two prefixes shows
  that **only the three static archives differ**, each in 4 bytes: the ar
  header mtime of the `__.SYMDEF SORTED` member, which `ranlib` rewrites at
  install time on Apple. So the cross-run difference is an install-time
  timestamp, not an absolute path. `smbios_c.pc` and
  `libsmbiosConfig.cmake`/`libsmbiosTargets.cmake` are identical across
  prefixes. The Targets file is relocatable (`_IMPORT_PREFIX` computed from
  its own location). The `.pc`, however, hard-codes `prefix=/usr/local`, the
  configure-time prefix, whatever `--prefix` is given at install. **That is
  the H1 evidence:** an extracted tarball's `smbios_c.pc` points at
  `/usr/local`, not at the extraction directory. The product archive is still
  reproducible for a given tree (byte-identical on two runs), and the CR-8
  check compares the digest within one run, so neither result is affected.
- **Evidence (macOS, AppleClang 14).** A Release tree was configured into
  `build/macos-arm64` and installed with `stage.py build --leg macos-arm64
  --only install` (`78 files`). Then `stage.py package --leg macos-arm64
  --reporter plain` printed `ok: package macos-arm64:
  libbios-1.0.0-macos-arm64.tar.gz, 78 files, 838069 bytes, sha256
  4d10fcb0965908ff, Release, macos_min 12.7`. `otool -l` agrees
  (`minos 12.7`), `shasum -a 256 -c` reports OK, a second run is
  byte-identical (`cmp`), the archive listing holds `lib/libsmbios.a`,
  `lib/libsmbios_c.dylib`, both headers, `smbios_c.pc` and
  `libsmbiosConfig.cmake`, and it holds no CLI/GUI entry. A missing tree
  exits 2 with one `error:` line. Second pass: `build --only install` printed
  `ok: install tree: 78 files, sha256 16649a59556d7d4a,
  install-macos-arm64.tar`. With `CI_INSTALL_SHA256` set to that digest,
  `unpack` printed `matches the build job's digest`. With the earlier
  `b6c6ecc2…` digest it failed: `install tree digest: expected
  b6c6ecc23bcf71ed -> actual 16649a59556d7d4a (not the tree the build stage
  tested)`, exit 1. **Unverified:** the Windows (`.zip`, `msvc`
  linker version) and Linux (`glibc`) legs on real trees. The installed file
  names were checked against the install rules, and the metadata readers are
  covered by synthetic ELF/PE tests.
