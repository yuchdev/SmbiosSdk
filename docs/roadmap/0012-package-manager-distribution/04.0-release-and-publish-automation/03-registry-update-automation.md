# 03 - Registry update in the publish stage

**Status:** 🔶 Implemented, not yet exercised end to end (2026-11-18). The registry update
is code, workflow jobs, a registry entry and lint rules, all green locally. Success criterion
1 is **blocked**, as Task 02's is: it needs the repository settings S-1..S-8 applied and read
back (threat model constraint 12), Task 04's `verify`, and a real tag. Criterion 2 is met **as
amended** below: one deliberate manual step remains. No tag was pushed, and no branch, PR or
release was created.

## Objective

Automate the circular steps of review finding H2 as the publish stage's registry jobs ([ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md) D6).

## Design

> **Superseded in part by the [threat model](/docs/security/2026-11-18-release-and-publish-automation.md).**
> Where the two differ, the threat model wins (the same precedence as Task 02). Three
> conflicts were resolved; see [As built](#as-built-2026-11-18):
>
> - RP-2 splits the single job below into an unprivileged job and a REST-only job;
> - RP-9 and constraint 10 replace "opens a PR" with "creates a branch";
> - RP-3 and the 1.0.1 ruling forbid re-registering 1.0.0.
>
> The original text is kept for the record.

This is `python3 scripts/ci/stage.py publish --only registry-pr`, run after `release-promote`. The job:

- checks out the registry branch/repo;
- sets the port `REF` to the released tag's commit;
- runs `vcpkg x-add-version smbios-c`, which computes `git-tree` and updates `versions/baseline.json` and `versions/s-/smbios-c.json`;
- **validates the result in the same run**, with the Story 02.0 Task 03 consumer test against a `filesystem` registry pointing at that checkout;
- only then opens a PR through `scripts/ci/open_registry_pr.py` (REST, `CI_PUBLISH_TOKEN`).

The validation must run before the PR because a PR opened with `GITHUB_TOKEN` triggers no workflow, so no PR-side check would ever run (ADR 0005 D6).

Two items carry over from the review and stay in this task:

- Registry hosting (this repo's `master` vs a dedicated registry repo) is an open maintainer decision (ADR 0005 Open questions 4), recorded before implementation. *(Stale: decided in status.md on 2026-11-16, this repository, consumers pin a commit SHA.)*
- The portfile fixes from review finding M4 (`vcpkg_from_git` arguments, the real repo URL, `vcpkg_check_linkage`), and a decision on M5 (root CMake options for the CLI and tests). *(M4 was done in Story 02.0. M5 is still the follow-up recorded there; not in scope here.)*

## Success criteria

- [ ] Publishing a scratch release produces a registry PR with a consistent `git-tree`, which passes `vcpkg x-add-version --verbose` without edits.
      **Blocked, not attempted.** Like Task 02's criterion 1, it needs a real `v*` tag. Constraint
      12 forbids that until S-1..S-8 are applied, read back and recorded. It also needs Task 04's
      `verify`, because `registry-prepare` needs `release-promote`, which needs verify. As
      amended by RP-9, the expected result is a **branch** `registry/v<version>`, not a PR.
      What is proven without a tag:
      - the branch phase against a loopback Git Data API stub (`BranchTests`). The branch commit's
        `ports/smbios-c` tree equals the registered `git-tree`, its parent is the base, the
        message is the RP-10 template, and the writes are exactly 3 blobs, 1 tree, 1 commit and
        1 ref, with no PR call;
      - the prepare phase against throwaway git repositories with a Python stand-in for
        `x-add-version` (`test_registry_prepare.py`);
      - the pure-Python `git-tree` reproduces the real registered `483562d4…` from the committed
        port (`test_RealPortTreeEqualsTheRegisteredGitTree`);
      - `--dry-run` shows the full plan, and on a tree bumped to 1.0.1 that the change would be
        accepted.

      The real `vcpkg x-add-version --verbose` and the cold consumer build run only in
      `registry-prepare` on CI.
- [x] ~~No manual step remains in the "bump baseline" procedure doc. That doc becomes a description of the automation.~~
      **Amended (threat model RP-9, setting S-6, constraint 10): one deliberate manual step
      remains: a human opens and merges the PR from `registry/v<version>`.** Opening it with
      `GITHUB_TOKEN` needs "Allow GitHub Actions to create and approve pull requests". That
      setting lets *any* workflow approve PRs, and S-6 requires it off. Such a PR would also run
      no CI. The amended criterion is met: [docs/dev/vcpkg-registry.md](/docs/dev/vcpkg-registry.md)
      "Releasing a new `smbios-c` version" now describes the automation. It names the
      human-opened PR as the one manual step and shows no overwrite switch. `master` stays green
      in every commit (see "Task 01's procedural defect" below).

## Carried forward from Story 02.0 (2026-11-16, named items, not optional)

Story 02.0 delivered the port, `versions/`, `check_vcpkg_registry.py` and a clean-room
driver, but deliberately not these; this task or Task 04 owns them:

- **Wire `scripts/ci/vcpkg_registry_smoke.py`** as a `verify`-stage step on Linux, macOS and
  Windows (it was verified on macOS only; a cold Boost build exceeds the build job's 8-minute
  test-step budget, so it belongs in `stage-verify.yml` with its own budget).
  **-> Task 04** (the `verify` stage does not exist; `stage.py verify` exits 2). This task runs
  the same driver once, inside `registry-prepare` on Linux, as the pre-write validation.
- **Windows** (`x64-windows`, the static triplet question M4): the `.dll` symbol check is
  skipped today (`symbols=skipped`); use `dumpbin /exports` or `llvm-nm`. **-> Task 04.**
- **`REF` reachability lint**: fail when the portfile `REF` is not an ancestor of `master`
  (needs full history in the lint checkout), since a squash merge orphans it. **Done** (check 6).
- **`git-tree` overwrite lint**: fail when a `(version, port-version)` already on `master`
  changes its `git-tree` (guards `x-add-version --overwrite-version`). **Done**, history-wide
  per RP-3 (check 5), plus check 7 (`REF` equals its tag's commit).
- **Installed-file allowlist**: assert the exact file set under `vcpkg_installed/<triplet>`
  so a new root install rule cannot ship silently (the portfile prunes a fixed list). **Done**
  in `vcpkg_registry_smoke.check_installed_files` (it runs where the driver runs).

## As built (2026-11-18)

**Files.**

- New:
  - `scripts/ci/registry_update.py` (pure rules: git object ids, the `REF` edit, the judge);
  - `scripts/ci/publish_vcpkg_registry.py` (the destination: REST `branch` phase, dry run);
  - `scripts/ci/registry_prepare.py` (the unprivileged `prepare` phase);
  - tests `test_registry_update.py` (16), `test_publish_vcpkg_registry.py` (25),
    `test_registry_prepare.py` (10);
  - policy fixtures `p12_publish_prepare_in_credentialed_job` and
    `p12_registry_pr_pull_requests_write`.
- Changed:
  - `check_vcpkg_registry.py` (pending registration, baseline rules, history checks 5-7);
  - `check_versions.py` and `version_sites.json` (rule `registered`);
  - `stage.py` (the `prepare`/`branch` phases, per-destination phase lists), `plan.py`
    (constraint 8), `vcpkg_registry_smoke.py` (allowlist), `check_workflow_policy.py` (one P12
    rule), `env_names.py` (`CI_REGISTRY_PREPARED`);
  - `publish_jobs.json`, `publish_destinations.json`, `step_budgets.json`;
  - `stage-publish.yml`, `ci.yml`, `stage-lint.yml` (`fetch-depth: 0`);
  - `scripts/release/prepare_release.py` (docstring only);
  - docs: `vcpkg-registry.md`, `ci.md`, `adding-a-publish-destination.md`, ADR 0005
    (amendment);
  - updated tests in `test_check_vcpkg_registry.py`, `test_check_versions.py`, `test_plan.py`,
    `test_prepare_release.py`, `test_stage.py`, `test_check_workflow_policy.py` and
    `test_vcpkg_registry_smoke.py`.

### Conflict 1: RP-2 rewrites the job topology

The spec ran `x-add-version`, a cold vcpkg build of four Boost ports and the repository, and
the consumer test in the job holding the write token. Hosted runners give passwordless `sudo`,
so any of that code can read the token from the runner's memory. Stripping it from child
environments does not help. The job is split in two, and the validation still happens in the
same run, before any write:

- **`registry-prepare`** (`stage.py publish --destination vcpkg-registry --only prepare`) has
  `contents: read`, no `environment`, and no secret, and is **not** in `publish_jobs.json`. It
  `needs: release-promote`. It:
  - installs the pinned tools and bootstraps vcpkg cold (no cache in `stage-publish.yml`, P15);
  - makes a credential-less `git fetch --no-tags origin +refs/heads/master:refs/ci/master`;
    the base is that commit, and `CI_COMMIT` must be its ancestor;
  - makes a shared clone at the base, so this job's own scripts are never swapped for
    `master`'s. The base must be *pending registration* for exactly `manifest.version`;
  - sets `REF` := `manifest.commit` and commits locally (never pushed);
  - runs `vcpkg x-add-version smbios-c --x-builtin-ports-root=./ports
    --x-builtin-registry-versions-dir=./versions --verbose`;
  - requires `git status` to show exactly the two `versions/` files, and commits locally;
  - self-checks: only the three files differ from the base; the `git-tree` equals the
    pure-Python one; the judge accepts; the full lint passes;
  - runs `vcpkg_registry_smoke.run` (consumer, symbols, installed-file allowlist);
  - copies the three files to `$CI_ARTIFACT_DIR/registry/` and sets the job output
    `registry_prepared` = `{base, git_tree, files: {path: sha256}}`. The upload step exports
    `registry_artifact_id`.

  `stage.py` refuses this phase if `CI_PUBLISH_TOKEN` was set at all
  (`test_PreparePhaseRefusesWhenACredentialIsSet`).
- **`registry-pr`** (`--only branch`) has `contents: write` only and `environment: release`,
  and is listed in `publish_jobs.json`. Its steps are checkout, the two downloads by artifact
  ID, and the one publish step. `publish_vcpkg_registry.py` imports no `subprocess`, step
  runner or toolchain module (AST test), and a full run passes with `Popen` patched to fail.
  It:
  - checks the hand-over files against `CI_REGISTRY_PREPARED` before parsing them;
  - runs the settings preflights: Task 02's S-3/S-4/S-5, plus S-6 for RP-9;
  - runs the commit preflight (constraint 5);
  - requires the base to descend from `CI_COMMIT` and to be on `master` (compare API);
  - reads the base's registry files through the **Git Data API**, checking every blob against
    its object id;
  - recomputes the `git-tree` in Python;
  - applies the judge `registry_update.check_registry_change`. A compromised prepare job can
    hand over nothing but the one allowed change: the base portfile with only `REF` changed;
    one new entry prepended, with every registered entry byte-for-byte unchanged; and the
    baseline moved to it. A matching digest does not help it
    (`test_TamperedPortfileWithAMatchingDigestIsRefused`);
  - creates 3 blobs, a tree on `base_tree`, then checks the new tree's `ports/smbios-c` id
    against the `git-tree`;
  - creates a commit with parent = base and the fixed RP-10 message;
  - repeats the commit preflight, then `POST git/refs`.

**New policy rule (P12).** A listed (credentialed) publish job may not run a toolchain phase
(`--only prepare`). Fixture: `p12_publish_prepare_in_credentialed_job`.

### Conflict 2: RP-9 and constraint 10 remove the PR opening

`registry-pr` creates the **new** branch `registry/v<version>` and nothing else:

- an existing branch is a hard failure with no write (`test_ExistingBranchIsAHardFailureWithNoWrite`);
- a creation race returns 422, never a `PATCH`;
- it never calls a pulls endpoint (asserted) and never writes `master`;
- its `ok:` line is the compare URL the maintainer opens the PR from.

`pull-requests: write` is not granted (fixture `p12_registry_pr_pull_requests_write`, the RP-9
verification). No `open_registry_pr.py` exists. Success criterion 2 is amended accordingly
(above).

### Conflict 3: RP-3 and the 1.0.1 ruling

The registry is append-only (constraint 9):

- `versions/s-/smbios-c.json` keeps `1.0.0` at `483562d4…`;
- the judge refuses to re-register any registered version
  (`test_AlreadyRegisteredOrOlderVersion`);
- `x-add-version` runs without the overwrite switch;
- the overwrite switch appears in no script, workflow or documented procedure. A test scans
  `scripts/`, `.github/workflows/`, the portfile and `vcpkg-registry.md`
  (`NoOverwriteSwitchTest`).

The first automated release is `1.0.1`, as ruled.

### The append-only lint (constraint 9), and full history

`check_vcpkg_registry.py` adds three history-wide checks:

5. every `(version, port-version)` recorded by any commit reachable from `HEAD`
   (`git log --full-history --topo-order`) still exists with the same `git-tree`;
6. the `REF` read **from each registered tree** (not from the working copy) is an ancestor of
   `origin/master`, else `master`;
7. where a `v<version>` tag exists, `REF` equals the tag's dereferenced commit.

It also checks that the baseline names an existing entry and that it is the highest. Each
demonstration fixture fails with exactly one line; the real tree passes (gates below).

**Full history.** The lint job's checkout now has `fetch-depth: 0`, which also fetches every
branch, so `refs/remotes/origin/master` and every tag are present. A shallow checkout fails
closed (`git history is shallow: ... (fetch-depth: 0)`), and so does a missing `master`. The
`registry-prepare` checkout has `fetch-depth: 0` too. Its shared clone gets `refs/ci/master`
and the tags, so the same lint runs there with `--master-ref refs/ci/master`.

**The one exemption, recorded here and in status.md.** Story 02.0 regenerated `versions/`
twice while `1.0.0` was still under review (`e63c659` → `06cb80b5…`, `e1e3add` →
`6a4f0020…`). Both commits reached `master` through PR #17's merge commit, and the published
value is `483562d4…` (`ff56d77`). A strict history check is therefore red on the real tree
(demonstrated: removing the exemption gives
`smbios-c 1.0.0#0 git-tree: expected 06cb80b5f6e2 (recorded e63c65984f87) -> actual
6a4f002069e7 (rewritten e1e3addf7fca; ...)`). `SUPERSEDED` lists exactly those two trees, by
value, for `(1.0.0, 0)` only. A test pins the list (`test_SupersededListIsExactlyTheTwoPrePublicationTrees`).
It cannot make `HEAD` carry another `1.0.0` tree: the current value must still equal
`483562d4…`. This is **not** RP-3's rejected option (b), which would have allowed a *future*
overwrite of 1.0.0. **For security review:** confirm that neither superseded tree was ever
announced as a baseline.

### Task 01's procedural defect, fixed: `master` is never red

D5 required the port version **and** the registry baseline to equal the root VERSION in every
commit. Reordering the steps alone cannot satisfy that. The tagged release commit `C` must
carry `version = X.Y.Z` (the tag check). But the `X.Y.Z` entry records a `REF` equal to `C`
itself, so it can only exist in a later commit: the lint was bound to be red at `C`. The fix
follows the threat model's own file list for `registry-prepare` (portfile, `baseline.json`,
`versions/s-/smbios-c.json`). The baseline moves **with** the entry:

- `version_sites.json`: the baseline row's rule is now **`registered`**, not newer than the
  authoritative value (`RegisteredRuleTests`). `prepare_release.py` edits only `equal` sites,
  so it no longer touches the baseline. Its tests were updated to match, and a test pins that
  even a non-canonical baseline is left alone;
- `check_vcpkg_registry.py` accepts **pending registration**: the port is strictly newer than
  every entry, and the baseline still names the newest entry
  (`ok: ...; 1.0.1 pending registration (newest registered 1.0.0)`). Any other mismatch is
  still red, including a baseline bumped ahead of its entry;
- ADR 0005 D5/D6 carry a dated amendment; the architect still has to ratify it.

So: the release commit is green; the tag run's lint is green; `registry-prepare` produces the
entry; the merged branch is green and consistent.

### Constraint 8 (pre-release tags never touch the registry)

`plan.py` gives a `v<X.Y.Z>-<pre>` tag no `registry-prepare`/`registry-pr` rows, nor a tag
outside the release grammar (`PreReleaseRegistryTests`). That is driven by the
`publish_jobs.json` keys `prerelease: false` and `prepare_job: registry-prepare`. `ci.yml`
passes `registry: contains(plan.publish, '"registry-pr"')` into `stage-publish.yml`, whose two
registry jobs require it. Both phases also refuse a pre-release in code, before any git or
request.

### Which constraints are enforced where

| Constraint | Test-enforced | Policy-enforced (gate) | Unproven until settings / a real tag |
|---|---|---|---|
| 1 No toolchain in a credentialed job | AST test; full branch run with `Popen` patched; prepare refuses with a token | P12 step shape (Task 02) + new rule: no `--only prepare` in a listed job (`p12_publish_prepare_in_credentialed_job`) | - |
| 2 Credential in one place | AST test (no `subprocess`/`ci_step`/toolchain imports); token leaves `os.environ` | P8 (Task 02): `secrets.` only in the publish step's `env:` | - |
| 3 Token never leaks | Canary on success, 401/422/500 and crash paths; every request goes only to the API host | - | Real runner log masking |
| 4 Manifest anchored | Same `_publish_inputs` path as Task 02; hand-over files checked against the out-of-band digests before parsing (`HandoffTests`) | `download-artifact` by `artifact-ids` (both downloads) | The first CI run of `registry_prepared`/`registry_artifact_id` |
| 5 One release, one commit; `REF` = `manifest.commit` | `PreflightTests` (tag moved before the ref write, base not descending, base off `master`, other `REF`); `PortfileRefTests` | Tier guard (`v*` tag push) | Real compare/ref API responses |
| 7 Create-only | `BranchTests` (existing branch, 422 race, never `PATCH`) | - | Real API behaviour |
| 8 Pre-release never touches the registry | `PreReleaseRegistryTests`; both phases refuse in code | `if: inputs.registry` on both jobs | The first `-rc` tag run |
| 9 Append-only | `HistoryTest` (one fixture per failure), `JudgeTests`, `NoOverwriteSwitchTest`, `RealRepositoryTest` | Lint `registry` step on every event, `fetch-depth: 0` | - (S-1/S-2 are the actual boundary) |
| 10 Branch only, a human opens the PR | `BranchTests` (no pulls call; exactly blob/tree/commit/ref writes) | P12: `registry-pr` `contents: write` only (`p12_registry_pr_pull_requests_write`) | **S-6 not applied or read back**; the S-6 drift preflight is tested against the stub only |
| 12 Settings first | `test_RealRegistryShipsVcpkgRegistryDisabled` | - | **S-1..S-8 not applied or read back** |

**Unverifiable here.**
- The real `vcpkg x-add-version --verbose` on CI, and the real `git-tree` it writes. It is
  computed three ways (git, Python, the judge) and checked for equality, but only against the
  Python stand-in.
- The cold `registry-prepare` duration. Its 30-minute job budget (inventory C11's ceiling; the
  prepare step has 24) rests on an **estimated** 15-minute median: the tag-run packaging legs
  already build Boost cold within the 18-minute build budget. Story 02.0's single cold run took
  34 minutes on a developer Mac. If the first tag runs exceed 30, that is a C11 decision for
  the maintainer, not a silent raise.
- The installed-file allowlist on Linux (`.so`) and Windows (`.dll`): the list is taken from
  the arm64-osx packages only.
- Whether vcpkg fetches a `file://` registry from the shared clone (it did from a plain
  repository in Story 02.0).
- The Git Data API's real responses.
- RP-11: `registry-pr` uses `environment: release` (P8 requires environment = tier), which is
  a third reviewer approval per release. A separate `release-registry` environment would
  need a P8/loader change and its own setting; that is left to the maintainer.

**Checked here.** vcpkg 2026-07-27 accepts a **tag** as a registry `reference`: `vcpkg install
--dry-run` against a scratch copy of this registry tagged `v1.0.0` resolved `smbios-c 1.0.0 --
git+file://…@483562d4…`; a tag that does not exist failed with exit 1. That is the RP-3
consumer note in `vcpkg-registry.md`.

### Gates (local, macOS, 2026-11-18)

- `stage.py lint --reporter plain`: 8/8 green, exit 0, including:
  - `ok: scripts/ci tests (409.1s)`;
  - `ok: workflow policy (0.7s)`;
  - `ok: versions: 1.0.0 (root project() VERSION), 4 sites (0.0s)`;
  - `ok: vcpkg registry: smbios-c registry consistent; history: 1 recorded, 1 REF on master, 0 tagged (0.3s)`.
- New tests: 51 (`test_registry_update`, `test_publish_vcpkg_registry`,
  `test_registry_prepare`).
- The nine affected suites: 327, OK.
- `check_doc_links.py docs/`: 337 files, 0 problems.
- `--dry-run` against a synthetic three-leg `dist/` for `v1.0.1`:
  - on a tree bumped to 1.0.1 it lists every action and `would register smbios-c 1.0.1
    port-version 0, git-tree a8678a878903 ...; the judge accepts the simulated change`;
  - on the real tree (port still 1.0.0) it says a real run refuses.
  - The same inputs without `--dry-run` are refused at `vcpkg-registry: expected enabled ... ->
    actual disabled`. A pre-release dry run lists no registry action. `--only prepare` with a
    credential set is refused (RP-2).
- Append-only demonstrations, CLI, one throwaway repository each; each fails with one line:
  - overwrite;
  - disappearance;
  - `REF` not on `master`;
  - `REF` not equal to its tag's commit;
  - shallow checkout.

  The defect-free control passes, and so does the real tree. Removing `SUPERSEDED` turns the
  real tree red on exactly the pre-publication rewrite.

