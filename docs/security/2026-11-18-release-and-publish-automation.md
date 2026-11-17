# Threat Model - Release and publish automation (milestone 0012, Story 04.0) - 2026-11-18

This review was written before implementation, while all five tasks of
[Story 04.0](/docs/roadmap/0012-package-manager-distribution/04.0-release-and-publish-automation/README.md)
were still ⬜. It covers:

- the five task specs (`01-version-source-and-release-prep.md` to
  `05-publish-destination-plugins.md`);
- [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md): D4, D5, D6, D7 and D9, plus
  "Repository settings required" and its open questions;
- the milestone [plan](/docs/roadmap/0012-package-manager-distribution/plan.md) and
  [status](/docs/roadmap/0012-package-manager-distribution/status.md);
- the foundation as it is built today:
  - `scripts/ci/check_workflow_policy.py` (P1-P15);
  - `scripts/ci/publish_destinations.json` (empty) and `scripts/ci/publish_jobs.json` (empty);
  - `scripts/ci/plan.py`, `scripts/ci/stage.py`, `scripts/ci/package_archive.py`,
    `scripts/ci/check_versions.py`, `scripts/ci/reporters/__init__.py`;
  - `.github/workflows/ci.yml` and `stage-*.yml`;
  - the self-hosted registry (`ports/smbios-c/`, `versions/`) and
    [docs/dev/vcpkg-registry.md](/docs/dev/vcpkg-registry.md);
  - [docs/dev/adding-a-publish-destination.md](/docs/dev/adding-a-publish-destination.md).

**Standing input.** Every implementation briefing for Story 04.0 Tasks 01-05 must repeat the
section [Standing constraints for implementers](#standing-constraints-for-implementers). Every
finding and per-task requirement of [2026-11-13-ci-rework.md](2026-11-13-ci-rework.md) still
holds, CR-1, CR-2, CR-3, CR-5 and CR-8 in particular. Where this document conflicts with a task
spec, this document wins; those places are marked **[OVERRIDES SPEC]**.

**Facts taken as given.**

- Milestone 0014 is complete.
- The `package` stage is live (CI run 44112333813, 18 jobs green).
- Archives carry a relocatable `smbios_c.pc` and a per-leg `arch` field.
- The registry is hosted **in this repository**, and consumers pin a commit SHA. This was decided
  in status.md on 2026-11-16, so Task 03's "open maintainer decision" sentence is stale.
- `master` has no branch protection (0014 Q5).
- `.github/CODEOWNERS` does not exist.
- No `v*` tag exists yet.

## Assets & trust boundaries

**Assets**

- The integrity claim "the bytes a consumer downloads are the bytes this commit built and tested"
  (DD1). This covers the GitHub Release assets, `manifest.json`, and the release's tag and the
  commit it names.
- The registry chain a consumer resolves: `versions/baseline.json`, then
  `versions/s-/smbios-c.json` (`git-tree`), then the `ports/smbios-c/` tree (`portfile.cmake` is
  executed by vcpkg), then the `REF` commit (its `CMakeLists.txt` is executed at configure). Every
  link in this chain is **code that runs on consumer machines**.
- Publish credentials:
  - the job `GITHUB_TOKEN` with `contents: write` / `pull-requests: write`;
  - future environment secrets (`token-only` class);
  - the OIDC ID token, and the AWS session it buys (`oidc` class).
- `master` itself. It is the registry's version database for every consumer (see RP-3).

**Threat actors** (A1-A4 as in 2026-11-13)

- A1: a fork-PR author.
- A2: a collaborator, or a stolen token with `repo`/`workflow` scope, that can push branches and
  tags and push straight to `master` (Q5).
- A3: a compromised upstream: an action, a vcpkg port download or build script, a runner-image
  tool, `pip` wheels.
- A4: hostile data: REST responses, PR titles in generated notes, redirect targets.
- A5 (new): **the maintainer's own mistakes**: tagging the wrong commit, re-running a half-failed
  release, the `--overwrite-version` foot-gun.

**Boundaries**

- B1: event to the YAML that runs. For `push`/tag/`pull_request`, this is the triggering ref's
  YAML.
- B6: publish job to GitHub Releases, `master`/registry, and S3.
- B9 (new): published registry state to consumer `vcpkg install` (consumer code execution).
- B10 (new): public release URL to consumer download.

### Where the publishing boundary actually is

For every event that can reach `publish`, GitHub runs the workflow YAML of the ref being
published. ADR 0005 D6 says so. As a result, every in-repo guard is written by whoever last
changed that ref:

- the `if:` tag guard;
- `needs: verify`;
- `environment: release`;
- P8/P12;
- the ancestry check.

That makes them defence in depth against **mistakes** (A5) and a check that the code stays
honest. They are not a boundary against A2. Most important, **the `release` environment does not
scope `GITHUB_TOKEN`** (RP-1). Here is what each control actually stops:

| Control | Stops | Does not stop | Lives in |
|---|---|---|---|
| `if:` tag guard, `needs: verify`, P8/P12 | A5; an accidental publish from a PR/branch | A2 (edits them in the same commit) | Code (triggering ref) |
| `environment: release` + required reviewer | Unreviewed use of **environment** secrets and OIDC `sub=…:environment:release` | Any job, on any branch, that requests `permissions: contents: write` for its own `GITHUB_TOKEN` | Repo setting |
| Tag ruleset on `v*` (create/update/delete: maintainers only) | A2 without bypass creating or moving a release tag | Changes to `master` before the maintainer tags it | Repo setting |
| `master` ruleset (PR + code-owner review, no force-push) | A2 changing the YAML, scripts or `versions/` that the next tag run executes or consumers resolve | A stolen **admin** credential | Repo setting (absent today, Q5) |
| Immutable releases | Asset or tag replacement after promotion | Tampering in the draft window | Repo setting |
| No repository/organisation-level secrets | Any branch workflow reading a publish secret | - | Repo setting |
| Cloud trust policy (`sub` = `…:environment:<tier>`) | Role assumption from another repo, branch or environment | Anything the environment's own deployment rule allows | Cloud setting |

The only boundary against A2 is the bottom five rows. Every one of them is a setting, and none of
them is applied today.

## Findings

Severity assumes the tasks were implemented exactly as specified, without this document. Every
finding names the attack, its precondition, the tasks it affects, and the file or setting that
must carry the fix.

### [HIGH] RP-1 The `release` environment does not gate `GITHUB_TOKEN`; the real boundary is unapplied repository settings (CWE-284, CWE-863, CWE-269)

- **Attack.** On any branch, A2 pushes a workflow job with `permissions: contents: write` and no
  `environment:`, and calls the Releases API with its `GITHUB_TOKEN`. The read-only default for
  workflow permissions only sets the **default**. A same-repo workflow that asks for `write`
  explicitly gets it. That job can:
  - create, edit or delete releases and their assets, including replacing an asset on an
    already-published release (unless immutable releases are on);
  - create a tag through `POST /releases` with `target_commitish` (unless a tag ruleset forbids
    it);
  - push to `master`, which has no protection (Q5).

  ADR 0005 D6 says "a credential is allowed only through the GitHub environment of its tier".
  That is true for environment secrets and OIDC. It is **false for the GitHub-Release and
  registry-PR destinations**, because both use `GITHUB_TOKEN` (`CI_PUBLISH_TOKEN` maps it; see
  the ADR's neutral-name table).
- **Second attack (no workflow edit needed).** A2 changes `scripts/ci/destinations/*.py` or
  `stage-publish.yml` on `master`, which needs no review today. The maintainer then tags `master`
  and approves the `release` deployment. The reviewer sees a deployment request, not a diff, so
  A2's code runs with the write token and any environment secret.
- **Precondition.** Write access to the repository, or a token with `repo` + `workflow` scope.
- **Affected tasks.** 02, 03, 05 (every `repo-write` and `token-only` destination).
- **Impact.**
  - A substituted release asset whose digest the manifest asset can be made to match, because
    the manifest is itself just an asset.
  - A moved tag.
  - A `versions/` change that every consumer resolves (RP-3).
- **Mitigation.**
  - **Settings (maintainer; the boundary).** Apply and read back, before the first `v*` tag:
    - the ADR 0005 "Repository settings required" list;
    - the three additions below.

    Record the read-back in `docs/dev/ci.md` and status.md.
    - **Branch ruleset on `master`.** Require a PR, code-owner review on `/.github/`,
      `/scripts/ci/`, `/scripts/release/`, `/ports/`, `/versions/`, `/CMakeLists.txt` and
      `/smbios_c/`; no force-push; no deletion. This reverses Q5 for this story. The maintainer
      must rule on it explicitly, because without it every other control below is decorative
      against A2.
    - **Create `.github/CODEOWNERS`** listing those paths. CODEOWNERS binds only through the
      ruleset above.
    - **Immutable releases** on (see RP-6).
  - **Code (defence in depth, Task 02).** `release-draft` runs a fail-closed settings preflight.
    Where the job token can read a setting it checks it, and it fails with
    `<setting>: expected <x> -> actual <y>` otherwise:
    - the `v*` tag ruleset exists;
    - `GET /repos/{o}/{r}/environments/release` shows a reviewer and a tag-only deployment
      rule;
    - immutable releases are enabled.

    A setting the token cannot read is listed in an `info:` line as "manual read-back
    required". This detects settings drift. It is not a boundary.
- **Verification.**
  - A recorded `gh api` read-back of rulesets, environments, immutable releases and
    `actions/secrets` (`total_count: 0`).
  - A one-time negative proof on a scratch branch: a job with `contents: write` that tries
    `PATCH` on a published release's asset and `POST /git/refs` for `refs/tags/v9.9.9`. Both must
    fail (immutable release; tag ruleset). Record the run ID.

### [HIGH] RP-2 Toolchain and third-party code runs inside credentialed jobs (Task 03 as specified; Task 04's post-publish check) (CWE-522, CWE-214, CWE-829)

- **Attack.** As specified, Task 03's `registry-pr` job runs all of these **in the same job that
  holds `contents: write` + `pull-requests: write`**:
  - checkout;
  - `vcpkg x-add-version`;
  - the full Story 02.0 consumer test (vcpkg builds four Boost ports and the whole repository at
    `REF`, compiles and runs a consumer);
  - then `open_registry_pr.py`.

  Any code in that build can steal the token: a poisoned port download or build script (A3), a
  runner-image compiler, or the repository's own CMake at the tag. GitHub-hosted runners grant
  passwordless `sudo`, so a child can read the runner worker's process memory. That is exactly
  how the CVE-2025-30066 `tj-actions/changed-files` payload dumped secrets. On Linux a same-uid
  child can also read `/proc/<ppid>/environ` of `stage.py` itself. `child_environment()`
  (`reporters/__init__.py:135-139`, which strips `CI_PUBLISH_*` and `AWS_*`) is therefore
  hygiene, **not** a boundary, for code that runs in the same job. The same applies to a
  post-publish check that extracts and compiles an archive, if it runs in a credentialed job.
- **Precondition.** A3 code reachable from a vcpkg/compiler/consumer build in a publish job.
- **Affected tasks.** 03 (directly), 04, 05 (`s3_staging.py` calls `aws`).
- **Impact.** The `contents: write` token pushes to unprotected `master` (consumer RCE through
  the registry, RP-3) or rewrites releases. The token stays valid until the job ends.
- **Mitigation [OVERRIDES SPEC: Task 03 Design, ADR 0005 D6 "Registry PR timing"].**
  - Split `registry-pr` into two jobs:
    - **`registry-prepare`.** `contents: read`, no `environment`, no credential, not in
      `publish_jobs.json`, `needs: release-promote`. It does all of the following:
      - checks out `master` at a fetched SHA;
      - sets `REF` = `manifest.commit` (never a re-resolved tag);
      - runs `vcpkg x-add-version`;
      - runs the filesystem-registry consumer test;
      - uploads exactly the three changed files (`ports/smbios-c/portfile.cmake`,
        `versions/baseline.json`, `versions/s-/smbios-c.json`) as an artifact;
      - exports as job outputs the artifact ID, each file's sha256, the base `master` SHA and the
        computed `git-tree`.
    - **`registry-pr`** (repo-write). It downloads by artifact ID, checks the three digests
      against the job outputs, and creates the branch commit through the REST Git Data API
      (blobs, tree with `base_tree` = the base SHA, commit, then `POST /git/refs`). It then
      checks that the API-returned tree SHA of `ports/smbios-c` equals the `git-tree` output.
      It runs no `git`, no `vcpkg`, no compiler and no checkout-dependent tool.
  - The same split applies to Task 04. The post-publish check is its own `contents: read` job
    with no environment. It downloads anonymously and only hashes (RP-7).
  - **Policy (Task 05, new rule; extend P12).** A job listed in `publish_jobs.json` may contain
    only:
    - `actions/checkout` (`persist-credentials: false`);
    - `actions/download-artifact` with `artifact-ids:` (never `name:`/`pattern:`);
    - exactly one `run: python3 scripts/ci/stage.py publish --destination <name>` step.

    Add a failing fixture per extra step kind.
  - `destinations/github_release.py` and `destinations/registry_pr.py` must not import
    `subprocess` or call `ci_step.run_step`. A unit test enforces this with an AST scan.
- **Verification.** Policy fixtures: a publish job with a `stage.py verify` step, a cache
  restore, or a `download-artifact` by `name:`. Each must produce exactly one violation. The AST
  test above. On the scratch run, the `registry-pr` job log shows no compiler or vcpkg line.

### [HIGH] RP-3 Registry integrity: a consumer's pinned baseline does not pin what `1.0.0` means; the first automated release collides with the existing `1.0.0` entry (CWE-494, CWE-345, CWE-829)

- **Attack.**
  1. A vcpkg git registry's `baseline` pins only `versions/baseline.json`. The **available
     versions**, meaning `versions/s-/smbios-c.json` and so `version -> git-tree`, are read from
     the registry's `reference`, which defaults to `HEAD`, which is `master` here. So a later
     commit on `master` that rewrites the `git-tree` of an existing `(version, port-version)`
     changes the code that every consumer pinned to that version gets on its next cold install.
     That includes consumers that pinned an older baseline SHA. Such a commit could come from
     `x-add-version --overwrite-version`, a hand edit, or A2 on unprotected `master`. The rewritten
     tree can change `portfile.cmake`, which vcpkg executes, or point `REF` at any commit
     GitHub will serve by SHA from this URL. That includes a commit that exists only in a fork
     ([vcpkg-registry.md](/docs/dev/vcpkg-registry.md) "Never reuse a `REF`…").
  2. `versions/s-/smbios-c.json` already registers `1.0.0` with `git-tree 483562d…`, whose `REF`
     is `0bfc9a0` (Story 02.0, "the version was unpublished"). The first automated `v1.0.0`
     release must set `REF` to the tag commit. That is a different `git-tree` for the same
     `(1.0.0, 0)`, which needs `--overwrite-version`. That flag is exactly what Task 03's carried
     "`git-tree` overwrite lint" must forbid. If the implementer adds a quiet exception, the
     lint is gone.
- **Precondition.** Any commit reaching `master` that changes `ports/` or `versions/`. The
  automation itself (A5) counts.
- **Affected tasks.** 01, 03, 04.
- **Impact.** Attacker-chosen CMake runs on every consumer's machine and CI, under a version
  number the consumer believes is fixed. A2 needs no release for this, only one push to
  `master`.
- **Mitigation.**
  - **Append-only lint [OVERRIDES SPEC: Task 03 lists it as a per-change lint].**
    `check_vcpkg_registry.py` gains a history-wide check, run in `lint` on every event with full
    history:
    - for every `(version, port-version)` that **any** ancestor commit of `HEAD` recorded in
      `versions/s-/smbios-c.json`, the `git-tree` is unchanged and the entry still exists;
    - `baseline.json` names an entry that exists and is the highest version;
    - for every registered `git-tree`, the port's `REF` (read with `git cat-file` from that
      tree) is an ancestor of `origin/master`;
    - once a `v<version>` tag exists, `REF` equals that tag's dereferenced commit.

    Fixtures: an overwrite, a removed entry, a `REF` not on `master`, and a `REF` different from
    the tag commit. Each fails with one discrepancy line.
  - **The `1.0.0` collision is decided in Task 01, before Task 03** (maintainer ruling). Either:
    - (a) the first automated release is a new version (`1.0.1`, or `port-version: 1`), and the
      `1.0.0` entry stays frozen; or
    - (b) a one-time, dated exception for exactly `(1.0.0, 0, 483562d…)` is recorded in
      status.md and hard-coded in the lint with that exact old `git-tree`. It is removed by the
      same release PR.

    Recommended: (a).
  - `REF` comes **only** from `manifest.commit`, after RP-5's commit binding passes. It never
    comes from `git rev-parse <tag>` or `HEAD`.
  - [vcpkg-registry.md](/docs/dev/vcpkg-registry.md) gains a consumer note. The baseline SHA does
    not freeze the version database. A consumer who needs immutability sets `"reference"` to a
    release tag; check during Task 03 that vcpkg accepts a tag there. Immutable releases (RP-6)
    then make that tag unmovable.
  - The `master` ruleset of RP-1, with CODEOWNERS on `/ports/` and `/versions/`, is the actual
    boundary.
- **Verification.** The four lint fixtures. A scratch-release registry branch passes
  `x-add-version --verbose` with no edits **and** the append-only lint.

### [HIGH] RP-4 CR-2 and CR-8 cannot be enforced by the package stage as built: no `binary_cache` in the manifest, no out-of-band manifest digest (CWE-345, CWE-354, CWE-494)

- **Evidence.**
  - `stage.py:795-797` sets `binary_cache` as a **step** output of the build job. It is not
    exported in `stage-build.yml` `jobs.build.outputs`, not passed to `stage-package.yml`, and not
    in `package_archive.LEG_FIELDS` (`package_archive.py:94-95`). A `release`-tier destination
    therefore has nothing to refuse on. Its value is also a self-report (`VCPKG_BINARY_SOURCES ==
    "clear"`), not an observation.
  - `stage-package.yml` declares no workflow-call or job `outputs:`. The per-leg `package_digest`
    step output (`stage.py:1238`) and the merged manifest's digest leave no out-of-band trace. The
    `dist` artifact is uploaded by **name** (`stage-package.yml:151`).
  - The manifest job checks each archive against its **own part's** manifest (`stage.py:1155-1166`),
    which travels in the same artifact. It never compares a part's `install_sha256` with the build
    job's `install_sha256_<leg>` output.
  - Task 03.0's verifier checks the extracted tree against the manifest's `install_sha256`. That
    is self-referential if the manifest is replaced.
- **Attack.** Any job of the same run that holds the Actions runtime token can delete and
  re-upload the artifacts `dist-<leg>` or `dist`, archive and manifest together (CR-8). That
  includes a verify leg compiling consumer code against runner-image Boost (A3). Publish
  downloading `dist` by name then ships consistent, substituted bytes. A restored-cache leg on a
  tag also cannot be refused.
- **Precondition.** A3 code in any build, package or verify job of the release run.
- **Affected tasks.** 02, 04, 05, plus a Story 03.0 follow-up in `stage-build.yml`,
  `stage-package.yml`, `stage.py` and `package_archive.py`.
- **Impact.** DD1 broken with every digest check green.
- **Mitigation.**
  - **`binary_cache`.**
    - The build job exports `binary_cache_<leg>`. Its value is `none` only if
      `VCPKG_BINARY_SOURCES` was exactly `clear` **and** `vcpkg_cache_report.parse_log` counted 0
      restored packages; otherwise it is `restored`, and unparseable counts as `restored`.
    - `ci.yml` passes it in the package matrix object, and `stage.py package` records it in the
      leg entry. Add it to `LEG_FIELDS` as a required field.
    - A `release`-tier destination refuses `restored` **and a missing field** (fail closed). It
      also cross-checks the manifest value against the build job output passed into the publish
      job's environment.
  - **Out-of-band digests.**
    - Each package job exports `archive_sha256_<leg>` and `dist_artifact_<leg>` (unique names,
      as the build job does).
    - The manifest job downloads by those IDs and checks every archive against
      `archive_sha256_<leg>` and every part's `install_sha256` against `install_sha256_<leg>`.
    - The manifest job exports `manifest_sha256` and `dist_artifact_id`, and `stage-package.yml`
      surfaces them as `workflow_call` outputs.
    - Verify and publish jobs download `dist` **by `artifact-ids`** and check
      `sha256(manifest.json)` against `manifest_sha256` before parsing it.
    - Verify compares each extracted tree with `install_sha256_<leg>` from the build job, not
      with the manifest.
  - Publish re-checks everything itself and never relies on `verify`'s verdict (CR-8).
- **Verification.**
  - Unit tests: a manifest without `binary_cache` is refused for `release` and accepted for
    `staging`; `restored` is refused.
  - The planned second CR-8 proof: replace both the archive and the manifest, and publish fails
    on `manifest_sha256`.
  - A scratch-tag run shows `binary_cache: none` and RC4 `cache miss` on every packaging leg.

### [MEDIUM] RP-5 The release is not bound to one commit: tag mutability, re-resolution, the TOCTOU window and pre-release tags (CWE-367, CWE-345, CWE-1288)

- **Attack and evidence.**
  - A tag can be deleted and recreated, or force-moved, between the run start (`github.sha`)
    and `release-draft`, `release-promote` or `registry-pr`. The window is hours long if the
    environment waits for a reviewer. Any step that resolves the tag **by name** at that point
    publishes or registers a different commit from the one built:
    - `POST /releases` with `tag_name` attaches the release to whatever the tag names now;
    - `git rev-parse v1.2.3` returns the tag's current target;
    - so does the API's `tag_name` field.
  - The ADR's ancestry check alone does not catch this, because both commits can be on `master`.
  - `check_versions.py:235-242` ignores the pre-release suffix. `v1.0.0-rc.1` passes against
    authority `1.0.0`. The manifest then says version `1.0.0`, the assets are named
    `libbios-1.0.0-*`, and Task 03 would open a registry PR registering **`1.0.0`** from a
    release candidate.
  - `plan.py:129` accepts any `refs/tags/v[A-Za-z0-9._+-]{1,100}` as a tag event.
  - ADR 0005's validation plan uses `v0.0.0-rc.1`, "deleted afterwards". That tag fails
    `check_versions` against `1.0.0`, and once immutable releases are on (RP-6), a published
    release's tag cannot be deleted.
- **Precondition.** A2 without a tag ruleset, or A5: a re-tag, a re-run after a re-tag, or an
  RC tag.
- **Affected tasks.** 01, 02, 03.
- **Mitigation.**
  - **Publish preflight** `stage.py publish --only preflight`, run as the **first step of every
    publish-class job** and repeated immediately before each write (draft create, promote, ref
    create). It requires all of the following:
    - `CI_REF_NAME` fully matches `^v(\d{1,6}\.\d{1,6}\.\d{1,6})(-[0-9A-Za-z.]{1,40})?$`;
    - the group-1 value equals `manifest.version`;
    - `manifest.commit == CI_COMMIT`;
    - the **remote** tag, dereferenced through annotated tags
      (`GET /repos/{o}/{r}/git/ref/tags/<tag>`, then `GET …/git/tags/<sha>` when the type is
      `tag`), equals `CI_COMMIT`;
    - `CI_COMMIT` is an ancestor of `master` as fetched in this step. Use a credential-less
      `git fetch --no-tags origin +refs/heads/master:refs/ci/master`, or the compare API,
      `GET …/compare/<CI_COMMIT>...master` with `status` in `{ahead, identical}`.
  - **Tag binding.** Releases are created with `tag_name` **and** `target_commitish = CI_COMMIT`.
    The returned release's tag is then dereferenced again. `REF` = `manifest.commit` (RP-3).
  - **Pre-releases.** A tag with a `-<pre>` suffix creates the release with `prerelease: true`
    and **never** runs `registry-prepare` or `registry-pr`. `plan.py` omits those rows, with a
    unit test. Asset names keep the D4 `<version>`. The release title and notes state the tag.
    Task 01 decides whether pre-release archives should carry the suffix in `<version>` (needs
    an ADR 0005 D4/D5 amendment). Until then, two releases (`v1.0.0-rc.1` and `v1.0.0`) holding
    differently-hashed assets with identical file names is an accepted, documented consequence.
  - **Validation plan [OVERRIDES ADR 0005 Validation].** End-to-end proofs run in a **fork or a
    scratch repository** with the same settings, or on this repository with a draft that is
    never promoted (`release-promote` disabled by a scratch-only `publish_destinations.json`
    entry). A tag is never deleted after a promoted release here.
- **Verification.** Unit tests of the preflight, each a single discrepancy line:
  - the tag moved (remote deref differs from `CI_COMMIT`);
  - `manifest.commit` differs;
  - `CI_COMMIT` is not on `master`;
  - `v1.0.0x`, `v1.0`, `v1.0.0-` and `v01.0.0` are rejected;
  - `v1.0.0-rc.1` gives `prerelease: true` and no registry rows.

### [MEDIUM] RP-6 Release assets are mutable: the draft window, published-asset replacement, and the name set (CWE-345, CWE-494)

- **Attack.**
  - Draft assets can be changed by anyone with push access until promotion.
  - Without immutable releases, published assets can be deleted and re-uploaded under the same
    name at any time.
  - GitHub rewrites some characters in asset names, so the published set can differ from the
    manifest names.
  - `release-promote` is a separate job, so the `release-draft` re-read (D6 step 2) happens long
    before promotion if a reviewer gate sits between them.
- **Precondition.** A2 (RP-1), or A5 re-running a half-failed draft.
- **Affected tasks.** 02, 04.
- **Mitigation.**
  - **Setting.** Enable **immutable releases** (GA 2025-10-28). Once a release is published, its
    tag and assets are frozen, and GitHub issues a release attestation. Drafts stay mutable,
    which matches the draft-then-promote flow.
  - **`release-promote`** re-lists the draft's assets immediately before
    `PATCH {draft: false}`. It requires:
    - the asset **name set** to equal exactly the manifest's files plus `manifest.json`, with no
      extra assets and no renamed ones;
    - each asset's `size` to equal the manifest `bytes`;
    - each asset's sha256 to equal the manifest's. Hash the downloaded bytes; the API `digest`
      field, where present, is a GitHub-side claim and only a secondary check.

    After promotion it checks that the release reports `immutable: true` (if the API exposes
    the field) or runs the RP-1 setting preflight. Anything else is a loud failure that names
    the release.
  - **Create-only.**
    - `release-draft` fails if **any** release (draft or published) already exists for the tag,
      unless that release is a draft whose body carries this run's `CI_RUN_ID` marker (re-run of
      the same run). In that case it may delete only assets of that draft.
    - It never touches a published release.
    - It never sets `make_latest` on a pre-release.
- **Verification.**
  - Stubbed-API tests: an extra asset, a renamed asset, a size mismatch, a digest mismatch, an
    existing published release, and an existing draft from another run. Each must cause a hard
    failure with no write call issued after it.
  - Settings read-back shows immutable releases on.

### [MEDIUM] RP-7 A post-publish check that reassures instead of detects (CWE-345, CWE-693)

- **Weakness.** A check is self-referential, and so proves nothing, if it:
  - downloads through the authenticated API (which also sees drafts and follows a different
    path from consumers);
  - compares against the `manifest.json` **asset of the same release** (which an attacker
    replaces together with the archives);
  - runs only once, seconds after promotion.

  Running in the credentialed job adds RP-2 on top.
- **Affected tasks.** 04, 05 (`check_published` of each destination).
- **Mitigation.** The post-publish job (`contents: read`, no environment, no token passed to the
  destination's `check_published`):
  - downloads every asset **anonymously** from its public `browser_download_url`;
  - follows redirects only to an allowlisted GitHub host set, over HTTPS only;
  - streams to disk with a cap of manifest `bytes` + 0 (anything more is a failure);
  - compares the bytes with digests from the **out-of-band** manifest (verified against the
    `manifest_sha256` job output, RP-4), not with the downloaded manifest asset;
  - then also checks that the downloaded `manifest.json` is byte-identical to the verified one;
  - checks the exact asset name set, that the release is not a draft, `prerelease` matching the
    tag, and the tag dereferencing to `manifest.commit`;
  - for the registry, after the human merge, a nightly `verify` step reads
    `versions/s-/smbios-c.json` from `master` anonymously and runs the RP-3 append-only check
    against the previous nightly's recorded state;
  - a **nightly re-check** of the last N (for example 5) releases repeats all of this. That is
    the detection control for later tampering, if immutable releases are ever off.

  A failure names the release and the first discrepancy. It is not a rollback (D6).
- **Verification.** Tests that drive a local HTTP stub:
  - the manifest asset replaced together with an archive is detected;
  - a redirect to a non-allowlisted host is refused;
  - an oversize body is cut off and fails;
  - a draft release is reported, not checked.

### [MEDIUM] RP-8 Credential leakage through argv, child processes, redirects, records and error text (CWE-532, CWE-214, CWE-522, CWE-200)

- **Attack and evidence.**
  - GitHub masks the literal secret value only in **logs**. It does not mask RC3 records or
    `summary-*.json` artifacts (uploaded by every job, `stage-build.yml:288-290`), and it does
    not mask transformed values such as base64 `Basic` auth or a URL with `x-access-token:` in
    it.
  - `git push https://x-access-token:<t>@…`, or `git -c http.extraheader=…`, puts the token in
    argv, which every local user can see in `ps`, and in the git error output that `ci_step`
    excerpts.
  - CPython's `urllib` `HTTPRedirectHandler` copies ordinary request headers, `Authorization`
    included, onto a redirect to **another host**. GitHub answers asset downloads
    (`Accept: application/octet-stream`) and some uploads with a redirect to a separate storage
    host.
  - `urllib.error.HTTPError`'s string and a response body can echo request details into
    `first_diagnostic`.
- **Affected tasks.** 02, 03, 05.
- **Mitigation.**
  - Destinations do REST in-process with stdlib `urllib`. The token is attached with
    `Request.add_unredirected_header("Authorization", …)`, so it is never forwarded on a
    redirect.
  - A custom opener refuses any URL whose host is not in a per-destination allowlist. The base
    is the constant `https://api.github.com`. Upload URLs from responses are accepted only if
    their host is `uploads.github.com`.
  - No `git` or `gh` child ever receives the token. RP-2 removes `git push` entirely.
  - The token is read once from `CI_PUBLISH_TOKEN`, deleted from `os.environ` in the destination
    process before any further work, and never stored on an object that is `repr`-ed or logged.
  - Error rendering uses only the HTTP status, the method and a fixed endpoint label (for
    example `POST releases: expected 201 -> actual 422`). It never uses the response body,
    headers or the exception string. A body field may be shown only through an allowlist regex,
    such as GitHub's `errors[].code` matching `^[a-z_]{1,40}$`.
  - `secrets.GITHUB_TOKEN` is mapped **only** in the `env:` of the single publish step, never at
    job or workflow level. That keeps it away from the `checkout`/`download-artifact` steps'
    environment; those actions still see `github.token` through their own `token` input default,
    which is one more reason RP-2 keeps their number at two. P8 is extended to fail on a
    `secrets.` reference at job or workflow `env:` level.
- **Verification.** A canary test per destination. A fake token `ghs_CANARY…` (and its base64
  form) is set, every path (success, 4xx, 5xx, redirect, exception) runs against a stub server,
  and the canary must appear nowhere in stdout/stderr, the results JSONL, the summary file or the
  RC1 output. A redirect-to-other-host stub receives no `Authorization` header. A policy fixture
  with job-level `CI_PUBLISH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` must fail.

### [MEDIUM] RP-9 Registry PR mechanics weaken review: bot-approvable PRs and PRs that trigger no CI (CWE-284, CWE-693)

- **Attack and evidence.**
  - Opening the PR with `GITHUB_TOKEN` needs the repository setting "Allow GitHub Actions to
    create and approve pull requests". With it on, **any** workflow (including A2's branch
    workflow) can also *approve* PRs with its token. That can satisfy a required-approval
    ruleset on `master` and nullifies RP-1's most important setting.
  - A PR opened by `GITHUB_TOKEN` triggers no workflows. The reviewing human therefore merges a
    diff to `ports/` and `versions/` with no CI status on it. The in-run validation (D6) is in a
    different run, which the reviewer must find by hand.
- **Affected tasks.** 03 (and ADR 0005 D6's permission table).
- **Mitigation [OVERRIDES SPEC: Task 03 "opens a PR"; D6 `registry-pr` permissions].**
  - `registry-pr` holds only `contents: write` (drop `pull-requests: write`).
  - It creates a **new** branch `registry/v<version>` through the Git Data API (RP-2). This is
    create-only: an existing ref is a hard failure, never an update.
  - It prints the compare URL as its `ok:` line.
  - The **maintainer opens the PR**. A human-opened PR triggers `pull_request` CI, including
    `check_vcpkg_registry.py` and the RP-3 append-only lint, so the merge carries a real status.
  - The setting "Allow GitHub Actions to create and approve pull requests" stays **off**, read
    back with `gh api repos/{o}/{r}/actions/permissions/workflow`
    (`can_approve_pull_request_reviews: false`).
  - If an automated PR is wanted later, it must come from a GitHub App installation token held
    as a `release` **environment** secret (its PRs do trigger CI). That is a new `token-only`
    destination class, not a setting change.
- **Verification.**
  - The policy fixture `registry-pr` with `pull-requests: write` is rejected once
    `publish_jobs.json` lists only `contents: write` for it.
  - A stubbed-API test: an existing `registry/v<version>` ref is a hard failure.
  - The scratch run's branch receives CI only after a human opens the PR.

### [LOW] RP-10 Untrusted text in generated release notes and the registry branch commit (CWE-79, CWE-116)

- **Evidence.** `generate_release_notes` copies PR titles and author logins (A1 controls fork PR
  titles) into the release body, which renders as Markdown on the release page and in
  notification emails. The registry commit message and the RP-9 compare-URL line must not carry
  them either.
- **Affected tasks.** 02, 03.
- **Mitigation.**
  - The release body is generated text **plus** a fixed, script-authored block: version, commit,
    and the manifest digest table, built only from manifest fields that passed their regexes.
  - The commit message is a fixed template:
    `registry: smbios-c <version> (REF <commit12>, run <id>)`.
  - Neither the release body nor the commit message is ever echoed into RC1/RC3. The job prints
    only counts and the release URL.
- **Verification.** A unit test: a manifest field with `|`, a backtick or a link is rejected
  before the body is built.

### [LOW] RP-11 Environment reviewer realism for a sole maintainer; per-job approvals (CWE-1068)

- **Evidence.** "Prevent self-review" plus a single maintainer who pushed the tag means the
  release can never be approved. Without "prevent self-review", the gate is a deliberate-click
  control, not two-person review. Each job that references `environment: release` asks for its
  own deployment approval. As specified, that is three to five approvals per release, which
  invites rubber-stamping.
- **Affected tasks.** 02, 03, 05.
- **Mitigation.**
  - The maintainer records in status.md who the reviewers are, and whether self-review is
    allowed, with the stated consequence.
  - Only `release-draft` and `release-promote` reference `release`. `registry-pr` writes only a
    new branch (RP-9), so it may use a separate `release-registry` environment with the same
    tag-only deployment rule and no reviewer. If it does, list that environment in P12's tier
    clause.
  - Reviewers approve with the run URL and the draft's digest table open. The `release-draft`
    `ok:` line names the draft URL for that purpose.

### [LOW] RP-12 Concurrent and re-run releases (CWE-362)

- **Evidence.**
  - Tag runs are never cancelled (`ci.yml` concurrency), so two tags pushed close together race
    on `master` for the registry branch.
  - "Re-run failed jobs" re-executes publish jobs with the same `CI_RUN_ID`, a new
    `CI_RUN_ATTEMPT`, and the original artifacts.
- **Affected tasks.** 02, 03.
- **Mitigation.**
  - Every write is create-only or guarded by the RP-6 draft marker.
  - `registry-prepare` bases on a fetched `master` SHA, and `registry-pr` passes it as
    `base_tree`/`parents`, so a conflicting concurrent change yields a separate branch the human
    reconciles, never a silent overwrite.
  - A re-run of `release-promote` against an already-published, matching release reports `ok`
    (idempotent). Against a non-matching release it fails.

### [LOW] RP-13 Later binding registries and the OIDC subject (forward constraint, Task 05 / milestone 0013) (CWE-284)

- **Evidence.**
  - An OIDC `sub` of `repo:<o>/<r>:environment:release` contains no ref. A trust policy
    conditioned only on it is exactly as strong as the environment's deployment rule plus the
    tag ruleset.
  - PyPI trusted publishing does not support a publish step inside a **reusable** workflow,
    which is where `stage-publish.yml` puts every publish job. Binding it would force either a
    publish job in `ci.yml` (a P12 layout change) or a stored token.
- **Mitigation.**
  - Cloud and registry trust policies add `job_workflow_ref` (or `workflow_ref`) and `ref`
    conditions where the provider supports them, alongside `sub`.
  - Before 0013 starts publishing, ADR 0005 D9 records which workflow file each trusted
    publisher is bound to. A fallback to a stored token is a recorded downgrade (as CR-14 does
    for TeamCity), held as a `release` environment secret, never a repository secret.

### [LOW] RP-14 The staging tier runs on every push to an unprotected `master`

- **Evidence.** D9 `staging` runs on `master` pushes with no reviewer (by design), accepts
  `restored` cache legs, and Q5 lets A2 push to `master` directly. Its impact is limited to the
  disposable `staging/*` prefix only if CR-3's cloud-side policy is in place.
- **Affected task.** 05.
- **Mitigation.**
  - `s3-staging` ships `enabled: false` (spec).
  - Enabling it needs the CR-3 cloud read-back recorded first. The P12 check for that is a
    status.md read-back line referenced by the entry's `about`; a reviewer checks it, it cannot
    be mechanised.
  - Staging objects carry `x-amz-meta-not-a-release: true`, and the staging README says they are
    unverified for distribution.

### [INFO] RP-15 Accepted residuals

- A stolen **admin** credential (owner PAT with `repo` scope, or a hijacked session) can change
  every setting this document relies on. Mitigations are account hygiene: hardware 2FA,
  fine-grained PATs without Administration permission, short expiry. They are outside this
  repository.
- `vcpkg_from_git`'s `REF` integrity rests on git object ids, which are SHA-1 with
  collision detection on GitHub. Accepted, as for every git-pinned dependency.
- A1 cannot reach any publish path: fork PRs get a read-only token and no secrets, and publish
  runs only on tags and `master` pushes. This holds as long as the CR-1 trigger allowlist (no
  `pull_request_target`/`workflow_run`) stays in P8.

## Controls that cannot be enforced in code

These live outside the triggering ref, so no task, test or policy rule can provide them. Each is
a maintainer action. Each is read back with the command shown and recorded in `docs/dev/ci.md`
and status.md **before the first `v*` tag is pushed**. A preflight (RP-1) can detect drift in
some of them but never enforces them.

| # | Setting | Read-back | Finding |
|---|---|---|---|
| S-1 | `master` ruleset: PR required, code-owner review, no force-push, no deletion, no bypass except the maintainer role (**reverses 0014 Q5; needs an explicit ruling**) | `gh api repos/{o}/{r}/rulesets` + `…/rulesets/<id>` | RP-1, RP-3 |
| S-2 | `.github/CODEOWNERS` for `/.github/`, `/scripts/ci/`, `/scripts/release/`, `/ports/`, `/versions/`, `/CMakeLists.txt`, `/smbios_c/` (a file, but binding only through S-1) | file review + S-1 | RP-1, RP-3 |
| S-3 | `v*` tag ruleset: create/update/delete restricted to maintainers | `gh api repos/{o}/{r}/rulesets` | RP-1, RP-5 |
| S-4 | Immutable releases enabled | repository settings page, or `gh api repos/{o}/{r}/immutable-releases` if available | RP-6, RP-7 |
| S-5 | Environment `release`: deployment rule "selected tags" `v*` only; required reviewer(s); self-review decision recorded; no admin bypass | `gh api repos/{o}/{r}/environments/release` | RP-1, RP-11 |
| S-6 | Default workflow permissions read-only, **and** "Allow GitHub Actions to create and approve pull requests" **off** | `gh api repos/{o}/{r}/actions/permissions/workflow` | RP-9 |
| S-7 | No repository-, organisation- or Dependabot-level secrets: publish credentials exist only as environment secrets | `gh api repos/{o}/{r}/actions/secrets` -> `total_count: 0` | RP-1, RP-8 |
| S-8 | Fork-PR workflows require approval for outside contributors | `gh api repos/{o}/{r}/actions/permissions` (and the settings page) | RP-15 |
| S-9 | (When `s3-staging` is enabled) the CR-3 cloud trust and role policy, plus `aws iam simulate-principal-policy` | as in ADR 0005 | RP-14 |

## Per-task requirements

**Task 01 - Version sites and release preparation**

1. Get the RP-3 `1.0.0` collision ruling ((a) new version, recommended; or (b) a one-time dated
   exception) and record it in status.md before Task 03 starts.
2. The Task 01 decision on whether pre-release tags carry their suffix in the D4 `<version>`
   (RP-5) is recorded. If they do not, `check_versions.py`'s acceptance of `v<X.Y.Z>-<pre>`
   stays, and the pre-release rules of RP-5 apply.
3. `prepare_release.py` never creates or pushes a tag, never runs `x-add-version`, and touches
   only the `equal` sites. Tagging is a human action under S-3.

**Task 02 - Publish stage (GitHub Release)**

4. RP-5 preflight before every write; RP-6 create-only draft, exact name set, size and digest
   re-read immediately before promotion.
5. Destination code is in-process `urllib` with RP-8 header, redirect and error rules, and has a
   canary test.
6. Inputs come only from `dist` downloaded by artifact ID and checked against `manifest_sha256`
   (RP-4). Release tier refuses `binary_cache != "none"` or a missing field.
7. `publish_jobs.json`: `release-draft` and `release-promote`, `repo-write`, `contents: write`
   only, `environment: release`, `needs: verify`, tag guard. Their steps are limited to the RP-2
   set.
8. The settings preflight (RP-1) is the first step. S-1 to S-8 are read back before the first
   real tag.

**Task 03 - Registry update**

9. Split into `registry-prepare` (unprivileged) and `registry-pr` (`contents: write` only, Git
   Data API, create-only branch, no PR creation) per RP-2 and RP-9.
10. `REF` = `manifest.commit` after the preflight. Pre-release tags never reach these jobs.
11. Append-only, history-wide registry lint in `check_vcpkg_registry.py`, plus `REF` reachability
    and tag-equality checks (RP-3). These replace the per-change "`git-tree` overwrite lint" and
    "`REF` reachability lint" bullets.
12. Update [vcpkg-registry.md](/docs/dev/vcpkg-registry.md): the procedure describes the
    automation, `--overwrite-version` is removed from the documented command, and the consumer
    note on `reference` is added.

**Task 04 - Verify stage and post-publish check**

13. `verify` downloads by artifact ID and checks extracted trees against the build job's
    `install_sha256_<leg>` output (RP-4). It holds no credential and restores no cache (P15).
14. The post-publish check is its own `contents: read` job, anonymous, uses out-of-band digests,
    an exact asset set and a redirect-host allowlist, and adds a nightly re-check (RP-7).

**Task 05 - Destination plug-ins**

15. Policy additions, each with one failing fixture:
    - the step allowlist for publish jobs, with `download-artifact` by `artifact-ids` only (RP-2);
    - no `secrets.` at job or workflow `env:` level (RP-8);
    - a `release`-tier entry whose module imports `subprocess` fails for the `repo-write` class
      (RP-2).
16. `check_published` takes no credential for public destinations. `s3_staging.py` keeps the CR-3
    and CR-5 rules (OIDC token in a 0600 file under `CI_TEMP_DIR`, only
    `AWS_WEB_IDENTITY_TOKEN_FILE` passed by name) and ships disabled.
17. The ADR 0005 D9 registry entry schema gains `binary_cache_policy`, implied by the tier and
    checked by P12 (`release` means `none-only`).

**Story 03.0 follow-up (prerequisite of Tasks 02 and 04).** Before Task 02 merges: the
`binary_cache_<leg>`, `archive_sha256_<leg>`, `dist_artifact_<leg>`, `manifest_sha256` and
`dist_artifact_id` job and workflow outputs, plus the manifest job's cross-checks (RP-4).

## Standing constraints for implementers

Every Story 04.0 task briefing repeats these verbatim. Each one is testable.

1. **No toolchain in a credentialed job.** A job listed in `publish_jobs.json` contains only
   `actions/checkout` (`persist-credentials: false`), `actions/download-artifact` with
   `artifact-ids:`, and exactly one `python3 scripts/ci/stage.py publish --destination <name>`
   step. Anything that runs vcpkg, a compiler, `pkg-config`, an extraction or a consumer test
   runs in a job without credentials and hands over files by artifact ID plus sha256 job outputs.
   *(Policy fixture per extra step kind.)*
2. **The credential lives in one place.** `secrets.GITHUB_TOKEN` (or an environment secret) is
   mapped only in the `env:` of that single publish step, only to a `CI_PUBLISH_*` name the
   destination's registry entry lists. It is never set at job or workflow level, never in a
   repository-level secret, never in argv, never passed to a child process, and never sent to
   `git`. The GitHub destinations import no `subprocess`. *(Policy fixture; AST test.)*
3. **The token never leaks.** A canary token and its base64 form appear nowhere in stdout/stderr,
   RC3 JSONL, the summary file or the exception text, on success, 4xx, 5xx, redirect or crash
   paths. `Authorization` is added with `add_unredirected_header` and is never sent to a host
   outside the destination's allowlist. *(Canary test per destination.)*
4. **The manifest is anchored out of band.** Verify and publish download `dist` by artifact ID,
   check `sha256(manifest.json)` against the package stage's `manifest_sha256` job output before
   parsing it, check every file against the manifest, and never recompute a digest into anything
   they publish. *(Test: archive and manifest replaced together, then fail.)*
5. **One release, one commit.** Before every write, require all of: the tag matches
   `^v\d{1,6}\.\d{1,6}\.\d{1,6}(-[0-9A-Za-z.]{1,40})?$`; its base equals `manifest.version`;
   `manifest.commit == CI_COMMIT`; the remote tag dereferenced through annotated tags equals
   `CI_COMMIT`; and `CI_COMMIT` is an ancestor of `master` fetched in that step. Releases are
   created with `target_commitish = CI_COMMIT`, and a registry `REF` is always
   `manifest.commit`. Nothing resolves a tag by name after the preflight. *(Unit test per
   condition.)*
6. **Release tier is cold-built only.** A `release`-tier destination refuses a manifest leg whose
   `binary_cache` is not exactly `none`, including a missing field, and cross-checks it against
   the build job's `binary_cache_<leg>` output. *(Unit test: missing, `restored`, mismatch.)*
7. **Every remote write is create-only.**
   - No published release, release asset, git ref or S3 key is ever overwritten or deleted.
   - An existing target is a hard failure.
   - The only exception is a draft carrying this run's `CI_RUN_ID` marker.
   *(Stubbed-API test per destination.)*
8. **Pre-release tags never touch the registry.** A tag with `-<pre>` creates a GitHub release
   with `prerelease: true` and produces no `registry-prepare`/`registry-pr` rows. *(`plan.py`
   test.)*
9. **The registry is append-only.** Lint fails if any `(version, port-version)` ever recorded on
   an ancestor of `HEAD` changes its `git-tree` or disappears, if a registered `REF` is not an
   ancestor of `master`, or if a registered `REF` differs from its `v<version>` tag's commit.
   `--overwrite-version` appears in no script and no doc procedure. *(Lint fixtures.)*
10. **Registry changes arrive as a branch, and a human opens the PR.** The automation creates
    `registry/v<version>` through the Git Data API with `contents: write` only. It never opens or
    approves a PR, and it never pushes to `master`. *(Policy: `registry-pr` permissions; stubbed
    test: existing ref, then fail.)*
11. **Post-publish checks see what consumers see.** They run in a `contents: read` job with no
    credential, download from public URLs only, follow allowlisted HTTPS redirect hosts only,
    cap each body at the manifest's `bytes`, require the exact asset name set, and compare against
    the out-of-band manifest, never the release's own `manifest.json` asset alone. A nightly
    re-check covers the last 5 releases. *(Stub-server tests.)*
12. **Settings come first.** No task may push its first real `v*` tag, or enable a `release` or
    `staging` destination, until S-1 to S-8 (and S-9 for S3) are read back and recorded in
    `docs/dev/ci.md` and status.md. A missing read-back blocks the merge of Task 02 at review.

## Verdict: PASS_WITH_FOLLOWUP

There is no CRITICAL finding. Nothing is implemented yet, and no shipped code is exploitable
today, so nothing blocks the **start** of Story 04.0. The following are **merge-blocking for the
task they name**. Any of them, implemented as currently specified, would be raised to CRITICAL
at code review:

- RP-1: the settings S-1 to S-8 are not read back before the first tag, or the ADR text keeps
  claiming that the environment scopes `GITHUB_TOKEN`.
- RP-2: any vcpkg, compiler or consumer step inside a credentialed job. **Task 03 as specified
  does exactly this.**
- RP-3: a registry lint that allows a `git-tree` overwrite, or an unruled `1.0.0` collision.
- RP-4: `release` publishing without `binary_cache` in the manifest, or without the out-of-band
  `manifest_sha256`. The package stage as built provides neither.

S-1 (a `master` ruleset) reverses maintainer ruling Q5. It needs an explicit maintainer decision.
Without it, RP-1 and RP-3 stay HIGH residuals against any collaborator.

Fixes go to `cpp-expert`: scripts, workflows, and the Story 03.0 follow-up outputs. Failing
fixtures, stub-API and canary tests, and the stub-server post-publish tests go to
`testing-expert`. Settings S-1 to S-9 go to the maintainer.

## Sources

- [Immutable releases are now generally available (GitHub changelog, 2025-10-28)](https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/)
- [vcpkg-configuration.json reference: `reference` "used to list available versions", defaults to `HEAD`; `baseline` pins `versions/baseline.json`](https://learn.microsoft.com/vcpkg/reference/vcpkg-configuration-json)
- [vcpkg registries concepts](https://learn.microsoft.com/vcpkg/concepts/registries)
- [pypa/gh-action-pypi-publish: Trusted Publishing in reusable workflows unsupported (#166)](https://redirect.github.com/pypa/gh-action-pypi-publish/issues/166)
- [CVE-2025-30066, tj-actions/changed-files (OSV)](https://osv.dev/vulnerability/CVE-2025-30066)
  (runner-memory secret dump; already cited by the 2026-11-13 model)
