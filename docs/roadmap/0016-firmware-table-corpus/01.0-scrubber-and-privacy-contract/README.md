# Story 01.0 - Scrubbing Tool and the Privacy Contract

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** nothing - this story touches no packaging, no CI and no other milestone, so it can start immediately

## Scope

Build `tools/smbios_scrub/`: a tool that takes a raw SMBIOS table dump and
removes every personally identifying field, leaving a **byte-valid** table
that exercises the same parser paths as the original. Write the privacy
contract (the milestone's contract C1) that states exactly which fields are
destroyed, and test it.

**This story is the milestone's safety gate.** Until it lands, no task in
any later story may ask a third party for a dump. Raw SMBIOS tables carry
machine serial numbers, UUIDs and asset tags; once one is committed it is
effectively permanent.

## Tasks

| #  | Document                                                      | Status         | Blocks |
|----|---------------------------------------------------------------|----------------|--------|
| 01 | [Identifying-field inventory and the scrub contract](01-identifying-field-inventory.md) | ⬜ Not started | 02     |
| 02 | [The `smbios_scrub` tool](02-scrub-tool.md)                   | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                          | ⬜ Not started | -      |

## Key constraints

- **Same-length replacement, or fail.** A replacement string must be the
  same length as what it replaces, so no length field needs rewriting.
  Where that is impossible, the tool **fails loudly** rather than silently
  reshaping the table - a reshaped table tests the scrubber's bug, not the
  vendor's firmware.
- **Preserve every structural byte**: lengths, handles, structure types,
  structure order, string-section framing and the double-NUL terminator.
- **Fail closed on the unknown.** A structure type whose layout the tool
  cannot reason about is reported and **excluded** from the corpus, never
  passed through on the assumption it is harmless. Silent pass-through is
  how an identifier escapes.
- **Idempotent.** Scrubbing a scrubbed table changes nothing.
- **Reads the project's own field model, not a second copy of it.** The
  entry classes and `get_fields()` already know each structure type's
  layout; a parallel hand-written table of offsets inside the tool would
  drift from the parser and scrub the wrong bytes.
- **No network, no telemetry, no implicit file writes.** The tool reads one
  file and writes one file, both named explicitly.

## Files or directories created or modified by this story

- `tools/smbios_scrub/` (new) - the tool, built as its own target.
- `docs/dev/corpus-privacy-contract.md` (new) - contract C1 in full: the
  exhaustive destroyed-field list, the preservation guarantees, and the
  fail-closed rule.
- `tests/unit/scrub_*_test.cpp` (new) - the tool's own test cases.
- `docs/test/test-subsystem.md` - the new test coverage.
