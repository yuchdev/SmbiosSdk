"""Tests for the workflow policy gate P1-P15 (milestone 0014, Story 02.0 Task 03).

Classification: unit (no network, no workflow run, no host tools). Fixtures:
``fixtures/workflows/<case>/`` - ``good/`` passes every rule, each
``p<N>_<what>/`` violates exactly rule ``P<N>`` once (P11 scans scripts, so its
fixtures are built in a temporary directory here), and ``combined/`` breaks most
rules at once to prove the 20-line bound.

On failure first check: which rule fired in addition to (or instead of) the one
the case directory names - the render in the assertion message shows it; then
``_WorkflowCheck`` for that rule in ``scripts/ci/check_workflow_policy.py``.

Run: ``python3 scripts/ci/run_tests.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

from ci_test_support import CI_DIR, FIXTURES

import check_workflow_policy as gate  # noqa: E402  (ci_test_support put scripts/ci on sys.path)

WORKFLOW_FIXTURES = FIXTURES / "workflows"
GOOD = WORKFLOW_FIXTURES / "good"
COMBINED = WORKFLOW_FIXTURES / "combined"
_CASE = re.compile(r"^(p[0-9]+)_")
_LINE = re.compile(r"^\S+:[^:]+: (?:P[0-9]+|yaml): expected .+ -> actual .+$")
#: Built by concatenation: P11 needles must not appear verbatim outside tests/
#: either, but this keeps the intent visible.
EXPR = "$" + "{{"


def policy_for(case_dir: Path, **overrides: Path) -> gate.Policy:
    """Load the policy with the case directory's own registry files where present."""
    def pick(name: str, default: Path) -> Path:
        return overrides.get(name) or (case_dir / name if (case_dir / name).exists() else default)

    files = gate.PolicyFiles(
        actions_allowlist=pick("actions_allowlist.json", gate.PolicyFiles.actions_allowlist),
        publish_jobs=pick("publish_jobs.json", gate.PolicyFiles.publish_jobs),
        publish_destinations=pick("publish_destinations.json",
                                  gate.PolicyFiles.publish_destinations),
        step_budgets=pick("step_budgets.json", gate.PolicyFiles.step_budgets),
        pins=pick("pins.json", gate.PolicyFiles.pins),
        destinations_dir=case_dir / "destinations")
    return gate.load_policy(files)


def run_case(case_dir: Path, branch: Optional[str] = None, **overrides: Path
             ) -> List[gate.Violation]:
    """Run every non-script rule on the workflows in ``case_dir`` (``branch``: P9 probe rule)."""
    return gate.run_gate(sorted(case_dir.glob("*.yml")), policy=policy_for(case_dir, **overrides),
                         workflows_dir=case_dir, scripts_dir=None, branch=branch)


def cli(args: List[str]) -> tuple:
    """Run ``check_workflow_policy.main`` in-process; return (exit, stdout lines)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = gate.main(["--reporter", "plain", *args])
    return rc, out.getvalue().splitlines()


def case_dirs() -> List[Path]:
    return sorted(p for p in WORKFLOW_FIXTURES.iterdir() if p.is_dir() and _CASE.match(p.name))


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-policy-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def copy_good(self) -> Path:
        dest = self.tmp / "good"
        shutil.copytree(GOOD, dest)
        return dest

    def edit_json(self, path: Path, edit) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        edit(data)
        path.write_text(json.dumps(data), encoding="utf-8")


class OneViolationPerRule(unittest.TestCase):
    """Each ``p<N>_*`` fixture produces exactly one violation of rule P<N> (normal + invalid input)."""

    def test_EveryCaseFiresOnlyItsRule(self) -> None:
        for case in case_dirs():
            with self.subTest(case=case.name):
                expected = _CASE.match(case.name).group(1).upper()
                found = run_case(case)
                self.assertEqual([v.rule for v in found], [expected],
                                 [v.render() for v in found])

    def test_EveryWorkflowRuleHasAFailingFixture(self) -> None:
        rules = {_CASE.match(c.name).group(1).upper() for c in case_dirs()}
        expected = {f"P{n}" for n in range(1, 16)} - {"P11"}  # P11: P11Rule below
        self.assertEqual(expected - rules, set())

    def test_ThreatModelFixturesExist(self) -> None:
        """The 2026-11-13 threat-model fixture list, each its own directory."""
        for name in ("p8_workflow_run", "p8_issue_comment", "p8_secrets_inherit",
                     "p12_staging_caller_without_verify", "p8_staging_reachable_from_dispatch",
                     "p8_staging_reachable_from_pull_request", "p12_caller_widening",
                     "p10_pull_request_title", "p10_head_ref", "p13_github_script",
                     "p13_local_action", "p2_docker", "p14_checkout_persists_credentials",
                     "p15_cache_in_verify", "p15_logs_directory", "p15_logs_glob",
                     "p15_upload_other_name_directory"):
            self.assertTrue((WORKFLOW_FIXTURES / name).is_dir(), name)

    def test_SameRepoComparisonIsTheOnlyHeadRepoUse(self) -> None:
        """P10 (Story 04.0 Task 02): only the whole boolean comparison is allowed in env:."""
        ok = "github.event.pull_request.head.repo.full_name == github.repository"
        for expr, allowed in ((ok, True), ("  " + ok.replace(" == ", "  ==  ") + " ", True),
                              ("github.event.pull_request.head.repo.full_name", False),
                              (ok + " && github.head_ref", False),
                              ("format('{0}', github.event.pull_request.head.repo.full_name)",
                               False)):
            with self.subTest(expr=expr), tempfile.TemporaryDirectory() as tmp:
                case = Path(tmp) / "case"
                shutil.copytree(WORKFLOW_FIXTURES / "p10_head_repo_name_raw", case)
                path = case / "ci.yml"
                text = path.read_text(encoding="utf-8")
                old = EXPR + " github.event.pull_request.head.repo.full_name }}"
                path.write_text(text.replace(old, EXPR + expr + "}}"), encoding="utf-8")
                found = run_case(case)
                rules = [v.rule for v in found]
                if allowed:
                    self.assertEqual(rules, [], [v.render() for v in found])
                else:
                    self.assertTrue(rules and set(rules) == {"P10"}, rules)
                    self.assertTrue(any("head.repo.full_name" in v.actual for v in found))

    def test_DiscrepancyNamesTheOffendingValue(self) -> None:
        cases = {"p3_latest_label": "ubuntu-latest", "p10_head_ref": "github.head_ref",
                 "p10_pull_request_title": "pull_request.title", "p2_docker": "docker://",
                 "p13_github_script": "actions/github-script", "p8_workflow_run": "workflow_run",
                 "p12_caller_widening": "pull-requests: write",
                 "p8_staging_reachable_from_dispatch": "dispatch-master"}
        for name, needle in cases.items():
            with self.subTest(case=name):
                (violation,) = run_case(WORKFLOW_FIXTURES / name)
                self.assertIn(needle, violation.actual)


class CompliantFixture(_TmpCase):
    """The compliant fixture set passes every rule (normal case)."""

    def test_GoodFixturePasses(self) -> None:
        self.assertEqual([v.render() for v in run_case(GOOD)], [])

    def test_GoodFixtureCliIsOneOkLine(self) -> None:
        rc, lines = cli(["--no-scripts", "--workflows-dir", str(GOOD),
                         "--publish-jobs", str(GOOD / "publish_jobs.json"),
                         "--publish-destinations", str(GOOD / "publish_destinations.json"),
                         "--destinations-dir", str(GOOD / "destinations"),
                         *map(str, sorted(GOOD.glob("*.yml")))])
        self.assertEqual(rc, 0, lines)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ok: workflow policy: 4 workflows"), lines)

    def test_RepositoryDataFilesPass(self) -> None:
        """The shipped allowlist, publish registries, budgets and pins are themselves valid."""
        found = gate.run_gate([], policy=gate.load_policy(), workflows_dir=self.tmp,
                              scripts_dir=None)
        self.assertEqual([v.render() for v in found], [])

    def test_OkLineFromRealDefaultsWithoutScripts(self) -> None:
        rc, lines = cli(["--no-scripts", "--workflows-dir", str(self.tmp)])
        self.assertEqual((rc, len(lines)), (0, 1), lines)


class BoundedOutput(unittest.TestCase):
    """RC1: at most 20 discrepancy lines plus ``(+k more)`` (edge case)."""

    def test_CombinedFixtureIsBounded(self) -> None:
        total = len(run_case(COMBINED))
        self.assertGreater(total, gate.MAX_LINES)
        rc, lines = cli(["--no-scripts", "--workflows-dir", str(COMBINED),
                         str(COMBINED / "ci.yml")])
        self.assertEqual(rc, 1)
        self.assertEqual(len(lines), gate.MAX_LINES + 1, lines)
        self.assertEqual(lines[-1], f"(+{total - gate.MAX_LINES} more)")
        for line in lines[:-1]:
            self.assertRegex(line, _LINE)
            self.assertLessEqual(len(line), gate.LINE_WIDTH)

    def test_CombinedCoversManyRules(self) -> None:
        rules = {v.rule for v in run_case(COMBINED)}
        self.assertTrue({"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P10", "P12", "P13",
                         "P14", "P15"} <= rules, rules)

    def test_RenderShape(self) -> None:
        v = gate.Violation("a.yml", "job/step", "P4", "timeout-minutes <= 10", "x" * 500)
        line = v.render()
        self.assertTrue(line.startswith("a.yml:job/step: P4: expected timeout-minutes <= 10 -> actual x"))
        self.assertLessEqual(len(line), gate.LINE_WIDTH)
        self.assertNotIn("\n", gate.Violation("f", "l", "P1", "a\nb", "c\x1b").render())

    def test_UnknownReporterIsUsageError(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main(["--reporter", "nope", "--no-scripts"])
        self.assertEqual(rc, gate.EXIT_USAGE)


class P11Rule(_TmpCase):
    """P11 scans scripts: platform syntax, platform variables and the single emitter (CR-4)."""

    def write(self, rel: str, text: str) -> None:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_PrintInNonExemptScriptIsOneViolation(self) -> None:
        self.write("tool.py", "def f():\n    print('x')\n")
        found = gate.check_scripts(self.tmp)
        self.assertEqual([(v.rule, v.location) for v in found], [("P11", "line 2")])

    def test_TestsDirectoryWithHostileFixturesPasses(self) -> None:
        """Positive control: tests/ may hold ::error:: and ##teamcity[ fixtures."""
        self.write("tests/fixtures/hostile.log", "::error::x\n##teamcity[buildStatus]\n")
        self.write("tests/test_x.py", "print('::warning::y')\n")
        self.write("reporters/github.py", "import sys\nsys.stdout.write('::error::z')\n")
        self.write("env_names.py", "X = 'RUNNER" + "_TEMP'\n")
        self.write("ci_step.py", "import sys\nsys.stdout.write('ok')\n")
        self.assertEqual(gate.check_scripts(self.tmp), [])

    def test_EveryNeedleAndEnvReadDetected(self) -> None:
        lines = [f"s = '{n}'" for n in gate.P11_NEEDLES]
        lines += ["os.environ['GITHUB" + "_OUTPUT']", "import sys", "sys.stderr.write('x')"]
        self.write("bad.py", "\n".join(lines) + "\n")
        found = gate.check_scripts(self.tmp)
        self.assertEqual(len(found), len(gate.P11_NEEDLES) + 2, [v.render() for v in found])
        self.assertTrue(all(v.rule == "P11" for v in found))

    def test_CiStepMayEmitButNotUsePlatformSyntax(self) -> None:
        self.write("ci_step.py", "import sys\nsys.stdout.write('ok')\nx = '::" + "error'\n")
        found = gate.check_scripts(self.tmp)
        self.assertEqual([v.location for v in found], ["line 3"])

    def test_NonPythonFilesAreScannedForSyntaxOnly(self) -> None:
        self.write("data.json", '{"x": "print(1)", "y": "' + EXPR + ' github.sha }}"}\n')
        found = gate.check_scripts(self.tmp)
        self.assertEqual(len(found), 1, [v.render() for v in found])

    def test_NewScriptsOfThisTaskPass(self) -> None:
        """The gate, installer and lint entry point obey P11 themselves."""
        found = [v for v in gate.check_scripts(CI_DIR)
                 if Path(v.file).name in ("check_workflow_policy.py", "install_actionlint.py",
                                          "lint.py", "actions_allowlist.json",
                                          "publish_jobs.json", "publish_destinations.json",
                                          "step_budgets.json")]
        self.assertEqual([v.render() for v in found], [])


class RegistryRules(_TmpCase):
    """P3/P4/P12/P13 checks of the data files themselves (invalid input)."""

    def test_AllowlistEntryWithoutVerificationDate(self) -> None:
        allow = self.tmp / "actions_allowlist.json"
        shutil.copy(gate.PolicyFiles.actions_allowlist, allow)
        self.edit_json(allow, lambda d: d["actions"][0].pop("tag_commit_verified_on"))
        found = run_case(GOOD, **{"actions_allowlist.json": allow})
        self.assertEqual([(v.rule, v.expected) for v in found],
                         [("P13", "field tag_commit_verified_on")])

    def test_AllowlistBadDateAndDeniedAction(self) -> None:
        allow = self.tmp / "actions_allowlist.json"
        shutil.copy(gate.PolicyFiles.actions_allowlist, allow)

        def edit(d: Dict) -> None:
            d["actions"][0]["tag_commit_verified_on"] = "2026-13-40"
            d["actions"].append(dict(d["actions"][1], action="actions/github-script"))
        self.edit_json(allow, edit)
        found = run_case(GOOD, **{"actions_allowlist.json": allow})
        self.assertEqual(sorted(v.actual for v in found),
                         ["2026-13-40", "actions/github-script"])
        self.assertTrue(all(v.rule == "P13" for v in found))

    def test_CompositeNeedsPinnedTransitiveUses(self) -> None:
        allow = self.tmp / "actions_allowlist.json"
        shutil.copy(gate.PolicyFiles.actions_allowlist, allow)
        self.edit_json(allow, lambda d: d["actions"][0].update(kind="composite"))
        (v,) = run_case(GOOD, **{"actions_allowlist.json": allow})
        self.assertEqual(v.rule, "P13")

    def test_DestinationModuleMissingAndWrongEnvironment(self) -> None:
        good = self.copy_good()
        (good / "destinations" / "s3_staging.py").unlink()
        self.edit_json(good / "publish_destinations.json",
                       lambda d: d["destinations"][1].update(environment="staging"))
        found = run_case(good)
        self.assertEqual(sorted((v.rule, v.location) for v in found),
                         [("P12", "github-release"), ("P12", "s3-staging")])

    def test_DestinationWithoutAPublishJobOfItsTierAndClass(self) -> None:
        good = self.copy_good()
        self.edit_json(good / "publish_destinations.json",
                       lambda d: d["destinations"][0].update(permission_class="token-only"))
        (v,) = run_case(good)
        self.assertEqual((v.rule, v.expected), ("P12", "a staging-tier token-only job in "
                                                       "publish_jobs.json"))

    def test_DisabledDestinationIsNotChecked(self) -> None:
        good = self.copy_good()
        self.edit_json(good / "publish_destinations.json",
                       lambda d: d["destinations"].append({"name": "x", "enabled": False}))
        self.assertEqual(run_case(good), [])

    def test_PublishJobPermissionsOutsideItsClass(self) -> None:
        good = self.copy_good()
        self.edit_json(good / "publish_jobs.json",
                       lambda d: d["jobs"][0]["permissions"].append("contents: write"))
        found = run_case(good)
        self.assertEqual([v.rule for v in found], ["P12"], [v.render() for v in found])

    def test_PublishJobListedButMissing(self) -> None:
        good = self.copy_good()
        self.edit_json(good / "publish_jobs.json",
                       lambda d: d["jobs"].append(dict(d["jobs"][0], job="ghost")))
        (v,) = run_case(good)
        self.assertEqual((v.rule, v.expected), ("P12", "listed job exists"))

    def test_BudgetNotTwiceTheMedian(self) -> None:
        budgets = self.tmp / "step_budgets.json"
        shutil.copy(gate.PolicyFiles.step_budgets, budgets)
        self.edit_json(budgets, lambda d: d["jobs"]["build"].update(budget_minutes=40))
        (v,) = run_case(GOOD, **{"step_budgets.json": budgets})
        self.assertEqual((v.rule, v.expected), ("P4", "budget_minutes 18"))

    def test_PinsFileFloatingLabel(self) -> None:
        pins = self.tmp / "pins.json"
        shutil.copy(gate.PolicyFiles.pins, pins)
        self.edit_json(pins, lambda d: d["runners"].update(linux="ubuntu-latest"))
        (v,) = run_case(GOOD, **{"pins.json": pins})
        self.assertEqual((v.rule, v.location), ("P3", "runners/linux"))

    def test_BrokenDataFileIsAViolationNotACrash(self) -> None:
        jobs = self.tmp / "publish_jobs.json"
        jobs.write_text("{not json", encoding="utf-8")
        found = run_case(WORKFLOW_FIXTURES / "p1_no_permissions", **{"publish_jobs.json": jobs})
        self.assertIn(("P12", "valid JSON"), [(v.rule, v.expected) for v in found])

    def test_VerifyTagsDereferencesAnnotatedTags(self) -> None:
        policy = gate.load_policy()
        sha = policy.allowlist["actions/checkout"]["sha"]
        answers = {"repos/actions/checkout/git/ref/tags/v7.0.1":
                   {"object": {"type": "tag", "sha": "a" * 40}},
                   f"repos/actions/checkout/git/tags/{'a' * 40}": {"object": {"sha": sha}}}

        def fake(endpoint: str) -> Optional[dict]:
            if endpoint in answers:
                return answers[endpoint]
            repo_tag = endpoint.split("/git/ref/tags/")
            action = next(a for a in policy.allowlist if a.startswith(
                repo_tag[0].replace("repos/", "")))
            return {"object": {"type": "commit", "sha": policy.allowlist[action]["sha"]}}

        self.assertEqual(gate.verify_tags(policy, fake), [])
        answers[f"repos/actions/checkout/git/tags/{'a' * 40}"] = {"object": {"sha": "b" * 40}}
        found = gate.verify_tags(policy, fake)
        self.assertEqual([(v.rule, v.location) for v in found], [("P13", "actions/checkout")])


class P15UploadEveryName(_TmpCase):
    """S3-1 (2026-11-13 Story 03.0 review): P15 path rules apply to every upload-artifact step.

    Type: unit. Scenario: the good ``stage-build.yml`` upload step, renamed and
    re-pathed. Boundaries: non-``logs-`` names, directory/glob/``.git`` paths,
    retention 3 vs 4 / unset. On failure first check: ``_WorkflowCheck.p15``.
    """

    def upload_case(self, name: str, path: str, retention: Optional[str] = "3") -> List[str]:
        shutil.rmtree(self.tmp / "good", ignore_errors=True)  # one fresh copy per subTest
        case = self.copy_good()
        wf = case / "stage-build.yml"
        text = wf.read_text(encoding="utf-8")
        old = (f"          name: logs-{EXPR} matrix.leg }}}}\n"
               f"          path: |\n"
               f"            {EXPR} runner.temp }}}}/ci-logs/configure.log\n"
               f"            {EXPR} runner.temp }}}}/ci-logs/build.log\n"
               f"          include-hidden-files: false\n"
               f"          retention-days: 3\n")
        self.assertIn(old, text)
        new = (f"          name: {name}\n          path: {path}\n"
               f"          include-hidden-files: false\n")
        if retention is not None:
            new += f"          retention-days: {retention}\n"
        wf.write_text(text.replace(old, new), encoding="utf-8")
        return [v.render() for v in run_case(case)]

    def test_OtherNameDirectoryFixtureFails(self) -> None:
        (violation,) = run_case(WORKFLOW_FIXTURES / "p15_upload_other_name_directory")
        self.assertEqual(violation.rule, "P15")
        self.assertIn("ci-logs/", violation.actual)

    def test_OtherNameLiteralFilePasses(self) -> None:
        self.assertEqual(self.upload_case("gui-logs", f"{EXPR} runner.temp }}}}/ci-logs/gui.log"),
                         [])

    def test_OtherNameGlobAndWorkspaceFail(self) -> None:
        for path in (f"{EXPR} runner.temp }}}}/ci-logs/*.log", ".", "build/"):
            with self.subTest(path=path):
                found = self.upload_case("summary-x", path)
                self.assertEqual(len(found), 1, found)
                self.assertIn("P15: expected literal file path", found[0])

    def test_GitComponentFails(self) -> None:
        found = self.upload_case("gui-logs", ".git/logs/HEAD.txt")
        self.assertEqual(len(found), 1, found)
        self.assertIn("no .git path component", found[0])

    def test_RetentionAboveThreeOrUnsetFails(self) -> None:
        for retention, actual in (("4", "4"), (None, "unset"), ("0", "0")):
            with self.subTest(retention=retention):
                found = self.upload_case("gui-logs", "a/gui.log", retention)
                self.assertEqual(len(found), 1, found)
                self.assertIn(f"retention-days 1..3 -> actual {actual}", found[0])

    def test_RetentionOneAndThreePass(self) -> None:
        for retention in ("1", "3"):
            with self.subTest(retention=retention):
                self.assertEqual(self.upload_case("gui-logs", "a/gui.log", retention), [])


REPO_ROOT = CI_DIR.parents[1]
PROBE_TEMPLATE = REPO_ROOT / "docs" / "dev" / "ci-probe-template.yml"
STRAY_PROBE = WORKFLOW_FIXTURES / "p9_stray_probe"


def _template_job_examples() -> List[str]:
    """Return the commented single-job ``jobs:`` blocks of the probe template, uncommented."""
    blocks: List[str] = []
    current: Optional[List[str]] = None
    for line in PROBE_TEMPLATE.read_text(encoding="utf-8").split("\n"):
        if line == "# jobs:":
            current = ["jobs:"]
            blocks.append("")
        elif current is not None and line.startswith("#   "):
            current.append(line[2:])
        elif current is not None:
            blocks[-1] = "\n".join(current) + "\n"
            current = None
    return blocks


class ProbeWorkflows(_TmpCase):
    """P9 probe rule and the probe template (milestone 0014 Story 04.0 Task 03).

    Type: unit (no network, no workflow run). Scenario: a ``probe-*.yml`` in
    the workflows directory on a non-probe branch (stray), on a ``probe/**``
    branch (allowed and fully checked), and the shipped template plus each of
    its commented single-job callers run through the gate as a fixture.
    Boundaries: ``probe/`` without a topic, pull-request and tag refs, a tag
    named ``probe/...``, CI without ``CI_REF``, a local detached HEAD.
    On failure first check: ``_name_violation``/``current_branch`` in
    ``scripts/ci/check_workflow_policy.py``; for a template failure, which rule
    the rendered line names (the template must follow every rule).
    """

    def probe_dir(self, text: str, name: str = "probe-template.yml") -> Path:
        case = self.tmp / "wf"
        shutil.rmtree(case, ignore_errors=True)
        case.mkdir()
        (case / name).write_text(text, encoding="utf-8")
        return case

    def gate_dir(self, case: Path, branch: Optional[str]) -> List[str]:
        """Run the gate as lint.py does: nothing named, the directory scan finds the probe."""
        found = gate.run_gate([], policy=gate.load_policy(), workflows_dir=case,
                              scripts_dir=None, branch=branch)
        return [v.render() for v in found]

    # -- P9 stray probe -------------------------------------------------------
    def test_StrayProbeOnNonProbeBranchIsOneP9(self) -> None:
        for branch in (None, "feature", "master", "probe", "probe/", "xprobe/x"):
            with self.subTest(branch=branch):
                (v,) = run_case(STRAY_PROBE, branch=branch)
                self.assertEqual((v.rule, v.expected, v.actual),
                                 ("P9", "stray probe workflow: 0", "1 (probe-x.yml)"))
                self.assertIn("P9: expected stray probe workflow: 0 -> actual 1 (probe-x.yml)",
                              v.render())

    def test_StrayProbeFoundByDirectoryScan(self) -> None:
        """Regression: lint.py never names the probe file; the directory scan must catch it."""
        case = self.probe_dir((STRAY_PROBE / "probe-x.yml").read_text(encoding="utf-8"))
        found = self.gate_dir(case, "milestone-0014-ci-rework")
        self.assertEqual(len(found), 1, found)
        self.assertIn("stray probe workflow", found[0])

    def test_ProbeAllowedOnProbeBranch(self) -> None:
        self.assertEqual([v.render() for v in run_case(STRAY_PROBE, branch="probe/x")], [])
        case = self.probe_dir((STRAY_PROBE / "probe-x.yml").read_text(encoding="utf-8"))
        self.assertEqual(self.gate_dir(case, "probe/runner-versions"), [])

    def test_ProbeOnProbeBranchGetsEveryRule(self) -> None:
        """A probe file found by the directory scan is checked, not just named."""
        text = (STRAY_PROBE / "probe-x.yml").read_text(encoding="utf-8")
        self.assertIn("\n    timeout-minutes: 10\n    steps:", text)
        case = self.probe_dir(text.replace("\n    timeout-minutes: 10\n    steps:",
                                           "\n    steps:"))
        found = self.gate_dir(case, "probe/x")
        self.assertEqual(len(found), 1, found)
        self.assertIn("P4: expected timeout-minutes", found[0])

    def test_ProbeTriggerIsPushToProbeBranchesOnly(self) -> None:
        text = (STRAY_PROBE / "probe-x.yml").read_text(encoding="utf-8")
        good = "on:\n  push:\n    branches: ['probe/**']\n"
        self.assertIn(good, text)
        for bad in ("on:\n  push:\n    branches: ['probe/**']\n  workflow_dispatch:\n",
                    "on:\n  push:\n    branches: ['**']\n",
                    "on:\n  push:\n    branches: ['probe/**']\n    tags: ['v*']\n",
                    "on:\n  pull_request:\n"):
            with self.subTest(trigger=bad):
                found = self.gate_dir(self.probe_dir(text.replace(good, bad)), "probe/x")
                self.assertEqual(len(found), 1, found)
                self.assertIn("P9: expected on: push: branches: [probe/**] only", found[0])

    def test_OtherFileNamesStillRejectedOnProbeBranch(self) -> None:
        (v,) = run_case(WORKFLOW_FIXTURES / "p9_release_yml", branch="probe/x")
        self.assertEqual((v.rule, v.actual), ("P9", "release.yml"))

    def test_AllowedFileNamesDoNotIncludeProbes(self) -> None:
        self.assertNotIn("probe-*.yml", gate.ALLOWED_WORKFLOW_FILES)
        (v,) = gate.check_workflow(STRAY_PROBE / "probe-x.yml", policy=gate.load_policy())
        self.assertEqual((v.rule, v.expected), ("P9", "stray probe workflow: 0"))
        self.assertEqual(gate.allowed_files_for("probe/x"),
                         gate.ALLOWED_WORKFLOW_FILES + ("probe-*.yml",))
        self.assertEqual(gate.allowed_files_for("feature"), gate.ALLOWED_WORKFLOW_FILES)

    # -- branch detection -----------------------------------------------------
    def branch_with(self, env: Dict[str, str], git: Optional[str] = "local-branch"
                    ) -> Optional[str]:
        full = {"CI_REF": "", "CI_PLATFORM": ""}
        full.update(env)
        with mock.patch.dict(os.environ, full):
            return gate.current_branch(lambda: git)

    def test_BranchFromNeutralCiRef(self) -> None:
        cases = {"refs/heads/probe/x": "probe/x", "refs/heads/master": "master",
                 "refs/pull/3/merge": None, "refs/tags/probe/x": None, "refs/heads/": None,
                 "probe/x": None}
        for ref, want in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(self.branch_with({"CI_REF": ref, "CI_PLATFORM": "github"}), want)

    def test_CiRunWithoutRefIsStrict(self) -> None:
        self.assertIsNone(self.branch_with({"CI_PLATFORM": "github"}, git="probe/x"))

    def test_LocalRunAsksGit(self) -> None:
        self.assertEqual(self.branch_with({}, git="probe/x"), "probe/x")
        self.assertIsNone(self.branch_with({}, git=None))  # detached HEAD or no git

    def test_CliReadsTheBranchFromCiRef(self) -> None:
        case = self.probe_dir((STRAY_PROBE / "probe-x.yml").read_text(encoding="utf-8"))
        for ref, rc in (("refs/heads/probe/x", 0), ("refs/heads/master", 1),
                        ("refs/pull/9/merge", 1)):
            with self.subTest(ref=ref), mock.patch.dict(os.environ, {"CI_REF": ref}):
                code, lines = cli(["--no-scripts", "--workflows-dir", str(case)])
                self.assertEqual(code, rc, lines)
                self.assertEqual(len(lines), 1, lines)
                if rc:
                    self.assertIn("stray probe workflow: 0 -> actual 1", lines[0])

    # -- the shipped template ---------------------------------------------------
    def test_TemplatePassesEveryRuleOnAProbeBranch(self) -> None:
        text = PROBE_TEMPLATE.read_text(encoding="utf-8")
        case = self.probe_dir(text)
        self.assertEqual(self.gate_dir(case, "probe/template"), [])
        self.assertEqual([v.render() for v in gate.check_workflow(
            case / "probe-template.yml", gate.allowed_files_for("probe/template"),
            gate.load_policy())], [])

    def test_TemplateIsStrayOffAProbeBranch(self) -> None:
        found = self.gate_dir(self.probe_dir(PROBE_TEMPLATE.read_text(encoding="utf-8")),
                              "master")
        self.assertEqual(len(found), 1, found)
        self.assertIn("stray probe workflow", found[0])

    def test_TemplateMatrixIsThePinnedLabels(self) -> None:
        root = gate.load_yaml(PROBE_TEMPLATE.read_text(encoding="utf-8"))
        (job,) = root["jobs"].values()
        pins = json.loads(gate.PolicyFiles.pins.read_text(encoding="utf-8"))
        self.assertEqual(list(job["strategy"]["matrix"]["os"]), list(pins["runners"].values()))

    def test_TemplateHeaderAndProbeSteps(self) -> None:
        text = PROBE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("# Probe rules:", text)
        for n in range(1, 5):
            self.assertIn(f"#   {n}. ", text)
        self.assertEqual(text.count("# PROBE STEPS"), 1)
        self.assertNotIn("workflow_dispatch:", text)

    def test_TemplateSingleJobCallersAreCorrect(self) -> None:
        """Each commented single-job caller passes the gate and asks for a leg stage.py runs.

        On a ``probe/**`` push stage.py applies the branch-push tier, so a leg
        outside ``legs.json`` ``tiers.push_branch`` would exit 2.
        """
        legs = json.loads((CI_DIR / "legs.json").read_text(encoding="utf-8"))
        text = PROBE_TEMPLATE.read_text(encoding="utf-8")
        head = text[:text.index("\njobs:\n") + 1]
        examples = _template_job_examples()
        used = set()
        for block in examples:
            with self.subTest(block=block.split("\n")[1]):
                self.assertEqual(self.gate_dir(self.probe_dir(head + block), "probe/x"), [])
                (job_id, job), = gate.load_yaml(block)["jobs"].items()
                uses = str(job["uses"])
                used.add(uses)
                stage = gate.load_yaml((REPO_ROOT / uses).read_text(encoding="utf-8"))
                inputs = stage["on"]["workflow_call"]
                if "with" not in job:
                    self.assertFalse(isinstance(inputs, dict) and inputs.get("inputs"), uses)
                    continue
                self.assertIn("legs", inputs["inputs"])
                for leg in json.loads(str(job["with"]["legs"])):
                    self.assertIn(leg, legs["tiers"]["push_branch"])
                    self.assertEqual(legs["legs"][leg].get("job", "build"), job_id)
        # stage-package.yml has no probe caller: the package stage never runs on a
        # branch push (ADR 0005 D7, stage.stage_selected) and needs the
        # install-<leg> artifacts of the same run's build stage (0012 Story 03.0).
        no_probe = {"./.github/workflows/stage-package.yml"}
        self.assertEqual(used, {f"./.github/workflows/{p.name}" for p in
                                (REPO_ROOT / ".github" / "workflows").glob("stage-*.yml")}
                         - no_probe)


class Guards(unittest.TestCase):
    """The guard evaluator behind P8, P12 and P15."""

    def test_TierGuards(self) -> None:
        self.assertEqual(gate.reachable(["github.event_name == 'push' && "
                                         "startsWith(github.ref, 'refs/tags/v')"]), {"push-tag-v"})
        self.assertEqual(gate.reachable([f"{EXPR} github.event_name == 'push' && "
                                         "github.ref == 'refs/heads/master' }}"]), {"push-master"})

    def test_CacheSaveGuard(self) -> None:
        self.assertEqual(gate.reachable(["(github.event_name == 'push' && github.ref == "
                                         "'refs/heads/master') || github.event_name == 'schedule'"]),
                         {"push-master", "schedule"})

    def test_ComparisonsAreCaseInsensitiveLikeThePlatform(self) -> None:
        self.assertEqual(gate.reachable(["github.event_name == 'PUSH' && github.ref == "
                                         "'refs/heads/MASTER'"]), {"push-master"})

    def test_JobAndStepGuardsCombine(self) -> None:
        self.assertEqual(gate.reachable(["github.event_name == 'push'",
                                         "github.ref == 'refs/heads/master'"]), {"push-master"})

    def test_UnknownIsFailClosed(self) -> None:
        everything = set(gate.SCENARIOS)
        self.assertEqual(gate.reachable(["fromJSON(inputs.x)"]), everything)
        self.assertEqual(gate.reachable(["github['ref'] == 'x'"]), everything)
        self.assertEqual(gate.reachable([f"{EXPR} false }} && true"]), everything)
        self.assertEqual(gate.reachable([None]), everything)

    def test_UnknownOperandCannotWidenAFalseConjunction(self) -> None:
        self.assertEqual(gate.reachable(["inputs.go == 'yes' && github.event_name == 'push' && "
                                         "github.ref == 'refs/heads/master'"]), {"push-master"})


class YamlSubset(unittest.TestCase):
    """The stdlib-only loader: supported constructs load, others are rejected (fail closed)."""

    def test_BlockScalarsFlowAndComments(self) -> None:
        root = gate.load_yaml(
            "a: |\n  one\n  # not a comment\n\n  two\nb: >-\n  x\n  y\n"
            "c: [p, 'q r', {k: v}]\n# lead\nd: e # tail\nf:\n- 1\n- k: v\n  m: n\n"
            "g: \"quo\\\"ted\"\nh: 'it''s'\n")
        self.assertEqual(root["a"], "one\n# not a comment\n\ntwo\n")
        self.assertEqual(root["b"], "x y")
        self.assertEqual(root["c"], ["p", "q r", {"k": "v"}])
        self.assertEqual(root.key_comments["d"], ["# lead", "# tail"])
        self.assertEqual(root["d"].comment, "# tail")
        self.assertEqual(root["f"], ["1", {"k": "v", "m": "n"}])
        self.assertEqual((root["g"], root["h"]), ('quo"ted', "it's"))

    def test_RealCiWorkflowLoads(self) -> None:
        path = CI_DIR.parents[1] / ".github" / "workflows" / "ci.yml"
        if not path.exists():
            self.skipTest("no ci.yml")
        root = gate.load_yaml(path.read_text(encoding="utf-8"))
        self.assertIn("jobs", root)

    def test_UnsupportedConstructsAreRejected(self) -> None:
        for text in ("a: &x 1\nb: *x\n", "a: !!str 1\n", "a: 1\n---\nb: 2\n", "a:\n\t- b\n",
                     "a: one\n  two\n", "a: \"open\n", "a: 1\na: 2\n", "a: b: c\n",
                     "a: [1, 2\n", "%YAML 1.2\n---\na: 1\n"):
            with self.subTest(text=text):
                with self.assertRaises(gate.YamlSubsetError):
                    gate.load_yaml(text)

    def test_UnsupportedYamlIsAViolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ci.yml"
            path.write_text("on: push\nanchors: &a 1\n", encoding="utf-8")
            (v,) = gate.check_workflow(path, gate.ALLOWED_WORKFLOW_FILES, gate.load_policy())
            self.assertEqual((v.rule, v.location), ("yaml", "line 2"))

    def test_RunBlocksExtractsEveryRunBody(self) -> None:
        bodies = gate.run_blocks(GOOD / "stage-build.yml")
        self.assertEqual(bodies, ["python3 scripts/ci/stage.py build"])


if __name__ == "__main__":
    unittest.main()
