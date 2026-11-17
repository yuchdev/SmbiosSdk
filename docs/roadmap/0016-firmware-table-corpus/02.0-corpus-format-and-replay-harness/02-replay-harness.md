# 02 - The `CorpusReplay` harness

**Status:** ⬜ Not started
**Depends on:** Task 01

## Objective
Implement contract C3: a `tests/unit/` harness that discovers every fixture and replays it through `SmbiosTableWalkTestAccess`, alongside the existing synthetic cases.

## Files
- **Create** `tests/unit/corpus_fixture_loader.{hpp,cpp}`: discovers fixture directories, reads `table.bin` and validates `meta.json` against the schema.
- **Create** `tests/unit/corpus_replay_test.cpp`: per fixture - walk the table, assert memory safety and non-crashing behaviour; assert a recovered parse only where `meta.json` says well-formed; then render every entry through **every registered output formatter**.
- **Modify** `tests/unit/CMakeLists.txt`: link the new sources and pass the corpus directory in (a compile definition or a test-time environment variable), so the suite finds fixtures from any build tree.

## Constraints
- **Use the existing seam.** No `ISmbiosSource`, no hardware, no privileges - this is what keeps the suite runnable under emulation on `linux-s390x`.
- **Every failure message names the fixture directory.** A corpus failure that says only "assertion failed" costs more time than the fixture saves.
- **An empty corpus must pass, loudly.** Before Story 03.0 the tree is empty; the suite should report "0 fixtures" rather than failing, but it must not be *silently* green forever - Story 03.0's CI task adds the minimum-count assertion.
- **Formatter rendering is a smoke test, not a golden-output test.** Real tables contain structure types and value shapes the synthetic builder never produces, so this is broad `get_fields()` coverage; pinning exact rendered bytes would make every formatter tweak a corpus-wide churn.
- Fixture discovery must be deterministic and sorted, so a failure is reproducible and the report order is stable.
- The loader must fail cleanly - not crash - on a corrupt or missing `meta.json`, naming the directory.

## Success criteria
- [ ] Every fixture in the tree is discovered, in a stable order, and replayed.
- [ ] A malformed-by-design fixture passes on memory safety without asserting a recovered parse.
- [ ] A deliberately corrupted `table.bin` produces a named failure, not a crash or a hang.
- [ ] Every entry of every fixture renders through every registered formatter without error.
- [ ] The suite runs with no hardware, no privileges and no network, on all three platforms.
- [ ] An empty corpus reports zero fixtures and passes.
