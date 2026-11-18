# 04 - Verify stage and post-publish check

**Status:** 🔶 Implemented, not yet exercised on CI (2026-11-18). The `verify` stage, its gate,
the post-publish check, the nightly registry consumer check and the nightly release re-check
are code, workflow jobs and tests, all green locally. Nothing here has run on a GitHub runner
yet, because nothing was committed or pushed. Success criterion 1 is met locally (a corrupted
asset fails `verify` before `publish`) and needs one CI run on all three OSes to be met as
written. Criterion 2 was run against the real published registry and **passed** (macOS x64, cold). No tag was
pushed, and no release, draft, branch or PR was created.

## Objective

Verify, automatically and on all three OSes, the exact archives that `publish` will upload ([ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) D1, D4). Then check what users actually download.

## Design

> **Governed by the [threat model](/docs/security/2026-11-18-release-and-publish-automation.md).**
> Standing constraints 1, 3, 4, 7 and 11 and finding RP-7 override this design where they
> differ (the same precedence as Tasks 02 and 03). Two places were made stricter; see
> [As built](#as-built-2026-11-18): the post-publish check holds no credential at all (RP-7,
> constraint 11), and the nightly re-check needs an out-of-band anchor per release, which no
> spec named (`scripts/ci/release_ledger.json`). The original text is kept for the record.

- **`verify` stage** (`stage.py verify --leg <leg>`, `stage-verify.yml`; runs on PRs to `master`, nightly and tags per D7). The stage:
  1. downloads `dist/` from the package stage of the same run;
  2. checks every digest against `manifest.json`;
  3. extracts each archive into an empty directory (no source checkout, no vcpkg);
  4. runs Story 03.0 Task 03's clean-room `pkg-config` and `find_package(libsmbios CONFIG)` smoke tests and the `nm`/`dumpbin` export-allowlist check.
- **Post-publish check** (`stage.py publish --only post-publish`, after `release-promote`). It downloads the public assets by URL and re-checks them against the manifest. A failure is loud and names the release. It is not a rollback.
- **Registry consumer check.** After the registry PR merges, a nightly `verify` step runs the Story 02.0 Task 03 vcpkg consumer test against the real registry baseline.

## Success criteria

- [ ] Each OS verifies its own archive, and a corrupted asset (digest mismatch) fails the pipeline before `publish`.
      **Met locally; the per-OS half needs the first CI run.** A flipped byte in the archive
      fails `stage.py verify --only artifact` with `libbios-1.0.0-macos-arm64.tar.gz sha256:
      expected 74daf933e804e584 (manifest) -> actual 5c7a10c946383c19`, before anything is
      extracted. A consistent substitution of archive, sidecar and manifest fails on the
      out-of-band anchor instead (Gates). The failed leg sets no `verified_<leg>`, so
      `verify-gate` refuses and `publish` refuses (requirement (b) below, tested end to end).
      Each OS verifying its own archive is wired (one matrix leg per packaging leg, on its own
      runner, `build=True`) but has not run on a hosted runner.
- [ ] The vcpkg consumer test passes against the published registry with no overlay flag.
      **Met on macOS (x64), cold; Linux and Windows wait for the first nightly.**
      `stage.py verify --only registry` with `VCPKG_BINARY_SOURCES=clear` resolved `smbios-c`
      `1.0.0` from `https://github.com/yuchdev/SmbiosSdk` at baseline `688736300da2`
      (`master`), with no overlay flag, built Boost and the port cold, and passed:
      `ok: registry consumer macos-arm64: https://github.com/yuchdev/SmbiosSdk @ 688736300da2,
      no overlay: smbios_open=ok structures=32, symbols=13, installed=10 (1924.9s)`.
- [x] `python scripts/check_doc_links.py docs/` reports zero problems.

## As built (2026-11-18)

**Files.**

- New:
  - `scripts/ci/post_publish_check.py` (anonymous release check, ledger, nightly re-check);
  - `scripts/ci/verify_deps.py` (the Boost dependency prefix);
  - `scripts/ci/msvc_env.py` (the `vcvars64.bat` environment for the Windows leg);
  - `scripts/ci/export_symbols.py` (the `nm`/`dumpbin` export allowlist, shared);
  - `scripts/ci/release_ledger.json` (empty; the nightly re-check's anchors);
  - `scripts/ci/requirements-verify.txt` (hash-pinned `pkgconf` for Windows);
  - tests `test_verify_stage.py` (37) and `test_post_publish_check.py` (21);
  - policy fixtures `p12_post_publish_inherited_permissions`, `p12_release_recheck_with_environment`.
- Changed:
  - `.github/workflows/stage-verify.yml` (the Task 02 placeholder, now four jobs),
    `stage-publish.yml` (the `post-publish` job), `ci.yml` (the verify and publish callers);
  - `scripts/ci/parity_check.py` (C11 honours a recorded ruling), `scripts/ci/stage.py` (`verify` steps `deps`, `artifact`, `gate`, `registry`, `releases`;
    the `post-publish` publish phase), `publish_github_release.py` (phase `post-publish`,
    `check_published`), `publish_inputs.py` (gate message), `verify_release_artifact.py`
    (check 6, the export allowlist), `vcpkg_registry_smoke.py` (published registry, `.dll`
    symbols), `check_workflow_policy.py` (one P12 rule), `plan.py` (docstrings only),
    `env_names.py` (`CI_VERIFY_RESULTS`), `legs.json` (`stages.verify`), `step_budgets.json`;
  - tests: `test_stage.py`, `test_plan.py`, `test_run_report.py`,
    `test_publish_github_release.py`, `test_check_workflow_policy.py`,
    `test_verify_release_artifact.py`, `test_vcpkg_registry_smoke.py`;
  - docs: [ci.md](/docs/dev/ci.md), [vcpkg-registry.md](/docs/dev/vcpkg-registry.md), Story
    03.0's [Task 03](../03.0-github-releases-prebuilt-artifacts/03-tests.md) and README, this
    story's README, `status.md`.

### (a) The `verify` stage

`ci.yml`'s `verify` job (`needs: [plan, build, package]`) calls `stage-verify.yml` wherever the
plan schedules `package`: PRs to `master`, `master` pushes, nightly and `v*` tags, never a
branch push or a docs-only change (D7). `plan.py` now counts `verify` as defined, which is what
gives a tag its publish rows. One job per packaging leg, each on a runner of its own OS and
architecture (`ubuntu-24.04`, `windows-2022`, `macos-15` arm64). No credential, no cache (P8,
P15). Two steps:

1. **`deps`** (`stage.py verify --only deps`). Pinned tools (plus a hash-pinned `pkgconf` wheel
   on Windows, whose image has no `pkg-config`), the RC4 header, the pinned vcpkg, then
   `boost-iostreams` built **cold** into `$CI_TEMP_DIR/verify-deps/installed`. That is the Boost
   a release consumer must bring: `libsmbiosConfig.cmake` calls
   `find_dependency(Boost COMPONENTS iostreams)`, and no hosted image ships Boost on all three
   OSes. It uses the same baseline the archive was built with, Release only (an overlay
   triplet with `VCPKG_BUILD_TYPE release`), and its own install root, outside the checkout and
   outside `VCPKG_ROOT`.
   **Deviation from "no vcpkg":** vcpkg builds only this third-party dependency. The archive
   is still found only through `CMAKE_PREFIX_PATH` = the extracted directory, the C check uses
   no Boost, and every `VCPKG_*` name is dropped from the consumer's environment. The verifier
   fails the run if any command line, tool output or generated file names the vcpkg root or
   this checkout.
2. **`artifact`** (`stage.py verify --only artifact`). In this order:
   - `sha256(dist/manifest.json)` must equal the package stage's `manifest_sha256`, **before
     the manifest is parsed** (constraint 4; a test makes `json.loads` raise to prove it);
   - every archive and `.sha256` of **every** leg must match the manifest
     (`publish_inputs.local_assets`, the same code `publish` uses). A corrupted asset fails here,
     before anything is extracted;
   - the leg's `install_sha256` must equal `CI_INSTALL_SHA256`, the build job's own output
     (RP-4: the extracted tree is compared with the tested tree, not only with the manifest);
   - on CI the runner must be the leg's own (`runner: expected a macos-arm64 host -> actual
     ...`); a mismatch fails, it never degrades to layout-only;
   - `verify_release_artifact.run(build=True, require_relocatable_pc=True)`: extraction into an
     empty directory, layout and tree digest, the **literal** `pkg-config --cflags --libs
     smbios_c` and the C consumer, the `find_package(libsmbios CONFIG)` consumer, and the new
     check 6, the export allowlist (`nm -D`/`nm -gU`; `dumpbin /exports` on Windows, inside
     the `vcvars64.bat` environment `msvc_env.py` sets up through `vswhere`);
   - only then, as its last act, it sets `verified_<leg>` to the manifest digest.

   A local run on another host (this Mac is x86_64) runs every host-independent check, prints
   `build/run NOT EXECUTED`, and sets no output (`a partial verify never counts`).

### (b) `verified_manifest_sha256`: how the gate cannot be satisfied without real verification

The value publish needs is produced in one place: `stage-verify.yml`'s `verify-gate` job. It is
the **only** source of the workflow output (a test pins it). Four independent conditions must
all hold:

1. **Every leg must succeed for the gate job to run.** `verify-gate` has `needs: verify` and
   **no `if:`**. Without a status function, GitHub applies `success()`, which is false when any
   matrix leg failed, was cancelled or was skipped. Then the job is skipped, its output is empty,
   and so is the workflow output. `fail-fast: false` and no `continue-on-error` anywhere in the
   file mean a failing leg cannot be reported as success. Pinned by `WorkflowWiringTests`.
2. **Every leg must have actually verified this manifest.** Each leg sets only its own
   `verified_<leg>`, so the matrix's combined outputs keep every leg's value (the
   `install_sha256_<leg>` pattern). Each value is set by the `artifact` step after every check
   passed with `build=True`, never on a partial run. The gate step (`stage.py verify --only
   gate`) receives them as `CI_VERIFY_RESULTS`; a leg that set nothing arrives as `""`
   (`|| '""'` in the YAML). It requires **every packaging leg of `legs.json`**, not the
   plan's list, to equal the anchored `manifest_sha256` exactly. A missing leg, another
   manifest's digest, a malformed value or an unknown extra leg is refused by name, for
   example `verify gate windows-x64: expected verified 700476c9b8690097 -> actual no result
   (leg skipped, cancelled or failed)`. A smaller matrix can therefore never pass. Pinned by
   `GateTests`.
3. **The publish caller only runs after verify succeeded.** `publish-release` needs `verify`,
   with no status function (P12 requires `needs: verify`). It passes the gate output through
   `fromJSON` and nothing else.
4. **Publish re-checks the value itself.** `stage.py publish` (every phase, `post-publish`
   included) refuses unless `CI_VERIFIED_MANIFEST_SHA256` equals the digest it anchored. This
   holds even if conditions 1-3 were weakened in the YAML (CR-8: publish never relies on
   verify's verdict alone).

`FailingLegLeavesPublishRefusingTests` runs the real code end to end: each leg's `artifact`
step, the workflow's output plumbing, the `gate` step, then `stage.py publish --only draft`
against the stub GitHub API.

- One failing leg, or legs that never ran: the gate exits 1 with no output, publish refuses
  (`verify gate: expected verify passed for manifest ... -> actual no verify result`), and the
  stub receives **zero** requests.
- All three legs passing: the draft is created (positive control).

The same is shown with the CLI in Gates.

### (c) The post-publish check (constraint 11, RP-7)

This is the `post-publish` job of `stage-publish.yml`: `needs: release-promote`, the `v*` tag
guard, **`permissions: contents: read`**, no `environment`, no secret, and no
`publish_jobs.json` entry. It runs `stage.py publish --destination github-release --only
post-publish` (`post_publish_check.py`):

- **No credential.**
  - `stage.py` refuses the phase if `CI_PUBLISH_TOKEN` is set at all.
  - The module never sets `Authorization`, ignores environment proxies, and imports no
    `subprocess`, step runner or `publish_github_release` (AST test).
  - The stub records the header on every request; all of them are `None`.
  - **New P12 rule:** a job running a post-publish or release re-check step must have explicit
    `permissions: contents: read` only, no environment, no secret, and must not be listed.
    Two fixtures pin it.
- **Public URLs only.** Metadata comes from the unauthenticated REST API (which, like a
  consumer, never sees a draft). The bytes come from each asset's `browser_download_url`, which
  must be exactly `https://github.com/<repo>/releases/download/<tag>/<name>`.
- **Redirects** are followed only to the three `*.githubusercontent.com` hosts, over HTTPS (the
  same set as Task 02, pinned equal by a test). API calls follow none. A refused host is never
  contacted.
- **Capped bodies.** Each download streams into SHA-256 and is cut off at the manifest's
  `bytes` + 0. Longer or shorter fails.
- **The exact asset name set**: archives, sidecars and `manifest.json`, nothing extra, missing
  or renamed. `state` must be `uploaded` and `size` must equal the manifest.
- **Out-of-band comparison.** Every digest is compared with this run's `dist/manifest.json`,
  which is anchored by `manifest_sha256`. The downloaded `manifest.json` must then also be
  byte-identical to it. Replacing an archive together with the release's own manifest is
  detected (test).
- The release must be published, `prerelease` must match the tag's suffix, and the tag must
  dereference (anonymously, through annotated tags) to `manifest.commit`.
- Every failure is one RC1 line that starts `release <tag>:`. Nothing writes anywhere, so it
  is not a rollback.
- On success it prints the row a maintainer appends to `release_ledger.json`.

**The nightly re-check of the last 5 releases** (`release-recheck` job of `stage-verify.yml`,
`stage.py verify --only releases`, `contents: read`, schedule only) needs an anchor that is not
part of the release. A nightly run has no `manifest_sha256` of its own. The anchor is
`scripts/ci/release_ledger.json`: one reviewed row per published release, `{tag, commit,
manifest_sha256, manifest_bytes}`. A maintainer appends it from the post-publish line through a
normal PR; automation never writes it.

For each of the newest 5 releases listed anonymously, the re-check:

- downloads `manifest.json` capped at the ledger's size;
- requires the ledger digest **before** parsing;
- validates the manifest, and requires its `commit`/`version` to match the row and the tag;
- then runs every check above.

A release with no ledger row fails by name. Every release is checked, and the failure names
the first one that failed (`(+n more release(s) failed)`). Today there are no releases and the
ledger is empty, so the step passes with `no published release to re-check`.

The ledger's integrity rests on S-1/S-2 (it lives under `/scripts/ci/`). It has no history lint
of its own, unlike the registry; see Unverifiable.

### (d) The registry consumer check

The `registry-consumer` job of `stage-verify.yml` (schedule only, one per packaging leg) runs
`stage.py verify --only registry`. It calls `vcpkg_registry_smoke.run` against the **published**
registry: `"repository": "https://github.com/<repo>"` at the scheduled run's `master` commit,
fetched like any consumer would fetch it, never this checkout. It uses no overlay flag, a cold
pinned vcpkg, the export allowlist and the exact installed-file set.

**Run against the real baseline (2026-11-18, this Mac, cold).** `1.0.0` is registered, so this
was genuinely exercisable:

- `VCPKG_BINARY_SOURCES=clear CI_TEMP_DIR=/tmp/registry-real python3 scripts/ci/stage.py
  verify --only registry --leg macos-arm64 --reporter plain`;
- vcpkg fetched the registry from GitHub (not this checkout) at the `master` commit
  `688736300da2` and resolved `smbios-c` through `versions/` to the `1.0.0` tree `483562d4…`;
- the `REF` `0bfc9a0` was fetched from GitHub, and Boost and the port built cold;
- the consumer opened the live table, the library exports exactly 13 `smbios_*` symbols, and
  the package installed exactly the 10 allowlisted files:
  `ok: registry consumer macos-arm64: https://github.com/yuchdev/SmbiosSdk @ 688736300da2, no
  overlay: smbios_open=ok structures=32, symbols=13, installed=10 (1924.9s)`;
- wall time **32 min 05 s**. For about its first 15 minutes it shared the CPU with a full lint
  run, so this is an upper bound for this machine.

The leg name only selects the step; the triplet was the host's `x64-osx`, because a local run
passes none.

RP-7 also asks a nightly step to read `versions/s-/smbios-c.json` from `master` anonymously and
run the RP-3 append-only check against the previous nightly's state. That is already stronger
in place: Task 03's `lint` `registry` step runs on every event, the nightly included, with
`fetch-depth: 0`, and checks every `(version, port-version)` ever recorded on `master`'s whole
history, not just the previous night's. No separate step was added.

### (e) Task 03's two deferred items

- **The registry smoke on Linux, macOS and Windows**: the nightly `registry-consumer` matrix
  above, in `stage-verify.yml` with its own budget. It is not in the build job's 8-minute test
  step.
- **The Windows `.dll` symbol check.** `vcpkg_registry_smoke.exported_symbols` no longer
  returns `symbols=skipped` for a `.dll`. It runs `dumpbin /exports` (from `PATH`, else the
  newest toolset `vswhere -find` reports) and parses its export table. A missing `dumpbin` is a
  failure, not a skip. Tests replace the old skip test. The `.so`/`.dll` installed-file lists
  are still unverified until the first nightly Linux/Windows run.

### Budgets, and why

Task 03 set a 30-minute budget on an estimate, and a real cold run then took 34 minutes. This
time the numbers come from measurements on the developer Mac (macOS 12.7, x86_64, AppleClang
14; slower than a hosted arm64 runner):

- **A cold build of every Boost port the project uses, for `arm64-osx`:** 872 s of vcpkg build
  time, summed from that configure's per-port times. `boost-test` alone took 294 s and
  `boost-program-options` 45 s, and the verify stage needs neither. Source tarballs were
  already downloaded, so network time is not included. A release-only `boost-iostreams`
  closure is therefore about 4-5 minutes here.
- **The Windows factor** is the build job's own measured ratio, 534 s / 261 s, so about 2x.

**Maintainer ruling (2026-11-18, relayed during this task): a step dominated by a cold
dependency build gets 45 minutes.** The evidence is the one real cold run of the port build,
34 minutes on the developer Mac (Story 02.0). Task 03's 30-minute budget on an estimated
15-minute median would have failed the first tag run on its budget rather than on a defect. It
applies to `registry-prepare`'s `prepare`, this task's `deps` (cold `boost-iostreams`) and the
nightly `registry` step (cold port build). The fast steps keep tight budgets: `artifact` (8),
`gate` (2), digest and layout checks, because inflating them would hide a regression.

How it is recorded: the policy gate computes a job budget as `ceil(2 x median)`, and a step may
not exceed its job. Inflating the median estimates to fit 45 would have been false data, so P4
now also accepts `ruling_minutes` with a mandatory `ruling` text in a job's
`step_budgets.json` entry. The budget is `max(2 x median, ruling_minutes)`, so a ruling can only
raise a budget and never without its reason; a test covers the malformed cases. **Three jobs now
exceed inventory C11's 30-minute job ceiling, by ruling, not silently:** `registry-prepare` 52,
`verify` 57 and `registry-consumer` 47. C11 itself was **not** raised. `parity_check.py` (which
enforces C11 on every called stage workflow) lets a job exceed 30 only up to its own recorded
`ruling_minutes`, and only when that entry carries a reason. Every other job keeps the
30-minute ceiling, and a ruled job one minute over its ruling fails C11 (test).

| Job | Median estimate (s) | Budget (min) | Basis |
|---|---|---|---|
| `verify` | linux 420, macos 420, windows 840 | 57 (ruling; formula 28) | Expected run: about 1 min checkout and download, about 2 min tools and vcpkg bootstrap, 4-5 min release-only Boost (x2 on Windows), about 2-3 min clean-room checks. Steps: `deps` 45 (ruling), `artifact` 8. Job = 2 + 2 + 45 + 8 |
| `verify-gate` | - | 5 | one comparison |
| `registry-consumer` (nightly) | linux 1500, macos 1500, windows 1800 | 60 (formula); job timeout 47 = 2 + `registry` 45 (ruling) | Two measured cold port builds on the developer Mac: 34 min (Story 02.0) and 32 min (this task, partly contended). Windows is estimated at about 1.5x. The `registry` step gets the ruled 45; the job timeout is 47 |
| `registry-prepare` (Task 03) | linux 900 | 52 (ruling; formula 30) | `prepare` 45 (ruling); 3 + 2 + 45 + 2 |
| `release-recheck` (nightly) | - | 10 (default) | at most 11 API reads and 35 downloads of about 1 MiB |
| `post-publish` | - | 10 (default) | 3 API reads and 7 downloads |

Every median above is an **estimate**, as Task 03's was. Replace each with the median of real
runs.

### Which constraints are enforced where

| Constraint | Test-enforced | Policy-enforced (gate) | Unproven without CI or a real release |
|---|---|---|---|
| 1 No toolchain in a credentialed job | `post-publish` refuses with a credential; the module imports no `subprocess` (AST) | New P12 rule: a post-publish or re-check job has `contents: read` only, no environment, no secret, and is not listed (2 fixtures). `post-publish` is also an unprivileged phase, so it may not appear in a listed job | - |
| 3 Token never leaks | No request carries `Authorization` (every stub test); the phase never reads the token (it leaves `os.environ` first) | - | - (no token exists in these jobs) |
| 4 Manifest anchored | `ArtifactStepTests` (anchor before `json.loads`, consistent substitution, corrupted asset); `post_publish` re-reads the anchored local manifest; re-check: ledger digest before parsing | `dist` by `artifact-ids` in every new job | The first CI run of `verify` and `post-publish` |
| 7 Create-only | Every new path is read-only (no write method exists on `AnonymousClient`) | - | - |
| 11 Post-publish sees what consumers see | `CheckReleaseTests` (manifest replaced with an archive, non-allowlisted redirect, oversize, draft, name set, size, prerelease, tag), `RecheckTests` (last 5, unanchored release, later tampering, draft, ledger validation), `NoCredentialTests` | The P12 rule above | Real GitHub responses: the API shape, the real redirect host, and the anonymous rate limit. **Only a real release can show these.** |
| (b) Gate needs every leg | `GateTests`, `FailingLegLeavesPublishRefusingTests`, `WorkflowWiringTests` | P12 `needs: verify` on the caller (Task 02) | GitHub's matrix-output merging on a real run (the `install_sha256_<leg>` precedent passed on run 44112333813) |

### Story 03.0 success criterion 1

This task runs `verify_release_artifact.run(build=True)` on each leg's own runner, which is
what Story 03.0 Task 03 could not do from one x86_64 Mac. The wiring is now complete. But the
criterion says the checks **pass** on Windows, Linux and macOS, and that needs a CI run that
has not happened, so **Story 03.0 cannot be marked ✅ yet**. Its Task 03 doc, README row and
`status.md` now say exactly that: it closes on the first green `verify` run on all three legs,
with the three `ok: verify <leg>` lines recorded there.

### Unverifiable without CI or a real release

- **Everything on a hosted runner.** The Windows path is the riskiest:
  - `vcvars64.bat` through `vswhere`;
  - `pkgconf` from PyPI (`pkg-config.exe`) and `${pcfiledir}` on Windows;
  - `cl` from the `pkg-config` flags;
  - Boost DLLs from the dependency prefix on `PATH`;
  - `dumpbin` parsing of the real `smbios_c.dll`.

  Also unverified there: the `.so` export list on Linux (`nm -D` may list `_init`/`_fini`),
  the real cold Boost time per leg, and the `install_sha256` cross-check with real outputs.
- **The post-publish check against GitHub.** The real `browser_download_url` and redirect host,
  the anonymous API shape (`assets` in `releases/tags/<tag>`), and the anonymous rate limit
  (60 requests per hour per IP). Proven only with the loopback stub and `--dry-run`.
- **The nightly jobs on a real schedule**, including the C11 exception's actual duration.
- **The ledger.** Its rows are reviewed by a human, and its protection is S-1/S-2, which are not
  applied. A history lint like the registry's is a candidate follow-up.
- **Story 03.0 criterion 1**: see above.

### Gates (local, macOS, 2026-11-18)

- **`stage.py lint --reporter plain`**: 8/8 green, exit 0 (final tree), including
  `ok: scripts/ci tests (491.9s)`, `ok: workflow policy (0.9s)`,
  `ok: versions: 1.0.0 (root project() VERSION), 4 sites (0.0s)` and
  `ok: vcpkg registry: smbios-c registry consistent; history: 1 recorded, 1 REF on master, 0
  tagged (0.5s)`.
- **`stage.py verify --leg macos-arm64 --reporter plain`**, against a `dist/` produced locally
  by `stage.py package --leg macos-arm64`. The arm64 Release tree was cross-built on this
  x86_64 Mac (`Mach-O 64-bit ... arm64`), 78 files, 929,346 bytes:
  `ok: verify macos-arm64: 3 files = manifest 91b8d1b1ebe6 (local, unanchored); tree 78 =
  install_sha256; pkg-config ok; exports 13 smbios_*; build/run NOT EXECUTED on macos-x64`.
  It also printed `info: macos-arm64 archive on a macos-x64 host: no verify result is set (a
  partial verify never counts)`.
- **Corrupted asset (criterion 1).** One byte flipped, anchored as on CI:
  `error: verify macos-arm64 failed (exit 1, 0.0s): libbios-1.0.0-macos-arm64.tar.gz sha256:
  expected 74daf933e804e584 (manifest) -> actual 5c7a10c946383c19`. Nothing was extracted.
  Archive, sidecar and manifest replaced consistently: `manifest.json sha256: expected
  91b8d1b1ebe6caf8 (manifest job output) -> actual dc3c6f8a7e3ff22b`.
- **A failing leg leaves publish refusing (b)**, with the CLI and Task 02's synthetic three-leg
  `dist/` (manifest `700476c9b869...`):
  - `verify --only gate` with `windows-x64` unset gives `error: verify gate failed (exit 1):
    verify gate windows-x64: expected verified 700476c9b8690097 -> actual no result (leg
    skipped, cancelled or failed)`, and no output;
  - with all three set it gives `ok: verify gate: 3 of 3 packaging legs verified manifest
    700476c9b8690097; verified_manifest_sha256 set`;
  - `publish --only draft --dry-run` with that empty value reports `verify gate refuses (no
    verify result)`, and with the gate's value `verify gate passes`.

  The real (non-dry) refusal against a stub API is `FailingLegLeavesPublishRefusingTests`.
- **Post-publish `--dry-run`**: it lists the anonymous reads, the
  `https://github.com/yuchdev/SmbiosSdk/releases/download/v1.0.1/<name>` downloads, and the
  redirect, cap and out-of-band comparison rules:
  `ok: publish github-release post-publish (dry run): v1.0.1: 7 assets match manifest
  700476c9b8690097, disabled (real run refuses), verify gate passes, 0 requests, credential
  unread`.
  - A real run is refused at `github-release: expected enabled in publish_destinations.json ->
    actual disabled`.
  - With `CI_PUBLISH_TOKEN` set it is refused with `expected no credential in this job
    (constraint 11: post-publish checks see what consumers see)`.
- **Tests.** New: `test_verify_stage` (37) and `test_post_publish_check` (21). Affected suites
  green: `test_stage` 77, `test_plan`, `test_run_report`, `test_classify_changes`,
  `test_parity_check` 20, `test_check_workflow_policy`, `test_publish_github_release` 47,
  `test_verify_release_artifact`, `test_vcpkg_registry_smoke`, `test_registry_prepare`,
  `test_publish_vcpkg_registry`, `test_ci_step`, `test_print_env`. The whole `scripts/ci` suite
  is green inside the lint run above.
- **Registry consumer, real baseline**: see (d).
- **`check_doc_links.py docs/`**: 337 files, 0 problems.
