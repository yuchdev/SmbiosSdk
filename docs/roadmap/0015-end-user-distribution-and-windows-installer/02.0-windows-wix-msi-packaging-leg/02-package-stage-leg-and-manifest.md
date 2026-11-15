# 02 - The `windows-msi` package-stage leg and manifest entry

**Status:** ⬜ Not started
**Depends on:** Task 01; milestone 0012 Story 04.0 Task 01 (version source); milestone 0014 (`stage.py`, `legs.json`, the report contract, `check_versions.py`)

## Objective
Turn Task 01's WiX sources into a `package`-stage leg: read the tested install tree, build the MSI, emit its `.sha256`, and add it to the same `manifest.json` the tarballs use - so `verify` and every publish destination handle it with no special-casing.

## Files
- **Create** `scripts/ci/package_windows_msi.py`: stdlib only. Resolves the install tree and the version from the neutral `CI_*` environment, generates `license.rtf` from `LICENSE`, substitutes the version into the WiX preprocessor variables, invokes the pinned WiX toolchain through `ci_step` (argv, never a shell), and writes `libbios-<version>-windows-<arch>.msi` plus its `.sha256` into `$CI_ARTIFACT_DIR/dist/`.
- **Modify** `scripts/ci/legs.json`: a `windows-msi` packaging leg, naming the configuration it packages (Release, and GUI-enabled if the GUI feature ships - ADR 0006 open questions 4 and 5).
- **Modify** `scripts/ci/stage.py`: the `package` stage runs the new leg on Windows; the MSI and its digest become `manifest.json` entries alongside the tarballs (ADR 0005 D4).
- **Modify** `scripts/ci/check_versions.py`: the MSI `Version` as a new version site, so a drift from the root `project()` version fails the pipeline instead of shipping.
- **Modify** milestone 0014's pins file: the WiX toolset version, as a declared tool requirement printed in the RC4 header (and therefore a TeamCity agent requirement on migration).
- **Create** `docs/dev/windows-installer.md`: contents, local build instructions, and the C2 upgrade-identity rules.

## Constraints
- **Never rebuild** (ADR 0005 D4). The leg's input is the build stage's tested install tree. If a feature's payload is absent from that tree, **fail loudly and name the missing path** - never ship a feature with no files, and never fall back to the build tree.
- **The `SDK_Static` feature is omitted entirely** if milestone 0012 Story 01.0 has not landed, because shipping `libsmbios.lib` without `libsmbiosConfig.cmake` reintroduces ADR 0004's DD1 footgun. Omission must be explicit and reported, not silent.
- **Refuse to package a Debug configuration.** An MSI carrying Debug binaries depends on the debug CRT, which is not redistributable and is absent on a user's machine - a user-visible defect. The leg asserts the configuration it was handed (ADR 0006 open question 4; 0014 C06 tests `-C Debug` today, so this assertion is load-bearing rather than theoretical).
- **Assert no test binary and no `smbios_gui_autoexit`** appears in the MSI's file table before emitting it.
- **Digests are computed once, here**, and every downstream destination reads them from the manifest (ADR 0005 D4/D9).
- **A code-signing hook, disabled.** Leave one clearly-marked, currently-inactive point where signing would occur, with a comment pointing at ADR 0006 open question 2. Do not add a credential, an environment name, or a registry row for it yet.
- **No new workflow file and no new entry-workflow job** - this is a leg of the existing `package` stage (ADR 0006 D5).

## Success criteria
- [ ] `python3 scripts/ci/stage.py package --leg windows-msi` runs on a developer Windows machine and on CI identically, producing the MSI, its `.sha256`, and a `manifest.json` entry.
- [ ] The `manifest.json` digest matches an independently computed digest of the MSI.
- [ ] Removing `bin/smbios.exe` from the install tree fails the leg with a message naming that exact path, rather than producing an MSI with an empty `CLI` feature.
- [ ] Handing the leg a Debug install tree fails with a message naming the configuration.
- [ ] `check_versions.py` fails when the MSI version is made to differ from the root `project()` version.
- [ ] The WiX version appears in the RC4 header, and a mismatch with the pin fails the step.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
