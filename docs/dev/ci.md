# CI developer notes

How CI works, how to read a run, and how to change it. Origin and full contracts: [milestone 0014](/docs/roadmap/0014-ci-rework-from-scratch/plan.md); design: [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md). Contents: [overview](#overview), [reading a run](#how-to-read-a-run), [local commands](#local-commands), [adding a check](#how-to-add-a-check), [pins](#pinned-versions-and-how-to-bump-them), [0012/0013 note](#note-for-milestones-0012-and-0013), then the reference sections (stage command, triggers, summaries, probes, lint, policy gate, budgets).

## Overview

`.github/workflows/ci.yml` (name `CI`) is the only entry workflow. Its jobs:

```text
plan -> lint -> build        (windows, macos, linux)
              -> sanitizers  (linux-asan, macos-asan)
              -> gui         (linux-gui, windows-gui, macos-gui)
        all of them -> report (always runs)
```

`plan` decides which legs run ([tier table](#triggers-concurrency-and-tiering-plan-e7)); `lint` gates the three build-type jobs (`needs: [lint, plan]`); `build`, `sanitizers` and `gui` are the reusable `stage-build.yml`, `stage-sanitizers.yml` and `stage-gui.yml`, one matrix leg per runner; `report` writes the one run-level table. Packaging stages (`package`, `verify`, `publish`) are declared in the plan. `package` (milestone 0012 Story 03.0) is the `package` job of `ci.yml` (`needs: [plan, build]`), calling `stage-package.yml`: one job per packaging leg plus a `manifest` job; `verify` (Story 04.0 Task 04) is the `verify` job of `ci.yml` (`needs: [plan, build, package]`), calling `stage-verify.yml`: one job per packaging leg on its own runner verifies its archive in a clean room, then `verify-gate` sets `verified_manifest_sha256` only if every leg passed; nightly it also runs the registry consumer check and the release re-check. `publish` (Story 04.0 Task 02) is the `publish-release` job of `ci.yml` (`needs: [plan, build, package, verify]`, `v*` tag push only), calling `stage-publish.yml`: `release-draft`, then `release-promote`. On a final `vX.Y.Z` tag (never a pre-release; Story 04.0 Task 03), `registry-prepare` (`contents: read`, no credential: it alone runs vcpkg and the registry consumer test) then `registry-pr` (`contents: write`, REST only) create the branch `registry/vX.Y.Z`. A maintainer opens the PR from it ([vcpkg-registry.md](vcpkg-registry.md#releasing-a-new-smbios-c-version)). After `release-promote`, the unprivileged `post-publish` job (`contents: read`, no credential) downloads the public release anonymously and compares it with the run's anchored manifest. Since Story 04.0 Task 05 every destination is a module of `scripts/ci/destinations/` behind ADR 0005 D9's interface, and `stage-publish.yml` also has one matrix job per tier and permission class (`publish-release-repo-write`, `publish-staging-oidc`) for single-phase destinations; a push to `master` reaches it through the second caller `publish-staging` (staging tier: `s3-staging`). The `github-release`, `vcpkg-registry` and `s3-staging` destinations ship `enabled: false`, and `stage.py publish` refuses without the same run's verify result (set only when every verify leg passed). Each of these fails closed on its own. A workflow step only calls `python3 scripts/ci/stage.py ...`; the logic lives in tested scripts under `scripts/ci/`.

**The five report contracts** (RC1-RC5, full text in [plan.md](/docs/roadmap/0014-ci-rework-from-scratch/plan.md#shared-contracts)):

- **RC1** a step prints one `ok: <label> (<n>s)` line on success; on failure one annotation, at most 20 diagnostic lines and a 40-line tail (64 lines / 16 KiB ceiling), with tool text fenced against workflow-command injection. Value comparisons print `<what>: expected <x> -> actual <y>`.
- **RC2** `scripts/ci/ci_step.py` runs every command without a shell, keeps the full log in a file, and is the only place that truncates, escapes and fences. Scripts read only the neutral `CI_*` names (`scripts/ci/env_names.py`).
- **RC3** every step appends one JSON record to `$CI_RESULTS_FILE`; summaries are built from these records, never from logs.
- **RC4** the first step of each job prints one `env:` line: job/leg, image, cmake, ctest, ninja, compiler, python, vcpkg commit, cache state.
- **RC5** a best-effort step is declared (`--best-effort "<why>"`), never fails the job, and is visible in the run table. `continue-on-error` is banned (P5).

## How to read a run

1. **Run table.** Open the run's summary page: the `report` job's `## CI: PASS|FAIL - ...` table has one row per job and a column per OS. A failure names the first failing step once, with the legs that share it. `not run: tier` / `not run: covered by push run` are not failures; `no record` is.
2. **Job block.** Each job's own summary (`### <job> (<leg>) - PASS|FAIL|PASS with <k> best-effort warnings`) holds the RC4 line and one row per step.
3. **Step excerpt.** In the failing job's log, the failing step prints its bounded block (annotation, diagnostics, tail). Other steps print one line each.
4. **Failure-log artifact.** A failing `build` leg uploads `logs-build-<leg>` (3-day retention) holding exactly `configure.log` and `build.log`. Test and live-table logs are never uploaded, `sanitizers` and `gui` upload no logs (P15), and `summary-<job>-<leg>` artifacts hold records only.

To reproduce a failure, run the same stage command locally (`python3 scripts/ci/stage.py build --leg <leg>`, see [Stage command](#stage-command)). To ask the runner a question without a full pipeline, use a [probe](#probe-workflows-and-single-job-runs-plan-e6).

## Local commands

```text
python3 scripts/ci/lint.py [--skip-tests]   # tests, actionlint, policy gate; --skip-tests is the fast half
python3 scripts/ci/run_tests.py             # the scripts/ci test suite alone
```

Both print one `ok:` line per step. The full `lint.py` is what the CI `lint` job runs (`python` instead of `python3` on Windows).

## How to add a check

1. Write the check as a script under `scripts/ci/` (stdlib-only Python, neutral `CI_*` names, no `GITHUB_*`), with tests in `scripts/ci/tests/`.
2. Call it through `ci_step` (`run_step`), so it follows RC1-RC3, from a step of `scripts/ci/stage.py`; add the step to `--only` and to the stage table below. A workflow `run:` stays a single `stage.py` line (P7).
3. Add an inventory-style row to the stage table in [Stage command](#stage-command), and to `scripts/ci/inventory.json` if it replaces a plan check.
4. Check it fails: weaken it once and see the test fail. A check that reads the live SMBIOS table prints counts only (`--hygiene counts-only`); one that may legitimately fail is declared best-effort (RC5).
5. Run `python3 scripts/ci/lint.py`. The [policy gate](#workflow-policy-gate-p1-p15) must pass, including P4 (a budget for every new workflow step in `scripts/ci/step_budgets.json`).

## Pinned versions and how to bump them

| Pin | Where | Bump by |
|-----|-------|---------|
| Runner labels (`ubuntu-24.04`, `macos-15`, `windows-2022`; no `-latest`, P3) | `scripts/ci/pins.json` `runners` | edit the label, update `runner_images_consulted`, check the Visual Studio generator still exists on `windows-*` |
| cmake, ninja (and aws CLI, recorded) | `pins.json` `tools` and `scripts/ci/requirements-tools.txt` | change both; the wheel hashes in the requirements file come from `https://pypi.org/pypi/<pkg>/<version>/json`, one wheel per runner platform (`test_print_env.py` checks the versions agree) |
| vcpkg commit | `pins.json` `vcpkg.commit` | move it together with `vcpkg.json` `builtin-baseline` and `vcpkg-configuration.json`, and refresh `tool_release_tag` and the Windows `vcpkg.exe` SHA-512 values in the same change |
| Action SHAs | `scripts/ci/actions_allowlist.json` | verify the tag commit, then see [Workflow policy gate](#workflow-policy-gate-p1-p15) |
| actionlint | `scripts/ci/install_actionlint.py` (version and SHA-256) | edit both |

Each bump is a reviewed diff; run `python3 scripts/ci/lint.py` afterwards.

## Note for milestones 0012 and 0013

`package`, `verify`, `publish` and the language-binding legs plug into this pipeline: each is a `stage-*.yml` reusable workflow called from `ci.yml`, with its logic in `stage.py` and neutral scripts ([ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md)). They use RC1-RC5 and SC1-SC4 from the plan, and must pass the policy gate. There is no `release.yml`: a release is the same pipeline on a `v*` tag. New publish targets follow [adding-a-publish-destination.md](/docs/dev/adding-a-publish-destination.md). The ADR holds the [neutral environment-name table](/docs/adr/0005-unified-build-package-publish-pipeline.md#neutral-environment-names) and the [TeamCity migration checklist](/docs/adr/0005-unified-build-package-publish-pipeline.md#migration-checklist-github-actions-to-teamcity).

## Stage command

```
python3 scripts/ci/stage.py <lint|build|test|package|verify> [--leg <leg>] [--only <step>]   # python on Windows
```

Every project-owned workflow step is one such line (plan SC1, policy P7); `.github/workflows/ci.yml` runs its own `plan` job, calls the reusable `stage-lint.yml`, `stage-build.yml`, `stage-sanitizers.yml` and `stage-gui.yml`, then runs its own `report` job. Locally the same command runs with the plain reporter and temporary directories; the leg defaults to the host OS (`linux`, `macos`, `windows`), and the build stage uses the host cmake/ninja instead of the pinned venv.

| Stage | Steps (`--only`) | Inventory rows |
|-------|------------------|----------------|
| `lint` | `env`, `self-test`, `lint`, `versions`, `registry` (`check_vcpkg_registry.py`: `versions/` match the committed `ports/smbios-c` or the port is pending registration, and the registry is append-only across the whole history; the lint job checks out with `fetch-depth: 0` for that, see [vcpkg-registry.md](vcpkg-registry.md#what-the-lint-enforces)), `cversions`, `doc-samples` | C01, C02, C21, C22 + SC3 |
| `build` | `prepare` (pinned tools, RC4 header, self-test, vcpkg cache key), `compile` (vcpkg bootstrap, configure, cache health, build), `install` (packaging legs only) | C03, C04, C11 |
| `test` | `unit` (C06, C13), `cli-formats` (C07, C08, C09), `live-table` (C10, C14, Q4; best-effort, counts-only), `consumer` (`build` job legs only: `scripts/ci/consumer_smoke.py` installs the built tree and builds `tests/packaging/libsmbios_consumer` against the installed `libsmbios` package, with no whole-archive flag of its own; milestone 0012 Story 01.0) | C06-C10, C13, C14 |
| `verify` | `deps` (pinned tools, `pkgconf` on Windows, RC4 header, pinned vcpkg, `boost-iostreams` built cold and Release-only into `$CI_TEMP_DIR/verify-deps`: the Boost a release consumer brings; a local run skips it), `artifact` (`dist/manifest.json` against `CI_MANIFEST_SHA256` before parsing, every `dist/` file against the manifest, `install_sha256` against `CI_INSTALL_SHA256`, then `verify_release_artifact.py` with `build=True`: extraction, layout and tree digest, the literal `pkg-config` + C consumer, the `find_package(libsmbios CONFIG)` consumer, the `nm`/`dumpbin` export allowlist; sets `verified_<leg>` last). Only with `--only`: `gate` (the `verify-gate` job: every packaging leg must report the anchored digest, then `verified_manifest_sha256` is set), `registry` (nightly: the vcpkg consumer test against the published registry, no overlay), `releases` (nightly: anonymous re-check of the last 5 releases against `scripts/ci/release_ledger.json`). Milestone 0012 Story 04.0 Task 04 | - |
| `package` | `env`, `unpack` (extracts the build job's one-file hand-off `$CI_ARTIFACT_DIR/install-<leg>.tar` and requires its install-tree digest to equal `CI_INSTALL_SHA256`, the build job's per-leg output; CR-8), `archive` (packaging legs only; `scripts/ci/package_archive.py`): archives `install/<leg>/` into `dist/libbios-<version>-<leg>.<tar.gz\|zip>` (reproducible), writes its `.sha256` and merges the leg into `dist/manifest.json` (ADR 0005 D4); `manifest` (`--only manifest`, the manifest job): merges the per-leg `dist-<leg>` parts into one `dist/manifest.json`. Never configures or builds; a missing hand-off exits 2 naming its path. Refuses a tree without `libsmbios` + `smbios_c` or with the `smbios` CLI/`smbios_gui` (0012 ruling I2) | - |

Legs, their preset, build configuration, runner and job, and the trigger tiers live in `scripts/ci/legs.json`. The sanitizer legs `linux-asan`/`macos-asan` (job `sanitizers`, inventory C12-C14) configure into `build/<preset>-asan` with ASan + fatal UBSan (`-fno-sanitize-recover=undefined`, `UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1`) and keep the old `ASAN_OPTIONS`. The packaging legs `linux-x64`/`windows-x64`/`macos-arm64` build the shipped configuration `Release` in `build/<leg>` (milestone 0012 ruling, 2026-11-16), which differs by design from the `-C Debug` the plain `windows` leg tests (C06). They are in `tiers.full` only (every event but a push to a branch other than `master`, ADR 0005 D7), run the `build` job's test stage against that Release tree (`unit`, `cli-formats`, `consumer`), and then `build --only install` writes `install/<leg>/` and its hand-off `install-<leg>.tar`, uploaded as one literal path (P15). The `build` job exports `install_sha256_<leg>` and `install_artifact_<leg>` (one uniquely named output per leg, so the matrix's combined outputs never collide); `ci.yml` passes them to `stage-package.yml`, which downloads by artifact ID. The run report shows them in their own `build (packaging)` row.

ctest runs go through `scripts/ci/ctest_report.py` and the named sets in `scripts/ci/test_sets.json` (`unprivileged`, `live_table`, `gui_ci`). The exit code decides; the `ok:` counts come from `ctest --output-junit` (`$CI_TEMP_DIR/junit-<label>.xml`), never from ctest's summary text, and an unreadable JUnit file prints `counts unavailable`. Live-table sets run counts-only and their logs and JUnit files are never uploaded. A leg outside the tier of `CI_EVENT`/`CI_REF` prints one `skip:` line. `--leg`, `--only`, `CI_EVENT` and `CI_REF` are re-validated; an unknown value exits 2 without being echoed. Packaging legs (`linux-x64`, `windows-x64`, `macos-arm64`) are declared but pending until milestone 0012 names the shipped configuration and adds the `libsmbios` install rules.

## Triggers, concurrency and tiering (plan E7)

```
python3 scripts/ci/stage.py plan                                  # ci.yml `plan` job
python3 scripts/ci/classify_changes.py [--base <rev> | --against-master] [--head <rev>]
```

`ci.yml` runs on a push to any branch except `probe/**`, a pull request to `master`, a `v*` tag push and a nightly schedule. There is no `workflow_dispatch` trigger (maintainer ruling) and no `paths:`/`paths-ignore:` filter: a workflow skipped by path never reports (plan Q5), so docs-only changes are classified inside the run and the skipped legs are reported. A `v*` tag run is always the full pipeline: `matrix_for` forces `full` for the `tag` event, so a docs-only classification never degrades a release.

Concurrency: `group: ci-<workflow>-<PR number or ref>`. A newer run cancels a superseded one, except on `refs/heads/master` and on `refs/tags/`, which are never cancelled.

The `plan` job (Linux, 2 minutes, `needs` nothing, full-history checkout without persisted credentials) runs `stage.py plan` (`scripts/ci/plan.py`). It first classifies the changes with `classify_changes.py`: `git diff --name-only --no-renames <base>...<head>`. The base depends on the event:

| Event | Diff base |
|-------|-----------|
| `pr` | the PR base (`CI_BASE_COMMIT` = `github.event.pull_request.base.sha`) |
| `push` to `master`, `tag` | the commit before the push (`CI_BASE_COMMIT` = `github.event.before`) |
| `push` to any other branch | `git merge-base refs/remotes/origin/master <head>`; `CI_BASE_COMMIT` is ignored |
| `schedule` | none, so `full` |

The merge base makes a branch's classification depend on everything the branch changes relative to `master`, not only on its last push: a branch with any code change is `full` on every push. So a docs-only push that cancels the run of an earlier code push (concurrency) still runs the Linux legs on the head commit, and the same-repo PR run can rely on them (`covered by push run`). The plan job's `fetch-depth: 0` checkout fetches every branch, so `refs/remotes/origin/master` is normally present; if it is not, `classify_changes.py` tries one `git fetch --no-tags origin +refs/heads/master:refs/remotes/origin/master` (argv list, no credential helper, no prompt; credentials are not persisted, P14). If master still cannot be resolved, or the branch shares no history with it, the result is `full`.

The result is `docs` only when every path is under `docs/` or a root-level `*.md`; anything else (including `tests/data/`, `scripts/`, `examples/` and `.github/`) is `full`. A missing, all-zero or non-ancestor base (a force-push, or no `before` at all), a git failure and an empty diff are all `full`. It prints one `ok: changes: <docs|full> (<n> paths)` line. Then `plan.matrix_for(event, ref, classification, pr_same_repo=...)` applies the table below to `scripts/ci/legs.json` `tiers`:

| Event (`CI_EVENT`, `CI_REF`) | lint | build | sanitizers | gui |
|------------------------------|------|-------|------------|-----|
| any event, `docs` changes | yes | - | - | - |
| `push` to a branch other than `master` | yes | linux | linux-asan | linux-gui |
| same-repo `pr` to `master` | yes | windows, macos + linux-x64, windows-x64, macos-arm64 | macos-asan | windows-gui, macos-gui |
| fork `pr` to `master`, `push` to `master`, `tag` (`v*`), `schedule` | yes | linux, windows, macos + linux-x64, windows-x64, macos-arm64 | linux-asan, macos-asan | linux-gui, windows-gui, macos-gui |

A same-repo PR runs only the legs the push tier did not run: status checks attach to the commit SHA, so its push run already reports the Linux legs, and the report shows them as `not run: covered by push run`. A fork PR has no push run in this repository and runs every leg. `CI_PR_SAME_REPO` is `true` only for a same-repo PR (`github.event.pull_request.head.repo.full_name == github.repository`, the one whole expression policy P10 allows with that context); anything else plans as a fork PR. A leg the plan does not run shows `not run: tier`.

Milestone 0012 stages (ADR 0005 extension). A stage not defined yet has the job output `[]` and the report shows `not run: stage not defined` where the tier would run it, `not run: tier` elsewhere. `package` is defined: where the tier runs it its output is the three packaging legs and the report row says `run: result in its own jobs` (its jobs publish no summary artifact yet):

| Event | package | verify | publish |
|-------|---------|--------|---------|
| any event, `docs` changes; `push` to a branch other than `master` | - | - | - |
| same-repo or fork `pr`, `schedule` | yes | yes | - |
| `tag` (`v*`) | yes | yes | `release`-tier jobs only |
| `push` to `master` | yes | yes | `staging`-tier jobs only |

Publish rows come only from `scripts/ci/publish_jobs.json` and `scripts/ci/publish_destinations.json` (`release-draft` and `release-promote` since 0012 Story 04.0 Task 02; `registry-pr`, preceded by its unlisted `prepare_job` `registry-prepare`, since Task 03, never on a pre-release tag; since Task 05 one row per scheduled single-phase destination in its class matrix job, also as the outputs `publish_repo_write`, `publish_oidc`, `publish_token_only`; a `release`-tier destination is scheduled on a tag even while disabled, so the run fails loudly, a `staging`-tier one on a `master` push only once enabled), and only while `verify` is defined (since Task 04; without it `publish` shows `not run: stage not defined`). There is no other path to `publish`; `CI_EVENT=manual` is rejected by the plan. The unprivileged `post-publish` job is not a plan row: it is part of the `publish-release` call and needs `release-promote`.

**After a release** (Task 04): the `post-publish` job's last `info:` line is the row to append to `scripts/ci/release_ledger.json`. Add it through a normal PR (with the registry PR for a final release, on its own for a pre-release). The nightly `release-recheck` job uses that row as the release's out-of-band anchor, and fails on a published release that has none (threat model constraint 11, RP-7). Rows are never edited or removed.

The plan sets the job outputs `build`, `sanitizers`, `gui`, `package`, `verify`, `publish`, `publish_repo_write`, `publish_oidc`, `publish_token_only` (JSON lists) and `plan` (the whole plan: per-leg and per-stage reasons, the pinned runner label of every scheduled leg), all through `Reporter.set_output()` (single-line JSON, random delimiter; CR-6/CR-8). `ci.yml` passes each leg list to its stage workflow as the `legs` input, whose matrix is `fromJSON(inputs.legs || '<full-tier legs>')`, and skips the job with `if: needs.plan.outputs.<job> != '[]'` (an empty matrix is an error). `lint` always runs. `CI_EVENT` (`push`/`pr`/`schedule`/`tag`), `CI_REF` (shape per event), `CI_BASE_COMMIT`/`CI_COMMIT` (hex SHA or allowlisted ref name) and `CI_PR_SAME_REPO` (`true`/`false`/empty) are re-validated; a value outside its allowlist or containing CR/LF exits 2 without being echoed and without setting any output. A tier leg that is not declared, pending, of an unknown job, or whose runner has no label in `scripts/ci/pins.json` also exits 2. `scripts/ci/tests/test_stage.py` checks that every stage workflow's fallback list is its job's full tier and that the plan's list for every `ci.yml` trigger equals the Q1 tier.

## Job summaries and the run report (plan E9)

```
python3 scripts/ci/stage.py summary [--leg <leg>] [--job-failed]   # last step of every job
python3 scripts/ci/stage.py report                                 # ci.yml `report` job
```

`summary` (`scripts/ci/job_summary.py`) reads the job's RC3 records (`$CI_RESULTS_FILE`) and the RC4 line `print_env.py` handed off, and publishes one block of at most 30 lines and 4 KiB through the reporter's `job_summary` event: a `### <job> (<leg>) - PASS | FAIL | PASS with <k> best-effort warnings` header, the RC4 line, one table row per record (detail at most 120 characters, failing and best-effort rows kept first, the rest folded into `(+k more)`), and `first failure:` when the job failed. Every stage job ends with two summary steps: `if: success()` without flags, and `if: failure() || cancelled()` with `--job-failed`, which adds a synthetic `job` failure when no record failed (a step timeout, a cancel, a failing platform step), so such a job is never PASS. It also writes `$CI_ARTIFACT_DIR/summary-<job>-<leg>.json` (at most 8 KiB, records only, no log content), uploaded as the artifact `summary-<job>-<leg>` (3-day retention). It always exits 0, except for a usage error.

`report` (`scripts/ci/run_report.py`) runs in the `report` job of `ci.yml` (`needs` plan, lint, build, sanitizers and gui, `if: always()`, `contents: read`). `actions/download-artifact` puts every `summary-*` artifact into `$CI_TEMP_DIR/summaries`, and the job writes one table of at most 40 lines: `## CI: <PASS|FAIL> - <n> legs passed, <f> failed, <w> with warnings, <s> not run (tier)`, one row per job with a linux/windows/macos column each, the first failure of the job's legs said once with the legs that share it, and one `versions:` line per distinct cmake/ctest/compiler set. The expected legs come from the plan job's `plan` output (`CI_PLAN`, re-validated by `plan.parse_plan`; a malformed value exits 2): a leg the plan does not run shows `not run: tier` or `not run: covered by push run` (counted as `, <c> not run (covered by push run)` in the header), a planned leg without a readable artifact `no record` (and fails the run); none of them is ever PASS. Without a plan (a local run, or a failed plan job) the `legs.json` tier of `CI_EVENT`/`CI_REF` decides. Rows `package`, `verify` and `publish` follow the leg jobs and carry only their reason (`not run: stage not defined` until milestone 0012). When a leg failed or has no record, the report adds the one run-level error annotation, `<n> legs failed: <list>` (title `CI`, the reporters' `error` event), and exits 1. All tool text is rendered inside code spans, so Markdown or HTML in it is inert.

Version consistency (`lint` step `versions`, plan SC3): `python3 scripts/ci/check_versions.py [--tag <name>]` checks every site in `scripts/ci/version_sites.json` against the root `project()` VERSION; until milestone 0012 adds that VERSION, `SMBIOS_C_VERSION` stands in for it.

## Probe workflows and single-job runs (plan E6)

```
git switch -c probe/<topic>
cp docs/dev/ci-probe-template.yml .github/workflows/probe-<topic>.yml   # keep only what the question needs
git push -u origin probe/<topic>
```

Debugging CI should not cost a full ~30-minute pipeline cycle. A probe is a throwaway workflow on a `probe/<topic>` branch, and it answers one of two questions:

- **"What is true on the runner?"** (DNS, a tool version, a file's mode). The template's `probe` job runs on every pinned label of `scripts/ci/pins.json` (comment out the ones you do not need). It prints the RC4 line through `stage.py lint --only env`, with `image=<label>` from `CI_IMAGE`, so `ctest --version` and the other tool versions need no extra step. It then runs the `# PROBE STEPS` section. The example there is a DNS probe of the apt hosts: `stage.py build --only packages` on the `linux-gui` leg prints `info: apt mirror: <default|kernel.org> (<k> of <n> hosts unresolved: ...)`, then installs the GUI packages. Policy P7 holds here too: every `run:` is one `python3 scripts/ci/stage.py ...` line. A question no stage step answers yet gets a `--only` step in `stage.py` on the probe branch itself, which is never merged.
- **"Does job X pass on leg Y now?"** Replace the template's `jobs:` block with one of its commented callers. Each is a job-level `uses: ./.github/workflows/stage-<job>.yml` with `legs: '["<leg>"]'`, the same `legs` input `ci.yml` passes from its plan job. `stage-lint.yml` takes no input. The probe does not touch `ci.yml` and does not wait for the plan, lint or the other legs. A caller job has no `timeout-minutes`, `permissions` or `secrets` of its own: the stage workflow brings its own budgets and `contents: read`, and no stage workflow needs a secret. On a `probe/**` push, `stage.py` applies the branch-push tier (plan Q1), so only `linux` (build), `linux-asan` (sanitizers) and `linux-gui` (gui) can run. Any other leg exits 2 (`scheduled on CI but not in the push tier`). To test a Windows or macOS leg, use a same-repo pull request from a normal branch.

There is no `workflow_dispatch` (maintainer ruling), so `gh workflow run` is not used. The template's only trigger is `on: push: branches: ['probe/**']`. The probe calls the same stage scripts as the pipeline, so the idea carries over to TeamCity unchanged: a build configuration on a probe branch that runs the same `stage.py` command.

Probe rules (also in the template header):

1. Branch `probe/<topic>`, never merged.
2. One question per probe. Print only the answer, in RC1 form.
3. No secrets, no `permissions` beyond `contents: read`, and no firmware values. A probe that reads the live table prints counts only.
4. Record the answer and the run ID in the relevant `status.md`, then delete the branch.

**P9 stray-probe rule.** `ci.yml` ignores `probe/**` pushes. On any other branch, on a pull request and on `master`, a `.github/workflows/probe-*.yml` is the violation `stray probe workflow: expected 0 -> 1 (<file>)`, so the gate fails before a probe can reach `master`. On a `probe/**` branch the file is allowed. It is then checked against every workflow rule, and its trigger must be `push` to `probe/**` only. `ALLOWED_WORKFLOW_FILES` itself stays `ci.yml`, `stage-*.yml`. The gate learns the branch from neutral inputs:

| Input | Branch |
|-------|--------|
| `CI_REF=refs/heads/<name>` | `<name>` |
| `CI_REF` set to anything else (a PR merge ref, a tag, even one named `probe/...`) | none |
| `CI_REF` unset, `CI_PLATFORM` not `local` | none |
| `CI_REF` unset, local run | `git symbolic-ref --short HEAD`, or none when detached or on error |

"None" is never a probe branch, so the rule fails closed. `CI_REF_NAME` is not used, because a tag can also be called `probe/x`. `scripts/ci/tests/test_check_workflow_policy.py` runs the gate on the template and on each of its commented callers as fixtures, so the template stays compliant when the rules change.

## Lint: one command, locally and in CI

```
python3 scripts/ci/lint.py        # python on Windows
```

It runs three steps through the step runner. On success it prints one `ok:` line per step:

1. `scripts/ci/run_tests.py`: every `scripts/ci` test.
2. actionlint 1.7.12, through `scripts/ci/install_actionlint.py --run`. The first run downloads the pinned release asset for the host into `build/ci-tools/`. Its SHA-256 is checked against the value in the script. Only the single `actionlint` member is extracted, into a fresh temporary directory. Later runs re-hash and reuse the cached archive. If `shellcheck` is on `PATH`, actionlint also checks `run:` blocks; the `ok:` line says whether it was found.
3. `scripts/ci/check_workflow_policy.py`: the policy gate below.

The workflows under test are `.github/workflows/ci.yml` (since the Story 03.0 cutover) and the reusable `stage-*.yml` files it calls. While none exists, step 2 prints one `skip:` line.

## Workflow policy gate (P1-P15)

```
python3 scripts/ci/check_workflow_policy.py [WORKFLOW...]
python3 scripts/ci/check_workflow_policy.py --verify-tags   # reviewer only: needs network and gh
```

The rules are specified in [0014 Story 02.0 Task 03](/docs/roadmap/0014-ci-rework-from-scratch/02.0-pinned-toolchain-and-hardened-environment/03-workflow-lint-and-policy-gate.md). Output is one `ok:` line, or at most 20 lines of the form `<file>:<job>/<step>: <rule>: expected <x> -> actual <y>`, plus `(+k more)`. The exit code is 1 on any violation.

The gate reads these data files. A change to any of them is a reviewed one-line diff:

| File | Rule | Holds |
|------|------|-------|
| `scripts/ci/actions_allowlist.json` | P13 | Every allowed `uses:` with SHA, version, justification, TeamCity equivalent and the date its tag commit was verified |
| `scripts/ci/publish_jobs.json` | P8, P12 | The only jobs that may hold a publish permission or read a secret, and the callers of `stage-publish.yml`: `release-draft`, `release-promote` and their caller `publish-release` (0012 Story 04.0 Task 02); `registry-pr` with `contents: write` only (Task 03); the class matrix jobs `publish-release-repo-write` and `publish-staging-oidc` and the caller `publish-staging` (Task 05). Each job is bound to one destination and phase, or is `"matrix": true`. `registry-prepare` is deliberately not listed: it holds no credential. |
| `scripts/ci/publish_destinations.json` | P12 | The publish destination registry (ADR 0005 D9): `github-release` and `vcpkg-registry` (shipped `enabled: false` until settings S-1..S-8 are read back) and `s3-staging` (also needs S-9). |

For every job listed in `publish_jobs.json`, the gate also enforces the 2026-11-18 threat model's constraints 1 and 2. The steps may only be `actions/checkout`, `actions/download-artifact` with `artifact-ids:` (never `name:` or `pattern:`), and exactly one `python3 scripts/ci/stage.py publish --destination <name>` step (P12). That step may not run a toolchain phase (`--only prepare`, Story 04.0 Task 03, RP-2), which belongs in a job without credentials. A `secrets.` reference may appear only in that step's `env:`, under a `CI_PUBLISH_*` key (P8). Since Task 05: a bound job's step must be exactly `--destination <its destination> --only <its phase>`; a class matrix job's step is exactly `python3 scripts/ci/stage.py publish` with `CI_DESTINATION: ${{ matrix.destination }}`. For the registry, P12 checks every entry, enabled or not: an existing module, a known tier and class, an environment equal to the tier, `binary_cache_policy` matching the tier, `CI_PUBLISH_*` credentials that the entry's job maps, a job of its tier and class (a bound one, or the class matrix job), that job's guard reaching only its tier's event (a `release` destination only from a `v*` tag), and, for `repo-write` and `token-only`, no child process or code evaluation in the module or any `scripts/ci` module it imports (RP-2).
| `scripts/ci/step_budgets.json` | P4 | Job time budgets (table below) |
| `scripts/ci/pins.json` | P3 | Runner labels and tool versions (Story 02.0 Task 01) |

To change the allowlist, check that the pinned SHA is the upstream tag's commit before you record `tag_commit_verified_on`. Run `gh api repos/<owner>/<repo>/git/ref/tags/<version>`, and dereference an annotated tag with `gh api repos/<owner>/<repo>/git/tags/<sha>`. `--verify-tags` does this lookup for every entry.

The gate keeps honest code honest. It is not the security boundary, because whoever can edit a workflow can also edit the gate in the same commit. The boundary is the repository settings in [ADR 0005 "Repository settings required"](/docs/adr/0005-unified-build-package-publish-pipeline.md#repository-settings-required).

## Time budgets (P4)

Every job has `timeout-minutes`. So does every step that has `run:` or `uses:`. A job's budget is 2x the median duration of its slowest leg, rounded up to whole minutes. The medians are from the Story 01.0 Task 01 baseline: the median of the last 5 green `master` runs of `ci.yml`, recorded in milestone 0014's `status.md` on 2026-11-12. The gate recomputes each budget from its medians, so the table and the data cannot drift apart.

| Job | Median per leg (s) | Budget (min) |
|-----|--------------------|--------------|
| `build` | linux 261, macos 230, windows 534 | 18 |
| `sanitizers` | linux 255, macos 306 | 11 |
| `gui` (baseline: the old `gui-build` job) | linux 310, macos 346, windows 654 | 22 |
| `registry-prepare` (estimate, 0012 Story 04.0 Task 03) | linux 900 | 52 (cold-build ruling; formula 30) |
| `verify` (estimate, 0012 Story 04.0 Task 04) | linux 420, macos 420, windows 840 | 57 (cold-build ruling; formula 28) |
| `registry-consumer` (nightly; estimate) | linux 1500, macos 1500, windows 1800 | 60 (job timeout 47) |
| any other job | no baseline | 10 (`default_job_minutes`) |

**Cold-build ruling (maintainer, 2026-11-18).** A step dominated by a cold dependency build gets 45 minutes: `registry-prepare/prepare`, `verify/deps` (and `-win`), `registry-consumer/registry` (and `-win`). The evidence is the one real cold port build, 34 minutes on a developer Mac (milestone 0012 Story 02.0). A job entry records it as `ruling_minutes` plus a mandatory `ruling` text, and P4 then requires `budget_minutes = max(2 x median, ruling_minutes)`. A ruling can only raise a budget, and the medians stay honest estimates. `registry-prepare` (52), `verify` (57) and `registry-consumer` (47) therefore **exceed inventory C11's 30-minute job ceiling by ruling**; `parity_check.py` allows each only up to its recorded `ruling_minutes` and keeps 30 for every other job. Fast steps are not raised.

A step's timeout may not exceed its job's timeout. `step_budgets.json` `steps` sets a tighter per-step budget, keyed `<job>/<step id>`. These values are configured estimates; no per-step baseline has been measured:

| Step | Budget (min) |
|------|--------------|
| `lint/checkout`, `build/checkout`, `build/upload-logs` | 3 |
| `lint/stage` | 8 |
| `build/prepare`, `build/prepare-win` | 6 |
| `build/cache-restore`, `build/cache-save` | 5 |
| `build/compile`, `build/compile-win` | 15 |
| `build/test`, `build/test-win` | 8 |
| `sanitizers/checkout` | 3 |
| `sanitizers/cache-restore`, `sanitizers/cache-save` | 4 |
| `sanitizers/prepare` | 5 |
| `sanitizers/test` | 6 |
| `sanitizers/compile` | 10 |
| `gui/checkout` | 3 |
| `gui/cache-restore`, `gui/cache-save` | 5 |
| `gui/prepare`, `gui/prepare-win` | 6 |
| `gui/test`, `gui/test-win` | 6 |
| `gui/packages` (C15, Linux apt) | 10 |
| `gui/compile`, `gui/compile-win` | 18 |
| `<job>/summary`, `<job>/summary-failed` (and `-win` twins), `<job>/upload-summary`, `report/checkout`, `report/download`, `report/report` | 2 |
| `verify/checkout`, `verify/download-dist`, `verify-gate/checkout`, `verify-gate/gate`, `registry-consumer/checkout`, `release-recheck/checkout`, `post-publish/checkout`, `post-publish/download-dist` | 2 |
| `verify/deps`, `verify/deps-win`, `registry-prepare/prepare`, `registry-consumer/registry`, `registry-consumer/registry-win` (cold-build ruling) | 45 |
| `verify/artifact`, `verify/artifact-win`, `release-recheck/releases` | 8 |
| `post-publish/post-publish` | 6 |

A job that calls a reusable workflow (`uses:` at job level) cannot carry `timeout-minutes`, so the budget applies to the jobs inside the reusable workflow instead.

The medians were measured with a warm vcpkg cache. The first runner run of the rebuilt pipeline (push, Linux legs) was a cold cache (59-76 packages built from source) and fit: `build (linux)` 3m41s, `sanitizers (linux-asan)` 5m00s, `gui (linux-gui)` 5m31s. Windows and macOS cold runs are not recorded.
