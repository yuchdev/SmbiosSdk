# 02 - The `smbios_scrub` tool

**Status:** ⬜ Not started
**Depends on:** Task 01 (the contract it implements)

## Objective
Implement `smbios_scrub`: raw table dump in, scrubbed byte-valid table dump out, implementing contract C1 exactly.

## Files
- **Create** `tools/smbios_scrub/CMakeLists.txt`, `tools/smbios_scrub/src/main.cpp` and its supporting sources: a CLI taking an input dump and an output path, plus `--report` (what it would destroy, no write) and `--verify` (assert a file is already scrubbed).
- **Modify** root `CMakeLists.txt`: add the tool behind `SMBIOS_BUILD_TOOLS` (default ON, cheap - it is a small CLI linking `libsmbios`), following the whole-archive link pattern every other `libsmbios` consumer uses.
- **Modify** `docs/dev/corpus-privacy-contract.md`: a short "how to run it" section.

## Constraints
- **Drive the walk from the project's own field model.** Use the entry classes and `get_fields()` to locate string and identifier fields; do not hand-write a second table of per-type offsets inside the tool - it would drift from the parser and scrub the wrong bytes.
- **Same-length replacement**, with an obviously-synthetic value (e.g. a fixed filler) so a reader can tell at a glance a fixture is scrubbed. If a same-length replacement is impossible, exit non-zero naming the type, the field and the offset.
- **Fail closed**: a structure type not covered by the contract causes a non-zero exit naming it. Never emit a partially-scrubbed file; write the output only after the whole table is processed successfully.
- **Idempotent**: re-scrubbing produces a byte-identical file.
- The UUID in Type 1 must be replaced with a fixed, obviously-synthetic UUID - not zeroed, because an all-zero UUID is itself a meaningful SMBIOS value and would change what the fixture tests.
- No network, no telemetry, no writes other than the named output path.

## Success criteria
- [ ] Scrubbing a real dump from the maintainer's own machine produces a byte-valid table with identical lengths, handles, type order and string framing.
- [ ] `--report` lists every field it would destroy and writes nothing.
- [ ] `--verify` exits zero on a scrubbed file and non-zero on an unscrubbed one.
- [ ] A dump containing an unknown structure type exits non-zero naming the type, and writes no output file.
- [ ] A forced same-length-impossible case exits non-zero naming type, field and offset.
- [ ] Scrubbing twice is byte-identical.
