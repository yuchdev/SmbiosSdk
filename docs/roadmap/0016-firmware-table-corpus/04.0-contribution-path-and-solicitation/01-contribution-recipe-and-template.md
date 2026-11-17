# 01 - Contribution recipe and submission template

**Status:** ⬜ Not started
**Depends on:** Stories 01.0-03.0; ADR 0007 D9's readiness bar recorded as met

## Objective
Turn the maintainer's capture recipe into something a stranger can follow safely in one pass, and give them a template that collects consent and metadata without collecting anything else.

## Files
- **Modify** `docs/dev/corpus-capture-recipe.md`: contributor-facing - capture, scrub, `--verify`, fill `meta.json`, open a PR. One command per OS where possible.
- **Modify** `CONTRIBUTING.md`: a corpus section stating plainly what the scrubber destroys, what it preserves, and that the result is **public and permanent**.
- **Create** the submission template under `.github/`: donor-stated platform, SMBIOS version, well-formedness, scrubber version, and an explicit consent checkbox referencing the removal procedure.

## Constraints
- **The recipe must scrub before anything leaves the machine.** No step may produce an unscrubbed artifact that a contributor could attach by mistake - prefer a flow where the scrubbed file is the only output written.
- **Tell the truth about permanence.** Git history makes a merged fixture effectively permanent even after deletion; the consent text must say so rather than implying a clean undo.
- The template must not ask for a hostname, username, IP address, or any machine-collected system detail.
- Assume no knowledge of CMake or this repository: a contributor may be running a prebuilt `smbios_scrub` from a release, not a build tree.
- Keep `--verify` in the recipe as the contributor's own confirmation step, so they can see for themselves that the file is clean before attaching it.

## Success criteria
- [ ] Someone unfamiliar with the repository can follow the recipe end to end on each OS and produce a valid, scrubbed, schema-conformant fixture.
- [ ] No step of the recipe can produce an unscrubbed file as its final output.
- [ ] The consent text states what is destroyed, what is preserved, and that merged fixtures are public and effectively permanent.
- [ ] The template collects no hostname, username, IP or machine-collected OS detail.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
