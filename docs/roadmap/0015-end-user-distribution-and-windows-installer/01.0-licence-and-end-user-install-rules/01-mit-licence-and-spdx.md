# 01 - MIT licence file and SPDX identification

**Status:** ⬜ Not started
**Depends on:** nothing

## Objective
Add the MIT `LICENSE` the repository has never had, so every channel in [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md) D2 has the metadata it requires. Resolves ADR 0006 open question 1 and milestone 0012's open question H3.

## Files
- **Create** `LICENSE`: the verbatim MIT text, copyright line `Copyright (c) 2026 Yurii Cherkasov`, at the repository root under exactly that filename (what GitHub, Chocolatey, WinGet, `brew audit` and `PKGBUILD` tooling all look for).
- **Modify** `README.md`: a `## License` section stating MIT and the SPDX identifier `MIT`, linking `LICENSE`.
- **Modify** root `CMakeLists.txt`: `include(GNUInstallDirs)` plus `install(FILES LICENSE DESTINATION ${CMAKE_INSTALL_DOCDIR})`, so the licence reaches the install tree (contract C1's last row) and therefore every artifact built from it.
- **Modify** `docs/dev/upstream_reference_review.md`: one sentence stating that this repository is MIT and that the GPL-2.0-or-later `c-versions/` upstreams remain reference-only, so a future reviewer reading either document finds the distinction stated in both.

## Constraints
- The licence covers **this repository's own code only**. Add no notice, attribution or licence text implying the `c-versions/` GPL upstreams are distributed; they are git-ignored, never compiled, never linked, never `#include`d, and milestone 0008's build-isolation guard must still pass unchanged.
- Do not add per-file SPDX headers to source files in this task. That is a separate, mechanical, whole-tree change and bundling it here would bury the licence decision in a thousand-line diff.
- `${CMAKE_INSTALL_DOCDIR}` resolves using `PROJECT_NAME`, which is `smbios_sdk`. Set `CMAKE_INSTALL_DOCDIR` explicitly to `${CMAKE_INSTALL_DATAROOTDIR}/doc/libbios` so the installed path matches contract C1 and the `libbios` name every package uses, rather than leaking the internal project name into every package's file list.

## Success criteria
- [ ] `LICENSE` exists at the repository root and contains unmodified MIT text with a correct copyright line.
- [ ] `README.md` names MIT and the SPDX identifier `MIT`.
- [ ] `cmake --install` places the licence at `share/doc/libbios/LICENSE` under the install prefix on all three platforms.
- [ ] Milestone 0008's build-isolation guard still passes: no `c-versions/` path is compiled, linked or included, and `/c-versions/` is still git-ignored.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
