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

No `--overlay-ports` flag is needed. `baseline` pins which `versions/` state
vcpkg reads; use the latest release commit listed in the release notes. The
package installs `include/smbios/smbios.h`, `include/smbios_c/smbios_c_export.h`,
`lib/smbios_c` (shared library) and `lib/pkgconfig/smbios_c.pc` (discover it
with `pkg-config smbios_c`, or `find_library`/`find_path` under the vcpkg
prefix). Only the intended `smbios_*` C symbols are exported.

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

The order matters, because the port's source `REF` and the registry's
`git-tree` are commit and tree hashes that cannot refer to the commit that
carries them.

1. Merge the source change to `master`, and note its full commit SHA: this is
   the **source commit**. It must contain `LICENSE` and be reachable from
   `master`, so merge with a merge commit (or rebase), never a squash, and do
   not delete the branch before the merge: a squash leaves the old commit
   unreachable and `vcpkg_from_git` may stop resolving it.
2. In a branch, edit `ports/smbios-c/portfile.cmake` (`REF` = the source
   commit) and `ports/smbios-c/vcpkg.json` (`"version"`; keep it equal to
   the root `CMakeLists.txt` `project()` VERSION, which
   `scripts/ci/check_versions.py` enforces). For a port-only fix at the same
   version, bump `"port-version"` instead.

   `python3 scripts/release/prepare_release.py X.Y.Z` sets the root VERSION,
   this `"version"` and `versions/baseline.json` in one step and runs the check;
   it never commits, tags, or touches `versions/s-/smbios-c.json`. Because
   `check_versions.py` requires all three to agree in every commit, the bump
   travels with the source commit of step 1, and the lint `registry` step
   reports `newest version differs from the port version` until step 4 adds the
   entry. Story 04.0 Task 03 automates this sequence.
3. **Commit that.** `vcpkg x-add-version` reads the port from git, not from the
   working tree.
4. From the repository root run:

   ```
   vcpkg x-add-version smbios-c \
       --x-builtin-ports-root=./ports \
       --x-builtin-registry-versions-dir=./versions \
       --overwrite-version        # only when re-releasing an unpublished version
   ```

   It fails on a dirty working tree. It adds the entry to `versions/s-/smbios-c.json` (the `git-tree` of
   `ports/smbios-c`) and updates `versions/baseline.json`.
5. Commit `versions/`. Open a PR. The commit that merges to `master` is the new
   registry **baseline** consumers pin.

`scripts/ci/check_vcpkg_registry.py` (lint stage) fails when the `git-tree` in
`versions/` does not match `ports/smbios-c`, when `REF` is not a full SHA, or when the
portfile `URL` is not this repository, so a port edit without step 3-5
cannot reach `master`.

`HEAD_REF master` in the portfile only affects `vcpkg install --head`, which builds
the unpinned tip of `master`; a normal install always builds the pinned `REF`.
Never reuse a `REF` that is not reachable from `master`: GitHub can serve a
commit that exists only in a fork when fetched by SHA from the parent URL.
