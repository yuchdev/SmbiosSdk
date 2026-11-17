# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Test the harness itself - the thing that will be trusted to validate every future donation - using synthetic fixtures, before any real one exists.

## Files
- **Create** `tests/unit/corpus_harness_test.cpp`: builds temporary fixture directories from `SyntheticTableBuilder` output and asserts the loader's and harness's behaviour on each - valid fixture, missing `meta.json`, schema-invalid `meta.json`, `sha256` mismatch, empty directory, unreadable `table.bin`.
- **Modify** `docs/test/test-subsystem.md`: record that the corpus suite is a `tests/unit/` category member and why (hermetic, no hardware), so nobody later reclassifies it as an integration suite and stops it running under emulation.

## Constraints
- **`sha256` mismatch must fail.** The digest in `meta.json` is what detects a fixture edited in place - the most likely way a scrubbed fixture silently becomes unscrubbed.
- Build fixture directories in a temporary location; never write into `tests/data/corpus/` from a test.
- The harness tests must not depend on any real fixture existing, so they keep passing in a fresh clone before Story 03.0.
- No network, no privileges, no display.

## Success criteria
- [ ] Each malformed-fixture case produces a named, non-crashing failure.
- [ ] A `sha256` mismatch fails and names the fixture.
- [ ] The suite passes on a tree with zero real fixtures.
- [ ] `docs/test/test-subsystem.md` states the classification and the reason.
