# The `smbios-c` vcpkg registry

This repository is a vcpkg **git registry** for one port, `smbios-c`: the C ABI
shim `smbios_c` over `libsmbios` (milestone 0012 Story 02.0; decision in
[ADR 0004](../adr/0004-package-manager-distribution.md); contract C2 in the
[milestone plan](../roadmap/0012-package-manager-distribution/plan.md)). It is
not a submission to the central `microsoft/vcpkg` registry, and it does not
package the `libsmbios` C++ surface.

## Using it (consumer side)

In the consumer project's `vcpkg-configuration.json`, keep the existing
default registry and add this repository as a second one:

```json
{
  "default-registry": {
    "kind": "git",
    "repository": "https://github.com/microsoft/vcpkg",
    "baseline": "<the consumer's own microsoft/vcpkg commit>"
  },
  "registries": [
    {
      "kind": "git",
      "repository": "https://github.com/yuchdev/SmbiosSdk",
      "baseline": "<a commit SHA of this repository that contains versions/>",
      "packages": ["smbios-c"]
    }
  ]
}
```

and in the consumer's `vcpkg.json`:

```json
{ "name": "my-app", "version": "0.1.0", "dependencies": ["smbios-c"] }
```

No `--overlay-ports` flag is needed. `baseline` pins which `versions/baseline.json`
vcpkg reads; use the registry commit listed in the release notes (the merge of
that release's `registry/v<version>` branch). The package installs
`include/smbios/smbios.h`, `include/smbios_c/smbios_c_export.h`, `lib/smbios_c`
(shared library) and `lib/pkgconfig/smbios_c.pc` (discover it with
`pkg-config smbios_c`, or `find_library`/`find_path` under the vcpkg prefix).
Only the intended `smbios_*` C symbols are exported.

**What the baseline does not pin.** A git registry's `baseline` pins only
`versions/baseline.json`. The list of available versions, and so the
`git-tree` behind each version, is read from the registry's `reference`, which
defaults to `HEAD`, which is `master` here
([threat model RP-3](../security/2026-11-18-release-and-publish-automation.md)).
This registry is append-only: the lint stage rejects any change to a
registered `git-tree` (see below). If you need the version database itself to
be immutable, also set `"reference"` to a release tag, for example
`"reference": "v1.0.1"`. vcpkg accepts a tag there: checked on 2026-11-18 with
vcpkg 2026-07-27 against a scratch copy of this registry. `vcpkg install
--dry-run` resolved `smbios-c` through the tag, and a tag that does not exist
failed. Once immutable releases are enabled (threat model S-4), that tag cannot
move. No `v*` tag exists yet; the first automated release is `1.0.1`.

## Repository layout

```
ports/smbios-c/vcpkg.json       port manifest (name, version, dependencies)
ports/smbios-c/portfile.cmake   vcpkg_from_git + the repository's own install() rules
versions/baseline.json          latest version of each port in this registry
versions/s-/smbios-c.json       every released version with its port-tree git hash
```

The root `vcpkg.json` and `vcpkg-configuration.json` are this project's own
*consumer* files and are unrelated to the registry role.

The port builds the whole repository (`smbios_c` is a subdirectory of the root
`CMakeLists.txt`, not a standalone project), so it needs the Boost libraries the
root build requires: `boost-date-time`, `boost-iostreams`,
`boost-program-options` (the CLI) and `boost-test` (the test suites). The root
build has no switch to skip the CLI or the tests; adding `SMBIOS_BUILD_CLI` and
`SMBIOS_BUILD_TESTS` options would drop the last two dependencies and is a
follow-up (it edits `CMakeLists.txt`, which the port story does not).
`libsmbios`'s own install output (static libraries, headers, CMake package,
Story 01.0) is deliberately pruned by the portfile.

## Releasing a new `smbios-c` version

The publish stage registers a release automatically. The automation creates a
branch; a maintainer opens and merges the PR. Milestone 0012 Story 04.0 Task 03
built this ([task](../roadmap/0012-package-manager-distribution/04.0-release-and-publish-automation/03-registry-update-automation.md));
the [threat model](../security/2026-11-18-release-and-publish-automation.md)
governs it.

**Why the entry follows the release commit.** The port's `REF` is the release
commit, and `versions/s-/smbios-c.json` records the `git-tree` of a port tree
that contains that `REF`. Neither can live in the commit it names, so a version
is registered in a commit **after** its release commit. The release commit is
then in the state *pending registration*:

- the port `"version"` already equals the root `project()` VERSION;
- `versions/baseline.json` still names the newest registered entry;
- that entry is older than the port version.

The lint `registry` step accepts exactly this state
(`ok: vcpkg registry: smbios-c registry consistent; 1.0.1 pending registration
(newest registered 1.0.0); ...`), and `check_versions.py` accepts a baseline
that trails the source version (site rule `registered`). So `master` stays
green in every commit of a release.

1. **Prepare the release commit.** On a branch, run
   `python3 scripts/release/prepare_release.py X.Y.Z`. It sets the root
   VERSION and the port `"version"` and runs the version check. It does not touch
   `versions/`, the portfile `REF` or `port-version`. Merge it to `master` with
   a merge commit or a rebase, never a squash. The lint is green: the port is
   pending registration.
2. **Tag that commit** `vX.Y.Z` (a human action; threat-model setting S-3
   restricts who may). A pre-release tag `vX.Y.Z-rc.N` publishes a GitHub
   pre-release only and never touches the registry (constraint 8: the plan
   gives it no registry rows).
3. **`registry-prepare`** runs after the GitHub Release is promoted, in the
   same run. The job holds **no credential** (`contents: read`, no
   environment) and is the only one that runs git, vcpkg and a compiler. It:
   - fetches `master` without credentials and uses that commit as the base.
     The release commit must be an ancestor of it, and the base must be
     pending registration for exactly `manifest.version`;
   - sets the portfile `REF` to `manifest.commit`. That is the commit the
     release was built from; the tag is never resolved by name (constraint 5);
   - commits that locally, then runs
     `vcpkg x-add-version smbios-c --x-builtin-ports-root=./ports
     --x-builtin-registry-versions-dir=./versions --verbose`. It never passes an
     overwrite switch, so a version that is already registered fails here;
   - checks its own result with the same rules `registry-pr` applies. Only the
     portfile, `versions/baseline.json` and `versions/s-/smbios-c.json` may
     change. The `git-tree` must equal the one computed independently from the
     base. The full registry lint must pass, including the append-only checks;
   - runs the consumer test `scripts/ci/vcpkg_registry_smoke.py`. It installs
     `smbios-c` through the prepared registry commit with vcpkg (cold, no
     binary cache), builds and runs a clean-room consumer, checks that only
     `smbios_*` symbols are exported, and checks the exact list of installed
     files;
   - uploads the three files by artifact ID and exports their sha256, the
     base commit and the `git-tree` as job outputs.
4. **`registry-pr`** holds `contents: write` and nothing else, and makes REST
   calls only. It:
   - repeats the commit preflight (tag, commit, `master` ancestry);
   - reads the base commit's registry files through the GitHub Git Data API;
   - accepts the hand-over only if it is exactly the one allowed change;
   - creates the **new** branch `registry/vX.Y.Z` (blobs, tree, commit, ref).
     If that branch already exists, the job fails; it never updates a ref.

   It prints the compare URL and **does not open a PR**. A PR opened with
   `GITHUB_TOKEN` would need "Allow GitHub Actions to create and approve pull
   requests", which lets any workflow approve PRs (threat model RP-9, setting
   S-6). It would also trigger no CI.
5. **Open the PR from that branch and merge it** (the one manual step). A
   human-opened PR runs normal CI, including the registry lint. Merge with a
   merge commit. That merge is the registry **baseline** consumers pin.

**A port-only fix at the same version** (`"port-version"` +1) is not
automated: no tag drives it. On a branch, edit the portfile and bump
`"port-version"`, commit, run the `x-add-version` command above (no overwrite
switch), commit `versions/`, and open a PR. The `REF` stays the version's tag
commit. The branch's first commit is pending registration, so it is green too.

**Re-releasing a version is not possible.** A registered
`(version, port-version)` is frozen. Release a new version instead; that is why
the first automated release is `1.0.1`
([status.md](../roadmap/0012-package-manager-distribution/status.md),
2026-11-18).

### What the lint enforces

`scripts/ci/check_vcpkg_registry.py` runs in the lint stage on every event.

**The current tree.** It fails when:
- the newest entry's `git-tree` does not match the committed `ports/smbios-c`
  (unless the port is pending registration, as described above);
- the baseline names no registered entry, or not the highest one;
- `REF` is not a full SHA, or the portfile `URL` is not this repository.

**The history (append-only, constraint 9).** The lint job checks out the full
history and all tags (`fetch-depth: 0`); a shallow checkout fails closed. It
fails when:
- any `(version, port-version)` recorded by **any** commit reachable from
  `HEAD` disappears or changes its `git-tree`;
- the `REF` inside a registered `git-tree` is not an ancestor of `master`
  (`origin/master`, else `master`);
- a `v<version>` tag exists and its commit differs from that `REF`.

The only exemption is `SUPERSEDED` in the script. It lists, by exact value, the
two `1.0.0` trees that Story 02.0 replaced before that version was ever
published. Adding to it needs a dated status entry and a security review.

`HEAD_REF master` in the portfile only affects `vcpkg install --head`, which
builds the unpinned tip of `master`; a normal install always builds the pinned
`REF`. Never use a `REF` that is not reachable from `master`. GitHub can serve
a commit that exists only in a fork when it is fetched by SHA from the parent
URL. The lint now enforces this.
