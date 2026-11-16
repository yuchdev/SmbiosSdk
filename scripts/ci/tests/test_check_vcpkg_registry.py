"""Tests for scripts/ci/check_vcpkg_registry.py (milestone 0012, Story 02.0 Task 02).

Classification: unit tests on temporary trees with the git lookup stubbed, plus
one test that runs the real ``git`` against a throwaway repository.
On failure, first check: the port edit was committed and ``vcpkg x-add-version``
was re-run (docs/dev/vcpkg-registry.md).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional

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
              ref: str = REF, baseline_pv: int = 0, row_pv: int = 0) -> None:
    port = {"name": "smbios-c", "version": version}
    if port_version:
        port["port-version"] = port_version
    write(root, "ports/smbios-c/vcpkg.json", json.dumps(port))
    write(root, "ports/smbios-c/portfile.cmake",
          f"vcpkg_from_git(\n    OUT_SOURCE_PATH SOURCE_PATH\n    URL https://x/y.git\n"
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
        return reg.check(self.root, tree_of())

    def test_ConsistentRegistryHasNoProblems(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_StaleGitTreeIsReported(self) -> None:
        make_tree(self.root, tree="c" * 40)
        (msg,) = reg.check(self.root, tree_of())
        self.assertIn("git-tree is stale", msg)

    def test_UnreadableGitTreeAsksToCommitThePort(self) -> None:
        make_tree(self.root)
        (msg,) = reg.check(self.root, tree_of(None))
        self.assertIn("commit the port first", msg)

    def test_BaselineVersionMismatch(self) -> None:
        (msg,) = self.problems(baseline="0.9.0")
        self.assertIn("baseline differs", msg)

    def test_NewestRowVersionMismatch(self) -> None:
        (msg,) = self.problems(row_version="0.9.0")
        self.assertIn("newest version differs", msg)

    def test_PortVersionMustMatchEverywhere(self) -> None:
        msgs = self.problems(port_version=1)
        self.assertEqual(len(msgs), 2)
        self.assertTrue(any("baseline.json: port-version" in m for m in msgs))
        self.assertTrue(any("newest port-version" in m for m in msgs))

    def test_PlaceholderOrShortRefIsRejected(self) -> None:
        for ref in ("0" * 40, "abc123", "main"):
            with self.subTest(ref=ref):
                (msg,) = self.problems(ref=ref)
                self.assertIn("REF is not a real 40-hex commit SHA", msg)

    def test_MissingRegistryFilesAreReportedNotRaised(self) -> None:
        write(self.root, "ports/smbios-c/vcpkg.json", '{"name": "smbios-c", "version": "1.0.0"}')
        write(self.root, "ports/smbios-c/portfile.cmake", f"REF {REF}\n")
        msgs = reg.check(self.root, tree_of())
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all("missing or unreadable" in m for m in msgs))

    def test_BadPortManifest(self) -> None:
        make_tree(self.root)
        write(self.root, "ports/smbios-c/vcpkg.json", '{"name": "other", "version": "x"}')
        msgs = reg.check(self.root, tree_of())
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
                             tree_of=tree_of())
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
                             results=root / "r.jsonl", tree_of=tree_of())
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


if __name__ == "__main__":
    unittest.main()
