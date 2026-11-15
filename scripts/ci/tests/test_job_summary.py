"""Tests for the per-job summary block and artifact (milestone 0014, Story 04.0 Task 01; plan E9).

Covers ``scripts/ci/job_summary.py``: the 30-line / 4 KiB block for 0, 1 and
50 records (with the ``(+k more)`` row), the ``PASS with <k> best-effort
warning(s)`` header, the synthetic failure of a job that failed outside any
recorded step, inert rendering of Markdown/HTML in a diagnostic, RC3 parsing
of malformed or hostile input, the 8 KiB artifact, its CR-11 file safety, and
the reporter backends' ``job_summary`` event with a real block.

Classification: unit (stdlib only; no network, no host tools; every file in
a temporary directory).
On failure first check: the caps in job_summary.py (``BLOCK_MAX_LINES``,
``BLOCK_MAX_BYTES``, ``DETAIL_MAX``, ``ARTIFACT_MAX_BYTES``) against plan E9 /
the task spec, then ``inert()`` for the rendering cases.

Run: ``python3 -m unittest discover -s scripts/ci/tests -p 'test_job_summary.py'``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List
from unittest import mock

from ci_test_support import neutral_env

import job_summary  # noqa: E402  (ci_test_support put scripts/ci on sys.path)
import print_env  # noqa: E402
import reporters  # noqa: E402

ENV_LINE = ("env: build/linux image=ubuntu24/20261110.1 | cmake 3.31.6 | ctest 3.31.6 | "
            "ninja 1.12.1 | cxx gcc 13.3.0 | python 3.12.3 | vcpkg 0123456789ab | cache exact")
#: Annotation needles built by concatenation (the P11 scan exempts tests/, kept anyway).
GH_ERROR = "::" + "error"


def record(label: str = "build", outcome: str = "ok", *, mode: str = "hard", rc: int = 0,
           summary: str = "", diag: str = None, counts: Dict[str, int] = None) -> dict:
    """Return one RC3 record."""
    return {"label": label, "mode": mode, "outcome": outcome, "exit": rc, "elapsed_s": 1.25,
            "summary": summary, "first_diagnostic": diag, "counts": counts}


def outside_spans(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


class BlockBoundsTests(unittest.TestCase):
    """Type: unit. Scenario: block size for 0, 1 and 50 records. Boundaries: 30 lines, 4 KiB."""

    def assertBounded(self, block: str) -> None:
        self.assertTrue(block.endswith("\n"))
        self.assertLessEqual(len(block.splitlines()), job_summary.BLOCK_MAX_LINES)
        self.assertLessEqual(len(block.encode("utf-8")), job_summary.BLOCK_MAX_BYTES)

    def test_ZeroRecordsIsBoundedAndSaysSo(self) -> None:
        block = job_summary.render_job_block([], ENV_LINE, "build", "linux")
        self.assertBounded(block)
        self.assertTrue(block.startswith("### build (linux) - PASS\n"), block)
        self.assertIn("(no step records)", block)
        self.assertNotIn("more)", block)

    def test_OneRecordIsOneRow(self) -> None:
        block = job_summary.render_job_block([record("configure", summary="ok")], ENV_LINE,
                                             "build", "linux")
        self.assertBounded(block)
        rows = [ln for ln in block.splitlines() if ln.startswith("| `configure`")]
        self.assertEqual(len(rows), 1)
        self.assertIn("ok (1.2s)", rows[0])
        self.assertNotIn("more)", block)
        self.assertNotIn("first failure", block)

    def test_FiftyRecordsAreCappedWithAMoreRow(self) -> None:
        long = "x" * 400
        records = [record(f"step {i}", summary=long) for i in range(50)]
        block = job_summary.render_job_block(records, ENV_LINE, "build", "linux")
        self.assertBounded(block)
        more = [ln for ln in block.splitlines() if re.match(r"^\| \(\+\d+ more\) \|", ln)]
        self.assertEqual(len(more), 1, block)
        shown = len([ln for ln in block.splitlines() if ln.startswith("| `step ")])
        hidden = int(re.search(r"\+(\d+) more", more[0]).group(1))
        self.assertEqual(shown + hidden, 50)

    def test_FiftyRecordsKeepTheFailingRow(self) -> None:
        records = [record(f"step {i}", summary="y" * 200) for i in range(50)]
        records[45] = record("tests unit", "fail", rc=8, diag="boost: check failed")
        block = job_summary.render_job_block(records, ENV_LINE, "build", "linux")
        self.assertBounded(block)
        self.assertEqual(len([ln for ln in block.splitlines()
                              if ln.startswith("| `tests unit`")]), 1)
        self.assertEqual(block.count("first failure:"), 1)

    def test_DetailCellIsAtMost120Characters(self) -> None:
        block = job_summary.render_job_block([record("s", summary="z" * 500)], ENV_LINE,
                                             "build", "linux")
        row = next(ln for ln in block.splitlines() if ln.startswith("| `s`"))
        detail = row.rstrip(" |").rsplit(" | ", 1)[1]
        self.assertLessEqual(len(detail), job_summary.DETAIL_MAX)

    def test_CustomCapsAreHonoured(self) -> None:
        records = [record(f"s{i}", summary="w" * 100) for i in range(20)]
        block = job_summary.render_job_block(records, ENV_LINE, "gui", "macos-gui",
                                             max_lines=12, max_bytes=1500)
        self.assertLessEqual(len(block.splitlines()), 12)
        self.assertLessEqual(len(block.encode("utf-8")), 1500)
        self.assertIn("more) |", block)


class StatusTests(unittest.TestCase):
    """Type: unit. Scenario: header status and the first-failure line."""

    def test_OneBestEffortWarningIsSingular(self) -> None:
        records = [record("unit"), record("live-table", "warn", mode="best-effort", rc=8,
                                          diag="no SMBIOS table")]
        block = job_summary.render_job_block(records, ENV_LINE, "build", "windows")
        self.assertEqual(block.splitlines()[0],
                         "### build (windows) - PASS with 1 best-effort warning")
        self.assertNotIn("first failure", block)

    def test_TwoWarningsArePlural(self) -> None:
        records = [record("a", "warn", rc=1), record("b", "warn", rc=1)]
        self.assertEqual(job_summary.status_text(*job_summary.leg_status(records)),
                         "PASS with 2 best-effort warnings")

    def test_FailureWinsAndIsSaidOnce(self) -> None:
        records = [record("a", "warn", rc=1), record("configure", "fail", rc=1,
                                                     diag="CMake Error at x"),
                   record("build", "fail", rc=2, diag="second")]
        block = job_summary.render_job_block(records, ENV_LINE, "build", "linux")
        lines = block.splitlines()
        self.assertEqual(lines[0], "### build (linux) - FAIL")
        self.assertEqual(lines[-1], "first failure: `configure: CMake Error at x`")
        self.assertEqual(block.count("CMake Error at x"), 2)  # its row + the one line
        self.assertNotIn("second`", lines[-1])

    def test_JobFailedOutsideAStepIsNeverPass(self) -> None:
        records = job_summary.effective_records([record("unit")], job_failed=True)
        self.assertEqual(job_summary.leg_status(records), ("FAIL", 0))
        block = job_summary.render_job_block(records, "", "sanitizers", "linux-asan")
        self.assertTrue(block.startswith("### sanitizers (linux-asan) - FAIL"))
        self.assertIn(job_summary.JOB_FAILED_TEXT, block)
        self.assertIn("env: (no RC4 line)", block)

    def test_JobFailedDoesNotDuplicateARecordedFailure(self) -> None:
        recorded = [record("unit", "fail", rc=1, diag="d")]
        self.assertEqual(job_summary.effective_records(recorded, job_failed=True), recorded)

    def test_UnknownJobOrLegNameIsNotEchoed(self) -> None:
        block = job_summary.render_job_block([], "", "x\n### y", "<b>")
        self.assertTrue(block.startswith("### ? (?) - PASS"))


class InertRenderingTests(unittest.TestCase):
    """Type: unit, security (CR-4). Scenario: Markdown/HTML in tool text is rendered inert."""

    HOSTILE = "<script>alert(1)</script> **bold** [x](http://e) | a `tick` # h"

    def test_ScriptAndMarkdownStayInsideCodeSpans(self) -> None:
        records = [record("tests unit", "fail", rc=1, diag=self.HOSTILE,
                          summary=self.HOSTILE)]
        block = job_summary.render_job_block(records, ENV_LINE, "build", "linux")
        outside = outside_spans(block)
        for needle in ("<script>", "**bold**", "](http", "`tick`"):
            self.assertNotIn(needle, outside, needle)
        self.assertIn("<script>alert(1)</script>", block)  # shown, as code
        # Every table row still has exactly four cell separators (pipes escaped).
        for line in block.splitlines():
            if line.startswith("| `tests unit`"):
                self.assertEqual(len(re.findall(r"(?<!\\)\|", line)), 4, line)

    def test_InertHelper(self) -> None:
        self.assertEqual(job_summary.inert("a`b|c", 40), "`a'b\\|c`")
        self.assertEqual(job_summary.inert("a|b", 40, in_table=False), "`a|b`")
        self.assertEqual(job_summary.inert("", 40), "")
        self.assertLessEqual(len(job_summary.inert("|" * 300, 50)), 50)
        self.assertEqual(job_summary.inert("line1\nline2", 40), "`line1`")
        self.assertEqual(job_summary.inert("x\x1b[31mred\x07", 40), "`xred`")

    def test_HostileWorkflowCommandIsNotALineStart(self) -> None:
        records = [record("s", "fail", rc=1, diag=GH_ERROR + "::forged")]
        block = job_summary.render_job_block(records, GH_ERROR + "::env", "build", "linux")
        for line in block.splitlines():
            self.assertFalse(line.startswith("::"), line)


class RecordsFileTests(unittest.TestCase):
    """Type: unit, invalid input. Scenario: RC3 file parsing and the RC4 hand-off file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_MalformedLinesAreSkippedAndCounted(self) -> None:
        path = self.tmp / "r.jsonl"
        lines = [json.dumps(record("a")), "not json", json.dumps({"label": "b"}),
                 json.dumps(record("c", "maybe")), json.dumps(["x"]),
                 json.dumps(record("d", "fail", rc=1, diag="e" * 900)), "x" * 9000]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        records, skipped = job_summary.load_records(path)
        self.assertEqual([r["label"] for r in records], ["a", "d"])
        self.assertEqual(skipped, 5)
        self.assertLessEqual(len(records[1]["first_diagnostic"]), 160)

    def test_MissingFileIsNoRecords(self) -> None:
        self.assertEqual(job_summary.load_records(self.tmp / "absent.jsonl"), ([], 0))

    def test_ReadIsBounded(self) -> None:
        path = self.tmp / "r.jsonl"
        path.write_text((json.dumps(record("a")) + "\n") * 1000, encoding="utf-8")
        records, _ = job_summary.load_records(path, limit=1024)
        self.assertLess(len(records), 20)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_SymlinkedResultsFileIsRefused(self) -> None:
        target = self.tmp / "target.jsonl"
        target.write_text(json.dumps(record("a")) + "\n", encoding="utf-8")
        link = self.tmp / "r.jsonl"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            job_summary.load_records(link)

    def test_NormaliseRejectsAndCleans(self) -> None:
        self.assertIsNone(job_summary.normalise_record("x"))
        self.assertIsNone(job_summary.normalise_record({"label": "", "outcome": "ok"}))
        clean = job_summary.normalise_record({"label": "a", "outcome": "ok", "exit": True,
                                              "elapsed_s": float("nan"), "mode": "weird",
                                              "counts": {"passed": 3, "Bad Key": 1, "x": "y"}})
        self.assertEqual((clean["exit"], clean["elapsed_s"], clean["mode"], clean["counts"]),
                         (0, 0.0, "hard", {"passed": 3}))

    def test_EnvLineHandOffFileName(self) -> None:
        self.assertEqual(job_summary.ENV_LINE_FILE, print_env.ENV_LINE_FILE)
        print_env.write_env_line(ENV_LINE, self.tmp)
        self.assertEqual(job_summary.read_env_line(self.tmp), ENV_LINE)
        self.assertEqual(job_summary.read_env_line(self.tmp / "nowhere"), "")


class ArtifactTests(unittest.TestCase):
    """Type: unit. Scenario: the summary-<job>-<leg>.json artifact. Boundary: 8 KiB."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ArtifactIsBoundedAndKeepsTheFailure(self) -> None:
        records = [record(f"step {i}", summary="s" * 160) for i in range(60)]
        records.insert(30, record("tests unit", "fail", rc=1, diag="d" * 160))
        payload = job_summary.artifact_payload(records, ENV_LINE, "build", "linux")
        data = job_summary.serialise(payload)
        self.assertLessEqual(len(data), job_summary.ARTIFACT_MAX_BYTES)
        self.assertGreater(payload["omitted"], 0)
        self.assertEqual(len(payload["records"]) + payload["omitted"], 61)
        self.assertIn("tests unit", [r["label"] for r in payload["records"]])
        self.assertEqual((payload["schema"], payload["job"], payload["leg"]),
                         (1, "build", "linux"))

    def test_SmallArtifactKeepsEverything(self) -> None:
        payload = job_summary.artifact_payload([record("a")], ENV_LINE, "lint", "linux")
        self.assertEqual(payload["omitted"], 0)
        self.assertEqual(payload["env_line"], ENV_LINE)

    def test_WrittenFileIsPrivateAndNamedForTheLeg(self) -> None:
        payload = job_summary.artifact_payload([record("a")], ENV_LINE, "gui", "windows-gui")
        path = job_summary.write_artifact(payload, self.tmp / "artifacts")
        self.assertEqual(path.name, "summary-gui-windows-gui.json")
        self.assertEqual(json.loads(path.read_text(encoding="ascii"))["records"][0]["label"], "a")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_SymlinkedArtifactIsRefused(self) -> None:
        directory = self.tmp / "artifacts"
        directory.mkdir()
        target = self.tmp / "elsewhere"
        target.write_text("keep", encoding="utf-8")
        (directory / "summary-lint-linux.json").symlink_to(target)
        payload = job_summary.artifact_payload([], "", "lint", "linux")
        with self.assertRaises(OSError):
            job_summary.write_artifact(payload, directory)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")


class RunAndReporterTests(unittest.TestCase):
    """Type: unit (reporter boundary). Scenario: ``run()`` through each backend's ``job_summary``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        results = self.tmp / "results.jsonl"
        results.write_text(json.dumps(record("configure")) + "\n" + json.dumps(
            record("tests unit", "fail", rc=8, diag="check failed")) + "\n", encoding="utf-8")
        print_env.write_env_line(ENV_LINE, self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_backend(self, backend: str, **env: str):
        out = io.StringIO()
        with mock.patch.dict(os.environ, neutral_env(self.tmp, **env), clear=True), \
                contextlib.redirect_stdout(out):
            rc, lines = job_summary.run(reporters.get_reporter(backend), job="build",
                                        leg="linux", results=self.tmp / "results.jsonl",
                                        temp_dir=self.tmp, artifact_dir=self.tmp / "artifacts")
        return rc, lines, out.getvalue()

    def test_GithubAppendsTheBlockToTheSummaryFile(self) -> None:
        summary = self.tmp / "summary.md"
        summary.write_text("env: earlier line\n", encoding="utf-8")
        rc, lines, printed = self.run_backend("github", CI_SUMMARY_FILE=str(summary))
        self.assertEqual(rc, 0)
        self.assertEqual(printed, "")
        text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("env: earlier line\n### build (linux) - FAIL\n"), text)
        self.assertEqual(text.count("first failure:"), 1)
        self.assertEqual(lines, ["info: job summary: build (linux) FAIL, 2 records"])
        self.assertFalse(any(GH_ERROR in ln for ln in lines), "no extra annotation")
        self.assertTrue((self.tmp / "artifacts" / "summary-build-linux.json").is_file())

    def test_TeamcityPublishesTheBlockAndAStatus(self) -> None:
        rc, _, printed = self.run_backend("teamcity", CI_ARTIFACT_DIR=str(self.tmp / "tc"))
        self.assertEqual(rc, 0)
        self.assertIn("##teamcity[buildStatus text='### build (linux) - FAIL']", printed)
        self.assertIn("### build (linux) - FAIL",
                      (self.tmp / "tc" / "ci-summary.md").read_text(encoding="utf-8"))

    def test_PlainPrintsTheBlock(self) -> None:
        rc, _, printed = self.run_backend("plain")
        self.assertEqual(rc, 0)
        self.assertTrue(printed.startswith("### build (linux) - FAIL\n"), printed)

    def test_UnwritableArtifactIsAnInfoLineNotAFailure(self) -> None:
        blocker = self.tmp / "artifacts"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        rc, lines, _ = self.run_backend("plain")
        self.assertEqual(rc, 0)
        self.assertTrue(any(ln.startswith("info: job summary: artifact not written")
                            for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
