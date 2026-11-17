# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Make the review checklist executable where it can be, so an unsafe fixture is caught by CI rather than by a tired reviewer.

## Files
- **Create** `scripts/ci/tests/test_corpus_submission_gate.py`: for every fixture in `tests/data/corpus/`, assert `meta.json` validates, `sha256` matches `table.bin`, `scrubber_version` is not older than the current contract version, and no path component matches an identifier-shaped pattern.
- **Modify** `scripts/ci/stage.py`'s `lint` stage: run the gate on every push, so a donated fixture is checked at PR time.
- **Modify** `docs/dev/corpus-review-checklist.md`: mark which checks are automated and which stay human.

## Constraints
- **The automated gate cannot prove a fixture is scrubbed** - only that it passes the checks the tool can express. `smbios_scrub --verify` in the reviewer's hands remains the real control, and the checklist must say so rather than implying CI has it covered.
- Run in the `lint` stage so a contributor gets feedback on their own PR without waiting for the full matrix.
- Python stdlib only; no network.
- The identifier-shaped path check must be conservative: a false positive that blocks a legitimate vendor name is a cheap annoyance, a false negative is a leak.

## Success criteria
- [ ] The gate runs on every push and fails on a stale `scrubber_version`, a `sha256` mismatch, an invalid `meta.json` and an identifier-shaped path component.
- [ ] A contributor's PR shows the gate result without a full matrix run.
- [ ] The checklist states plainly which checks are automated and which are not.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
