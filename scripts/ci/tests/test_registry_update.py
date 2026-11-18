"""Tests for ``scripts/ci/registry_update.py`` (milestone 0012 Story 04.0 Task 03).

Classification: unit tests (pure functions) plus integration tests that compare
the Python git object ids with a real ``git`` on a throwaway repository.

Scenarios map to the threat model ``docs/security/2026-11-18-release-and-publish-automation.md``:

- constraint 5 (``REF`` = ``manifest.commit``): :class:`PortfileRefTests`;
- RP-2 (the privileged job accepts nothing but the one allowed change, so a
  compromised ``registry-prepare`` cannot smuggle a portfile edit or a rewritten
  entry): :class:`JudgeTests`, one test per tampering kind;
- constraint 9 (append-only): :class:`JudgeTests` (already registered, older,
  rewritten, removed, reordered);
- RP-10 (fixed commit-message template): :class:`NameTests`;
- the hand-over output: :class:`PreparedTests`.

On failure first check: :func:`registry_update.check_registry_change` and the
real ``ports/smbios-c`` / ``versions/`` layout it assumes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict

from ci_test_support import CI_DIR

import registry_update as ru  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]
COMMIT = "c0" * 20
VERSION = "1.0.1"


def real_base() -> Dict[str, bytes]:
    """The repository's real registry files, with the port bumped as prepare_release would."""
    files = {rel: (REPO_ROOT / rel).read_bytes()
             for rel in (*ru.CHANGED_FILES, ru.PORT_MANIFEST)}
    port = json.loads(files[ru.PORT_MANIFEST])
    port["version"] = VERSION
    files[ru.PORT_MANIFEST] = (json.dumps(port, indent=2) + "\n").encode()
    return files


def real_entries(base: Dict[str, bytes]):
    return [(ru.FILE_MODE, "portfile.cmake", ru.git_blob_sha(base[ru.PORTFILE])),
            (ru.FILE_MODE, "vcpkg.json", ru.git_blob_sha(base[ru.PORT_MANIFEST]))]


def good_change(base: Dict[str, bytes]):
    portfile = ru.set_portfile_ref(base[ru.PORTFILE], COMMIT)
    tree = ru.port_tree_sha(real_entries(base), portfile)
    new = dict(ru.simulate_add_version(base, version=VERSION, port_version=0, git_tree=tree))
    new[ru.PORTFILE] = portfile
    return new, tree


class GitObjectIdTests(unittest.TestCase):
    """Type: integration. The Python ids equal git's, so registry-pr needs no git."""

    def test_BlobAndTreeIdsEqualGit(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            port = root / "ports" / "smbios-c"
            port.mkdir(parents=True)
            files = {"portfile.cmake": b"REF " + COMMIT.encode() + b"\n",
                     "vcpkg.json": b'{"name": "smbios-c"}\n', "usage": b""}
            for name, data in files.items():
                (port / name).write_bytes(data)
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                   "-c", "commit.gpgsign=false"]
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
            want = subprocess.run(git + ["rev-parse", "HEAD:ports/smbios-c"], check=True,
                                  stdout=subprocess.PIPE, text=True).stdout.strip()
            entries = [(ru.FILE_MODE, n, ru.git_blob_sha(d)) for n, d in files.items()]
            for name, data in files.items():
                blob = subprocess.run(git + ["hash-object", str(port / name)], check=True,
                                      stdout=subprocess.PIPE, text=True).stdout.strip()
                self.assertEqual(ru.git_blob_sha(data), blob, name)
        self.assertEqual(ru.git_tree_sha(reversed(entries)), want)

    def test_RealPortTreeEqualsTheRegisteredGitTree(self) -> None:
        """The committed 1.0.0 port reproduces its registered git-tree in pure Python."""
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-tree", "HEAD",
                              "ports/smbios-c/"], check=True, stdout=subprocess.PIPE,
                             text=True).stdout
        entries = []
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            mode, _kind, sha = meta.split()
            entries.append((mode, path.rsplit("/", 1)[1], sha))
        row = json.loads((REPO_ROOT / ru.VERSIONS).read_text())["versions"][0]
        portfile = (REPO_ROOT / ru.PORTFILE).read_bytes()
        self.assertEqual(ru.port_tree_sha(entries, portfile), row["git-tree"])

    def test_MalformedTreeEntriesAreRefused(self) -> None:
        for entries in ([("100644", "a", "x" * 40)], [("100600", "a", "a" * 40)],
                        [("100644", "../a", "a" * 40)],
                        [("100644", "a", "a" * 40), ("100644", "a", "b" * 40)]):
            with self.subTest(entries=entries), self.assertRaises(ru.RegistryError):
                ru.git_tree_sha(entries)
        with self.assertRaises(ru.RegistryError):
            ru.port_tree_sha([("100755", "portfile.cmake", "a" * 40),
                              ("100644", "vcpkg.json", "b" * 40)], b"")


class PortfileRefTests(unittest.TestCase):
    """Type: unit + invalid input. Constraint 5: REF is set to manifest.commit, nothing else changes."""

    def test_RealPortfileOnlyTheRefLineChanges(self) -> None:
        text = (REPO_ROOT / ru.PORTFILE).read_bytes()
        new = ru.set_portfile_ref(text, COMMIT)
        old_lines, new_lines = text.splitlines(True), new.splitlines(True)
        diff = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0][1].strip(), b"REF " + COMMIT.encode())
        self.assertEqual(ru.portfile_ref(new), COMMIT)

    def test_RefusedPortfilesAndCommits(self) -> None:
        good = b"vcpkg_from_git(\n    REF " + b"a" * 40 + b"\n)\n"
        for text in (b"no ref here\n", good + b"    REF " + b"b" * 40 + b"\n",
                     b"    REF main\n"):
            with self.subTest(text=text), self.assertRaises(ru.RegistryError):
                ru.set_portfile_ref(text, COMMIT)
        for commit in ("", "v1.0.1", "A" * 40, "a" * 39, "refs/tags/v1.0.1"):
            with self.subTest(commit=commit), self.assertRaises(ru.RegistryError):
                ru.set_portfile_ref(good, commit)


class JudgeTests(unittest.TestCase):
    """Type: unit + invalid input. RP-2, constraint 9: exactly one change is accepted."""

    def setUp(self) -> None:
        self.base = real_base()
        self.new, self.tree = good_change(self.base)

    def judge(self, **change) -> None:
        args = dict(base=self.base, new=self.new, version=VERSION, commit=COMMIT,
                    git_tree=self.tree)
        args.update(change)
        ru.check_registry_change(**args)

    def refused(self, needle: str, **change) -> None:
        with self.assertRaises(ru.RegistryError) as cm:
            self.judge(**change)
        self.assertIn(needle, str(cm.exception))
        self.assertRegex(str(cm.exception), r": expected .+ -> actual .+")

    def test_TheOneAllowedChangePasses(self) -> None:
        self.assertEqual(ru.check_registry_change(base=self.base, new=self.new, version=VERSION,
                                                  commit=COMMIT, git_tree=self.tree), (0, 2))

    def test_ExtraOrMissingFile(self) -> None:
        self.refused("registry files", new=dict(self.new, **{"ports/smbios-c/usage": b"x"}))
        self.refused("registry files", new={k: v for k, v in self.new.items()
                                            if k != ru.BASELINE})

    def test_PortfileWithAnyOtherChange(self) -> None:
        tampered = self.new[ru.PORTFILE] + b'execute_process(COMMAND curl evil)\n'
        self.refused("expected the base portfile with only REF set",
                     new=dict(self.new, **{ru.PORTFILE: tampered}))
        other_ref = ru.set_portfile_ref(self.base[ru.PORTFILE], "d" * 40)
        self.refused("expected the base portfile with only REF set",
                     new=dict(self.new, **{ru.PORTFILE: other_ref}))

    def test_BasePortNotBumped(self) -> None:
        self.refused("expected version 1.0.1 (manifest.version) -> actual 1.0.0",
                     base=dict(self.base, **{ru.PORT_MANIFEST:
                                             (REPO_ROOT / ru.PORT_MANIFEST).read_bytes()}))

    def test_AlreadyRegisteredOrOlderVersion(self) -> None:
        """The 1.0.0 collision (RP-3): re-registering 1.0.0 is refused, never overwritten."""
        base = dict(self.base, **{ru.PORT_MANIFEST: (REPO_ROOT / ru.PORT_MANIFEST).read_bytes()})
        self.refused("already registered (append-only, constraint 9)", base=base,
                     version="1.0.0")
        older = json.loads(base[ru.PORT_MANIFEST])
        older["version"] = "0.9.0"
        self.refused("expected newer than every registered version", version="0.9.0",
                     base=dict(base, **{ru.PORT_MANIFEST: json.dumps(older).encode()}))

    def test_RegisteredEntryRewrittenRemovedOrReordered(self) -> None:
        rows = json.loads(self.new[ru.VERSIONS])["versions"]
        for label, mutated in (
                ("rewritten", [rows[0], dict(rows[1], **{"git-tree": "e" * 40})]),
                ("removed", rows[:1]),
                ("duplicated", rows + [rows[1]])):
            with self.subTest(label):
                self.refused("expected every registered entry unchanged",
                             new=dict(self.new, **{ru.VERSIONS: json.dumps(
                                 {"versions": mutated}).encode()}))
        self.refused("expected the new entry", new=dict(self.new, **{ru.VERSIONS: json.dumps(
            {"versions": [rows[1], rows[0]]}).encode()}))

    def test_WrongNewEntry(self) -> None:
        rows = json.loads(self.new[ru.VERSIONS])["versions"]
        for bad in ({"git-tree": "e" * 40}, {"version": "1.0.2"}, {"port-version": 1}):
            with self.subTest(bad=bad):
                self.refused("expected the new entry", new=dict(self.new, **{
                    ru.VERSIONS: json.dumps({"versions": [dict(rows[0], **bad)] + rows[1:]})
                    .encode()}))
        self.refused("expected the new entry", git_tree="e" * 40)

    def test_BaselineChangedOtherwise(self) -> None:
        for baseline in ({"default": {"smbios-c": {"baseline": "1.0.0", "port-version": 0}}},
                         {"default": {"smbios-c": {"baseline": VERSION, "port-version": 0},
                                      "other": {"baseline": "1.0.0"}}}):
            with self.subTest(baseline=baseline):
                self.refused("expected only smbios-c moved to 1.0.1",
                             new=dict(self.new, **{ru.BASELINE: json.dumps(baseline).encode()}))

    def test_MalformedJsonIsRefusedNotRaised(self) -> None:
        for raw in (b"", b"[]", b"{", b"\xff", b'{"versions": [1]}', b"x" * (ru.MAX_FILE_BYTES + 1)):
            with self.subTest(raw=raw[:10]), self.assertRaises(ru.RegistryError):
                self.judge(new=dict(self.new, **{ru.VERSIONS: raw}))


class NameTests(unittest.TestCase):
    """Type: unit + invalid input. RP-9 branch name, RP-10 fixed commit message."""

    def test_TemplatesFromValidatedPartsOnly(self) -> None:
        self.assertEqual(ru.branch_name("1.0.1"), "registry/v1.0.1")
        self.assertEqual(ru.commit_message("1.0.1", COMMIT, "4242"),
                         f"registry: smbios-c 1.0.1 (REF {COMMIT[:12]}, run 4242)")
        for version in ("1.0.1-rc.1", "v1.0.1", "1.0", "1.0.1\n", "01.0.1", "[x](y)"):
            with self.subTest(version=version), self.assertRaises(ru.RegistryError):
                ru.branch_name(version)
        for commit, run_id in (("x" * 40, "1"), (COMMIT, "1`"), (COMMIT, "")):
            with self.assertRaises(ru.RegistryError):
                ru.commit_message("1.0.1", commit, run_id)


class PreparedTests(unittest.TestCase):
    """Type: unit + invalid input. The registry-prepare job output (CI_REGISTRY_PREPARED)."""

    def test_RoundTripAndRejections(self) -> None:
        files = {rel: rel.encode() for rel in ru.CHANGED_FILES}
        good = {"base": "a" * 40, "git_tree": "b" * 40, "files": ru.file_digests(files)}
        self.assertEqual(ru.parse_prepared(json.dumps(good)), good)
        self.assertIsNone(ru.parse_prepared(""))
        for bad in (dict(good, base="HEAD"), dict(good, extra=1),
                    dict(good, files={ru.PORTFILE: "a" * 64}), dict(good, git_tree=None)):
            with self.subTest(bad=bad), self.assertRaises(ru.RegistryError):
                ru.parse_prepared(json.dumps(bad))
        for text in ("{", json.dumps(good) + "\n", "x" * 5000):
            with self.assertRaises(ru.RegistryError):
                ru.parse_prepared(text)


if __name__ == "__main__":
    unittest.main()
