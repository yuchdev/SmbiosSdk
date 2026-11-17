# 01 - Fixture format and metadata sidecar

**Status:** ⬜ Not started
**Depends on:** Story 01.0

## Objective
Fix the on-disk shape of a corpus fixture (contract C2), including the metadata that makes a fixture traceable, removable and reprocessable.

## Files
- **Create** `tests/data/corpus/README.md`: the format, the naming rule, and an explicit statement that everything in the tree is scrubbed test data which ships in no package.
- **Create** `tests/data/corpus/_schema/meta.schema.json`: the `meta.json` schema - SMBIOS version, donor-stated platform (free text), scrubber version, `sha256` of `table.bin`, well-formedness flag, provenance and consent.
- **Modify** `docs/dev/corpus-privacy-contract.md`: the consent terms and the removal procedure.

## Constraints
- Layout is `<vendor>-<model-slug>-<nn>/` containing `table.bin`, optional `entry_point.bin` and `meta.json`. **No serial, asset tag or UUID fragment in any path component.**
- **`scrubber_version` is mandatory.** When contract C1 later grows a field, it is the only way to identify which fixtures need reprocessing. A corpus without it silently retains data a newer contract would have destroyed.
- **A well-formedness flag is mandatory**, because it decides what the harness may assert. Defaulting it to "well-formed" would make a deliberately malformed donation fail the suite instead of enriching it; default to "unknown" and require the contributor or maintainer to assert otherwise.
- No hostname, username, IP address or machine-collected OS detail in `meta.json`. The platform field is free text supplied by the donor.
- `entry_point.bin` is optional: many capture paths yield only the structure table, and refusing those donations would reject most of the corpus.

## Success criteria
- [ ] The schema validates the seed fixtures Story 03.0 produces, and rejects a `meta.json` missing `scrubber_version` or the well-formedness flag.
- [ ] `tests/data/corpus/README.md` states the ships-in-no-package rule.
- [ ] The removal procedure is written down and names who to contact.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
