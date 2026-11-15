# 02 - AUR `PKGBUILD` and destination module

**Status:** ⬜ Not started
**Depends on:** Story 01.0 (install rules + licence); milestone 0012 Story 04.0 Tasks 02-05 (release tarball + plug-in framework)

## Objective
Publish a `PKGBUILD` to the AUR building the CLI, GUI and libraries from the release tarball. The only zero-gate, zero-signing *native package* channel on Linux (ADR 0006 D2).

## Files
- **Create** `packaging/aur/PKGBUILD.in`: `pkgname=libbios`, `pkgver`/`source`/`sha256sums` substituted from the single version source and `manifest.json`, `license=('MIT')`, `depends=()` and `makedepends=()` covering Boost, CMake and the GLFW/OpenGL/X11 packages the GUI needs, a `build()` invoking CMake directly, a `package()` using `DESTDIR` so contract C1's tree lands under `$pkgdir`, and a `pkgdesc` **stating that reading SMBIOS on Linux requires root**.
- **Create** `packaging/aur/.SRCINFO.in`: regenerated from the `PKGBUILD` rather than hand-maintained (the AUR requires it and a stale one is a common breakage).
- **Create** `scripts/ci/destinations/aur.py`: `NAME = "aur"`; `REQUIRED_ENV` = the AUR SSH key plus the neutral version/artifact names; `validate` asserts the source tarball entry exists, the template renders, the rendered `sha256sums` equals the manifest's digest, and `.SRCINFO` is consistent with the `PKGBUILD`; `publish` pushes to the AUR over SSH with a pinned known-hosts entry; `check_published` re-reads the published `PKGBUILD` and compares.
- **Modify** `scripts/ci/publish_destinations.json`: `{ "name": "aur", "enabled": true, "tier": "release", "permission_class": "repo-write", "environment": "release" }`.
- **Modify** `scripts/ci/check_versions.py`: `pkgver` as a version site.
- **Modify** `docs/cli-usage.md`: an "Installing on Arch Linux" section stating the root requirement.

## Constraints
- **`pkgdesc` and the project's AUR page must state the root requirement** (ADR 0006 D6). This is the platform where silence causes the most damage: an unprivileged run falls through `/sys/firmware/dmi/tables` (mode 0400), the `/dev/mem` EFI source and the legacy scan, and reports nothing useful.
- **Not setuid, not setcap, no privileged helper** (ADR 0006 D6/DD3). A `PKGBUILD` that sets a capability on `smbios` to "make it work without sudo" is a hard failure, and Task 03 asserts its absence in the built package.
- **`.SRCINFO` is generated, never hand-edited.** A `.SRCINFO` that disagrees with the `PKGBUILD` is the single most common AUR breakage, and `validate` must catch it before pushing.
- **`sha256sums` comes from `manifest.json`**, and `source` points at the release tarball - never a git ref, never `HEAD`.
- **`package()` must install through `DESTDIR`**, so contract C1's layout lands under `$pkgdir` unmodified; do not re-derive paths inside the `PKGBUILD`.
- **The SSH known-hosts entry is pinned**, so a push cannot be redirected by DNS or a changed host key. The key comes only through the `release` environment, passed by name, never logged.
- Build the GUI here (unlike the Homebrew formula): Linux has GLFW's X11/OpenGL dependencies as ordinary packages, so `SMBIOS_BUILD_GUI=ON` with those declared in `depends`/`makedepends`. Declare them explicitly - a missing GLFW dependency produces a configure failure on a user's machine, not on CI.

## Success criteria
- [ ] `stage.py publish --destination aur --dry-run` prints the rendered `PKGBUILD` and `.SRCINFO`, the AUR repository and the commit, with no network write and no secret read.
- [ ] `validate` fails on a digest mismatch, a missing tarball entry, and a `.SRCINFO` inconsistent with the `PKGBUILD`.
- [ ] `makepkg -si` in a clean Arch container builds and installs the CLI, GUI and libraries; `sudo smbios --format json` emits parsable output and an unprivileged run exits cleanly with a diagnostic.
- [ ] `namcap` reports no error on the built package.
- [ ] No file in the built package is setuid or setcap.
- [ ] `pkgdesc` states the root requirement.
- [ ] `check_published` fails when the published `PKGBUILD`'s digest disagrees with the manifest.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
