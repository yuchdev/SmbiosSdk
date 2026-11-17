"""Tests for ``scripts/release/prepare_release.py`` (milestone 0012 Story 04.0 Task 01; ADR 0005 D5).

Classification: unit tests on temporary repository trees built from **this
repository's real** version-site files and ``version_sites.json``, so a
change to either is exercised here. The dirty-tree lookup is stubbed except
in :class:`GitTests`, which runs the real ``git`` against a throwaway
repository, and the command-line test, which runs the script as a child.
No network.

Scenarios: round-trip bump; idempotency (re-run, interrupted run); refused
versions, pre-releases and downgrades with nothing written; uncommitted
target files refused; success criterion 1 (``check_versions.py`` fails with a
discrepancy line when any one site is edited alone); the script never tags,
pushes, commits or runs ``x-add-version`` (threat model Task 01 req. 3).

On failure, first check: the four rows of ``scripts/ci/version_sites.json``
and their files (root ``CMakeLists.txt`` ``project()``, the two derived
``set(...)`` lines, ``ports/smbios-c/vcpkg.json``, ``versions/baseline.json``);
then whether ``check_versions.py``'s extractors changed without
``prepare_release.py``'s editors following.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from ci_test_support import CI_DIR, neutral_env

import check_versions as cv  # noqa: E402
from reporters import get_reporter  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release" / "prepare_release.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

import prepare_release as pr  # noqa: E402

#: Every file a site of the real version_sites.json names, plus the sites file.
SITE_FILES = ("CMakeLists.txt", "smbios_c/CMakeLists.txt", "libsmbios/CMakeLists.txt",
              "ports/smbios-c/vcpkg.json", "versions/baseline.json")


def _clean(_root: Path, _files: Sequence[str]) -> List[str]:
    return []


class _Tree(unittest.TestCase):
    """A temporary copy of the real version sites (and only them)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel in SITE_FILES:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / rel, self.root / rel)
        self.sites = self.root / "version_sites.json"
        shutil.copyfile(cv.SITES_FILE, self.sites)
        self.start = cv.check(self.root, self.sites).authority or ""

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def snapshot(self) -> Dict[str, bytes]:
        return {rel: (self.root / rel).read_bytes() for rel in SITE_FILES}

    def prepare(self, version: str, dirty=_clean) -> Tuple[int, List[str]]:
        out = io.StringIO()
        rc = pr.run(self.root, version, sites_file=self.sites, dirty=dirty, out=out)
        return rc, out.getvalue().splitlines()

    def json_at(self, rel: str) -> dict:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))


class BumpTests(_Tree):
    """Normal case: one argument moves the authoritative and every equal site."""

    def test_StartingTreeIsTheRealRepositoryState(self) -> None:
        outcome = cv.check(self.root, self.sites)
        self.assertEqual(outcome.problems, [])
        self.assertFalse(outcome.interim, "root project() VERSION must be authoritative")

    def test_RoundTripBump(self) -> None:
        before = self.snapshot()
        rc, lines = self.prepare("7.8.9")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], f"ok: prepare release: {self.start} -> 7.8.9, 3 files "
                                   "(CMakeLists.txt, ports/smbios-c/vcpkg.json, "
                                   "versions/baseline.json)")
        self.assertEqual(lines[1], "ok: versions: 7.8.9 (root project() VERSION), 4 sites")
        outcome = cv.check(self.root, self.sites)
        self.assertEqual((outcome.authority, outcome.problems), ("7.8.9", []))
        # Derived sites are untouched: they follow ${PROJECT_VERSION}.
        after = self.snapshot()
        for rel in ("smbios_c/CMakeLists.txt", "libsmbios/CMakeLists.txt"):
            self.assertEqual(after[rel], before[rel], rel)
        # Only the version value changed in each edited file.
        port_before = json.loads(before["ports/smbios-c/vcpkg.json"])
        port_before["version"] = "7.8.9"
        self.assertEqual(self.json_at("ports/smbios-c/vcpkg.json"), port_before)
        base = self.json_at("versions/baseline.json")
        self.assertEqual(base["default"]["smbios-c"], {"baseline": "7.8.9", "port-version": 0})
        root_before = before["CMakeLists.txt"].decode()
        self.assertEqual((self.root / "CMakeLists.txt").read_text(encoding="utf-8"),
                         root_before.replace(f"VERSION {self.start} LANGUAGES",
                                             "VERSION 7.8.9 LANGUAGES", 1))

    def test_CommentedProjectCallIsNotEdited(self) -> None:
        root = self.root / "CMakeLists.txt"
        root.write_text("# project(decoy VERSION 0.0.1)\n#[[ project(d VERSION 0.0.2) ]]\n"
                        + root.read_text(encoding="utf-8"), encoding="utf-8")
        rc, lines = self.prepare("9.0.0")
        self.assertEqual(rc, 0, lines)
        text = root.read_text(encoding="utf-8")
        self.assertIn("# project(decoy VERSION 0.0.1)", text)
        self.assertIn("project(d VERSION 0.0.2)", text)
        self.assertIn("VERSION 9.0.0 LANGUAGES", text)

    def test_CrlfLineEndingsArePreserved(self) -> None:
        root = self.root / "CMakeLists.txt"
        root.write_bytes(root.read_bytes().replace(b"\n", b"\r\n"))
        rc, lines = self.prepare("9.0.0")
        self.assertEqual(rc, 0, lines)
        data = root.read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"VERSION 9.0.0 LANGUAGES", data)


class IdempotencyTests(_Tree):
    """A repeated or resumed run converges and writes nothing more."""

    def test_SecondRunIsANoOp(self) -> None:
        self.assertEqual(self.prepare("2.0.0")[0], 0)
        after_first = self.snapshot()
        calls: List[Sequence[str]] = []

        def must_not_be_called(_root: Path, files: Sequence[str]) -> List[str]:
            calls.append(files)
            return list(files)  # would refuse if consulted

        rc, lines = self.prepare("2.0.0", dirty=must_not_be_called)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[0], "ok: prepare release: already 2.0.0, nothing written")
        self.assertEqual(self.snapshot(), after_first)
        self.assertEqual(calls, [], "a no-op run must not need a clean tree")

    def test_CurrentVersionOnACleanTreeIsANoOp(self) -> None:
        before = self.snapshot()
        rc, lines = self.prepare(self.start)
        self.assertEqual(rc, 0, lines)
        self.assertIn("already", lines[0])
        self.assertEqual(self.snapshot(), before)

    def test_InterruptedRunResumes(self) -> None:
        # As if a first run wrote only the root CMakeLists.txt: the check fails ...
        root = self.root / "CMakeLists.txt"
        root.write_text(root.read_text(encoding="utf-8").replace(
            f"VERSION {self.start} ", "VERSION 3.0.0 ", 1), encoding="utf-8")
        self.assertTrue(cv.check(self.root, self.sites).problems)
        # ... and a re-run writes only the remaining two files.
        rc, lines = self.prepare("3.0.0")
        self.assertEqual(rc, 0, lines)
        self.assertIn("-> 3.0.0, 2 files (ports/smbios-c/vcpkg.json, versions/baseline.json)",
                      lines[0])


class RefusalTests(_Tree):
    """Invalid input: exit 2 and not one byte written."""

    def assertRefused(self, version: str, needle: str) -> List[str]:
        before = self.snapshot()
        rc, lines = self.prepare(version)
        self.assertEqual(rc, 2, lines)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("error: prepare release: "), lines)
        self.assertIn(needle, lines[0])
        self.assertEqual(self.snapshot(), before)
        return lines

    def test_BadVersionIsRefused(self) -> None:
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "1.0.0-", "1.0.0-rc_1", "", " 1.0.0",
                    "1.0.0\n", "1234567.0.0", "1.0.0-" + "a" * 41, "1.0.0+build", "x.y.z"):
            with self.subTest(version=bad):
                self.assertRefused(bad, "version must be X.Y.Z")

    def test_HostileVersionIsNotEchoed(self) -> None:
        lines = self.assertRefused("::error::x##teamcity[y]", "got <invalid>")
        self.assertNotIn("::error::", lines[0])
        self.assertNotIn("##teamcity[", lines[0])

    def test_PrereleaseIsRefusedNotTruncated(self) -> None:
        # RP-5: 1.1.0-rc.1 must never become 1.1.0 in the port or the registry baseline.
        for pre in ("1.1.0-rc.1", "1.1.0-beta", "1.1.0-0.3.7"):
            with self.subTest(version=pre):
                lines = self.assertRefused(pre, f"pre-release {pre} refused")
                self.assertIn(f"then tag v{pre}", lines[0])
        self.assertEqual(self.json_at("ports/smbios-c/vcpkg.json")["version"], self.start)
        self.assertEqual(self.json_at("versions/baseline.json")["default"]["smbios-c"]
                         ["baseline"], self.start)

    def test_DowngradeIsRefused(self) -> None:
        self.prepare("2.1.0")
        for lower in ("2.0.9", "1.9.9", "0.0.0"):
            with self.subTest(version=lower):
                self.assertRefused(lower, "is lower than the current 2.1.0")

    def test_DirtyTargetFileIsRefused(self) -> None:
        before = self.snapshot()
        rc, lines = self.prepare("4.0.0", dirty=lambda _r, files: [files[-1]])
        self.assertEqual(rc, 2)
        self.assertIn("uncommitted changes in versions/baseline.json", lines[0])
        self.assertEqual(self.snapshot(), before)

    def test_UneditableSiteAbortsBeforeAnyWrite(self) -> None:
        # The root CMakeLists.txt is planned first; the bad JSON last. Nothing is written.
        base = self.root / "versions" / "baseline.json"
        base.write_text(json.dumps(self.json_at("versions/baseline.json")), encoding="utf-8")
        self.assertRefused("5.0.0", "canonical 2-space form")
        base.write_text('{"default": {"smbios-c": {"baseline": 1}}}', encoding="utf-8")
        self.assertRefused("5.0.0", "smbios-c registry baseline (versions/baseline.json)")

    def test_EqualSiteHoldingAVariableIsRefused(self) -> None:
        sites = json.loads(self.sites.read_text(encoding="utf-8"))
        sites["sites"].append({"site": "extra", "file": "smbios_c/CMakeLists.txt",
                               "extractor": "cmake_set:SMBIOS_C_VERSION", "rule": "equal"})
        self.sites.write_text(json.dumps(sites), encoding="utf-8")
        self.assertRefused("5.0.0", "holds a variable reference, not a literal")

    def test_MissingAuthoritativeValueIsRefused(self) -> None:
        root = self.root / "CMakeLists.txt"
        root.write_text(root.read_text(encoding="utf-8").replace(
            f"VERSION {self.start} LANGUAGES CXX", "CXX"), encoding="utf-8")
        self.assertRefused("5.0.0", "root project() VERSION: no X.Y.Z value")


class SingleSiteEditTests(_Tree):
    """Success criterion 1: editing any one site alone fails the check with a discrepancy line."""

    def run_check(self) -> Tuple[int, List[str]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cv.run(self.root, sites_file=self.sites, reporter=get_reporter("plain"),
                        results=self.root / "results.jsonl")
        return rc, out.getvalue().splitlines()

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, (rel, old))
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_EachSiteEditedAloneFails(self) -> None:
        s = self.start
        cases = (
            ("CMakeLists.txt", f"VERSION {s} LANGUAGES", "VERSION 9.9.9 LANGUAGES",
             f"smbios-c port version: expected 9.9.9 -> actual {s}"),
            ("smbios_c/CMakeLists.txt", "set(SMBIOS_C_VERSION ${PROJECT_VERSION})",
             f'set(SMBIOS_C_VERSION "{s}")',
             f"SMBIOS_C_VERSION: expected ${{PROJECT_VERSION}} -> actual {s}"),
            ("libsmbios/CMakeLists.txt", "set(LIBSMBIOS_VERSION ${PROJECT_VERSION})",
             'set(LIBSMBIOS_VERSION "9.9.9")',
             "LIBSMBIOS_VERSION: expected ${PROJECT_VERSION} -> actual 9.9.9"),
            ("ports/smbios-c/vcpkg.json", f'"version": "{s}"', '"version": "9.9.9"',
             f"smbios-c port version: expected {s} -> actual 9.9.9"),
            ("versions/baseline.json", f'"baseline": "{s}"', '"baseline": "9.9.9"',
             f"smbios-c registry baseline: expected {s} -> actual 9.9.9"),
        )
        pristine = self.snapshot()
        for rel, old, new, line in cases:
            with self.subTest(site=rel):
                self.assertEqual(self.run_check()[0], 0)
                self.edit(rel, old, new)
                rc, lines = self.run_check()
                self.assertEqual(rc, 1, lines)
                self.assertIn(line, "\n".join(lines))
                (self.root / rel).write_bytes(pristine[rel])


class GitTests(_Tree):
    """The real ``git status`` lookup against a throwaway repository."""

    def setUp(self) -> None:
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.git = ["git", "-C", str(self.root), "-c", "user.name=t",
                    "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(self.git + ["add", "."], check=True)
        subprocess.run(self.git + ["commit", "-q", "-m", "x"], check=True)

    def test_CleanTreeBumps(self) -> None:
        out = io.StringIO()
        rc = pr.run(self.root, "6.0.0", sites_file=self.sites, out=out)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(pr.git_dirty(self.root, list(SITE_FILES)),
                         ["CMakeLists.txt", "ports/smbios-c/vcpkg.json",
                          "versions/baseline.json"])

    def test_UnstagedOrStagedEditOfATargetIsRefused(self) -> None:
        port = self.root / "ports" / "smbios-c" / "vcpkg.json"
        data = self.json_at("ports/smbios-c/vcpkg.json")
        data["description"] = "hand edit"
        port.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    subprocess.run(self.git + ["add", "ports"], check=True)
                before = self.snapshot()
                out = io.StringIO()
                rc = pr.run(self.root, "6.0.0", sites_file=self.sites, out=out)
                self.assertEqual(rc, 2, out.getvalue())
                self.assertIn("uncommitted changes in ports/smbios-c/vcpkg.json",
                              out.getvalue())
                self.assertEqual(self.snapshot(), before)

    def test_DirtyNonTargetFileDoesNotBlock(self) -> None:
        (self.root / "smbios_c" / "CMakeLists.txt").write_text("# edit\n", encoding="utf-8")
        self.assertEqual(pr.git_dirty(self.root, ["CMakeLists.txt"]), [])

    def test_NotAGitWorkTreeIsRefused(self) -> None:
        shutil.rmtree(self.root / ".git")
        with tempfile.TemporaryDirectory() as outside:
            # A tree outside any repository (the temp dir itself might be inside one).
            other = Path(outside)
            for rel in SITE_FILES:
                (other / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / rel, other / rel)
            probe = subprocess.run(["git", "-C", str(other), "rev-parse"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                self.skipTest("temporary directory is inside a git work tree")
            out = io.StringIO()
            rc = pr.run(other, "6.0.0", sites_file=self.sites, out=out)
            self.assertEqual(rc, 2)
            self.assertIn("not a git work tree", out.getvalue())


class BoundaryTests(unittest.TestCase):
    """Threat model Task 01 requirement 3 and the real repository."""

    def test_NeverTagsPushesCommitsOrAddsAVersion(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for word in ("tag", "push", "commit", "add", "x-add-version", "--overwrite-version",
                     "fetch", "reset", "checkout"):
            self.assertNotIn(word, literals)
        subprocess_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Attribute)
                            and isinstance(n.func.value, ast.Name)
                            and n.func.value.id == "subprocess"]
        self.assertEqual(len(subprocess_calls), 1)
        argv = subprocess_calls[0].args[0]
        self.assertIsInstance(argv, ast.List)
        self.assertEqual([e.value for e in argv.elts if isinstance(e, ast.Constant)][:2],
                         ["git", "-C"])
        self.assertIn("status", [e.value for e in argv.elts if isinstance(e, ast.Constant)])

    def test_RealRepositoryIsAlreadyAtItsOwnVersion(self) -> None:
        current = cv.check(REPO_ROOT).authority or ""
        self.assertEqual(pr.plan(REPO_ROOT, current).edits, [])

    def test_CommandLine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            for rel in SITE_FILES:
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / rel, tmp / rel)
            current = cv.check(tmp).authority or ""
            args = [sys.executable, "-I", str(SCRIPT), "--repo-root", str(tmp)]
            ok = subprocess.run(args + [current], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=neutral_env(tmp), timeout=60,
                                text=True)
            bad = subprocess.run(args + ["1.0.0-rc.1"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=neutral_env(tmp), timeout=60,
                                 text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertRegex(ok.stdout, r"^ok: prepare release: already .*\nok: versions: ")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("pre-release 1.0.0-rc.1 refused", bad.stdout)


if __name__ == "__main__":
    unittest.main()
