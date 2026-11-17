# 02 - Publish stage: gated draft, upload, promote

**Status:** 🔶 Implemented, not yet exercised end to end (2026-11-18). The code, workflows,
registry entries and RP-4 plumbing are in place and green locally. Success criterion 1 is
**blocked**: on the repository settings S-1..S-8 being applied and read back (threat model
constraint 12) and on Task 04's `verify` stage. Until both land, publish fails closed in three
independent ways (see As built). No tag was pushed and no release, draft or otherwise, was
created.

## Objective

Define the `publish` stage of the [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) pipeline for the GitHub Release. It replaces Story 03.0's former three-way parallel upload (review M2).

## Design

This is `python3 scripts/ci/stage.py publish --destination github-release --only draft|promote`, run as jobs in `stage-publish.yml`. (The spec said `--only github-release`. The threat model fixes the step as `--destination <name>`, and `--only` now names the phase.) Ordering, the tag guard, `needs: verify`, the `release` environment and permissions are ADR 0005 D6 and 0014 SC4, enforced by the policy gate.

The [threat model](/docs/security/2026-11-18-release-and-publish-automation.md) governs this task. Its standing constraints 1-7 and 12, and findings RP-1, RP-4, RP-5, RP-6, RP-8 and RP-12, override this design where they differ.

1. **`release-draft`:**
   - run `check_versions.py --tag "$CI_REF_NAME"` (this is the `lint` stage's `versions` step on a tag event; the publish job also refuses a tag whose base differs from `manifest.version`);
   - create **one** draft release with generated notes;
   - upload exactly the files listed in `dist/manifest.json`, plus the manifest itself;
   - re-read the draft's assets and compare their digests with the manifest.
2. **`release-promote`:** promote the draft to a published release.
3. **No marketplace action** (P13). `scripts/ci/publish_github_release.py` calls the GitHub REST API with `CI_PUBLISH_TOKEN`.

## Success criteria

- [ ] A scratch pre-release tag yields one draft with three archives, three checksums and the manifest. The draft is promoted only after `verify` passed in the same run.
      **Blocked, not attempted.** Pushing any `v*` tag is forbidden until S-1..S-8 are applied,
      read back and recorded (constraint 12). Immutable releases (S-4) also make a bad release
      undeletable. The criterion also needs Task 04's `verify`. What is proven without a tag:
      the stub-API tests show one draft holding exactly the 7 expected assets (`CreateOnlyTests`),
      a pre-release tag giving `prerelease: true` (`test_PreReleaseTagGivesAPreRelease`), and a
      promotion that re-reads all 7 before `PATCH` (`PromoteTests`). `--dry-run` lists the same 7
      uploads offline. The verify ordering is wired (`needs: verify`, P12) but cannot pass yet:
      by design publish refuses without a verify result.
- [x] A tag whose version mismatches fails in `lint` and publishes nothing.
      *`stage.py lint` runs `check_versions.py --tag $CI_REF_NAME` on a tag event:
      `error: versions failed (exit 1): tag: expected v1.0.0 -> actual v1.0.1` (checked by hand;
      `test_check_versions.py` `test_MismatchedOrMalformedTagFails`). `publish-release` needs
      `build`, which needs `lint`, so nothing after a failing lint runs. Independently, publish
      refuses `tag v1.0.2 version: expected 1.0.1 (manifest) -> actual 1.0.2`
      (`TagAndCommitTests`).*
- [x] No upload race: only `release-draft` creates the release.
      *`release-promote` `needs: release-draft` and never sends `POST releases`. A stub test
      counts exactly one create (`test_DraftCreatesOneDraftWithExactlyTheManifestFiles`). A
      second draft attempt, from another run or with any published release present, is a hard
      failure with no write after it (`CreateOnlyTests`).*

## As built (2026-11-18)

**Files.**

- New:
  - `scripts/ci/publish_github_release.py` (the destination);
  - `scripts/ci/publish_inputs.py` (destination-neutral input rules);
  - `.github/workflows/stage-publish.yml`;
  - `.github/workflows/stage-verify.yml` (a placeholder, see below);
  - `scripts/ci/tests/test_publish_github_release.py` (47 tests).
- Changed:
  - `scripts/ci/stage.py`: `run_publish`, the RP-4 build and package outputs, and the
    manifest job's cross-checks;
  - `scripts/ci/plan.py`;
  - `scripts/ci/package_archive.py`: `binary_cache` is a required `LEG_FIELDS` field;
  - `scripts/ci/env_names.py`: seven neutral names;
  - `scripts/ci/check_workflow_policy.py`: the publish-job shape rule;
  - `publish_destinations.json`, `publish_jobs.json`;
  - `ci.yml`, `stage-build.yml`, `stage-package.yml`;
  - tests and fixtures.

**Two phases, one job each.** `release-draft` and `release-promote` (`stage-publish.yml`) are
both `repo-write` with `contents: write` only (no `pull-requests: write`, RP-9), both in the
`release` environment and both guarded to a `v*` tag push. They are called by `publish-release`
in `ci.yml`, which needs `[plan, build, package, verify]`. Each job is exactly:

1. checkout with `persist-credentials: false`;
2. `download-artifact` of `dist` by artifact ID;
3. one `stage.py publish --destination github-release --only draft|promote` step, which is the
   only place `CI_PUBLISH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` is mapped.

`--only preflight` runs both preflights and writes nothing. `--dry-run` lists every read and write
offline. The `github-release` registry entry is tier `release`, environment `release`,
permission class `repo-write`, credentials `["CI_PUBLISH_TOKEN"]`, and **`enabled: false`**.

**Structural issue 1: `needs: verify` before `verify` exists. Wired now, and failing closed in
three independent ways.** The caller carries `needs: verify` (P12 requires it). That needs a
`verify` job, so `ci.yml` gained one that calls a **placeholder** `stage-verify.yml`. It downloads
`dist` by ID and runs `stage.py verify`, which still exits 2. Task 04 completes it, including the
`verified_manifest_sha256` output publish requires. Until then:

1. **Plan.** `plan.py` reports `verify` as `not run: stage not defined`, so the verify job is
   skipped. Publish rows also require a defined `verify`, so `publish` is `not run: stage not
   defined` too and the caller is skipped (`PublishGatingTests`).
2. **Stage gate.** `stage.py publish` refuses unless `CI_VERIFIED_MANIFEST_SHA256` equals the
   anchored manifest digest: `verify gate: expected verify passed for manifest <sha> -> actual no
   verify result (the verify stage is not defined yet: ...)`. This holds even if the plan and
   workflow guards were bypassed (`test_VerifyGateFailsClosedWithoutAVerifyResult`).
3. **Registry.** `enabled: false` makes every real run refuse before reading any input
   (constraint 12).

**Structural issue 2: criterion 1 cannot be exercised safely.** No tag was pushed. Behaviour is
proven against a stub GitHub API on loopback and with `--dry-run`. The criterion is recorded
above as blocked on the settings and on Task 04.

**RP-4 plumbing (the Story 03.0 follow-up this task needed).**

- **Build job.** `stage-build.yml` exports `binary_cache_<leg>`. It is `none` only when
  `VCPKG_BINARY_SOURCES` was exactly `clear` **and** `vcpkg_cache_report.parse_log` counted 0
  restored packages. An unparseable log counts as `restored`.
- **Into the manifest.** `ci.yml` passes `binary_cache_<leg>` in each package leg's matrix
  object (`CI_BINARY_CACHE`). `stage.py package` records it in the leg entry. `binary_cache` is
  now a required `LEG_FIELDS` field, and a local run records `restored`.
- **Package jobs.** Each exports `archive_sha256_<leg>` and `dist_artifact_<leg>`.
- **Manifest job.**
  - It downloads the parts **by those IDs** (no longer `pattern: dist-*`).
  - It refuses a part whose archive digest, `install_sha256` or `binary_cache` differs from the
    job outputs. These arrive as `CI_PACKAGE_ARCHIVES` and `CI_PACKAGE_LEGS` through a one-entry
    matrix, because P10 lets `env:` read only `matrix.*`.
  - It exports `manifest_sha256` and `dist_artifact_id`, which `stage-package.yml` surfaces as
    workflow outputs.
- **Publish.** It downloads `dist` by that ID and checks `sha256(manifest.json)` against
  `manifest_sha256` before parsing.

**Release-tier rules in code.**

- **Commit binding (constraint 5).** The preflight requires a full `TAG` match. `v1.0.0x`,
  `v1.0`, `v1.0.0-` and `v01.0.0` are rejected. The tag base must equal `manifest.version`,
  `manifest.commit` must equal `CI_COMMIT`, and the **remote** tag, dereferenced through
  annotated tags, must equal `CI_COMMIT`. `CI_COMMIT` must be an ancestor of `master`, proven
  with the compare API (`ahead`/`identical`, with that merge base) because no `git` child may
  run. The preflight runs at the start of each phase and again immediately before
  `POST releases` and before `PATCH`. The release is created with
  `target_commitish = CI_COMMIT`, and the tag is dereferenced again after promotion.
- **Cold build (constraint 6).** Every leg's `binary_cache` must be exactly `none`, and must equal
  `CI_BUILD_BINARY_CACHE`, the build job's outputs.
- **Create-only (constraint 7, RP-12).**
  - Any published release for the tag, or a draft from another run, is a hard failure.
  - This run's own draft, whose body starts with `<!-- ci-run-id: <CI_RUN_ID> -->`, may have
    only its own assets deleted and re-uploaded.
  - Promotion re-reads the exact name set, the sizes and the **downloaded** sha256 immediately
    before `PATCH` (RP-6). The API `digest` field is a secondary check.
  - A re-run against this run's already-published, identical release reports `ok` and writes
    nothing.
  - After `PATCH`, the response must report `immutable: true`. If the field is absent, an
    `info:` line asks for the S-4 manual read-back.
- **Settings drift (RP-1).** The draft and promote phases read the `v*` tag ruleset, the
  `release` environment and immutable releases. A readable wrong value fails. A setting
  `GITHUB_TOKEN` cannot read (expected for most) prints one `info:` line naming it as "manual
  read-back required".

**Credential handling (constraints 2, 3; RP-8).**

- **Where it lives.** `stage.py` pops `CI_PUBLISH_TOKEN` from `os.environ` before anything else
  runs (a dry run discards it unread). It wraps the value in a `Secret` whose `repr`/`str` are
  `<redacted>`, and starts no child process.
- **How it is sent.** The destination uses stdlib `urllib` with `add_unredirected_header`. The
  token goes only to the constant `https://api.github.com` and `https://uploads.github.com`.
  Environment proxies are ignored. API calls follow no redirect. Asset downloads follow
  redirects only to three `*.githubusercontent.com` hosts over HTTPS, and without the token.
- **What errors show.** Errors are `<METHOD> <label>: expected <codes> -> actual <status>`, plus
  GitHub `errors[].code` values that match `^[a-z_]{1,40}$`. An internal error prints only the
  exception type.
- **The release body.** It is a fixed block (marker, tag, commit, digest table) built only from
  allowlist-validated manifest fields (RP-10), followed by GitHub's generated notes. Neither is
  echoed to logs or records.

**Which constraints are enforced where.**

| Constraint | Test-enforced | Policy-enforced (gate) | Unproven until a real run |
|---|---|---|---|
| 1 No toolchain in a credentialed job | `test_FullDraftRunStartsNoChildProcess` | New P12 rule: a publish job's steps are only checkout, `download-artifact` with `artifact-ids`, and exactly one `stage.py publish --destination` step. Fixtures `p12_publish_extra_step` and `p12_publish_download_by_name` | - |
| 2 Credential in one place | AST test: no `subprocess`, `ci_step`, `os.system`/`exec`/`eval`; the token leaves `os.environ` | New P8 rule: `secrets.` only in that step's `env:` under `CI_PUBLISH_*` (fixture `p8_publish_secret_at_job_level`); P12 `contents: write` only | - |
| 3 Token never leaks | Canary on success, 4xx (422/401), 5xx, redirect, network error and crash: stdout, stderr, RC3 JSONL, summary file and artifact, exception text; no `Authorization` reaches any non-API host | - | The real runner's log masking is not tested |
| 4 Manifest anchored | `AnchorTests` (archive + manifest replaced together; anchor checked before `json.loads`); manifest-job cross-check tests | `download-artifact` by `artifact-ids` (new P12 rule) | First CI run of the new package outputs (`manifest_sha256`, `dist_artifact_id`, part downloads by ID) |
| 5 One release, one commit | `TagAndCommitTests`, `PreflightTests` (moved, annotated, missing, not on master, re-run before the write) | P8/P12 tier guard (`v*` tag push only) | Real compare/ref API responses |
| 6 Cold-built only | `BinaryCacheTests`, `BinaryCacheStateTests`, package binary-cache tests | - | The first tag run's actual `binary_cache_<leg>` values |
| 7 Create-only | `CreateOnlyTests`, `PromoteTests` (extra, renamed, resized, tampered, digest field, other run's draft, published release; idempotent re-run) | - | Real API behaviour, immutable releases (S-4) |
| 12 Settings first | `test_RealRegistryShipsGithubReleaseDisabled`; disabled-destination gate test | - | **S-1..S-8 are not applied or read back** (maintainer action) |

**Local evidence.**

- `stage.py lint` is green on all 8 steps (status.md 2026-11-18 has the lines).
- `--dry-run` was run against a synthetic three-leg `dist/` (no real build).
- The same inputs without `--dry-run` are refused at the disabled-destination gate.

**Not done here.** These stay with the tasks that own them:

- the registry PR (Task 03);
- post-publish checks and the real `verify` (Task 04);
- the `destinations/` package interface, which moves `publish_github_release.py` to
  `destinations/github_release.py` (Task 05).
