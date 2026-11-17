# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Prove the scrubber removes everything the contract names and breaks nothing it promises to preserve. These are `tests/unit/` cases - hermetic, no hardware, no privileges - because the scrubber operates on bytes.

## Files
- **Create** `tests/unit/scrub_contract_test.cpp`: for **every** destroy-classified field in contract C1, build a synthetic table with `SyntheticTableBuilder` containing a known sentinel in that field, scrub it, and assert the sentinel is absent from the output bytes. This is the test that must grow whenever the contract grows.
- **Create** `tests/unit/scrub_structure_preservation_test.cpp`: assert lengths, handles, type order, string-section framing and the double-NUL terminator are byte-identical before and after; assert the scrubbed table still parses to the same structure count and the same handles.
- **Create** `tests/unit/scrub_failure_modes_test.cpp`: unknown structure type exits non-zero and writes nothing; same-length-impossible replacement fails loudly; idempotence holds; a truncated or malformed input fails cleanly rather than crashing (reusing `malformed_table_test.cpp`'s stance).
- **Modify** `docs/test/test-subsystem.md`: the new coverage.

## Constraints
- **The sentinel test must be exhaustive against the contract, and visibly so.** If a field is in C1's destroy list and has no sentinel case, that is a test gap that leaks real data. Prefer a data-driven case list that fails when the contract has an entry the test does not cover, over a hand-maintained list of `BOOST_AUTO_TEST_CASE`s that silently falls behind.
- **Search the whole output buffer for the sentinel**, not just the field's own offset. A string accidentally duplicated into another structure, or left in the string section after a bad rewrite, must fail the test.
- No real firmware dump is used as test input in this story. Synthetic tables are sufficient and keep the suite hermetic; real fixtures arrive in Story 03.0, after the scrubber is already trusted.
- No network, no privileges, no display.

## Success criteria
- [ ] Every destroy-classified field in C1 has a sentinel case, enforced structurally rather than by convention.
- [ ] Removing a field from the scrubber's implementation makes its sentinel case fail.
- [ ] Structure preservation holds byte-for-byte across all synthetic shapes tested.
- [ ] All three failure modes (unknown type, impossible replacement, malformed input) fail cleanly and write no output.
- [ ] The suite runs with no hardware, no privileges and no network, on all three platforms.
