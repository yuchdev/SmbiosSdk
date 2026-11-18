# Adding a publish destination

Design: [ADR 0005 D9](/docs/adr/0005-unified-build-package-publish-pipeline.md). Contract: SC5 in [milestone 0014's plan](/docs/roadmap/0014-ci-rework-from-scratch/plan.md). Built by [milestone 0012 Story 04.0 Task 05](/docs/roadmap/0012-package-manager-distribution/04.0-release-and-publish-automation/05-publish-destination-plugins.md). Three destinations exist, each a module of `scripts/ci/destinations/` (registry key with `-` as `_`) and an entry of `scripts/ci/publish_destinations.json`, all shipped `enabled: false`:

| Destination | Module | Tier | Class | Jobs |
|-------------|--------|------|-------|------|
| `github-release` | `github_release.py` | `release` | `repo-write` | `release-draft`, `release-promote` (phases `draft`, `promote`), then the unprivileged `post-publish` |
| `vcpkg-registry` | `vcpkg_registry.py` | `release` | `repo-write` | the unprivileged `registry-prepare` (phase `prepare`), then `registry-pr` (phase `branch`) |
| `s3-staging` | `s3_staging.py` | `staging` | `oidc` | `publish-staging-oidc` (the class matrix job) |

`stage.py publish` drives a module only through the D9 interface (`destinations.INTERFACE`); `destinations.load()` refuses a module that lacks a member. A destination whose work spans several jobs (D6: a reviewer gate between two writes, or a toolchain step that must run without a credential) lists them in `PHASES`, the toolchain or anonymous ones also in `UNPRIVILEGED_PHASES` (`stage.py` refuses those when any credential is set), and gets one `publish_jobs.json` job per privileged phase, bound with `"destination"` and `"phase"`. Every other destination has one write phase and runs in its class's matrix job.

A destination is where verified artifacts go: a GitHub Release, the vcpkg registry, crates.io, PyPI, an S3 staging area. Adding one never changes the stage logic or the workflow shape.

## Steps

1. **Write the module** `scripts/ci/destinations/<name with - as _>.py` exposing `NAME`, `REQUIRED_ENV`, `validate`, `publish` and `check_published` (table in ADR 0005 D9; signatures and the `Env`/`Result` types in `scripts/ci/destinations/__init__.py`). The manifest arrives as a `publish_inputs.ReleaseInputs` already anchored and checked; credentials arrive as redacted `destinations.Secret` values in `env.credentials`, never through `os.environ`. Python stdlib only; run external tools as argv lists through the step runner, never through a shell, and only in the `oidc` class: a `repo-write` or `token-only` module, and every `scripts/ci` module it imports, may start no child process (P12, RP-2). Read only neutral names, never `GITHUB_*` or `TEAMCITY_*` (policy rule P11).
2. **Register it** in `scripts/ci/publish_destinations.json` (the shipped entry; it stays `enabled: false` until the settings below are read back):
   ```json
   {"name": "s3-staging", "enabled": false, "tier": "staging",
    "permission_class": "oidc", "environment": "staging",
    "credentials": ["CI_PUBLISH_S3_BUCKET", "CI_PUBLISH_S3_PREFIX", "CI_PUBLISH_S3_ROLE",
                    "CI_PUBLISH_S3_REGION"], "binary_cache_policy": "any"}
   ```
   `binary_cache_policy` follows the tier (`release`: `none-only`, `staging`: `any`; P12). The `credentials` must be `CI_PUBLISH_*` names the class job's publish step already maps (P12 checks this); a new name is one line in that step's `env:`, an adapter change.
3. **Create the GitHub environment** named in the entry (`staging`), and for OIDC add the cloud-side trust to this repository and environment. Do this in repository settings, not in code. These settings, not the module's own checks, are the security boundary: follow the checklist in [ADR 0005 "Repository settings required"](/docs/adr/0005-unified-build-package-publish-pipeline.md#repository-settings-required) (exact `sub` claim, no wildcard trust, a role scoped to `staging/*` with no delete, release content in a separate bucket or behind an explicit deny).
4. **Add tests** under `scripts/ci/tests/`: `validate` rejects missing or malformed inputs, `publish --dry-run` lists the exact files and keys without any network call, `check_published` reports a digest mismatch as `<what>: expected <x> -> actual <y>`, and the module contains no platform-specific names. For a storage destination also test path confinement and create-only writes (see the S3 example's test list). Add the destination's row to `test_publish_destinations.EveryDestinationDryRunTests.EXPECT` (the test fails until you do). Credentialed destinations also need a canary test on every path (success, 4xx, 5xx, redirect, network error, crash), as `test_publish_github_release.CanaryTests` and `test_s3_staging` do.
5. **Run it locally** against a scratch location: `python3 scripts/ci/stage.py publish --destination s3-staging --dry-run`, then for real with the three variables exported.
6. **Pin any new tool** (here the `aws` CLI) in the toolchain pins file so the version header and TeamCity agent requirements stay honest.

No workflow edit is needed while the destination fits a tier and permission class that already has a matrix job (`publish-release-repo-write`, `publish-staging-oidc`) and needs only the credential names that job maps. `plan.py` puts it in the class's `publish_<class>` output, the job runs one matrix row per destination, and `stage.py` reads the row as `CI_DESTINATION` and refuses it unless its tier and class equal the job's (`publish_jobs.json`, checked through `CI_JOB`). `test_publish_destinations.FakeDestinationRegistrationTests` proves this with two fake destinations against the unchanged workflows. A new class or tier (for example `token-only`) is one reviewed change in `stage-publish.yml` plus `publish_jobs.json`; the policy gate fails closed until both agree. A `release`-tier destination is scheduled on every `v*` tag even while disabled (so an early tag fails loudly); a `staging`-tier one only once enabled.

## Example: S3 staging area

Purpose: keep a disposable copy of every verified `master` build, for testers, outside GitHub Releases. It runs only on a push to `master`, after `verify` passed in the same run, through the `staging` environment ([ADR 0005 D9](/docs/adr/0005-unified-build-package-publish-pipeline.md), amended 2026-11-13).

The example below was corrected on 2026-11-13 after the [CI-rework threat model](/docs/security/2026-11-13-ci-rework.md) (CR-3) showed that the earlier `prefix.startswith("staging")` check accepted `staging-release` and `staging/../release`, that unvalidated manifest paths could read any runner file, and that `aws s3 cp` silently overwrote an earlier attempt of the same run. Confinement is now by canonical path, and every write is create-only.

```python
# scripts/ci/destinations/s3_staging.py  (illustrative)
import os, posixpath, re
from pathlib import Path

NAME = "s3-staging"
REQUIRED_ENV = ("CI_PUBLISH_S3_BUCKET", "CI_PUBLISH_S3_PREFIX", "CI_PUBLISH_S3_ROLE",
                "CI_RUN_ID", "CI_RUN_ATTEMPT")

_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")
_VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?")
_DIGITS = re.compile(r"\d{1,20}")
_FILE = re.compile(r"libbios-[0-9A-Za-z.+-]+-[a-z0-9]+-[a-z0-9]+\.(tar\.gz|zip)(\.sha256)?|manifest\.json")

def _prefix(raw):
    parts = raw.split("/")                           # no strip(): a stray "/" is an error
    if parts[0] != "staging":                        # rejects "staging-release", "stagingX"
        raise ValueError("prefix: expected staging[/...] -> actual <rejected>")
    for p in parts:
        if p in (".", "..") or not _COMPONENT.fullmatch(p):
            raise ValueError("prefix: expected safe components -> actual <rejected>")
    return "/".join(parts)

def _key(manifest, env, name):
    if not _VERSION.fullmatch(manifest["version"]):
        raise ValueError("version: expected X.Y.Z[-pre] -> actual <rejected>")
    run, attempt = env["CI_RUN_ID"], env["CI_RUN_ATTEMPT"]  # as built: Env.get()
    if not (_DIGITS.fullmatch(run) and _DIGITS.fullmatch(attempt)):
        raise ValueError("run id: expected digits -> actual <rejected>")
    if not _FILE.fullmatch(name):                    # basename only: no "/", no ".."
        raise ValueError("file: expected D4 basename -> actual <rejected>")
    key = f"{_prefix(env['CI_PUBLISH_S3_PREFIX'])}/{manifest['version']}/{run}-{attempt}/{name}"
    if posixpath.normpath(key) != key or not key.startswith("staging/"):
        raise ValueError("key: expected canonical staging/ key -> actual <rejected>")
    return key

def _source(artifact_dir, name):
    dist = Path(artifact_dir, "dist").resolve(strict=True)
    path = dist / name
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != dist:
        raise ValueError("file: expected regular file in dist/ -> actual <rejected>")
    return path

def validate(manifest, env):                         # runs before any write, also in --dry-run
    for f in manifest["legs"]:                       # D4: legs[] of {leg, file, sha256, ...}
        _key(manifest, env, f["file"])

def publish(manifest, artifact_dir, env, dry_run):
    validate(manifest, env)
    for f in manifest["legs"]:
        key = _key(manifest, env, f["file"])
        cmd = ["aws", "s3api", "put-object",
               "--bucket", env["CI_PUBLISH_S3_BUCKET"], "--key", key,
               "--body", str(_source(artifact_dir, f["file"])),
               "--if-none-match", "*",                # create-only: an existing key -> PreconditionFailed
               "--metadata", f"sha256={f['sha256']}"]
        run_step(cmd, dry_run=dry_run,                # argv, via ci_step, no shell
                 pass_env=("AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN"))  # nothing else reaches aws
    return {"destination": NAME, "files": len(manifest["legs"])}

def check_published(manifest, env): ...              # head-object metadata sha256 == manifest
```

The shipped module, `scripts/ci/destinations/s3_staging.py`, follows this sketch and adds: `CI_PUBLISH_S3_REGION` (the aws CLI must not guess a region), `--checksum-sha256` with the manifest digest (S3 itself refuses other bytes), metadata `not-a-release=true` (RP-14), the aws version checked against `pins.json` `tools.aws_cli` before the first write, a re-read of every object (`get-object`, size and sha256) as `check_published`, and an RC1 failure line that never carries aws output (which can name the assumed role).

Behaviour worth keeping:

- **Canonical-path confinement.** Every key is assembled only from validated parts (prefix components, version, run id, attempt, D4 basename), and the assembled key must equal its own `normpath` and start with `staging/`. Rejection messages never echo the rejected value.
- **Create-only.** `--if-none-match '*'` makes S3 refuse to overwrite any existing object; `PreconditionFailed` is a hard failure, never a retry with overwrite. Objects land under `staging/<version>/<run-id>-<attempt>/`, so a re-run attempt writes a fresh location instead of overwriting the previous one. The destination never deletes.
- **Local files** come only from manifest entries resolved to regular, non-symlink files directly under `dist/`.
- **Credentials.** The role is assumed through OIDC (no stored keys). The destination exchanges the OIDC token itself, writes it to a 0600 file under `CI_TEMP_DIR`, and passes only that file's path to `aws`; every other credential name is scrubbed from the child environment (0014 Story 02.0 Task 04).
- **Logs** show counts and the base location only, never the role ARN, a bucket policy, a presigned URL or a credential.
- **The cloud-side policy is the real boundary.** These client-side checks are defence in depth; a write outside `staging/*` or a delete must also be denied by the role policy (ADR 0005 "Repository settings required").

Tests (`scripts/ci/tests/test_s3_staging.py`, stub `aws`, loopback OIDC stub): `validate` rejects the prefixes `staging-release`, `stagingX`, `staging/../x`, `/staging`, and `a/../../b` (and twelve more); a manifest name containing `/` or `..`; a non-numeric run id or attempt; a symlink in `dist/`. A stub `aws` returning `PreconditionFailed` for an existing key gives a hard failure, and no second write is attempted. The stub records the child environment: only the `AWS_*` names the module passes, the token file `0600` in a `0700` directory and removed afterwards, no `CI_PUBLISH_*` name. Canaries for the role ARN, the OIDC request token and the JWT appear in no output on the success, 4xx, 5xx, redirect, network-error, aws-failure and crash paths.

## Checklist before merging a new destination

- [ ] Module, registry entry and tests added; `--dry-run` works offline.
- [ ] Tier is right: `release` only if it must run on tags; `staging` runs only on `master` pushes and must not write release locations.
- [ ] Remote paths are confined by canonical path (not a string prefix), and writes are create-only.
- [ ] The cloud-side or registry-side permission is scoped to the destination's location, and the ADR 0005 "Repository settings required" items for it are applied.
- [ ] Environment and credentials documented; nothing secret in logs, summaries or artifacts.
- [ ] Policy gate passes (P11, P12, P13).
- [ ] TeamCity note: which neutral names the adapter must map.
