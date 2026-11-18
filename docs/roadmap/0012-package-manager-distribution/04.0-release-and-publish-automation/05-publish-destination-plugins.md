# 05 - Publish destination plug-ins and the S3 staging example

**Status:** 🔶 Implemented (2026-11-18), not yet exercised on CI. The D9 interface, the
`scripts/ci/destinations/` package, the class matrix jobs and `s3-staging` are code, workflow
jobs and tests, green locally. Success criteria 1-4 are met locally (below). Every destination
ships `enabled: false` (threat model constraint 12), so nothing here can publish until the
maintainer applies and reads back S-1..S-8 (and S-9 for `s3-staging`). No tag was pushed, and
no release, draft, branch, PR, S3 object or bucket was created.

**Depends on:** Tasks 02-03; 0014 Stories 02.0-03.0 (SC5, policy gate P12)

## Objective
Implement ADR 0005 D9: the destination interface, the registry file, and the existing GitHub Release and registry-PR publishing as the first two destinations, plus `s3-staging` as the worked example of adding a third.

## Files

> **Governed by the [threat model](/docs/security/2026-11-18-release-and-publish-automation.md)**
> (standing constraints 1, 2, 3, 7, 12; RP-1, RP-12, RP-13, RP-14; setting S-9), which wins
> where it differs from this spec, as in Tasks 02-04. Three conflicts with the original text
> are resolved in [As built](#as-built-2026-11-18); the original wording is kept below.

- **Create** `scripts/ci/destinations/{__init__,github_release,registry_pr,s3_staging}.py`: each implements `NAME`, `REQUIRED_ENV`, `validate`, `publish`, `check_published`. Tasks 02 and 03's scripts become the first two modules rather than standalone scripts.
- **Create** `scripts/ci/publish_destinations.json` (`github-release` and `registry-pr` enabled, tier `release`; `s3-staging` present, `enabled: false` until the maintainer provides a bucket and role).
- **Modify** `stage.py publish --destination <name> [--dry-run]`; `plan.py` emits the publish matrix per permission class.
- **Modify** `stage-publish.yml`: one matrix job per permission class, each `needs: verify`, with the tier and tag/branch guards from D6/D9.
- **Create** tests under `scripts/ci/tests/` as listed in `docs/dev/adding-a-publish-destination.md`.

## Constraints
- A destination reads the manifest only; it never rebuilds or recomputes digests.
- `staging` tier never runs on pull requests or forks and never writes a `release` location (the S3 prefix check).
- Enabling `s3-staging` is a maintainer action (bucket, OIDC trust, `staging` environment); it ships disabled.

## Success criteria
- [x] Adding a fourth destination in an existing class needs no workflow edit, shown by a unit test that registers a fake destination.
      **Met locally.** `test_publish_destinations.FakeDestinationRegistrationTests` registers
      `fake-mirror` (`release`, `repo-write`) and `fake-bucket` (`staging`, `oidc`) as a module
      plus a registry entry only. `plan.py` puts them in `publish_repo_write`/`publish_oidc`
      (rows of `publish-release-repo-write`/`publish-staging-oidc`) and on no PR, nightly or
      branch push; the policy gate reports **zero violations on the unchanged
      `.github/workflows/`** (the test also asserts the workflow bytes did not change); and
      `stage.py` runs the fake through the D9 interface in its class job, handing it only
      `CI_PUBLISH_TOKEN` as a `Secret`, and refuses it in any other job. Negative controls in
      the same class prove the test can fail: a `token-only` destination (no class job), an
      unmapped credential, and a `repo-write` module importing `subprocess` each fail P12.
      What a unit test cannot show is the platform's matrix expansion (`include` added to every
      `destination` row) and caller-permission validation; see
      [Hosted-runner differences](#what-could-differ-on-a-hosted-runner).
- [x] `--dry-run` of every destination lists exact files and keys with no network or secret access.
      **Met.** `test_publish_destinations.EveryDestinationDryRunTests` runs all three with
      canary credentials and OIDC variables set, `urllib`'s opener, `subprocess.Popen` and
      `destinations.Secret` patched to raise: `github-release` lists the 7 uploads,
      `vcpkg-registry` the 3 blobs, tree, commit and ref, `s3-staging` the 7 `put-object` keys
      `s3://<bucket>/<prefix>/<version>/<run>-<attempt>/<file>`; no canary appears. The bucket
      and prefix are staging-environment secrets, so a dry run (which reads no credential)
      shows them as placeholders; every other key part is exact and validated. Output in
      [Gates](#gates-local-macos-2026-11-18).
- [x] Policy gate P12 fails on a registry entry with an unknown class, a missing module, or a `release` tier reachable from a non-tag event.
      **Met.** `test_publish_destinations.RegistryPolicyTests`: `known permission class ->
      actual admin`; `existing module -> actual destinations/s3_staging.py missing` (for a
      **disabled** entry: P12 now checks every entry); and, with `release-draft`'s guard widened
      to a `master` push, `github-release: P12: expected release destination reachable from v*
      tag push only -> actual release-draft: reachable from push-master`.
- [x] `python3 scripts/check_doc_links.py docs/` reports zero problems.

## As built (2026-11-18)

**Files.**

- New:
  - `scripts/ci/destinations/__init__.py` (the D9 interface: `INTERFACE`, `load()`,
    `check_interface()`, `Env`, `Result`, `Secret`, classes and tiers);
  - `scripts/ci/destinations/s3_staging.py`;
  - tests `test_s3_staging.py` (27) and `test_publish_destinations.py` (17).
- Moved (no copy left behind; a test asserts the old paths are gone):
  `scripts/ci/publish_github_release.py` -> `scripts/ci/destinations/github_release.py`,
  `scripts/ci/publish_vcpkg_registry.py` -> `scripts/ci/destinations/vcpkg_registry.py`; each
  gains `validate`/`publish`/`check_published`, the phase logic that used to sit in
  `stage.py` (API client, preflight, hand-over loading) moves behind `publish()`.
- Changed:
  - `scripts/ci/stage.py` (`run_publish` drives destinations only through D9;
    `check_job_binding`; `CI_DESTINATION`; every `CI_PUBLISH_*` and the OIDC request leave
    `os.environ` first; staging-tier inputs), `plan.py` (rows from both registries;
    `publish_repo_write`/`publish_oidc`/`publish_token_only`), `check_workflow_policy.py`
    (P12, below), `publish_inputs.py` (`check_staging_binding`), `env_names.py`
    (`CI_DESTINATION`), `reporters/__init__.py` + `reporters/github.py`
    (`take_oidc_request`), `publish_destinations.json` (`s3-staging`, `binary_cache_policy`),
    `publish_jobs.json` (bindings, two class jobs, caller `publish-staging`);
  - `.github/workflows/stage-publish.yml` (`publish-release-repo-write`,
    `publish-staging-oidc`), `ci.yml` (plan outputs, caller `publish-staging`, union grant);
  - tests: `test_publish_github_release.py`, `test_publish_vcpkg_registry.py`,
    `test_post_publish_check.py`, `test_plan.py`, `test_classify_changes.py`, `test_stage.py`,
    `test_check_workflow_policy.py`, `ci_test_support.py`, 10 policy fixture directories;
  - docs: [adding-a-publish-destination.md](/docs/dev/adding-a-publish-destination.md),
    [ci.md](/docs/dev/ci.md), [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md)
    (D9 as-built note, portability table, `CI_DESTINATION`), this story's README,
    `status.md`.

### Conflict 1: the spec ships `github-release` and `registry-pr` enabled

**Resolved: every destination ships `enabled: false`.** Constraint 12: no `release` or
`staging` destination may be enabled until S-1..S-8 (and S-9 for S3) are applied, read back
and recorded in `docs/dev/ci.md` and `status.md`. None is. The threat model wins, as in Tasks
02-04. `stage.py` refuses a disabled destination on a real run; `--dry-run` works on all.

### Conflict 2: two names for the registry destination

**Resolved: `vcpkg-registry` everywhere; the spec's `registry-pr`/`registry_pr` was not adopted.**
The destination writes a **branch** and never opens a PR (RP-9, constraint 10), so a name
containing "pr" would be wrong, and `vcpkg-registry` was already the registry key, the
`--destination` value in `stage-publish.yml`, and the name in the docs and tests since Task 03.
What changed: the module is `scripts/ci/destinations/vcpkg_registry.py` (registry key with `-`
as `_`, the rule P12 and `destinations.load()` share), and `publish_jobs.json` now binds the job
`registry-pr` to destination `vcpkg-registry`, phase `branch`. `registry-prepare` and
`registry-pr` remain **job** names (one per phase, D6), not destination names. Threat-model
RP-2's sentence about `destinations/registry_pr.py` applies to `destinations/vcpkg_registry.py`
(the document itself is not edited); its AST test targets that file.

### Conflict 3: `publish_destinations.json` already exists

**Resolved: extended, not created.** It existed since 0014 Story 02.0 Task 03, with
`github-release` (Task 02) and `vcpkg-registry` (Task 03). Task 05 adds `s3-staging` and a
`binary_cache_policy` per entry (threat-model requirement 17: `none-only` for `release`, `any`
for `staging`; P12 checks it against the tier).

### Further deviations, for review

- **"One matrix job per permission class" is one per tier and class, for single-phase
  destinations.** A job fixes its `environment:` and its tier guard, so the class job is
  per tier: `publish-release-repo-write` (empty today) and `publish-staging-oidc`
  (`s3-staging`). `github-release` and `vcpkg-registry` keep their D6 jobs, each bound in
  `publish_jobs.json` to one destination and phase. A matrix cannot order its rows, so
  folding them in would lose either the reviewer-gated promote after the draft (RP-6, RP-11) or
  the unprivileged `registry-prepare` before the token (RP-2). This was not unblocked by
  restructuring verified jobs; their shape is unchanged. ADR 0005 D9 carries an as-built note
  for the architect.
- **Both callers grant `contents: write` + `id-token: write`.** GitHub validates every nested
  job of a called workflow against the caller's grant, including jobs the event skips, so a
  staging caller granting only `id-token: write` (or the release caller only
  `contents: write`) would make the run invalid. Each nested job still declares only its own
  permissions and its own tier guard.
- **Scheduling asymmetry.** `plan.py` schedules a `release`-tier destination on a `v*` tag even
  while disabled (as Task 02 did: an early tag fails loudly, actor A5), but a `staging`-tier one
  only when enabled, so the disabled `s3-staging` never turns a `master` push red. With
  `s3-staging` disabled, `publish-staging` is skipped on every push today.
- **`CI_PUBLISH_S3_REGION`** joins D9's three S3 names; the aws CLI must not guess a region.
- **P12 checks disabled entries too.** The old `test_DisabledDestinationIsNotChecked` asserted
  that `{"name": "x", "enabled": false}` passed; it is now
  `test_DisabledDestinationIsCheckedToo` and asserts the opposite. Enabling an entry must need
  no other change, so its module, class, jobs and credential mapping are checked before.

### What the destination package enforces

| Rule | Where | Test |
|------|-------|------|
| D9 interface before use; `NAME` equals the key; phases well formed | `destinations.load()` | `InterfaceTests` |
| Constraint 1: a privileged phase runs only in the job `publish_jobs.json` binds it to (class, tier, destination, phase) | `stage.check_job_binding` (`CI_JOB`) | `JobBindingTests`, `FakeDestinationRegistrationTests.test_StageBindsTheFakeToItsClassJobOnly`, `StagingTierTests` |
| Constraint 1: class job step shape: exactly `stage.py publish` + `CI_DESTINATION: matrix.destination`; a bound job's step names exactly its destination and phase | P12 `publish_job_shape` | `test_RealPublishWorkflowHasTheAllowedShapeOnly`, fixtures |
| Constraint 2: every `CI_PUBLISH_*` and the OIDC request leave `os.environ` first; a destination gets only the credentials its entry and its `REQUIRED_ENV` both name, as `Secret`; none in a dry run or an unprivileged phase | `stage.run_publish`, `_take_credentials` | `StagingTierTests` (`left == []`), dry-run tests (`Secret` patched to raise) |
| Constraint 2 / RP-2: `repo-write`/`token-only` modules and every `scripts/ci` module they import start no child process | P12 `_child_process_use` (transitive); AST unit tests | `RegistryPolicyTests.test_ChildProcessInARepoWriteModuleTransitively`, `NoChildProcessTests`, `NoToolchainTests` |
| Constraint 3: canary on success, 4xx, 5xx, redirect, network error and crash, per destination | destinations + `stage.run_publish` | `test_publish_github_release.CanaryTests`, `test_publish_vcpkg_registry.CanaryTests`, `test_s3_staging` (`OidcTests`, `PublishTests`, `test_CrashAndFailurePathsPrintNoCanary`) |
| Constraint 7 for S3: `put-object --if-none-match *`; `PreconditionFailed` is a hard failure; no further write, no retry, never a delete | `s3_staging._put` | `test_ExistingKeyIsAHardFailureWithNoSecondWrite`, `test_SecondAttemptOfTheSameRunWritesAFreshLocation` |
| Canonical-path confinement (CR-3) | `s3_staging.staging_prefix`, `object_key`, `source_file` | `ConfinementTests`, `SourceFileTests` |
| CR-5: OIDC token in a 0600 file in a 0700 directory, removed afterwards; the child gets only `CHILD_ENV_NAMES` | `s3_staging._aws_session`, `ci_step.run_step(pass_env=...)` | `test_ChildGetsTheTokenFileOnlyAndNoCredentialName` |
| Staging tier: master push only, verify gate, `restored` accepted but cross-checked, manifest commit bound | `stage._publish_gates`, `_publish_inputs`, `publish_inputs.check_staging_binding` | `StagingTierTests` |
| P12 registry: module, class, tier, environment, `binary_cache_policy`, credential mapping, a job of the class, guard reaches only the tier's event | `check_workflow_policy.check_publish_registry` | `RegistryPolicyTests`, `FakeDestinationRegistrationTests.test_NegativeControlsFailP12` |
| RP-12: re-run writes `<run>-<attempt>/`, never the previous attempt's keys | `s3_staging.object_key` | `test_SecondAttemptOfTheSameRunWritesAFreshLocation` |
| RP-13: OIDC trust bound to `stage-publish.yml@refs/heads/master` and `environment:staging` | S-9 (settings; recorded in ADR 0005 D9) | not mechanisable |
| RP-14: objects carry `not-a-release=true`; ships disabled; enabling needs the S-9 read-back | `s3_staging._put`; registry | `test_SuccessCreatesEveryObjectOnceAndRereadsIt` |
| RP-1 / S-9: the cloud-side role policy is the boundary | settings | not mechanisable |

### Tests that changed shape, and why

No assertion was weakened. Each change below either retargets a test at the moved code or
makes it stricter.

- **AST scans** (`NoChildProcessTests`, `NoToolchainTests`, `test_post_publish_check`) now read
  `destinations/github_release.py`, `destinations/vcpkg_registry.py` and
  `destinations/__init__.py` (imported by every destination, newly scanned) through one shared
  scanner, `ci_test_support.used_names`. The scanner is stricter than before: `from os import
  system` now records `os.system`, and `os.spawn*` is caught. `post_publish_check.py` may not
  import the `destinations` package. New tests assert that the scanned file is the one
  `destinations.load()` imports, that the old top-level files are gone, and that every
  registered `repo-write`/`token-only` module is scanned.
- **Stage-level publish tests** set `CI_JOB` to the phase's job, as the workflow does
  (`PHASE_JOBS`, `REGISTRY_PHASE_JOBS`). Without it a real run is now refused by the job binding.
- **The dry-run "credential unread" tests** patched `publish_github_release.Secret`; `stage.py`
  now builds credentials as `destinations.Secret`, so they patch that. A mutation that builds a
  `Secret` in a dry run fails them (below).
- **Plan tests** (`test_plan`, `test_classify_changes`): rows now carry `class`, `destination`
  and `phase`. `PlanData` carries the destination registry; the synthetic tier test now uses
  class matrix jobs plus registered destinations (and gains a disabled staging destination
  that must get no row). `test_MalformedFlagsAreRejected` now passes the real registry and
  asserts the message, so it cannot pass for the wrong reason (an unregistered destination).
- **Policy tests and fixtures**: the fixture `publish_jobs.json` files bind each job or mark it
  `matrix`. `test_PublishJobListedButMissing` copies the bound job (a second matrix job of one
  class is now its own violation). `test_DisabledDestinationIsNotChecked` became
  `test_DisabledDestinationIsCheckedToo` (stricter).

### Proof that the moved security tests still bind

A mutation harness applied one deliberate non-compliance at a time to the shipped code, ran the
tests that guard it, and restored the file. Every mutant failed its tests; none passed:

| Mutant | Guarding tests | Result |
|--------|----------------|--------|
| `import subprocess` in `destinations/github_release.py` | AST scan; P12 on the real registry | FAILED (2 of 2) |
| `from ci_step import run_step` in `destinations/vcpkg_registry.py` | AST scan | FAILED |
| `import registry_prepare` in `destinations/vcpkg_registry.py` | AST scan | FAILED |
| `os.system` in `destinations/__init__.py` | AST scan | FAILED |
| `github_release.publish` logs the token | `CanaryTests` (5 tests) | FAILED (6) |
| `vcpkg_registry.publish` logs the token | `CanaryTests` | FAILED (2) |
| `stage.py` prints the exception message on a crash | the three crash canaries | FAILED (3) |
| `Authorization` via `add_header` instead of `add_unredirected_header` | `test_DownloadRedirectCarriesNoAuthorization` | FAILED |
| redirect allowlist accepts any host | `test_RedirectAndNetworkErrorPaths` | FAILED |
| `draft` skips the constraint-5 preflight before the create | `PreflightTests` | FAILED |
| `branch` skips the constraint-5 preflight before the ref write | `PreflightTests` | FAILED |
| S3 put without `--if-none-match *` | `PublishTests` | FAILED (2 + 1 error) |
| `PreconditionFailed` retried as an overwrite | `test_ExistingKeyIsAHardFailureWithNoSecondWrite` | FAILED |
| S3 prefix checked with `startswith("staging")` | `ConfinementTests` | FAILED (11) |
| role ARN in the S3 `ok:` summary | two S3 canaries | FAILED (2) |
| `stage.py` builds a `Secret` in a dry run | the three dry-run tests | FAILED (5) |
| job binding removed from `stage.py` | binding tests | FAILED (8) |
| P12 module scan disabled | P12 tests | FAILED (2) |

The S3 stub models create-only faithfully: without `--if-none-match *` it overwrites, as S3
does, so the second mutant fails on behaviour, not only on the argv check.

### What could differ on a hosted runner

None of this has run on GitHub, and `publish-staging` is skipped while `s3-staging` is
disabled. Watch for these on the first push:

1. **Matrix expansion.** The class jobs combine a `destination: ${{ fromJSON(...) }}` axis with
   one `include` entry that adds `manifest_sha256` etc. to every row (documented GitHub
   behaviour; the existing jobs use `include` alone). If the platform did not add them,
   `CI_MANIFEST_SHA256` would be empty and `stage.py` would refuse (fail closed).
2. **Caller permissions.** Both callers now grant `id-token: write`. If GitHub rejected a
   reusable-workflow job's permissions at run creation, the **whole run** would be invalid on
   every event. The union grant is meant to prevent exactly that; confirm the next push's run
   starts at all.
3. **Masked secrets.** The S3 settings are environment secrets, so GitHub masks their values in
   logs; a prefix such as `staging/libbios` would make every `staging` in later log lines
   appear as `***`. Cosmetic.
4. **When enabled only:** the runner image's `aws` version must equal `pins.json`
   `tools.aws_cli` (2.40.2) or the job fails before any write (ADR 0005 D3); runner images
   move weekly. The OIDC host allowlist assumes the token service is under
   `*.actions.githubusercontent.com` over HTTPS. `--if-none-match` and `--checksum-sha256` on
   `s3api put-object` have not been run against a real bucket.

### Gates (local, macOS, 2026-11-18)

`python3 scripts/ci/stage.py lint --reporter plain` (8/8; 1001 tests):

```
env: lint/linux image=- | cmake 3.31.12 | ctest 3.31.12 | ninja 1.13.2 | cxx appleclang 14.0.0 | python 3.12.10 | vcpkg 00c5775211f4 | cache -
ok: ci_step self-test (3.12.10, darwin)
ok: scripts/ci tests (300.0s)
ok: actionlint: 1.7.12, 8 workflows, shellcheck no (1.1s)
ok: workflow policy (0.7s)
ok: versions: 1.0.0 (root project() VERSION), 4 sites (0.0s)
ok: vcpkg registry: smbios-c registry consistent; history: 1 recorded, 1 REF on master, 0 tagged (0.2s)
ok: c-versions isolation (0.3s)
ok: doc samples (0.1s)
```

`--dry-run` of all three destinations (a scratch `dist/` from the test helper, anchored with
`CI_MANIFEST_SHA256`, canary `CI_PUBLISH_TOKEN`/`CI_PUBLISH_S3_ROLE` set and absent from the
output), the `ok:` line and one listed write each:

```
ok: publish github-release (dry run): v1.0.1: 7 assets match manifest c9ba9911dd83e1b6, disabled (real run refuses), verify gate refuses (no verify result), 0 requests, credential unread (0.0s)
info: dry run: would upload manifest.json (1332 bytes, sha256 c9ba9911dd83e1b6)
ok: publish vcpkg-registry (dry run): v1.0.1: 7 assets match manifest c9ba9911dd83e1b6, disabled (real run refuses), verify gate refuses (no verify result), 0 requests, credential unread (0.0s)
info: dry run: would POST 3 blobs, a tree on the base tree, the commit "registry: smbios-c 1.0.1 (REF 872ac29693a7, run 0)" and git/refs refs/heads/registry/v1.0.1
ok: publish s3-staging (dry run): 1.0.1 (staging): 7 assets match manifest c9ba9911dd83e1b6, disabled (real run refuses), verify gate refuses (no verify result), 0 requests, credential unread (0.0s)
info: dry run: would put-object dist/manifest.json (1332 bytes, sha256 c9ba9911dd83e1b6) -> s3://<bucket>/<prefix>/1.0.1/0-1/manifest.json (--if-none-match *, metadata not-a-release=true)
```

`python3 scripts/check_doc_links.py docs/`: `337 file(s) scanned, 0 problem(s) found.`
