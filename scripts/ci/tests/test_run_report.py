"""Tests for the run-level report (milestone 0014, Story 04.0 Task 01; plan E9).

Covers ``scripts/ci/run_report.py`` (the 40-line run table, ``dedupe_failures``,
``no record`` / ``not run: tier`` cells, the single run-level error
annotation, untrusted artifact parsing), the reporters' new ``error`` event,
the ``stage.py summary`` / ``stage.py report`` wiring (subprocess, plain
reporter) and the workflow wiring (summary step in every stage job, the
``report`` job in ``ci.yml``, the expected legs kept in sync with
``scripts/ci/legs.json`` tiers).

Classification: unit + command-line (stdlib only; no network, no host tools).
On failure first check: ``scripts/ci/legs.json`` tiers versus
``run_report.expected_legs``, then the ``summary``/``report`` steps of
``.github/workflows/ci.yml`` and ``stage-*.yml``.

Run: ``python3 -m unittest discover -s scripts/ci/tests -p 'test_run_report.py'``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

from ci_test_support import CI_DIR, neutral_env

import check_workflow_policy as gate  # noqa: E402
import job_summary  # noqa: E402
import reporters  # noqa: E402
import run_report  # noqa: E402
import stage  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STAGE = CI_DIR / "stage.py"
LEGS = stage.load_legs()
GUI_LEGS = (("gui", "linux-gui"), ("gui", "windows-gui"), ("gui", "macos-gui"))
GUI_FIRST = "tests gui-ci: 1 of 12 tests failed (smbios_gui_ci_test)"
ENV_LINE = ("env: gui/linux-gui image=ubuntu24/1 | cmake 3.31.6 | ctest 3.31.6 | ninja 1.12.1 | "
            "cxx gcc 13.3.0 | python 3.12.3 | vcpkg 0123456789ab | cache exact")


def rec(label: str = "build", outcome: str = "ok", diag: str = None, rc: int = 0) -> dict:
    return {"label": label, "mode": "hard", "outcome": outcome, "exit": rc, "elapsed_s": 1.0,
            "summary": "", "first_diagnostic": diag, "counts": None}


def full_run(**overrides: List[dict]) -> Tuple[Dict[Tuple[str, str], List[dict]],
                                               List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Return (records, expected, not run) of a green full-tier run, with overrides."""
    expected, not_run = run_report.expected_legs(LEGS, tuple(LEGS["tiers"]["full"]))
    records = {key: [rec()] for key in expected}
    for name, value in overrides.items():
        job, leg = name.split("__")
        records[(job, leg.replace("_", "-"))] = value
    return records, expected, not_run


class RunTableTests(unittest.TestCase):
    """Type: unit. Scenario: the run table. Boundary: 40 lines."""

    def test_GreenRunIsOnePassTable(self) -> None:
        records, expected, not_run = full_run()
        table = run_report.render_run_table(records, expected, not_run=not_run)
        lines = table.splitlines()
        self.assertEqual(lines[0], f"## CI: PASS - {len(expected)} legs passed, 0 failed, "
                                   "0 with warnings, 0 not run (tier)")
        self.assertEqual(table.count("## CI:"), 1)
        self.assertLessEqual(len(lines), run_report.TABLE_MAX_LINES)
        self.assertIn("| job | linux | windows | macos | first failure (once) |", lines)
        self.assertNotIn("no record", table)

    def test_SameFirstDiagnosticOnThreeLegsIsOneRow(self) -> None:
        failing = [rec("tests gui-ci", "fail", "1 of 12 tests failed (smbios_gui_ci_test)", 8)]
        records, expected, not_run = full_run(gui__linux_gui=failing, gui__windows_gui=failing,
                                              gui__macos_gui=failing)
        table = run_report.render_run_table(records, expected, not_run=not_run)
        self.assertEqual(table.count(GUI_FIRST), 1, table)
        row = next(ln for ln in table.splitlines() if ln.startswith("| gui |"))
        self.assertIn(f"`{GUI_FIRST}` (linux-gui, windows-gui, macos-gui)", row)
        self.assertEqual(row.split(" | ")[1:4], ["FAIL", "FAIL", "FAIL"])
        self.assertTrue(table.startswith("## CI: FAIL - "))
        self.assertIn(" 3 failed,", table.splitlines()[0])

    def test_DedupeFailuresGroupsInFirstSeenOrder(self) -> None:
        firsts = {("gui", "linux-gui"): "a", ("gui", "windows-gui"): "b",
                  ("gui", "macos-gui"): "a"}
        self.assertEqual(run_report.dedupe_failures(firsts),
                         [("a", ["linux-gui", "macos-gui"]), ("b", ["windows-gui"])])
        self.assertEqual(run_report.dedupe_failures({}), [])

    def test_MissingArtifactIsNoRecordNeverPass(self) -> None:
        records, expected, not_run = full_run()
        del records[("build", "windows")]
        table = run_report.render_run_table(records, expected, not_run=not_run)
        row = next(ln for ln in table.splitlines() if ln.startswith("| build |"))
        self.assertEqual(row.split(" | ")[1:4], ["PASS", "no record", "PASS"])
        self.assertTrue(table.startswith("## CI: FAIL"), table.splitlines()[0])
        self.assertTrue(table.splitlines()[0].endswith(", 1 no record"))
        self.assertEqual(run_report.failed_legs(records, expected),
                         ["build/windows (no record)"])

    def test_PushTierShowsNotRunNeverPass(self) -> None:
        expected, not_run = run_report.expected_legs(LEGS, tuple(LEGS["tiers"]["push_branch"]))
        records = {key: [rec()] for key in expected}
        records[("build", "windows")] = [rec()]  # a stray artifact must not turn into PASS
        table = run_report.render_run_table(records, expected, not_run=not_run)
        build = next(ln for ln in table.splitlines() if ln.startswith("| build |"))
        self.assertEqual(build.split(" | ")[1:4], ["PASS", "not run: tier", "not run: tier"])
        self.assertIn(f", {len(not_run)} not run (tier)", table.splitlines()[0])
        self.assertTrue(table.startswith("## CI: PASS"))

    def test_WarningLegIsCountedAndShown(self) -> None:
        records, expected, not_run = full_run(build__windows=[rec(), rec("live", "warn", "x", 8)])
        table = run_report.render_run_table(records, expected, not_run=not_run)
        self.assertIn(", 1 with warnings,", table.splitlines()[0])
        self.assertIn("PASS (1 warn)", table)
        self.assertTrue(table.startswith("## CI: PASS"))

    def test_VersionsAreDeduplicated(self) -> None:
        records, expected, not_run = full_run()
        envs = {key: ENV_LINE for key in expected}
        envs[("build", "windows")] = ENV_LINE.replace("gcc 13.3.0", "msvc 19.44")
        table = run_report.render_run_table(records, expected, not_run=not_run, env_lines=envs)
        versions = [ln for ln in table.splitlines() if ln.startswith("versions:")]
        self.assertEqual(len(versions), 2, versions)
        self.assertTrue(versions[0].startswith(
            "versions: `cmake 3.31.6 | ctest 3.31.6 | cxx gcc 13.3.0` (lint/linux, "), versions[0])
        self.assertIn("(build/windows)", versions[1])

    def test_ManyDistinctVersionsStayWithin40Lines(self) -> None:
        records, expected, not_run = full_run()
        envs = {("build", f"leg{i}"): ENV_LINE.replace("13.3.0", f"13.{i}") for i in range(60)}
        table = run_report.render_run_table(records, expected, not_run=not_run, env_lines=envs)
        self.assertLessEqual(len(table.splitlines()), run_report.TABLE_MAX_LINES)
        self.assertTrue(table.splitlines()[-1].startswith("versions: (+"), table)
        tight = run_report.render_run_table(records, expected, 9, not_run=not_run,
                                            env_lines=envs)
        self.assertLessEqual(len(tight.splitlines()), 9)

    def test_HostileDiagnosticIsInert(self) -> None:
        hostile = "<script>alert(1)</script> **b** | `x`"
        records, expected, not_run = full_run(build__linux=[rec("unit", "fail", hostile, 1)])
        table = run_report.render_run_table(records, expected, not_run=not_run)
        outside = re.sub(r"`[^`]*`", "", table)
        self.assertNotIn("<script>", outside)
        self.assertNotIn("**b**", outside)
        row = next(ln for ln in table.splitlines() if ln.startswith("| build |"))
        self.assertEqual(len(re.findall(r"(?<!\\)\|", row)), 6, row)

    def test_ManyDistinctFailuresKeepTheCellBounded(self) -> None:
        failing = {f"gui__{leg.replace('-', '_')}": [rec("s", "fail", f"{leg} " + "d" * 150, 1)]
                   for _, leg in GUI_LEGS}
        records, expected, not_run = full_run(**failing)
        row = next(ln for ln in run_report.render_run_table(
            records, expected, not_run=not_run).splitlines() if ln.startswith("| gui |"))
        self.assertIn("more)", row)
        self.assertEqual(row.count("`") % 2, 0, "no code span is cut")


class ExpectedLegsTests(unittest.TestCase):
    """Type: unit (data sync). Scenario: expected legs follow legs.json tiers."""

    def test_FullTierExpectsEveryTierLegPlusLint(self) -> None:
        expected, not_run = run_report.expected_legs(LEGS, None)
        self.assertEqual(not_run, [])
        self.assertEqual(expected[0], ("lint", "linux"))
        self.assertEqual({leg for _, leg in expected[1:]}, set(LEGS["tiers"]["full"]))
        for job, leg in expected[1:]:
            self.assertEqual(job, LEGS["legs"][leg].get("job", "build"))

    def test_PushTierRunsOnlyLinuxLegs(self) -> None:
        tier = stage.tier_legs("push", "refs/heads/feature", LEGS)
        expected, not_run = run_report.expected_legs(LEGS, tier)
        self.assertEqual({run_report.leg_os(leg) for _, leg in expected}, {"linux"})
        self.assertEqual({leg for _, leg in expected[1:]} | {leg for _, leg in not_run},
                         set(LEGS["tiers"]["full"]) | set(LEGS["tiers"]["push_branch"]))

    def test_EveryTierLegHasAColumnMatchingItsRunner(self) -> None:
        for name in set(LEGS["tiers"]["full"]) | set(LEGS["tiers"]["push_branch"]):
            with self.subTest(leg=name):
                self.assertEqual(run_report.leg_os(name), LEGS["legs"][name]["runner"])

    def test_EveryLegJobIsATableRow(self) -> None:
        expected, _ = run_report.expected_legs(LEGS, None)
        self.assertLessEqual({job for job, _ in expected}, set(run_report.JOB_ORDER))
        self.assertEqual(set(run_report.JOB_ORDER), set(stage.SUMMARY_JOBS))


class ArtifactLoadTests(unittest.TestCase):
    """Type: unit, invalid input. Scenario: downloaded artifacts are untrusted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "summaries"
        self.dir.mkdir()
        self.allowed = {("lint", "linux"), ("build", "linux")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, job: str, leg: str, records: List[dict], *, name: str = None,
              sub: bool = False) -> Path:
        payload = job_summary.artifact_payload(records, ENV_LINE, job, leg)
        directory = self.dir / f"summary-{job}-{leg}" if sub else self.dir
        directory.mkdir(exist_ok=True)
        path = directory / (name or f"summary-{job}-{leg}.json")
        path.write_bytes(job_summary.serialise(payload))
        return path

    def test_FlatAndNestedArtifactsAreRead(self) -> None:
        self.write("lint", "linux", [rec("lint")])
        self.write("build", "linux", [rec("unit", "fail", "d", 1)], sub=True)
        records, envs, rejected = run_report.load_artifacts(self.dir, self.allowed)
        self.assertEqual(rejected, 0)
        self.assertEqual(set(records), self.allowed)
        self.assertEqual(envs[("lint", "linux")], ENV_LINE)

    def test_BadArtifactsAreRejected(self) -> None:
        self.write("build", "linux", [rec()], name="summary-lint-linux.json")  # name mismatch
        self.write("gui", "linux-gui", [rec()])  # leg not allowed
        (self.dir / "summary-x-y.json").write_text("{not json", encoding="utf-8")
        (self.dir / "summary-big-z.json").write_bytes(b" " * (run_report.ARTIFACT_READ_MAX + 10))
        (self.dir / "summary-old-y.json").write_text(json.dumps({"schema": 0}), encoding="utf-8")
        records, _, rejected = run_report.load_artifacts(self.dir, self.allowed)
        self.assertEqual(records, {})
        self.assertEqual(rejected, 5)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_SymlinkedArtifactIsRejected(self) -> None:
        real = self.write("lint", "linux", [rec()], name="real.json")
        (self.dir / "summary-lint-linux.json").symlink_to(real)
        records, _, rejected = run_report.load_artifacts(self.dir, self.allowed)
        self.assertEqual((records, rejected), ({}, 1))

    def test_MissingDirectoryIsNoArtifacts(self) -> None:
        self.assertEqual(run_report.load_artifacts(self.dir / "absent", self.allowed),
                         ({}, {}, 0))


class ReportRunTests(unittest.TestCase):
    """Type: unit (reporter boundary). Scenario: exit code and the single run-level annotation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.summaries = self.tmp / "summaries"
        self.summary_file = self.tmp / "summary.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def artifacts(self, failing: Tuple[Tuple[str, str], ...] = ()) -> None:
        expected, _ = run_report.expected_legs(LEGS, None)
        for job, leg in expected:
            records = [rec("tests gui-ci", "fail", "1 of 12 tests failed", 8)] \
                if (job, leg) in failing else [rec()]
            job_summary.write_artifact(job_summary.artifact_payload(records, "", job, leg),
                                       self.summaries)

    def run_github(self) -> Tuple[int, List[str]]:
        env = neutral_env(self.tmp, CI_SUMMARY_FILE=str(self.summary_file))
        with mock.patch.dict(os.environ, env, clear=True):
            return run_report.run(reporters.get_reporter("github"), summaries=self.summaries,
                                  legs_data=LEGS, tier=None)

    def test_GreenRunExits0WithoutAnnotation(self) -> None:
        self.artifacts()
        rc, lines = self.run_github()
        self.assertEqual(rc, 0)
        self.assertFalse(any(ln.startswith("::") for ln in lines), lines)
        self.assertEqual(self.summary_file.read_text(encoding="utf-8").count("## CI: PASS"), 1)

    def test_FailingLegsGiveExactlyOneRunLevelError(self) -> None:
        self.artifacts(GUI_LEGS)
        rc, lines = self.run_github()
        self.assertEqual(rc, 1)
        annotations = [ln for ln in lines if ln.startswith("::")]
        self.assertEqual(annotations, ["::" + "error title=CI::3 legs failed: gui/linux-gui, "
                                       "gui/windows-gui, gui/macos-gui"])
        summary = self.summary_file.read_text(encoding="utf-8")
        self.assertEqual(summary.count("1 of 12 tests failed"), 1)
        self.assertLessEqual(len(summary.splitlines()), run_report.TABLE_MAX_LINES)

    def test_NoArtifactsAtAllFails(self) -> None:
        rc, lines = self.run_github()
        self.assertEqual(rc, 1)
        self.assertIn("no record", self.summary_file.read_text(encoding="utf-8"))


class PlanDrivenReportTests(unittest.TestCase):
    """Type: unit. Scenario: the plan (``CI_PLAN``) decides the cells (Story 04.0 Task 02).

    Boundary: skipped legs are ``not run: tier`` / ``not run: covered by push
    run``, never PASS and never ``no record``, even with an artifact present.
    On failure first check: ``run_report.legs_from_plan`` and ``plan.matrix_for``.
    """

    @staticmethod
    def parsed(event: str, ref: str, classification: str, same_repo: bool = False) -> dict:
        import plan
        text = json.dumps(plan.matrix_for(event, ref, classification, pr_same_repo=same_repo))
        return plan.parse_plan(text, LEGS)

    def table(self, parsed: dict, records: Dict[Tuple[str, str], List[dict]]) -> str:
        expected, not_run, covered = run_report.legs_from_plan(LEGS, parsed)
        return run_report.render_run_table(records, expected, not_run=not_run, covered=covered,
                                           stages=parsed["stages"])

    def test_DocsOnlyPushShowsLintAndNotRunTier(self) -> None:
        parsed = self.parsed("push", "refs/heads/topic", "docs")
        table = self.table(parsed, {("lint", "linux"): [rec()]})
        self.assertTrue(table.startswith("## CI: PASS - 1 legs passed, 0 failed, 0 with "
                                         f"warnings, {len(LEGS['tiers']['full'])} not run (tier)"),
                        table)
        self.assertNotIn("no record", table)
        for row in ("build", "sanitizers", "gui"):
            line = next(ln for ln in table.splitlines() if ln.startswith(f"| {row} |"))
            self.assertNotIn("PASS", line)
            self.assertIn("not run: tier", line)

    def test_SameRepoPrShowsCoveredByPushRun(self) -> None:
        parsed = self.parsed("pr", "refs/pull/4/merge", "full", same_repo=True)
        expected, _, covered = run_report.legs_from_plan(LEGS, parsed)
        records = {key: [rec()] for key in expected}
        records[("build", "linux")] = [rec()]  # a stray record never turns covered into PASS
        table = self.table(parsed, records)
        build = next(ln for ln in table.splitlines() if ln.startswith("| build |"))
        self.assertEqual(build.split(" | ")[1:4],
                         ["not run: covered by push run", "PASS", "PASS"])
        self.assertIn(f", {len(covered)} not run (covered by push run)", table.splitlines()[0])
        self.assertNotIn("no record", table)
        self.assertTrue(table.startswith("## CI: PASS"))

    def test_UndefinedStagesAreReasonRows(self) -> None:
        # package (0012 Story 03.0), publish (Story 04.0 Task 02) and verify (Task 04)
        # are defined: scheduled on a tag, each row says where the result is (never
        # PASS). An undefined stage still renders its reason (verify forced back).
        undefined = self.parsed("tag", "refs/tags/v1.0.0", "full")
        undefined["stages"]["verify"] = undefined["stages"]["publish"] = \
            "not run: stage not defined"
        for event, ref, want, names, parsed in (
                ("tag", "refs/tags/v1.0.0", "not run: stage not defined", ("verify", "publish"),
                 undefined),
                ("tag", "refs/tags/v1.0.0", run_report.STAGE_RUN, run_report.PIPELINE_STAGES,
                 None),
                ("push", "refs/heads/topic", "not run: tier", run_report.PIPELINE_STAGES, None)):
            with self.subTest(event=event, want=want):
                table = self.table(parsed or self.parsed(event, ref, "full"), {})
                for stage_name in names:
                    row = next(ln for ln in table.splitlines()
                               if ln.startswith(f"| {stage_name} |"))
                    self.assertEqual(row.count(want), len(run_report.OS_COLUMNS), row)
                self.assertNotIn("PASS |", table.split("| package |")[1])

    def test_PackagingLegsHaveTheirOwnRow(self) -> None:
        parsed = self.parsed("pr", "refs/pull/4/merge", "full", same_repo=False)
        expected, _, _ = run_report.legs_from_plan(LEGS, parsed)
        records = {key: [rec()] for key in expected}
        table = self.table(parsed, records)
        build = next(ln for ln in table.splitlines() if ln.startswith("| build |"))
        packaging = next(ln for ln in table.splitlines()
                         if ln.startswith(f"| {run_report.PACKAGING_ROW} |"))
        self.assertEqual(build.split(" | ")[1:4], ["PASS", "PASS", "PASS"])
        self.assertEqual(packaging.split(" | ")[1:4], ["PASS", "PASS", "PASS"])
        records.pop(("build", "windows-x64"))
        table = self.table(parsed, records)
        packaging = next(ln for ln in table.splitlines()
                         if ln.startswith(f"| {run_report.PACKAGING_ROW} |"))
        self.assertEqual(packaging.split(" | ")[2], "no record")
        self.assertTrue(table.startswith("## CI: FAIL"))

    def test_PlannedLegWithoutArtifactIsNoRecord(self) -> None:
        parsed = self.parsed("push", "refs/heads/topic", "full")
        table = self.table(parsed, {("lint", "linux"): [rec()]})
        self.assertTrue(table.startswith("## CI: FAIL"), table)
        self.assertIn(", 3 no record", table.splitlines()[0])

    def test_StageReportReadsAndValidatesCiPlan(self) -> None:
        import plan
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            (tmp / "summaries").mkdir()
            job_summary.write_artifact(job_summary.artifact_payload([rec()], "", "lint", "linux"),
                                       tmp / "summaries")
            good = json.dumps(plan.matrix_for("push", "refs/heads/topic", "docs"),
                              separators=(",", ":"))
            proc = run_stage_cli(tmp, ["report"], CI_EVENT="push", CI_REF="refs/heads/topic",
                                 CI_PLAN=good)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("not run: tier", proc.stdout)
            self.assertIn("| package | not run: tier", proc.stdout)
            for hostile in ('{"schema":1,"legs":{"evil":"run"}}', good + "\nevil", "evil"):
                with self.subTest(plan=hostile[:40]):
                    proc = run_stage_cli(tmp, ["report"], CI_EVENT="push",
                                         CI_REF="refs/heads/topic", CI_PLAN=hostile)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertNotIn("evil", proc.stdout + proc.stderr)


class ReporterErrorEventTests(unittest.TestCase):
    """Type: unit, security (CR-4). Scenario: the run-level ``error`` event on every backend."""

    def test_EveryBackendRendersOneSanitisedLine(self) -> None:
        text = "2 legs failed: a, b\nforged second line"
        expected = {"github": "::" + "error title=CI::2 legs failed: a, b",
                    "teamcity": "##teamcity[buildProblem description='CI: 2 legs failed: a, b' "
                                "identity='CI']",
                    "plain": "error: CI: 2 legs failed: a, b"}
        for backend in reporters.BACKEND_NAMES:
            with self.subTest(backend=backend):
                self.assertEqual(reporters.get_reporter(backend).error("CI", text),
                                 [expected[backend]])

    def test_GithubEscapesAndCuts(self) -> None:
        (line,) = reporters.get_reporter("github").error("C:I,", "50% " + "x" * 400)
        self.assertTrue(line.startswith("::" + "error title=C%3AI%2C::50%25 "), line)
        self.assertLessEqual(len(line.split("::", 2)[2]), 3 * 160)

    def test_TeamcityNeutralisesServiceMessages(self) -> None:
        (line,) = reporters.get_reporter("teamcity").error("CI", "##teamcity[x]")
        self.assertEqual(line.count("##teamcity["), 1, line)


def run_stage_cli(tmp: Path, args: List[str], **extra: str) -> subprocess.CompletedProcess:
    env = neutral_env(tmp, **{"CI_WORKSPACE": str(REPO_ROOT), "CI_PLATFORM": "local",
                              "CI_ARTIFACT_DIR": str(tmp / "artifacts"), **extra})
    return subprocess.run([sys.executable, "-I", str(STAGE), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                          cwd=str(tmp), timeout=60, text=True)


class StageWiringTests(unittest.TestCase):
    """Type: command-line. Scenario: ``stage.py summary`` / ``stage.py report`` end to end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_SummaryThenReport(self) -> None:
        (self.tmp / "results.jsonl").write_text(
            json.dumps(rec("unit", "fail", "boom", 1)) + "\n", encoding="utf-8")
        proc = run_stage_cli(self.tmp, ["summary"], CI_JOB="build", CI_LEG="linux")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("### build (linux) - FAIL\n"), proc.stdout)
        artifact = self.tmp / "artifacts" / "summary-build-linux.json"
        self.assertTrue(artifact.is_file())
        summaries = self.tmp / "summaries"
        summaries.mkdir()
        artifact.rename(summaries / artifact.name)
        proc = run_stage_cli(self.tmp, ["report"], CI_EVENT="push", CI_REF="refs/heads/topic")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("## CI: FAIL - 0 legs passed, 1 failed", proc.stdout)
        self.assertIn("not run: tier", proc.stdout)
        self.assertEqual(len([ln for ln in proc.stdout.splitlines()
                              if ln.startswith("error: CI:")]), 1, proc.stdout)

    def test_SummaryJobFailedWithoutRecords(self) -> None:
        proc = run_stage_cli(self.tmp, ["summary", "--job-failed"], CI_JOB="gui",
                             CI_LEG="macos-gui")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("### gui (macos-gui) - FAIL\n"), proc.stdout)

    def test_UsageErrorsExit2WithoutEcho(self) -> None:
        cases = [(["summary"], {"CI_JOB": "evil\njob", "CI_LEG": "linux"}),
                 (["summary"], {"CI_JOB": "build", "CI_LEG": "no-such-leg"}),
                 (["summary", "--only", "unit"], {"CI_JOB": "build", "CI_LEG": "linux"}),
                 (["report", "--leg", "linux"], {}),
                 (["report", "--job-failed"], {}),
                 (["build", "--job-failed"], {}),
                 (["report"], {"CI_EVENT": "pull_request_target"})]
        for args, env in cases:
            with self.subTest(args=args, env=env):
                proc = run_stage_cli(self.tmp, args, **env)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("evil", proc.stderr)
                self.assertNotIn("no-such-leg", proc.stderr)
                self.assertNotIn("pull_request_target", proc.stderr)


class WorkflowWiringTests(unittest.TestCase):
    """Type: unit (data sync). Scenario: every job publishes a summary; ci.yml has the report job."""

    def load(self, name: str):
        return gate.load_yaml((WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_EveryStageJobSummarisesAndUploads(self) -> None:
        for name, job in (("stage-lint.yml", "lint"), ("stage-build.yml", "build"),
                          ("stage-sanitizers.yml", "sanitizers"), ("stage-gui.yml", "gui")):
            with self.subTest(workflow=name):
                steps = self.load(name)["jobs"][job]["steps"]
                runs = [(str(s.get("if", "")), str(s["run"]).strip()) for s in steps if "run" in s]
                summaries = [r for r in runs if "stage.py summary" in r[1]]
                self.assertTrue(any(r[1].endswith("summary") and r[0].startswith("success()")
                                    for r in summaries), summaries)
                self.assertTrue(any(r[1].endswith("--job-failed")
                                    and r[0].startswith("(failure() || cancelled())")
                                    for r in summaries), summaries)
                self.assertTrue(all(i > max(j for j, s in enumerate(steps) if "run" in s
                                            and "summary" not in str(s["run"]))
                                    for i, s in enumerate(steps) if "run" in s
                                    and "summary" in str(s["run"])), "summary steps run last")
                upload = [s for s in steps if str(s.get("uses", "")).startswith(
                    "actions/upload-artifact@") and str(s["with"]["name"]).startswith("summary-")]
                self.assertEqual(len(upload), 1)
                self.assertTrue(str(upload[0]["if"]).startswith("always()"))
                self.assertIn(f"summary-{job}-", str(upload[0]["with"]["path"]))
                self.assertEqual(str(upload[0]["with"]["retention-days"]), "3")

    def test_CiHasTheReportJob(self) -> None:
        job = self.load("ci.yml")["jobs"]["report"]
        self.assertEqual([str(x) for x in job["needs"]],
                         ["plan", "lint", "build", "sanitizers", "gui"])
        self.assertEqual(str(job["if"]), "always()")
        self.assertEqual(str(job["runs-on"]), "ubuntu-24.04")
        self.assertEqual(str(job["timeout-minutes"]), "5")
        self.assertEqual(dict(job["permissions"]), {"contents": "read"})
        uses = [str(s["uses"]) for s in job["steps"] if "uses" in s]
        self.assertTrue(any(u.startswith("actions/download-artifact@") for u in uses))
        download = next(s for s in job["steps"] if "download-artifact" in str(s.get("uses")))
        self.assertEqual(str(download["with"]["pattern"]), "summary-*")
        self.assertEqual([str(s["run"]).strip() for s in job["steps"] if "run" in s],
                         ["python3 scripts/ci/stage.py report"])
        self.assertIn(run_report.SUMMARIES_DIR, str(download["with"]["path"]))


if __name__ == "__main__":
    unittest.main()
