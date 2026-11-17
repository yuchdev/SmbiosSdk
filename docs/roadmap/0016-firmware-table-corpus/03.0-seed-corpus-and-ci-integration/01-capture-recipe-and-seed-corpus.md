# 01 - Capture recipe and the seed corpus

**Status:** ⬜ Not started
**Depends on:** Stories 01.0, 02.0

## Objective
Write the per-OS capture recipe and use it to produce the first real fixtures, from the maintainer's own machines.

## Files
- **Create** `docs/dev/corpus-capture-recipe.md`: per-OS instructions to capture the raw table and scrub it. Linux: read `/sys/firmware/dmi/tables/DMI` and `smbios_entry_point` (root). macOS: the `AppleSMBIOS` IORegistry properties - no privileges needed. Windows: `GetSystemFirmwareTable()` - no privileges needed. Then `smbios_scrub` and `--verify`. Written for a stranger, because Story 04.0 hands it to one unchanged.
- **Create** `tests/data/corpus/<vendor>-<model>-<nn>/` for each available machine: `table.bin`, optional `entry_point.bin`, `meta.json`.
- **Modify** `docs/roadmap/0016-firmware-table-corpus/status.md`: record which machines contributed, and therefore what the corpus does and does not yet cover.

## Constraints
- **Scrub everything, including the maintainer's own machines**, and record `--verify` passing for each fixture.
- **Capture must not use anything under `c-versions/`.** Those GPL `dmidecode` sources are reference-only (milestone 0008's isolation guard); the recipe uses this project's own tools and the OS interfaces directly. A fixture produced by compiling GPL sources would entangle test data with a licence this project does not use.
- Prefer diversity over count: three machines from three vendors or three SMBIOS versions are worth more than six identical corporate laptops.
- Record honestly in `status.md` what is **not** covered - no ARM server, no pre-UEFI legacy BIOS machine, or whatever is missing. An inventory that overstates coverage is worse than a small one.
- Mark each fixture's well-formedness flag from observation, not assumption.

## Success criteria
- [ ] The recipe works on all three OSes and is written for someone with no knowledge of this repository.
- [ ] At least one fixture exists per available OS, each `--verify`-clean.
- [ ] Every `meta.json` validates against the schema.
- [ ] `status.md` records both what the seed corpus covers and what it does not.
- [ ] No step of the recipe references `c-versions/`.
