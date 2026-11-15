# 01 - WiX sources, feature tree and upgrade identity

**Status:** ⬜ Not started
**Depends on:** Story 01.0 (install rules + licence); milestone 0012 Story 01.0 (for the `SDK_Static` payload)

## Objective
Author the WiX sources implementing contract C2's feature tree and the identity values that make upgrades behave, with no version literal and no auto-generated GUIDs.

## Files
- **Create** `packaging/windows/libbios.wxs`: the `Package`/`Product` with the permanent `UpgradeCode`, a `MajorUpgrade` element (so an upgrade replaces rather than coexists), the four `Feature` elements of C2 with `CLI`/`GUI` at `Level="1"` and `SDK_Shared`/`SDK_Static` at a level the UI exposes as opt-in, the `Program Files` directory layout, a per-machine `PATH` entry for the CLI directory, and a Start Menu shortcut for the GUI.
- **Create** `packaging/windows/components.wxs`: one component per installed file, each with an explicitly-authored, stable `Guid`. Payload paths mirror contract C1's install tree exactly.
- **Create** `packaging/windows/identity.md`: records the `UpgradeCode` GUID, states it must never change for the life of the product, and gives the component-GUID rule (authored once, never regenerated) with the failure each prevents.
- **Create** `packaging/windows/license.rtf`: the MIT text in RTF for WiX's `WixUI_FeatureTree` licence page, **generated** from the root `LICENSE` by the build driver, so the two cannot drift.

## Constraints
- **`Version` is a placeholder** (e.g. a WiX preprocessor variable), substituted at package time from ADR 0005 D5's single source. No version literal in any `.wxs`.
- **Per-machine (`InstallScope="perMachine"`)**, and the installed applications request no elevation of their own - Windows' `GetSystemFirmwareTable()` needs none. Install no service, driver, scheduled task, or privileged helper (ADR 0006 D6).
- **`WixUI_FeatureTree`**, not a custom UI: the whole point of D6/DD6 is that the user selects features, and the stock feature-tree dialog is exactly that, already localized and tested.
- The `PATH` entry applies to the `CLI` feature only (`Component` keyed on it), so deselecting the CLI leaves no stale `PATH` entry, and uninstall removes it.
- The GUI gets a shortcut but **no `PATH` entry** - it is an application, not a command.
- Reference files by their contract C1 install-tree paths. Do not reach into the build tree, and do not enumerate files with a directory harvest (`heat`): an implicit harvest silently ships whatever happens to be in the tree, including a stray test binary.

## Success criteria
- [ ] `wix build` (or the pinned WiX toolchain's equivalent) produces an MSI from a staged install tree, with no version literal present in any source file.
- [ ] Each of the four features installs exactly contract C2's payload and nothing else, verified by reading the built MSI's file table rather than by inspecting the source.
- [ ] `identity.md` records the `UpgradeCode` and the never-change rule, and the GUID in it matches the one in `libbios.wxs`.
- [ ] `license.rtf` is generated from `LICENSE`, and regenerating it after editing `LICENSE` changes it.
- [ ] No component references a path outside contract C1, and no `heat`-style harvest is used.
