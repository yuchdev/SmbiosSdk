# Story 04.0 - Homebrew Tap and AUR Publish Destinations

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Story 01.0 (the CLI/GUI install rules both packages build and install, and the MIT licence both declare); milestone 0012 Story 04.0 Tasks 02-05 (the GitHub Release destination whose tarball digests both consume, and the plug-in framework); milestone 0014 (`stage.py`, `check_versions.py`, the policy gate). **Independent of Stories 02.0 and 03.0** - no file overlap, no MSI dependency

## Scope

The macOS and Linux end-user channels of [ADR 0006](/docs/adr/0006-end-user-distribution-and-windows-installer.md)
D2's Wave 1, as contract C3 destination plug-ins:

- **`homebrew-tap`** - a formula in this project's own
  `homebrew-libbios` tap, building from the release tarball.
- **`aur`** - a `PKGBUILD` published to the AUR, building from the same
  tarball.

These are the **cheapest and most robust** channels in the whole matrix
and the reason ADR 0006 DD5 orders channels the way it does: both build
from source on the user's machine, so **neither needs code signing**, and
both are updated by pushing to a repository this project controls, so
**neither has an external review queue** (ADR 0006 DD1). They are listed
after Stories 02.0/03.0 only because those cover the channels the
maintainer's request named - not because anything blocks them.

## Tasks

| #  | Document                                                        | Status         | Blocks |
|----|-----------------------------------------------------------------|----------------|--------|
| 01 | [Homebrew tap formula and destination module](01-homebrew-tap-destination.md) | ⬜ Not started | 03     |
| 02 | [AUR `PKGBUILD` and destination module](02-aur-destination.md)  | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                            | ⬜ Not started | -      |

## Key constraints

- **Both build from the release tarball, pinned by the manifest's
  digest.** Neither fetches a git ref, a branch, or `HEAD`: the formula's
  `sha256` and the `PKGBUILD`'s `sha256sums` come from `manifest.json`
  (ADR 0005 D4/D9), so a user's build and the released artifact are
  provably the same bytes.
- **Both must state the privilege requirement in their package
  description** (ADR 0006 D6). An unprivileged `smbios` run on Linux
  falls through the entire source chain and reports nothing useful - a
  user who typed `yay -S libbios` and then ran `smbios` will read that as
  "broken", not as "needs root". The AUR description and the formula
  caveat both say so explicitly.
- **Neither package is setuid or setcap, and neither installs a
  privileged helper** (ADR 0006 D6/DD3). The documented answer is
  `sudo smbios`.
- **The GUI's treatment differs per platform, deliberately.** The AUR
  package may build `smbios_gui` (Linux has the X11/OpenGL packages GLFW
  needs as ordinary dependencies). The **Homebrew formula ships the CLI
  and libraries only**: a GUI on macOS wants a `.app` bundle that does not
  exist yet, and a Homebrew *cask* of an unsigned binary still fails
  Gatekeeper - both Wave 2 per ADR 0006 D2. The formula must not install a
  bare `smbios_gui` executable into a Homebrew `bin`.
- **The formula declares its vcpkg/Boost dependencies as Homebrew
  dependencies, not as a vendored vcpkg bootstrap.** A formula that
  downloads and bootstraps vcpkg during `brew install` violates
  Homebrew's build sandbox expectations and makes the build
  non-reproducible; Boost is already a Homebrew formula.
- **Credentials only through the `release` environment**, passed by name,
  never logged (ADR 0005 D6). `homebrew-tap` pushes to a repository this
  project owns; `aur` pushes over SSH to the AUR, which needs an SSH key
  with a known-hosts pin.
- **`check_published` re-reads the published formula and `PKGBUILD`** and
  compares their digests with the manifest.
- **No new workflow file and no new entry-workflow job** (ADR 0006 D5),
  and no new permission class - both are `repo-write`-shaped.

## Files or directories created or modified by this story

- `packaging/homebrew/libbios.rb.in` (new) - the formula template.
- `packaging/aur/PKGBUILD.in` (new), `packaging/aur/.SRCINFO.in` (new).
- `scripts/ci/destinations/homebrew_tap.py` (new),
  `scripts/ci/destinations/aur.py` (new) - contract C3 modules.
- `scripts/ci/publish_destinations.json` - two new rows.
- `scripts/ci/check_versions.py` - two new version sites.
- `scripts/ci/tests/` - unit tests for both modules.
- `docs/cli-usage.md` - "Installing on macOS" (`brew tap` + `brew
  install`) and "Installing on Arch Linux" sections, each stating the
  privilege requirement.
- An external repository, `homebrew-libbios` (the tap), created outside
  this tree - see ADR 0006 open question 3 (hosting), which this story
  needs answered before Task 01 can push anything.
