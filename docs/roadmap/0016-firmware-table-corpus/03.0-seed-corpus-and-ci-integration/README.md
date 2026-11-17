# Story 03.0 - Seed Corpus and CI Integration

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Stories 01.0 and 02.0. The endian-proof deliverable additionally needs [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md) D3's `linux-s390x` leg

## Scope

Fill the corpus from the machines the maintainer already owns, and wire
the suite into CI so it runs on every Tier A leg **and** on the
big-endian `linux-s390x` leg.

That last part is this story's headline. Milestone 0003 rewrote the parser
to be endian-safe because the old technique "is not portable to a
big-endian host", and that claim has never been executed anywhere. Real
vendor tables replayed byte-for-byte on s390x are the strongest available
evidence for it - considerably stronger than synthetic tables, which this
project authored itself and which therefore cannot surprise it.

## Tasks

| #  | Document                                              | Status         | Blocks |
|----|-------------------------------------------------------|----------------|--------|
| 01 | [Capture recipe and the seed corpus](01-capture-recipe-and-seed-corpus.md) | ⬜ Not started | 02     |
| 02 | [CI integration and the endian proof](02-ci-integration-and-endian-proof.md) | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                  | ⬜ Not started | -      |

## Key constraints

- **Seed fixtures come from the maintainer's own machines only.** No third
  party is asked for anything in this story - that is Story 04.0, and it is
  gated.
- **Every seed fixture passes through `smbios_scrub`**, including the
  maintainer's own. A corpus where some fixtures skipped the tool cannot be
  audited, and the maintainer's serial numbers are no less permanent in git
  history than anyone else's.
- **The suite must not become silently empty.** Once seeded, CI asserts a
  minimum fixture count, so a deletion or a path regression fails loudly
  instead of quietly reducing coverage to nothing.
- **No new workflow file and no new job.** The corpus suite is part of
  `smbios_unit_test`, which existing legs already run.
- **The `linux-s390x` leg must not need vcpkg** (ADR 0007 D3) - the
  container's own Boost satisfies the plain `find_package(Boost ...)`.

## Files or directories created or modified by this story

- `tests/data/corpus/<vendor>-<model>-<nn>/` - the seed fixtures.
- `docs/dev/corpus-capture-recipe.md` (new) - how to capture a dump on each
  OS and scrub it, written so it can later be handed to a contributor
  unchanged.
- `scripts/ci/legs.json` - the `linux-s390x` Tier B leg (ADR 0007 D3), if
  not already added by the ADR 0007 rollout.
- `scripts/ci/test_sets.json` - the corpus cases as a named set.
- `docs/test/test-subsystem.md`, `docs/dev/ci.md` - the new coverage.
