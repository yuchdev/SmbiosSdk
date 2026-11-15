# 01 - Homebrew tap formula and destination module

**Status:** ⬜ Not started
**Depends on:** Story 01.0 (install rules + licence); milestone 0012 Story 04.0 Tasks 02-05 (release tarball + plug-in framework). **Blocked on ADR 0006 open question 3** - the tap repository must exist

## Objective
Publish a Homebrew formula in this project's own tap, building the CLI and libraries from the release tarball. The highest-value macOS channel: no signing, no review queue, and `brew install` is the idiom this audience expects.

## Files
- **Create** `packaging/homebrew/libbios.rb.in`: a formula with `desc`, `homepage`, `url` (the release tarball), `sha256` and `version` substituted from `manifest.json` and the single version source, `license "MIT"`, `depends_on "cmake" => :build`, `depends_on "boost"`, the standard `cmake -S . -B build` + `cmake --install` install block using this project's own toolchain expectations, a `test do` block, and a `caveats` section stating that macOS needs no privileges but that the tool reads firmware tables.
- **Create** `scripts/ci/destinations/homebrew_tap.py`: `NAME = "homebrew-tap"`; `REQUIRED_ENV` = the tap-push token plus the neutral version/artifact names; `validate` asserts the macOS tarball entry exists in the manifest, the template renders, and the rendered `sha256` equals the manifest's; `publish` renders the formula and pushes it to the tap repository; `check_published` re-reads the formula from the tap and compares its `url`/`sha256` with the manifest.
- **Modify** `scripts/ci/publish_destinations.json`: `{ "name": "homebrew-tap", "enabled": true, "tier": "release", "permission_class": "repo-write", "environment": "release" }`.
- **Modify** `scripts/ci/check_versions.py`: the formula version as a version site.
- **Modify** `docs/cli-usage.md`: an "Installing on macOS" section.

## Constraints
- **CLI and libraries only - no GUI.** `smbios_gui` on macOS wants a `.app` bundle that does not exist, and a cask of an unsigned binary still fails Gatekeeper (ADR 0006 D2, Wave 2). Configure the build with `SMBIOS_BUILD_GUI=OFF` explicitly rather than relying on the default, so a future default flip cannot silently start installing a bare GUI executable into `bin`.
- **Boost comes from Homebrew, not vcpkg.** A formula that bootstraps vcpkg during `brew install` breaks Homebrew's build sandbox expectations and reproducibility. This means the formula's configure step cannot use this project's `macos` CMake preset (which wires `CMAKE_TOOLCHAIN_FILE` to `$env{VCPKG_ROOT}`); it invokes CMake directly against Homebrew's Boost. State that divergence in a comment so nobody "fixes" the formula by switching it to the preset.
- **`sha256` and `url` come from `manifest.json`**, never recomputed and never pointed at a branch or `HEAD`.
- **The `test do` block must assert real behaviour**, not just that the binary exists: macOS needs no privileges, so it can run `smbios --format json` and assert parsable output - a genuine end-to-end check that `brew audit` will also exercise.
- `brew audit --strict --new` must pass; its complaints are the de facto formula style gate.
- The push credential comes only through the `release` environment, passed by name, never logged.

## Success criteria
- [ ] `stage.py publish --destination homebrew-tap --dry-run` prints the rendered formula, the tap path and the commit it would make, with no network write and no secret read.
- [ ] `validate` fails when the rendered `sha256` differs from the manifest's, and when the macOS tarball entry is absent.
- [ ] `brew install --build-from-source` from the tap succeeds on a clean macOS runner; `smbios --format json` emits parsable output with no privileges.
- [ ] `brew audit --strict` passes, and the `test do` block passes.
- [ ] No `smbios_gui` executable is installed.
- [ ] `check_published` fails when the published formula's digest disagrees with the manifest.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
