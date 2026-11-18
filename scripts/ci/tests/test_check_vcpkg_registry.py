"""Tests for scripts/ci/check_vcpkg_registry.py (milestone 0012, Story 02.0 Task 02; Story 04.0 Task 03).

Classification: unit tests on temporary trees with the git lookup stubbed
(checks 1-4, ``history=False``); integration tests that build throwaway git
repositories and run the real ``git`` for the append-only history checks 5-7
(threat model 2026-11-18 RP-3, constraint 9): one fixture per failure kind
(an overwrite, a removed entry, a ``REF`` not on ``master``, a ``REF`` that
differs from its ``v<version>`` tag, a shallow checkout, no ``master``), each
failing with exactly one discrepancy line, and the real repository passing.
On failure, first check: the port edit was committed and the registry branch
of a release was merged (docs/dev/vcpkg-registry.md); for a history failure,
which commit recorded the other ``git-tree`` (``git log -p -- versions/``).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from ci_test_support import CI_DIR  # noqa: F401  (puts scripts/ci on sys.path)

import check_vcpkg_registry as reg  # noqa: E402
from reporters import get_reporter  # noqa: E402

TREE = "a" * 40
REF = "b" * 40


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_tree(root: Path, *, version: str = "1.0.0", port_version: int = 0,
              baseline: str = "1.0.0", row_version: str = "1.0.0", tree: str = TREE,
              ref: str = REF, baseline_pv: int = 0, row_pv: int = 0,
              url: str = "https://github.com/yuchdev/SmbiosSdk.git") -> None:
    port = {"name": "smbios-c", "version": version}
    if port_version:
        port["port-version"] = port_version
    write(root, "ports/smbios-c/vcpkg.json", json.dumps(port))
    write(root, "ports/smbios-c/portfile.cmake",
          f"vcpkg_from_git(\n    OUT_SOURCE_PATH SOURCE_PATH\n    URL {url}\n"
          f"    REF {ref}\n    HEAD_REF master\n)\n")
    write(root, "versions/baseline.json", json.dumps(
        {"default": {"smbios-c": {"baseline": baseline, "port-version": baseline_pv}}}))
    write(root, "versions/s-/smbios-c.json", json.dumps(
        {"versions": [{"version": row_version, "port-version": row_pv, "git-tree": tree}]}))


def tree_of(value: Optional[str] = TREE):
    return lambda _root, _path: value


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def problems(self, **kw):
        make_tree(self.root, **kw)
        return reg.check(self.root, tree_of(), history=False)

    def test_ConsistentRegistryHasNoProblems(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_StaleGitTreeIsReported(self) -> None:
        make_tree(self.root, tree="c" * 40)
        (msg,) = reg.check(self.root, tree_of(), history=False)
        self.assertIn("git-tree is stale", msg)

    def test_UnreadableGitTreeAsksToCommitThePort(self) -> None:
        make_tree(self.root)
        (msg,) = reg.check(self.root, tree_of(None), history=False)
        self.assertIn("commit the port first", msg)

    def test_BaselineNamingNoRegisteredEntryFails(self) -> None:
        """RP-3: a baseline must name an entry that exists (here 0.9.0 is not registered)."""
        (msg,) = self.problems(baseline="0.9.0")
        self.assertIn("baseline names no registered entry", msg)
        (msg,) = self.problems(baseline_pv=3)
        self.assertIn("baseline names no registered entry", msg)

    def test_PortOlderThanTheNewestEntryFails(self) -> None:
        (msg,) = self.problems(row_version="2.0.0", baseline="2.0.0")
        self.assertIn("newest version differs", msg)
        (msg,) = self.problems(row_pv=1, baseline_pv=1)
        self.assertIn("newest port-version differs", msg)

    def test_PendingRegistrationIsGreen(self) -> None:
        """Task 03: the release commit (port ahead, baseline still the newest entry) passes."""
        for kw in ({"version": "1.0.1"}, {"port_version": 1}, {"version": "2.0.0"}):
            with self.subTest(**kw):
                make_tree(self.root, **kw)
                report = reg.check_report(self.root, tree_of("f" * 40), history=False)
                self.assertEqual(report.problems, [])
                self.assertTrue(any("pending registration" in n for n in report.notes))

    def test_PendingWithABaselineAheadOfTheEntriesFails(self) -> None:
        """A bumped baseline before the entry exists is the old, red procedure."""
        (msg,) = self.problems(version="1.0.1", baseline="1.0.1")
        self.assertIn("baseline names no registered entry", msg)

    def test_EntriesMustBeNewestFirstAndUnique(self) -> None:
        make_tree(self.root)
        rows = [{"version": "1.0.0", "port-version": 0, "git-tree": TREE},
                {"version": "1.1.0", "port-version": 0, "git-tree": "c" * 40}]
        write(self.root, "versions/s-/smbios-c.json", json.dumps({"versions": rows}))
        msgs = reg.check(self.root, tree_of(), history=False)
        self.assertIn("versions/s-/smbios-c.json: entries are not unique and newest first", msgs)

    def test_PlaceholderOrShortRefIsRejected(self) -> None:
        for ref in ("0" * 40, "abc123", "main"):
            with self.subTest(ref=ref):
                (msg,) = self.problems(ref=ref)
                self.assertIn("REF is not a real 40-hex commit SHA", msg)

    def test_ForeignPortfileUrlIsRejected(self) -> None:
        for url in ("https://github.com/someone-else/SmbiosSdk.git",
                    "https://github.com/yuchdev/SmbiosSdk"):
            with self.subTest(url=url):
                (msg,) = self.problems(url=url)
                self.assertIn("URL is not https://github.com/yuchdev/SmbiosSdk.git", msg)

    def test_WrongJsonTypesAreReportedNotRaised(self) -> None:
        make_tree(self.root)
        for rel, text in (("versions/baseline.json", '{"default": []}'),
                          ("versions/baseline.json", "[]"),
                          ("ports/smbios-c/vcpkg.json", "null"),
                          ("versions/baseline.json", "null"),
                          ("versions/s-/smbios-c.json", "null"),
                          ("versions/s-/smbios-c.json", '{"versions": [1]}'),
                          ("versions/s-/smbios-c.json", '{"versions": "x"}')):
            with self.subTest(rel=rel, text=text):
                write(self.root, rel, text)
                self.assertTrue(reg.check(self.root, tree_of(), history=False))
            make_tree(self.root)

    def test_MissingRegistryFilesAreReportedNotRaised(self) -> None:
        write(self.root, "ports/smbios-c/vcpkg.json", '{"name": "smbios-c", "version": "1.0.0"}')
        write(self.root, "ports/smbios-c/portfile.cmake",
              f"URL https://github.com/yuchdev/SmbiosSdk.git\nREF {REF}\n")
        msgs = reg.check(self.root, tree_of(), history=False)
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all("missing or unreadable" in m for m in msgs))

    def test_BadPortManifest(self) -> None:
        make_tree(self.root)
        write(self.root, "ports/smbios-c/vcpkg.json", '{"name": "other", "version": "x"}')
        msgs = reg.check(self.root, tree_of(), history=False)
        self.assertTrue(any("name is not smbios-c" in m for m in msgs))
        self.assertTrue(any("version is not X.Y.Z" in m for m in msgs))


class RunTest(unittest.TestCase):
    def test_RunPrintsOneOkLineAndRecord(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_tree(root)
            results = root / "results.jsonl"
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = reg.run(root, reporter=get_reporter("plain"), results=results,
                             tree_of=tree_of(), history=False)
            self.assertEqual(rc, 0)
            self.assertTrue(out.getvalue().startswith("ok: vcpkg registry: smbios-c registry"))
            self.assertEqual(len(results.read_text().splitlines()), 1)

    def test_RunFailureIsOneErrorLine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_tree(root, baseline="9.9.9")
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = reg.run(root, reporter=get_reporter("plain"),
                             results=root / "r.jsonl", tree_of=tree_of(), history=False)
            self.assertEqual(rc, 1)
            self.assertTrue(out.getvalue().startswith("error: vcpkg registry failed"))


class GitTreeTest(unittest.TestCase):
    def test_RealGitTreeEqualsRevParse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run(["git", "init", "-q", tmp], check=True)
            write(root, "ports/smbios-c/vcpkg.json", "{}")
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
            expected = subprocess.run(git + ["rev-parse", "HEAD:ports/smbios-c"], check=True,
                                      stdout=subprocess.PIPE, text=True).stdout.strip()
            self.assertEqual(reg.git_tree(root, "ports/smbios-c"), expected)
            self.assertIsNone(reg.git_tree(root, "ports/missing"))


# ---------------------------------------------------------------------------
# Append-only history (checks 5-7): real git, throwaway repositories
# ---------------------------------------------------------------------------

class GitRegistry:
    """A throwaway registry repository built commit by commit, as the release procedure does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_base = ["git", "-C", str(root), "-c", "user.name=t",
                         "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        self.git("symbolic-ref", "HEAD", "refs/heads/master")
        self.rows: list = []
        self.baseline = ("1.0.0", 0)

    def git(self, *args: str) -> str:
        return subprocess.run(self.git_base + list(args), check=True, stdout=subprocess.PIPE,
                              text=True).stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def source_commit(self, text: str) -> str:
        write(self.root, "src.txt", text)
        return self.commit("source " + text)

    def write_versions(self) -> None:
        write(self.root, "versions/s-/smbios-c.json", json.dumps({"versions": self.rows}))
        write(self.root, "versions/baseline.json", json.dumps(
            {"default": {"smbios-c": {"baseline": self.baseline[0],
                                      "port-version": self.baseline[1]}}}))

    def release(self, version: str, ref: str, *, extra: str = "") -> str:
        """Port commit (REF = ``ref``), then the versions commit; return the git-tree."""
        port = {"name": "smbios-c", "version": version}
        write(self.root, "ports/smbios-c/vcpkg.json", json.dumps(port))
        write(self.root, "ports/smbios-c/portfile.cmake",
              f"vcpkg_from_git(\n    OUT_SOURCE_PATH SOURCE_PATH\n    URL {reg.PORT_URL}\n"
              f"    REF {ref}\n    HEAD_REF master\n)\n{extra}")
        if not self.rows:
            self.write_versions()
        self.commit(f"port {version}")
        tree = self.git("rev-parse", "HEAD:ports/smbios-c")
        self.rows.insert(0, {"git-tree": tree, "version": version, "port-version": 0})
        self.baseline = (version, 0)
        self.write_versions()
        self.commit(f"versions {version}")
        return tree

    def problems(self, **kw) -> list:
        return reg.check(self.root, **kw)


class HistoryTest(unittest.TestCase):
    """Type: integration (real git). Constraint 9: each broken fixture fails with ONE line."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = GitRegistry(Path(self._tmp.name))
        self.src = self.repo.source_commit("one")
        self.tree = self.repo.release("1.0.0", self.src)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def one(self, needle: str, **kw) -> str:
        msgs = self.repo.problems(**kw)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn(needle, msgs[0])
        self.assertRegex(msgs[0], r"expected .+ -> actual .+")
        return msgs[0]

    def test_ConsistentHistoryPasses(self) -> None:
        src2 = self.repo.source_commit("two")
        self.repo.release("1.1.0", src2)
        self.repo.git("tag", "v1.1.0", src2)
        report = reg.check_report(self.repo.root)
        self.assertEqual(report.problems, [])
        self.assertIn("history: 2 recorded, 2 REF on master, 1 tagged", report.notes)

    def test_OverwriteInHistoryFails(self) -> None:
        """The overwrite-switch shape: same (version, port-version), new git-tree, committed."""
        write(self.repo.root, "ports/smbios-c/portfile.cmake",
              (self.repo.root / "ports/smbios-c/portfile.cmake").read_text() + "# evil\n")
        self.repo.commit("port edit")
        new_tree = self.repo.git("rev-parse", "HEAD:ports/smbios-c")
        self.repo.rows[0]["git-tree"] = new_tree
        self.repo.write_versions()
        self.repo.commit("overwrite 1.0.0")
        msg = self.one("smbios-c 1.0.0#0 git-tree: expected")
        self.assertIn(f"expected {self.tree[:12]}", msg)
        self.assertIn(f"actual {new_tree[:12]} (rewritten", msg)
        self.assertLess(len(msg), 160)

    def test_UncommittedOverwriteIsCaughtBeforeCommit(self) -> None:
        self.repo.rows[0]["git-tree"] = "e" * 40
        self.repo.write_versions()
        msgs = self.repo.problems()
        self.assertTrue(any(f"git-tree: expected {self.tree[:12]}" in m and "actual eeeeeeeeeeee"
                            in m for m in msgs), msgs)

    def test_RemovedEntryFails(self) -> None:
        src2 = self.repo.source_commit("two")
        self.repo.release("1.1.0", src2)
        self.repo.rows.pop()  # drop 1.0.0, keep 1.1.0 newest and consistent
        self.repo.write_versions()
        self.repo.commit("remove 1.0.0")
        self.assertLess(len(self.one("smbios-c 1.0.0#0: expected the entry recorded")), 160)

    def test_RefNotOnMasterFails(self) -> None:
        self.repo.git("checkout", "-q", "-b", "side")
        side = self.repo.source_commit("fork-only")
        self.repo.git("checkout", "-q", "master")
        self.repo.git("checkout", "-q", "side", "--", "src.txt")
        self.repo.git("checkout", "-q", "master", "--", "src.txt")
        self.repo.release("1.1.0", side)
        msg = self.one("smbios-c 1.1.0#0 REF: expected an ancestor of master")
        self.assertIn(f"actual {side[:12]} not on master", msg)

    def test_RefDifferentFromItsTagFails(self) -> None:
        other = self.repo.source_commit("retagged")
        self.repo.git("tag", "-a", "-m", "release", "v1.0.0", other)
        msg = self.one("smbios-c 1.0.0#0 REF: expected")
        self.assertIn(f"expected {other[:12]} (tag v1.0.0) -> actual {self.src[:12]}", msg)

    def test_ShallowCheckoutFailsClosed(self) -> None:
        clone = Path(self._tmp.name) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        self.repo.root.as_uri(), str(clone)], check=True)
        msgs = reg.check(clone)
        self.assertEqual(msgs, ["git history is shallow: the append-only check needs the full "
                                "history (fetch-depth: 0)"])

    def test_NoMasterFailsClosed(self) -> None:
        self.repo.git("branch", "-m", "master", "trunk")
        msgs = self.repo.problems()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("master cannot be resolved", msgs[0])
        self.assertEqual(self.repo.problems(master_ref="refs/heads/trunk"), [])

    def test_SupersededTreesAreExemptOnlyByExactValue(self) -> None:
        """The one exemption: a tree replaced before publication, listed by its exact id."""
        first = self.tree
        write(self.repo.root, "ports/smbios-c/portfile.cmake",
              (self.repo.root / "ports/smbios-c/portfile.cmake").read_text() + "# review\n")
        self.repo.commit("port review fix")
        self.repo.rows[0]["git-tree"] = self.repo.git("rev-parse", "HEAD:ports/smbios-c")
        self.repo.write_versions()
        self.repo.commit("regenerate before publication")
        self.assertEqual(len(self.repo.problems()), 1)
        with mock.patch.dict(reg.SUPERSEDED, {("1.0.0", 0): frozenset({first})}, clear=True):
            self.assertEqual(self.repo.problems(), [])
        with mock.patch.dict(reg.SUPERSEDED, {("1.0.0", 1): frozenset({first})}, clear=True):
            self.assertEqual(len(self.repo.problems()), 1)


class RealRepositoryTest(unittest.TestCase):
    """Type: integration. The shipped registry passes; the exemption is load-bearing and exact."""

    def setUp(self) -> None:
        rc, out = reg._git(reg.REPO_ROOT, "rev-parse", "--is-shallow-repository")
        if rc != 0 or out.strip() != "false":
            self.skipTest("needs a full-history checkout of this repository")
        if reg.resolve_master(reg.REPO_ROOT, reg._git, None) is None:
            self.skipTest("no master ref in this checkout")

    def test_RealTreePasses(self) -> None:
        self.assertEqual(reg.check(reg.REPO_ROOT), [])

    def test_RealTreeFailsWithoutTheSupersededList(self) -> None:
        with mock.patch.dict(reg.SUPERSEDED, {}, clear=True):
            msgs = reg.check(reg.REPO_ROOT)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("smbios-c 1.0.0#0 git-tree: expected", msgs[0])
        self.assertIn("(rewritten", msgs[0])

    def test_SupersededListIsExactlyTheTwoPrePublicationTrees(self) -> None:
        self.assertEqual(reg.SUPERSEDED, {("1.0.0", 0): frozenset({
            "06cb80b5f6e24f2e6da8b9b529e21d83f106a726",
            "6a4f002069e70b4dc28846125d0c406f883d9c17"})})


class NoOverwriteSwitchTest(unittest.TestCase):
    """Constraint 9: the overwrite switch appears in no script and in no documented procedure."""

    SWITCH = "--" + "overwrite-version"

    def test_NoScriptOrProcedureNamesIt(self) -> None:
        root = reg.REPO_ROOT
        hits = []
        paths = [p for p in (root / "scripts").rglob("*") if p.is_file()
                 and "__pycache__" not in p.parts]
        paths += [root / "docs" / "dev" / "vcpkg-registry.md",
                  root / "ports" / "smbios-c" / "portfile.cmake"]
        paths += list((root / ".github" / "workflows").glob("*.yml"))
        for path in paths:
            try:
                if self.SWITCH in path.read_text(encoding="utf-8", errors="replace"):
                    hits.append(str(path.relative_to(root)))
            except OSError:
                continue
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
