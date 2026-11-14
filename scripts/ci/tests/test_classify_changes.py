"""Tests for ``classify_changes.py`` and the tier table of ``plan.matrix_for`` (0014 Story 04.0 Task 02).

Classification: unit tests (``classify``, ``valid_rev``, the tier table row
by row) plus git-backed tests of ``changed_paths`` against a throw-away
repository in a temporary directory (stdlib ``git`` subprocess, no network),
and command-line tests of ``classify_changes.py`` in a subprocess.

Scenarios: a docs-only change is ``docs``; a golden file under ``tests/data/``,
``scripts/``, ``examples/`` or ``.github/`` is ``full``; a missing, all-zero,
unknown or non-ancestor (force-push) base is ``full``; a rename out of the
source tree into ``docs/`` is ``full``; hostile revisions exit 2 without
being echoed. Tier table: every row of ``docs/dev/ci.md`` through
``matrix_for``, including that dispatch (``manual``) is rejected, a ``master``
push yields only ``staging``-tier publish rows and a tag only ``release``-tier
ones.

On failure, first check: ``scripts/ci/legs.json`` ``tiers`` (the leg lists
the table expects), then ``plan.matrix_for``; for the git cases, whether
``git`` is on ``PATH`` (they are skipped without it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

from ci_test_support import CI_DIR, neutral_env

import classify_changes as cc  # noqa: E402
import plan  # noqa: E402
from reporters import get_reporter  # noqa: E402

SCRIPT = CI_DIR / "classify_changes.py"
HAVE_GIT = shutil.which("git") is not None
LEGS = plan.load_data().legs
FULL_BUILD = ["linux", "windows", "macos"]
FULL_SAN = ["linux-asan", "macos-asan"]
FULL_GUI = ["linux-gui", "windows-gui", "macos-gui"]


class ClassifyTests(unittest.TestCase):
    """Type: unit. Scenario: path lists to docs/full. Boundary: empty and None."""

    def test_DocsOnlyIsDocs(self) -> None:
        self.assertEqual(cc.classify(["docs/dev/ci.md", "README.md", "CLAUDE.md",
                                      "docs/roadmap/x/status.md"]), cc.DOCS)

    def test_GoldenFileIsFull(self) -> None:
        self.assertEqual(cc.classify(["docs/output-formats.md",
                                      "tests/data/output_formats/sample.json"]), cc.FULL)

    def test_ScriptsExamplesWorkflowsAreFull(self) -> None:
        for path in ("scripts/check_doc_samples.py", "examples/read_fields.cpp",
                     ".github/workflows/ci.yml", "scripts/ci/README.md", "smbios/README.md",
                     "docs", "CMakeLists.txt", "x.mdx"):
            with self.subTest(path=path):
                self.assertEqual(cc.classify(["docs/a.md", path]), cc.FULL)

    def test_MissingOrEmptyDiffIsFull(self) -> None:
        self.assertEqual(cc.classify(None), cc.FULL)
        self.assertEqual(cc.classify([]), cc.FULL)

    def test_SummaryText(self) -> None:
        self.assertEqual(cc.summary_text("docs", ["a.md", "b.md"]), "docs (2 paths)")
        self.assertEqual(cc.summary_text("full", None), "full (no diff base)")


class ValidRevTests(unittest.TestCase):
    """Type: invalid input (CR-6). Scenario: revisions git could misread."""

    def test_ShasAndRefNamesPass(self) -> None:
        for value in ("a" * 40, "0123abc", "f" * 64, "HEAD", "refs/heads/feature/x-1",
                      "origin/master", "v1.2.3"):
            with self.subTest(value=value):
                self.assertTrue(cc.valid_rev(value))

    def test_HostileValuesFail(self) -> None:
        for value in ("", "-x", "--output=/tmp/x", "a..b", "a...b", "HEAD@{1}", "x\ny",
                      "x\r", "$(id)", "a;b", "a b", "refs/heads/", "x.lock", "a//b",
                      "A" * 40 + "\n", "x" * 201):
            with self.subTest(value=value):
                self.assertFalse(cc.valid_rev(value))

    def test_ChangedPathsRejectsHostileWithoutGit(self) -> None:
        with self.assertRaises(cc.UsageError):
            cc.changed_paths("-x", "HEAD")
        with self.assertRaises(cc.UsageError):
            cc.changed_paths("a" * 40, "HEAD\nx")

    def test_EmptyOrZeroBaseIsNoneWithoutGit(self) -> None:
        for base in (None, "", "0" * 40, "0" * 64):
            with self.subTest(base=base):
                self.assertIsNone(cc.changed_paths(base, "HEAD", cwd=Path("/nonexistent")))


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
class GitDiffTests(unittest.TestCase):
    """Type: integration (git subprocess). Scenario: real diffs in a temporary repository."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-classify-")
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.git("init", "-q", "-b", "master")
        self.write("src/main.cpp", "int main() {}\n")
        self.write("docs/guide.md", "# guide\n")
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                    "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.repo)})
        return subprocess.run(["git", *args], cwd=str(self.repo), env=env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def paths(self, base: Optional[str], head: str) -> Optional[List[str]]:
        return cc.changed_paths(base, head, cwd=self.repo)

    def test_DocsOnlyPushIsDocs(self) -> None:
        self.write("docs/guide.md", "# guide v2\n")
        self.write("README.md", "# readme\n")
        head = self.commit("docs")
        paths = self.paths(self.base, head)
        self.assertEqual(sorted(paths or []), ["README.md", "docs/guide.md"])
        self.assertEqual(cc.classify(paths), cc.DOCS)

    def test_GoldenFileUnderTestsDataIsFull(self) -> None:
        self.write("docs/guide.md", "# guide v2\n")
        self.write("tests/data/output_formats/sample.json", "{}\n")
        head = self.commit("golden")
        self.assertEqual(cc.classify(self.paths(self.base, head)), cc.FULL)

    def test_MissingBaseIsFull(self) -> None:
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs")
        for base in (None, "", "0" * 40, "1" * 40):
            with self.subTest(base=base):
                paths = self.paths(base, head)
                self.assertIsNone(paths)
                self.assertEqual(cc.classify(paths), cc.FULL)

    def test_ForcePushBaseIsFull(self) -> None:
        """A base that is not an ancestor of the head (history rewritten) is ``full``."""
        self.write("docs/guide.md", "# old tip\n")
        old_tip = self.commit("old tip")
        self.git("reset", "-q", "--hard", self.base)
        self.write("docs/guide.md", "# new tip\n")
        new_tip = self.commit("new tip")
        self.assertIsNone(self.paths(old_tip, new_tip))

    def test_RenameIntoDocsIsFull(self) -> None:
        """Regression guard: rename detection would list only ``docs/main.md``."""
        self.git("mv", "src/main.cpp", "docs/main.md")
        head = self.commit("rename")
        paths = self.paths(self.base, head) or []
        self.assertIn("src/main.cpp", paths)
        self.assertEqual(cc.classify(paths), cc.FULL)

    def test_EmptyCommitIsFull(self) -> None:
        head = self.commit("re-run")
        self.assertEqual(self.paths(self.base, head), [])
        self.assertEqual(cc.classify(self.paths(self.base, head)), cc.FULL)

    def track_master(self, rev: str) -> None:
        """Point ``refs/remotes/origin/master`` at ``rev`` (what the CI checkout fetches)."""
        self.git("update-ref", cc.MASTER_REMOTE_REF, rev)

    def test_CodeBranchWithDocsOnlyHeadIsFull(self) -> None:
        """A docs-only push after a code push still plans the Linux legs (merge base, not before)."""
        self.track_master(self.base)
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/main.cpp", "int main() { return 1; }\n")
        code = self.commit("code")
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs only")
        self.assertEqual(cc.classify(self.paths(code, head)), cc.DOCS, "the before-diff")
        base = cc.branch_base(head, cwd=self.repo, fetch=False)
        self.assertEqual(base, self.base)
        self.assertEqual(cc.classify(self.paths(base, head)), cc.FULL)

    def test_DocsOnlyBranchAgainstMasterIsDocs(self) -> None:
        self.track_master(self.base)
        self.git("checkout", "-q", "-b", "feature")
        self.write("docs/guide.md", "# guide v2\n")
        self.commit("docs 1")
        self.write("README.md", "# readme\n")
        head = self.commit("docs 2")
        base = cc.branch_base(head, cwd=self.repo, fetch=False)
        self.assertEqual(cc.classify(self.paths(base, head)), cc.DOCS)

    def test_MasterMovedOnStillUsesTheMergeBase(self) -> None:
        """Code that landed on master after the branch point does not count against the branch."""
        self.git("checkout", "-q", "-b", "feature")
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs")
        self.git("checkout", "-q", "master")
        self.write("src/main.cpp", "int main() { return 2; }\n")
        self.track_master(self.commit("master code"))
        base = cc.branch_base(head, cwd=self.repo, fetch=False)
        self.assertEqual(base, self.base)
        self.assertEqual(cc.classify(self.paths(base, head)), cc.DOCS)

    def test_UnresolvableMergeBaseIsFull(self) -> None:
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs")
        with self.subTest(case="no origin/master and no remote to fetch from"):
            base = cc.branch_base(head, cwd=self.repo)
            self.assertIsNone(base)
            self.assertEqual(cc.classify(self.paths(base, head)), cc.FULL)
        with self.subTest(case="no shared history"):
            self.git("checkout", "-q", "--orphan", "other")
            self.write("docs/other.md", "# other\n")
            self.track_master(self.commit("unrelated root"))
            self.assertIsNone(cc.branch_base(head, cwd=self.repo, fetch=False))
        with self.subTest(case="hostile head"), self.assertRaises(cc.UsageError):
            cc.branch_base("--evil", cwd=self.repo)

    def test_MissingTrackingRefIsFetchedOnce(self) -> None:
        """origin/master absent from the checkout: one credential-less fetch restores it."""
        remote = self.repo / "remote.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("checkout", "-q", "-b", "feature")
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs")
        self.assertFalse(cc._commit_exists(cc.MASTER_REMOTE_REF, self.repo))
        self.assertEqual(cc.branch_base(head, cwd=self.repo), self.base)
        self.assertTrue(cc._commit_exists(cc.MASTER_REMOTE_REF, self.repo))

    def test_PlanDiffsABranchPushAgainstMaster(self) -> None:
        """plan.run ignores CI_BASE_COMMIT on a branch push; uses it on a master push."""
        self.track_master(self.base)
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/main.cpp", "int main() { return 1; }\n")
        code = self.commit("code")
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs only")
        for ref, want in (("refs/heads/feature", "full"), ("refs/heads/master", "docs")):
            with self.subTest(ref=ref):
                env = {"CI_EVENT": "push", "CI_REF": ref, "CI_BASE_COMMIT": code,
                       "CI_COMMIT": head, "CI_PR_SAME_REPO": ""}
                outputs: Dict[str, object] = {}
                reporter = get_reporter("plain")
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(reporter, "set_output",
                                          lambda n, v: outputs.__setitem__(n, v)):
                    lines = plan.run(reporter, cwd=self.repo)
                self.assertEqual(outputs["plan"]["classification"], want, lines)
                self.assertTrue(lines[0].startswith(f"ok: changes: {want} "), lines)

    def test_CliPrintsOneOkLine(self) -> None:
        self.write("docs/guide.md", "# guide v2\n")
        head = self.commit("docs")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-I", str(SCRIPT), "--base", self.base,
                                   "--head", head, "--reporter", "plain"],
                                  cwd=str(self.repo), env=neutral_env(Path(tmp)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        self.assertRegex(lines[0], r"^ok: changes: docs \(1 paths\) \(\d+\.\d+s\)$")


class CliValidationTests(unittest.TestCase):
    """Type: invalid input. Scenario: hostile --base/--head exit 2 without echo."""

    def test_HostileRevisionExits2(self) -> None:
        for args in (["--base", "--evil"], ["--base", "a..b"], ["--head", "x;evil"],
                     ["--base", "evil\nmatrix=1"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run([sys.executable, "-I", str(SCRIPT), *args,
                                       "--reporter", "plain"], env=neutral_env(Path(tmp)),
                                      cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("evil", proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")


def _data(publish_jobs=(), defined=frozenset()) -> plan.PlanData:
    base = plan.load_data()
    return plan.PlanData(base.legs, base.pins, tuple(publish_jobs), frozenset(defined))


class TierTableTests(unittest.TestCase):
    """Type: unit. Scenario: the tier table of docs/dev/ci.md, row by row, through ``matrix_for``."""

    def jobs(self, result: Dict[str, object]) -> Dict[str, object]:
        return {job: result[job] for job in ("lint", "build", "sanitizers", "gui")}

    def test_DocsChangesRunLintOnlyOnEveryEvent(self) -> None:
        for event, ref, same in (("push", "refs/heads/feature", False),
                                 ("push", "refs/heads/master", False),
                                 ("pr", "refs/pull/3/merge", True),
                                 ("pr", "refs/pull/3/merge", False),
                                 ("schedule", "refs/heads/master", False)):
            with self.subTest(event=event, ref=ref, same=same):
                result = plan.matrix_for(event, ref, "docs", pr_same_repo=same)
                self.assertEqual(self.jobs(result), {"lint": ["linux"], "build": [],
                                                     "sanitizers": [], "gui": []})
                self.assertEqual(set(result["legs"].values()), {plan.NOT_RUN_TIER})
                self.assertEqual(set(result["stages"].values()), {plan.NOT_RUN_TIER})
                self.assertEqual(result["runners"], {})

    def test_TagRunsTheFullMatrixEvenForDocsChanges(self) -> None:
        """A release (tag) run never degrades to docs-only, whatever the classifier said."""
        docs = plan.matrix_for("tag", "refs/tags/v1.0.0", "docs")
        full = plan.matrix_for("tag", "refs/tags/v1.0.0", "full")
        self.assertEqual(docs, full)
        self.assertEqual(docs["classification"], "full")
        self.assertGreater(len(docs["build"]), 1)

    def test_BranchPushRunsTheLinuxLegs(self) -> None:
        result = plan.matrix_for("push", "refs/heads/feature", "full")
        self.assertEqual(self.jobs(result), {"lint": ["linux"], "build": ["linux"],
                                             "sanitizers": ["linux-asan"], "gui": ["linux-gui"]})
        run = {leg for leg, reason in result["legs"].items() if reason == plan.RUN}
        self.assertEqual(run, set(LEGS["tiers"]["push_branch"]))
        self.assertEqual(set(result["stages"].values()), {plan.NOT_RUN_TIER})

    def test_SameRepoPrRunsOnlyWhatThePushTierDidNot(self) -> None:
        result = plan.matrix_for("pr", "refs/pull/3/merge", "full", pr_same_repo=True)
        self.assertEqual(self.jobs(result), {"lint": ["linux"], "build": ["windows", "macos"],
                                             "sanitizers": ["macos-asan"],
                                             "gui": ["windows-gui", "macos-gui"]})
        covered = {leg for leg, r in result["legs"].items() if r == plan.NOT_RUN_COVERED}
        self.assertEqual(covered, set(LEGS["tiers"]["push_branch"]))
        self.assertEqual(result["stages"], {"package": plan.NOT_RUN_UNDEFINED,
                                            "verify": plan.NOT_RUN_UNDEFINED,
                                            "publish": plan.NOT_RUN_TIER})

    def test_EveryLegRunsOnceAcrossPushAndSameRepoPr(self) -> None:
        """Success criterion: no leg twice, every leg at least once, for one commit."""
        push = plan.matrix_for("push", "refs/heads/feature", "full")
        pr = plan.matrix_for("pr", "refs/pull/3/merge", "full", pr_same_repo=True)
        for leg in LEGS["tiers"]["full"]:
            with self.subTest(leg=leg):
                ran = [r for r in (push["legs"][leg], pr["legs"][leg]) if r == plan.RUN]
                self.assertEqual(len(ran), 1)

    def test_FullMatrixEvents(self) -> None:
        for event, ref, same in (("pr", "refs/pull/3/merge", False),
                                 ("push", "refs/heads/master", False),
                                 ("schedule", "refs/heads/master", False),
                                 ("tag", "refs/tags/v1.0.0", False),
                                 ("push", "refs/heads/master", True)):  # ignored off-PR
            with self.subTest(event=event, ref=ref, same=same):
                result = plan.matrix_for(event, ref, "full", pr_same_repo=same)
                self.assertEqual(self.jobs(result), {"lint": ["linux"], "build": FULL_BUILD,
                                                     "sanitizers": FULL_SAN, "gui": FULL_GUI})
                self.assertEqual(set(result["legs"].values()), {plan.RUN})
                self.assertEqual(set(result["runners"]), set(LEGS["tiers"]["full"]))
                self.assertEqual(result["stages"]["package"], plan.NOT_RUN_UNDEFINED)

    def test_PublishIsTierOnlyOnTagAndMasterPush(self) -> None:
        cases = {("tag", "refs/tags/v1.0.0"): plan.NOT_RUN_UNDEFINED,
                 ("push", "refs/heads/master"): plan.NOT_RUN_UNDEFINED,
                 ("schedule", "refs/heads/master"): plan.NOT_RUN_TIER,
                 ("pr", "refs/pull/3/merge"): plan.NOT_RUN_TIER,
                 ("push", "refs/heads/feature"): plan.NOT_RUN_TIER}
        for (event, ref), want in cases.items():
            with self.subTest(event=event, ref=ref):
                result = plan.matrix_for(event, ref, "full")
                self.assertEqual(result["stages"]["publish"], want)
                self.assertEqual(result["publish"], [])

    def test_PublishRowsFollowTheirTier(self) -> None:
        """A master push yields only staging rows; a tag only release rows; others none."""
        data = _data([{"job": "release-gh", "tier": "release"},
                      {"job": "staging-s3", "tier": "staging"}],
                     {"package", "verify", "publish"})
        rows = {(e, r): plan.matrix_for(e, r, "full", data=data)["publish"]
                for e, r in (("tag", "refs/tags/v2.0.0"), ("push", "refs/heads/master"),
                             ("push", "refs/heads/feature"), ("pr", "refs/pull/1/merge"),
                             ("schedule", "refs/heads/master"))}
        self.assertEqual(rows[("tag", "refs/tags/v2.0.0")],
                         [{"job": "release-gh", "tier": "release"}])
        self.assertEqual(rows[("push", "refs/heads/master")],
                         [{"job": "staging-s3", "tier": "staging"}])
        for key in (("push", "refs/heads/feature"), ("pr", "refs/pull/1/merge"),
                    ("schedule", "refs/heads/master")):
            self.assertEqual(rows[key], [], key)
        # A tag is always the full pipeline: its release rows survive a `docs` classification.
        docs_tag = plan.matrix_for("tag", "refs/tags/v2.0.0", "docs", data=data)
        self.assertEqual(docs_tag["publish"], [{"job": "release-gh", "tier": "release"}])
        # A docs-only master push is lint only: nothing is staged.
        docs_master = plan.matrix_for("push", "refs/heads/master", "docs", data=data)
        self.assertEqual(docs_master["publish"], [])

    def test_DispatchEventIsRejected(self) -> None:
        """Maintainer ruling: no workflow_dispatch, so ``manual`` never plans (or publishes)."""
        data = _data([{"job": "staging-s3", "tier": "staging"}], {"publish"})
        for ref in ("refs/heads/master", "refs/heads/feature", "refs/tags/v1.0.0"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    plan.matrix_for("manual", ref, "full", data=data)
        for event in ("workflow_dispatch", "pull_request", "pull_request_target", "", "PUSH"):
            with self.subTest(event=event), self.assertRaises(plan.PlanError):
                plan.matrix_for(event, "refs/heads/master", "full")

    def test_RefMustMatchTheEvent(self) -> None:
        for event, ref in (("push", "refs/tags/v1.0.0"), ("tag", "refs/tags/1.0.2.202611131"),
                           ("tag", "refs/heads/master"), ("pr", "refs/heads/master"),
                           ("pr", "refs/pull/x/merge"), ("push", "refs/heads/a..b"),
                           ("push", "refs/heads/x\nmatrix=1"), ("schedule", "")):
            with self.subTest(event=event, ref=ref), self.assertRaises(plan.PlanError):
                plan.matrix_for(event, ref, "full")

    def test_UnknownClassificationIsRejected(self) -> None:
        for value in ("", "partial", "docs\n", "FULL"):
            with self.subTest(value=value), self.assertRaises(plan.PlanError):
                plan.matrix_for("push", "refs/heads/master", value)

    def test_OutputCarriesNoRefOrBranchName(self) -> None:
        result = plan.matrix_for("push", "refs/heads/secret-branch-name", "full")
        self.assertNotIn("secret-branch-name", repr(result))


if __name__ == "__main__":
    unittest.main()
