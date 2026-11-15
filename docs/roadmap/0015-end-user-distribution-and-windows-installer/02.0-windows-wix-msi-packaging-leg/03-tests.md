# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Prove the MSI installs, upgrades, uninstalls and - for the SDK features - actually *works*, on a clean Windows runner. Packaging-correctness tests, not Boost.Test cases.

## Files
- **Create** `tests/packaging/windows_msi/install_uninstall.ps1`: silent install of selected feature sets, assert each feature's files land at their contract C2 paths, assert the Start Menu shortcut exists for `GUI` and not otherwise, assert `PATH` contains the CLI directory for `CLI` and not otherwise, then uninstall and assert nothing is left behind (no files, no `PATH` entry, no Start Menu entry, no registry residue).
- **Create** `tests/packaging/windows_msi/major_upgrade.ps1`: install a previous version, install the current one, assert exactly one product is registered (not two side by side), that files were replaced rather than accumulated, and that the user's feature selection carried forward.
- **Create** `tests/packaging/windows_msi/sdk_consumer/`: a clean-room external CMake project - **no reference to this repository's source or build tree** - that `find_package(libsmbios CONFIG REQUIRED)`s the installed `SDK_Static` tree, links `libsmbios::libsmbios`, and **parses a synthetic table**, asserting a non-null entry for a known structure type. A second consumer links the `SDK_Shared` import library and calls the `smbios_c` C ABI.
- **Modify** `scripts/ci/stage.py`'s `verify` stage: run the above against the exact MSI named in `manifest.json` (ADR 0005 D4: verify re-tests the artifact, it does not rebuild).
- **Modify** `docs/test/test-subsystem.md` and `docs/test/manual-testing.md`: the new automated coverage, and what remains manual (the installer's visual UI pages, the GUI's own pixels).

## Constraints
- **The `SDK_Static` consumer must assert a correct *parse*, not merely a successful link.** A link-only check passes even when every `REGISTER_SMBIOS_ENTRY` initializer was dropped - which is precisely the silent `SMBiosEntryFactory::create() == nullptr` regression the exported target's whole-archive `INTERFACE_LINK_OPTIONS` exists to prevent. The synthetic-table route (milestone 0006's `SyntheticTableBuilder` pattern, reimplemented in the consumer as plain bytes) keeps the test hermetic and unprivileged.
- **Clean runner, every time.** The install assertions are meaningless on a machine that already has the product; the harness must verify the product is absent before installing.
- **No network access** beyond fetching the MSI already present in the artifact directory.
- The harness tests the MSI from `manifest.json`, never one rebuilt locally.
- Assert the MSI's file table contains **no** test binary and no `smbios_gui_autoexit`.

## Success criteria
- [ ] Install, selective-feature install, upgrade and uninstall all pass on a clean Windows runner, and uninstall leaves no residue.
- [ ] The `SDK_Static` consumer builds and **correctly parses** a synthetic table from the installed tree alone; deliberately stripping the exported target's whole-archive link option makes it fail with a null entry, proving the test detects the regression.
- [ ] The `SDK_Shared` consumer calls the C ABI successfully against the installed import library and DLL.
- [ ] A deliberately duplicated `UpgradeCode` change is caught: the upgrade test fails when two products register side by side.
- [ ] No test binary or `smbios_gui_autoexit` is present in the MSI.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
