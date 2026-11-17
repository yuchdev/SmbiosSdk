# 02 - Review checklist and the public call

**Status:** ⬜ Not started
**Depends on:** Task 01

## Objective
Give the maintainer a review checklist that catches an unsafe donation before it merges, then make the public call for dumps.

## Files
- **Create** `docs/dev/corpus-review-checklist.md`: the per-submission checks - `smbios_scrub --verify` passes; `meta.json` validates and its `sha256` matches `table.bin`; `scrubber_version` is current; no identifier in any path; the fixture adds coverage the corpus lacks; the suite passes with it.
- **Modify** `docs/roadmap/0016-firmware-table-corpus/status.md`: record ADR 0007 D9's readiness bar as met, with evidence, **before** the call.
- The call itself: the channels ADR 0007 D9 lists, pointing at the recipe and `CONTRIBUTING.md`.

## Constraints
- **Re-verify every submission; trust none.** A contributor may have used an older scrubber, skipped `--verify`, or hand-edited the file afterwards. The `sha256` check is what catches the last case.
- **Reject rather than repair.** If a submission fails verification, ask the contributor to re-run the recipe; do not scrub it yourself and merge it, because the unscrubbed bytes would then exist in the reviewer's own history and possibly in the PR.
- **A fixture that adds no coverage is still allowed to be declined** - a corpus of fifty identical corporate laptops costs CI time and adds nothing. Say so in the checklist so declining is a normal, non-awkward outcome.
- **Do not make the call before the gate record exists.** The point of the gate is that the project can absorb what the call produces.
- The call text must not promise support, a release cadence or a response time the project cannot keep.

## Success criteria
- [ ] The checklist catches each of: an unscrubbed file, a `sha256` mismatch, a stale `scrubber_version`, an identifier in a path.
- [ ] The gate record in `status.md` cites concrete evidence (green legs, a tagged release, the executed manual checklists).
- [ ] The call points at the recipe and the consent terms, and over-promises nothing.
- [ ] A dry-run review of a deliberately bad submission is declined by the checklist, not repaired.
