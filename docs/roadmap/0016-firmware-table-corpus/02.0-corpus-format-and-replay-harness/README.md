# Story 02.0 - Corpus Format, Storage and Replay Harness

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Story 01.0 (nothing may be stored until it can be scrubbed)

## Scope

Define what a corpus fixture **is** (milestone contract C2) and build the
harness that replays it (contract C3). The fixture format decides what a
contributor will later be asked for, so it lands before anyone is asked -
changing the format afterwards wastes other people's effort.

The harness feeds each fixture through `SmbiosTableWalkTestAccess`, the
same seam `SyntheticTableBuilder` uses, so the corpus suite stays in
`tests/unit/`: hermetic, unprivileged, and therefore runnable under
emulation on ADR 0007 D3's big-endian `linux-s390x` leg.

## Tasks

| #  | Document                                                 | Status         | Blocks |
|----|----------------------------------------------------------|----------------|--------|
| 01 | [Fixture format and metadata sidecar](01-fixture-format.md) | ⬜ Not started | 02     |
| 02 | [The `CorpusReplay` harness](02-replay-harness.md)       | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                     | ⬜ Not started | -      |

## Key constraints

- **Fixtures are bytes plus metadata, never parsed output.** Storing a
  rendered parse would make the fixture a snapshot of today's parser and
  useless as a regression test against tomorrow's.
- **The corpus suite stays hermetic.** No `ISmbiosSource` reads from
  `tests/data/corpus/`; no fixture needs root, a display or a network. This
  is precisely what lets the suite run emulated on `linux-s390x`.
- **A fixture is never silently repaired.** Deliberately malformed real
  tables are the most valuable fixtures in the corpus; the harness asserts
  memory safety and non-crashing behaviour, and asserts a recovered parse
  only where `meta.json` declares the table well-formed - the stance
  `tests/unit/malformed_table_test.cpp` already takes.
- **Failures name the fixture.** A red build that does not say which
  vendor's table broke makes the corpus worse than useless.
- **No identifier in a path.** Directory names carry no serial, asset tag
  or UUID fragment.
- **Every fixture records provenance and consent**, so a contributor can
  later request removal and the project can honour it.

## Files or directories created or modified by this story

- `tests/data/corpus/` (new) - the fixture tree, with a `README.md`
  describing the format.
- `tests/unit/corpus_replay_test.cpp`, `tests/unit/corpus_fixture_loader.{hpp,cpp}` (new).
- `tests/unit/CMakeLists.txt` - the corpus cases, and the fixture-path
  plumbing the suite needs to find `tests/data/corpus/` from a build tree.
- `docs/dev/corpus-privacy-contract.md` - the `meta.json` consent fields.
- `docs/test/test-subsystem.md` - the new coverage.
