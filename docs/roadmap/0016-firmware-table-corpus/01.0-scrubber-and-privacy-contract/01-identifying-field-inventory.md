# 01 - Identifying-field inventory and the scrub contract

**Status:** ⬜ Not started
**Depends on:** nothing

## Objective
Produce the **exhaustive** list of SMBIOS fields that can identify a machine, a site or a person, and turn it into the written scrub contract (milestone contract C1). This is a research-and-decision task; the tool in Task 02 implements its output.

## Files
- **Create** `docs/dev/corpus-privacy-contract.md`: for every implemented structure type, each field classified **destroy** / **preserve**, with a one-line reason. Plus the preservation guarantees (lengths, handles, order, string framing), the same-length replacement rule, the idempotence rule and the fail-closed rule.
- **Modify** `docs/roadmap/0016-firmware-table-corpus/status.md`: record the maintainer's ruling on the borderline fields below.

## Constraints
- **Walk the implemented entry classes, not the spec index.** The list must cover every type this project actually parses (milestone 0010 implemented the non-obsolete set), because an unparsed type is excluded by the fail-closed rule anyway.
- At minimum, these are known identifying fields and must appear as **destroy**: Type 0 BIOS version/release strings where vendors embed service tags, Type 1 serial number + UUID + SKU, Type 2 serial + asset tag, Type 3 serial + asset tag, Type 4 serial + asset tag + part number, Type 17 serial + asset tag + part number.
- **Borderline fields need a maintainer ruling, not a silent choice.** Type 11 (OEM strings) and Type 12 (system configuration options) are free text and have been observed carrying site-specific data. Destroying them loses real parser coverage; preserving them risks leaking. Put a recommendation to the maintainer and record the answer.
- The contract must be written so a reviewer can check the tool against it field by field. A prose paragraph is not a contract; a table is.

## Success criteria
- [ ] Every structure type this project parses appears in the table with every string and identifier-bearing field classified.
- [ ] The borderline Type 11/12 question is presented with a recommendation and resolved in `status.md`.
- [ ] The contract states the same-length rule, the fail-closed rule and idempotence explicitly.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
