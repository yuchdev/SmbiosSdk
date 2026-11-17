# 02 - Artifact naming, checksums, and versioning

**Parent story:** [README.md](README.md)
**Status:** ✅ Complete (2026-11-17)
**Depends on:** Task 01

> **Amended 2026-11-12 by [ADR 0005](/docs/adr/0005-unified-build-package-publish-pipeline.md).**
> Three things in this task are superseded:
>
> - **The checksum step.** The `shasum` YAML step below is withdrawn.
>   Checksums come from `scripts/ci/package_archive.py` (Task 01; stdlib,
>   so it also works on Windows, review L1).
> - **The artifact name.** It is now `libbios-<version>-<leg>.<ext>`, where
>   `<version>` is the D5 version (the tag without `v`) and `<leg>` is
>   `<os>-<arch>`. It is no longer `libbios-<tag>-<preset>`.
> - **Version drift.** The statement that the tag "may drift" from
>   `SMBIOS_C_VERSION`/`LIBSMBIOS_VERSION` is **reversed**: one
>   authoritative CMake version, checked in CI (D5; Story 04.0 Task 01).
>
> What remains of this task is the documentation deliverable
> (`docs/dev/release-artifacts.md`: naming, verifying a download against
> `.sha256`/`manifest.json`, and the supported arch/glibc baseline from
> review M6). The illustrative YAML below is kept only as history.

## Objective

Define a minimal, reproducible artifact naming convention and add
checksums, without inventing a semantic-versioning policy this project
has not actually adopted (per the milestone's own Key constraints and
ADR 0004 DD2).

## Files

- **Modify** `.github/workflows/release.yml` (Task 01): add a checksum
  step per artifact:
  ```yaml
  - name: Checksum
    run: shasum -a 256 libbios-${{ github.ref_name }}-${{ matrix.preset }}.* > libbios-${{ github.ref_name }}-${{ matrix.preset }}.sha256
  ```
  (Windows runner: `sha256sum`/`Get-FileHash` equivalent - confirm the
  cross-platform form during implementation.)
- **Create** documentation (same location Story 03.0's README names,
  e.g. `docs/dev/release-artifacts.md`) recording:
  - The artifact filename convention: `libbios-<tag>-<preset>.{tar.gz,zip}`
    (`<tag>` is the pushed git tag verbatim, `<preset>` one of
    `windows`/`linux`/`macos` - **not** a separately-maintained product
    version number, since none exists yet for the combined
    `libsmbios`+`smbios_c` artifact).
  - That `<tag>` currently has **no enforced relationship** to
    `smbios_c/CMakeLists.txt`'s own `SMBIOS_C_VERSION` or Story 01.0's
    `LIBSMBIOS_VERSION` - they may drift; reconciling them into one
    policy is explicitly deferred (same deferral as milestone 0007's own
    Non-goals), not resolved by this task.
  - A short note on how to verify a downloaded artifact against its
    `.sha256` file.

## Functions and Classes

N/A - documentation and CI metadata only.

## Constraints

- **Does not introduce a semver policy.** The tag format
  (`on: push: tags: ["v*"]` from Task 01) is a release-trigger
  convention only, not a promise about compatibility between tags.
- **Checksums are published alongside, not instead of, GitHub's own
  built-in artifact integrity** (GitHub Releases already serves
  artifacts over HTTPS with its own hosting integrity) - this is purely
  for a consumer who wants to verify a downloaded file against a
  separately-obtained hash (e.g. one pasted into an announcement),
  matching common OSS release practice.

## Tests

- Covered by Task 03.

## Success criteria

- [x] Every artifact has a matching `.sha256` file. Verified on CI run
      `44074901424`: three archives, three `.sha256` files and one
      `manifest.json`. *Attaching* them to a GitHub Release is the
      `publish` stage (Story 04.0 Task 02), not this task.
- [x] The naming/versioning documentation is written down
      ([docs/dev/release-artifacts.md](/docs/dev/release-artifacts.md))
      and linked from a discoverable doc - the README's `## Documentation`
      list. It is **not** linked from `docs/cli-usage.md`, deliberately:
      these artifacts contain no CLI, so pointing CLI users at them would
      mislead.
- [x] `python3 scripts/check_doc_links.py docs/` reports zero problems.

## As built (2026-11-17)

Only the documentation deliverable remained; the amendment banner above
withdrew the `shasum` YAML step (checksums come from Task 01's
`package_archive.py`), fixed the name as `libbios-<version>-<leg>.<ext>`,
and reversed the "versions may drift" statement.

- **Written from verified artifacts, not from this spec.** Every figure
  in the document comes from CI run `44074901424` on commit `6cf73e4`
  and from the downloaded `dist` artifact - archive layouts per platform,
  the manifest's field set, and the platform floors.
- **M6 (state the arch/glibc baseline) is answered concretely:**
  `linux-x64` requires **glibc 2.38**, `windows-x64` is x86_64 with
  **MSVC toolset 14.44**, and `macos-arm64` is **arm64 only with a macOS
  15.0 floor**. The macOS floor is called out as a real limitation: it
  follows the pinned runner's SDK and is markedly newer than a local
  build produces (12.7 on an older Xcode), so it narrows the audience
  more than the artifact name suggests. ADR 0007 D2's `macos-x64`,
  `linux-arm64` and `windows-arm64` legs are named as the gap.
- **Both documented verification routes were executed against the real
  downloads** before being written down: `shasum -a 256 -c` on the
  detached file (`libbios-1.0.0-macos-arm64.tar.gz: OK`) and the
  `manifest.json` snippet (all three legs `OK`). The `.sha256` files use
  the standard `<hash>  <filename>` form, so `sha256sum -c` and
  `shasum -a 256 -c` both accept them unmodified.
- **The libraries-only rule is documented as a decision with its
  rationale**, including the nuance that `bin/` *does* exist on the
  Windows leg (it holds `smbios_c.dll`, per platform convention) while
  containing no executable - checked, no `.exe` in any leg.
- **Versioning is documented as a non-promise**: one authoritative value
  checked by `check_versions.py` (ADR 0005 D5), currently the interim
  `SMBIOS_C_VERSION` until Story 04.0 Task 01, with no semver guarantee
  (ADR 0004 DD2) beyond `libsmbiosConfigVersion.cmake`'s
  `SameMajorVersion` boundary.
