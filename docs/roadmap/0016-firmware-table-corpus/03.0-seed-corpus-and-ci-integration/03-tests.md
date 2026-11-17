# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Verify the seeded corpus and its CI wiring behave on every platform and architecture, including the emulated one.

## Files
- **Create** `scripts/ci/tests/test_corpus_integration.py`: asserts the minimum-count rule fires on an empty tree; asserts the corpus test set is selected by the legs that should run it and not by those that should not; asserts the `linux-s390x` leg's declared configuration (no vcpkg, unit tests only, nightly/tag triggers) matches `legs.json`.
- **Modify** `docs/test/test-subsystem.md`: the corpus suite's place in the four-category scheme and its architecture coverage.

## Constraints
- Python stdlib only, matching `scripts/ci/`'s rule; runnable on a developer machine.
- The integration test asserts **configuration**, not emulated execution - running QEMU inside a unit test would be slow and flaky. The real proof is the nightly leg going green.
- No network.

## Success criteria
- [ ] The corpus suite passes on all three OSes locally and on all six Tier A legs.
- [ ] A nightly `linux-s390x` run is green, with at least one real fixture parsed.
- [ ] Emptying the corpus fails CI by the minimum-count rule.
- [ ] Changing the s390x leg to use vcpkg, or to run on pull requests, fails the configuration test.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
