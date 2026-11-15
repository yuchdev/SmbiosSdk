# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Test both destination modules without publishing, then verify the real published packages install on a clean machine. Packaging-correctness tests, not Boost.Test cases.

## Files
- **Create** `scripts/ci/tests/test_destination_chocolatey.py` and `scripts/ci/tests/test_destination_winget.py`: exercise `validate`, `publish(dry_run=True)` and `check_published` against a synthetic `manifest.json` and a fake HTTP/CLI layer. Assert the digest-mismatch, missing-MSI-entry, unreachable-URL and schema-pin-mismatch failures each produce a named error; assert no secret value reaches any emitted line on any path, including every failure path.
- **Create** `scripts/ci/tests/test_destination_registry.py` (or extend milestone 0012's): asserts both new rows have a known permission class and an existing module, that both are `release` tier, and that neither is reachable from a non-tag event - the checks the policy gate P12 performs.
- **Create** `tests/packaging/windows_channels/`: a clean-runner harness running `choco install libbios` and `winget install <Publisher>.libbios`, asserting the MSI's payload lands and `smbios --version` resolves through `PATH`, then uninstalling via each channel.
- **Modify** `docs/test/test-subsystem.md`: the new destination-test coverage.

## Constraints
- **No network and no credential access in the unit tests.** Everything is a dry run against a fake layer; a test that reaches Chocolatey or GitHub is not a unit test and will fail in a fork PR.
- The unit tests must assert the **digest provenance**: that both modules read the digest from `manifest.json` and that a module modified to recompute it fails the test. This is the single most important invariant in the story and it must be impossible to regress silently.
- The clean-runner channel tests run **after** a real release, not in the normal pipeline, because they depend on third-party moderation and merge timing. Wire them as a post-publish check (milestone 0012 Story 04.0 Task 04's pattern) with a clear, loud failure naming the release - not as a gate that blocks a tag.
- Fork PRs must be able to run every unit test here.

## Success criteria
- [ ] Both module unit-test suites pass with no network access and no credentials available.
- [ ] A module altered to recompute a digest instead of reading the manifest fails its own test.
- [ ] Every failure path is asserted secret-free.
- [ ] The registry test fails on an unknown permission class, a missing module, and a `release`-tier row reachable from a non-tag event.
- [ ] `choco install` and `winget install` both succeed on a clean Windows runner against the real published packages, and both uninstall cleanly.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
