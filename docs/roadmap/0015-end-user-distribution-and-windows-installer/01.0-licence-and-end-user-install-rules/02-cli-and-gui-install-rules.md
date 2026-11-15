# 02 - End-user install rules for the CLI and GUI

**Status:** ⬜ Not started
**Depends on:** Task 01 (the `GNUInstallDirs` include and the docdir setting land there)

## Objective
Give `smbios` (CLI) and `smbios_gui` their first `install()` rules, establishing this milestone's contract C1 install layout. Without this, ADR 0005 D4's "the `package` stage archives the tested `cmake --install` tree" cannot see either executable, so **no channel can ship them** - the prerequisite ADR 0006 D7.3 records.

## Files
- **Modify** `smbios/src/CMakeLists.txt`: `install(TARGETS smbios RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})`. The CLI is built unconditionally, so this rule is unconditional.
- **Modify** `smbios_gui/CMakeLists.txt`: the same for `smbios_gui`, **inside the existing `SMBIOS_BUILD_GUI` guard** so a default (GUI-off) build still has a valid, complete install step. Do not install `libsmbios_gui_model` (a build-time static library with no consumer outside this repository) and do not install `smbios_gui_autoexit` (the local-tier test twin compiled with `SMBIOS_GUI_TEST_HOOKS`, which must never ship).
- **Modify** `docs/cli-usage.md`: a short "Installing" section naming the C1 layout and `cmake --install`, so the CLI's own documentation stops implying the build tree is the only place the binary exists.
- **Create** `docs/dev/install-layout.md`: contract C1's table as the one place every later packaging story reads the layout from, with the privilege note (Linux needs `sudo`; Windows and macOS do not) and an explicit statement that nothing installed is setuid or setcap.

## Constraints
- **No setuid, setcap, or privileged helper** (ADR 0006 D6/DD3). Install plain-mode executables. The Linux answer is `sudo smbios`, documented, not engineered around.
- **`GNUInstallDirs` variables only** - no literal `bin`/`lib`/`include` strings - so one rule set produces a correct tree under a vcpkg prefix, a Homebrew cellar, an AUR `pkgdir` and the MSI staging prefix.
- **Do not touch `CMAKE_RUNTIME_OUTPUT_DIRECTORY`.** The existing build-tree `smbios/bin/` and `smbios_gui/bin/` outputs are what the test suites, `scripts/check_doc_samples.py` and the manual-test docs already use; installing is additive, and silently relocating build outputs would break all three.
- **No `include(CPack)`** (ADR 0006 D3; milestone Key constraints).
- **Do not install any test target** - not `smbios_unit_test`, the `gui-ci`/`gui-local` executables, the fuzzer, or `smbios_gui_autoexit`. A test binary in a shipped package is a defect, and `smbios_gui_autoexit` specifically carries test hooks the shipped GUI must not contain.
- Install `smbios_c`'s runtime artifact through its **existing** milestone 0007 rules; do not add a second rule for it here.

## Success criteria
- [ ] `cmake --install build/<preset> --prefix <tmp>` on a default (GUI-off) build produces `bin/smbios[.exe]`, `share/doc/libbios/LICENSE`, and `smbios_c`'s existing payload - and succeeds with no error about a missing GUI target.
- [ ] The same on a `-DSMBIOS_BUILD_GUI=ON` build additionally produces `bin/smbios_gui[.exe]`.
- [ ] The installed `bin/smbios` runs from the install tree on all three platforms and prints a parsed table (as root on Linux), with no reference to the build tree.
- [ ] No installed file carries a setuid or setcap bit, and no test target appears anywhere in the install tree.
- [ ] `docs/dev/install-layout.md` matches the tree an actual `cmake --install` produces, field for field.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
