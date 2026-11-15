# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Test both destination modules without publishing, and build both packages in clean containers. Packaging-correctness tests, not Boost.Test cases.

## Files
- **Create** `scripts/ci/tests/test_destination_homebrew_tap.py` and `scripts/ci/tests/test_destination_aur.py`: exercise `validate`, `publish(dry_run=True)` and `check_published` against a synthetic `manifest.json` and a fake push layer. Assert the digest-mismatch, missing-tarball-entry and (for AUR) `.SRCINFO`-inconsistency failures each produce a named error, and that no secret reaches any emitted line on any path.
- **Create** `tests/packaging/brew_formula/`: `brew install --build-from-source` from a local tap copy on a clean macOS runner, then `brew audit --strict` and the `test do` block; asserts no `smbios_gui` is installed.
- **Create** `tests/packaging/aur_pkgbuild/`: `makepkg` in a clean Arch container, then `namcap`; asserts contract C1's layout under `$pkgdir`, that `pkgdesc` names the root requirement, and that **no file is setuid or setcap**.
- **Modify** `docs/test/test-subsystem.md`: the new coverage.

## Constraints
- **No network and no credential access in the unit tests**; fork PRs must be able to run them all.
- The unit tests must assert **digest provenance** - that both modules read digests from `manifest.json` - and a module altered to recompute a digest must fail its own test.
- The macOS formula test must assert a **real parse** (`--format json` producing parsable output), which is possible because macOS needs no privileges. The Arch test must assert a clean non-crashing exit with a diagnostic for the unprivileged case and a real parse only under `sudo`, because Linux sysfs is root-only - asserting a parse unprivileged would fail on exactly the platform it most needs to pass on.
- The setuid/setcap assertion runs against the **built package contents**, not the source tree, because that is where such a bit would actually be introduced.
- The container/runner builds are slow; wire them to the same tier as the other packaging legs rather than to every push (ADR 0005 D7).

## Success criteria
- [ ] Both module unit-test suites pass with no network access and no credentials.
- [ ] A module altered to recompute a digest fails its own test.
- [ ] Every failure path is asserted secret-free.
- [ ] `brew install --build-from-source` + `brew audit --strict` pass on a clean macOS runner, and no `smbios_gui` is installed.
- [ ] `makepkg` + `namcap` pass in a clean Arch container, contract C1's layout is present under `$pkgdir`, and no file is setuid or setcap.
- [ ] Unprivileged `smbios` in the Arch container exits cleanly with a diagnostic; `sudo smbios --format json` parses.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
