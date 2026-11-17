# 02 - CI integration and the endian proof

**Status:** ⬜ Not started
**Depends on:** Task 01; ADR 0007 D3's `linux-s390x` leg for the endian half

## Objective
Run the corpus suite everywhere the unit tests already run, and add the big-endian leg that turns milestone 0003's endian-safety claim into a tested one.

## Files
- **Modify** `scripts/ci/test_sets.json`: the corpus cases as a named set, so legs select them by name like every other set.
- **Modify** `scripts/ci/legs.json`: the `linux-s390x` Tier B leg - QEMU container, distro Boost, **no vcpkg**, unit tests only, nightly and tag triggers (ADR 0007 D3). If the ADR 0007 rollout already added it, this task only attaches the corpus set.
- **Modify** `docs/dev/ci.md` and `docs/test/test-subsystem.md`: the new leg and the new coverage.

## Constraints
- **Assert a minimum fixture count in CI.** Once seeded, a corpus that silently becomes empty - a deleted directory, a broken fixture path on one platform - would turn a passing suite into a meaningless one. Fail loudly on zero.
- **No new workflow file and no new job** - the corpus is part of `smbios_unit_test`, already run by existing legs.
- **The s390x leg runs `tests/unit/` only.** No packaging, no live-table test, no GUI: none is meaningful under emulation, and all would multiply an already-slow leg.
- **Nightly and tag only, never pull requests** (ADR 0007 D3). An emulated leg on every PR would dominate pipeline latency.
- Record the leg's Boost source explicitly in `legs.json`, so the no-vcpkg divergence is visible and does not get "fixed" back to a preset.
- The emulated leg's failure output must still obey the report contract (bounded excerpt, no megabytes).

## Success criteria
- [ ] The corpus suite runs on all six Tier A legs and on `linux-s390x`.
- [ ] **A real vendor table parses correctly on a big-endian host** - milestone 0003's claim demonstrated rather than asserted.
- [ ] Emptying the corpus directory fails CI with a message naming the minimum-count rule.
- [ ] The s390x leg does not invoke vcpkg, and says where Boost came from.
- [ ] The leg runs on nightly and tag events only.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
