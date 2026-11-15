# 03 - Tests

**Status:** ⬜ Not started
**Depends on:** Tasks 01-02

## Objective
Prove the install tree is complete, runnable and safe to package, on all three platforms. These are **packaging-correctness tests, not unit tests** - none is a Boost.Test case under `tests/unit|mock|perf|integration/` (the four categories `docs/dev/cpp_coding_standard.md` defines). They follow the clean-room pattern milestone 0007 Story 03.0 Task 02 established.

## Files
- **Create** `scripts/ci/tests/test_install_layout.py`: asserts, against a real `cmake --install` prefix, that every contract C1 path required for the current configuration exists; that no path outside C1 appears; that no file is setuid or setcap; and that no test-target name (`*_test`, `smbios_gui_autoexit`, `read_smbios_table_fuzzer`) appears anywhere in the tree.
- **Create** `tests/packaging/install_tree_smoke/`: a clean-room consumer harness - a script that runs the installed `bin/smbios --format json` from the install prefix alone and asserts it emits parsable JSON, with **no reference to this repository's source or build tree** on the include path, the library path, or `PATH`.
- **Modify** `scripts/ci/stage.py`'s `test` stage (or the `package` stage's pre-archive check, whichever milestone 0014 settled): one step running both of the above, reported through the RC1-RC5 contract like any other step.
- **Modify** `docs/test/test-subsystem.md`: a section naming this new packaging-test category and stating explicitly that it is *not* a fifth Boost.Test category.

## Constraints
- Python stdlib only, matching `scripts/ci/`'s own rule, so the checks run identically on a developer machine and on any CI platform (ADR 0005 DD5).
- The smoke harness must tolerate the Linux privilege case: an unprivileged run falls through the entire source chain, so assert a **clean, non-crashing** exit and a well-formed diagnostic, not a populated table. Asserting a parsed table unprivileged would make the test fail on exactly the platform it most needs to pass on.
- The layout assertion must be **configuration-aware**: a GUI-off build legitimately has no `bin/smbios_gui`, and a build without milestone 0012 Story 01.0 legitimately has no `lib/cmake/libsmbios/`. Encode which paths are conditional rather than pinning one golden tree.
- No network access.

## Success criteria
- [ ] Both checks pass on Windows, Linux and macOS, in GUI-on and GUI-off configurations.
- [ ] Deliberately breaking each rule fails the matching check loudly: deleting `LICENSE` from the tree, adding a setuid bit, and dropping a test binary into `bin/` each produce a named failure, not a pass.
- [ ] The smoke harness fails if `PATH` or the library path is made to include the build tree, proving the clean-room property is actually enforced.
- [ ] Unprivileged Linux produces a clean non-crashing exit with a diagnostic, and the check treats that as a pass.
- [ ] `python3 scripts/check_doc_links.py docs/` reports zero problems.
