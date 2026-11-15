"""Tests for the plan step (``plan.py`` / ``stage.py plan``; milestone 0014 Story 04.0 Task 02).

Classification: unit tests (data-file validation, ``parse_plan``) and
command-line tests of ``stage.py plan`` in a subprocess (GitHub reporter
writing to a temporary ``CI_OUTPUT_FILE``), plus data-sync tests of
``.github/workflows/ci.yml`` (triggers, concurrency, the ``plan`` job and how
the leg jobs consume its outputs). No network; the plan's git diff runs
against this repository's own checkout.

Scenarios: hostile ``CI_*`` input such as ``all\\nmatrix=...`` exits 2, is
never echoed and leaves no job output behind; every output value is one
line of JSON; a leg whose runner is unknown or missing from ``pins.json``
exits 2; the report's ``CI_PLAN`` read-back rejects anything unknown.

On failure, first check: ``scripts/ci/legs.json`` / ``pins.json`` (the data
the plan validates), then ``plan.read_inputs``/``plan.matrix_for``, then the
``plan`` job and the leg jobs' ``if:``/``with:`` in ``ci.yml``.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List
from unittest import mock

from ci_test_support import CI_DIR, neutral_env

import check_workflow_policy as gate  # noqa: E402
import plan  # noqa: E402
from reporters import get_reporter  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]
STAGE = CI_DIR / "stage.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
EXPR = "$" + "{{"
#: The heredoc entries Reporter.set_output writes (github backend).
_ENTRY = re.compile(r"^([a-z_]+)<<(ci_output_[0-9a-f]{32})\n(.*)\n\2$", re.M)


def run_plan_cli(tmp: Path, **env: str) -> subprocess.CompletedProcess:
    full = neutral_env(tmp, **{"CI_WORKSPACE": str(REPO_ROOT), "CI_PLATFORM": "github",
                               "CI_REPORTER": "github", "CI_OUTPUT_FILE": str(tmp / "out"),
                               **env})
    return subprocess.run([sys.executable, "-I", str(STAGE), "plan"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=full,
                          cwd=str(REPO_ROOT), timeout=60, text=True)


def outputs(tmp: Path) -> Dict[str, str]:
    path = tmp / "out"
    if not path.exists():
        return {}
    text = path.read_text(encoding="ascii")
    found = {m.group(1): m.group(3) for m in _ENTRY.finditer(text)}
    return found


class PlanCliTests(unittest.TestCase):
    """Type: command-line. Scenario: ``stage.py plan`` end to end. Boundary: hostile input."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-plan-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_EveryOutputIsOneJsonLine(self) -> None:
        """A tag with no base: deterministic whatever branch this checkout is on."""
        proc = run_plan_cli(self.tmp, CI_EVENT="tag", CI_REF="refs/tags/v9.8.7-secret",
                            CI_BASE_COMMIT="", CI_COMMIT="HEAD")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 2, proc.stdout)
        self.assertRegex(lines[0], r"^ok: changes: full \(no diff base\) ")
        self.assertRegex(lines[1], r"^ok: plan: tag, full: build linux,windows,macos \| ")
        found = outputs(self.tmp)
        self.assertEqual(sorted(found), sorted(plan.OUTPUTS))
        raw = (self.tmp / "out").read_text(encoding="ascii")
        self.assertEqual(raw.count("\n"), 3 * len(plan.OUTPUTS), "one value line per entry")
        for name, value in found.items():
            with self.subTest(output=name):
                self.assertNotIn("\r", value)
                self.assertNotIn("\n", value)
                json.loads(value)
        self.assertEqual(json.loads(found["build"]), ["linux", "windows", "macos"])
        self.assertEqual(json.loads(found["publish"]), [])
        whole = json.loads(found["plan"])
        self.assertEqual(whole["stages"]["publish"], plan.NOT_RUN_UNDEFINED)
        self.assertNotIn("secret", found["plan"])

    def test_SameRepoPrSkipsThePushTierLegs(self) -> None:
        proc = run_plan_cli(self.tmp, CI_EVENT="pr", CI_REF="refs/pull/12/merge",
                            CI_BASE_COMMIT="", CI_PR_SAME_REPO="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        found = outputs(self.tmp)
        self.assertEqual(json.loads(found["build"]), ["windows", "macos"])
        self.assertEqual(json.loads(found["sanitizers"]), ["macos-asan"])
        self.assertEqual(json.loads(found["plan"])["legs"]["linux"], plan.NOT_RUN_COVERED)
        self.assertIn("(3 covered by push run)", proc.stdout)

    def test_ForkPrRunsEveryLeg(self) -> None:
        for same in ("false", ""):
            with self.subTest(same=same):
                (self.tmp / "out").unlink(missing_ok=True)
                proc = run_plan_cli(self.tmp, CI_EVENT="pr", CI_REF="refs/pull/12/merge",
                                    CI_PR_SAME_REPO=same)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(outputs(self.tmp)["gui"]),
                                 ["linux-gui", "windows-gui", "macos-gui"])

    def test_HostileInputExits2WithoutEchoOrOutput(self) -> None:
        cases = [{"CI_EVENT": "all\nmatrix=[\"evil\"]", "CI_REF": "refs/heads/x"},
                 {"CI_EVENT": "manual", "CI_REF": "refs/heads/master"},
                 {"CI_EVENT": "workflow_dispatch", "CI_REF": "refs/heads/master"},
                 {"CI_EVENT": "", "CI_REF": "refs/heads/master"},
                 {"CI_EVENT": "push", "CI_REF": "refs/heads/x\nbuild=[\"evil\"]"},
                 {"CI_EVENT": "push", "CI_REF": "refs/heads/$(evil)"},
                 {"CI_EVENT": "push", "CI_REF": "refs/heads/x", "CI_BASE_COMMIT": "--evil"},
                 {"CI_EVENT": "push", "CI_REF": "refs/heads/x", "CI_BASE_COMMIT": "a\nevil"},
                 {"CI_EVENT": "push", "CI_REF": "refs/heads/x", "CI_COMMIT": "evil..x"},
                 {"CI_EVENT": "pr", "CI_REF": "refs/pull/1/merge", "CI_PR_SAME_REPO": "yes evil"},
                 {"CI_EVENT": "pr", "CI_REF": "refs/pull/1/merge",
                  "CI_PR_SAME_REPO": "true\nevil"}]
        for env in cases:
            with self.subTest(env=env):
                (self.tmp / "out").unlink(missing_ok=True)
                proc = run_plan_cli(self.tmp, **env)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("evil", proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertFalse((self.tmp / "out").exists(), "no partial job output")

    def test_FlagsAreRejected(self) -> None:
        for args in (["--leg", "linux"], ["--only", "x"], ["--job-failed"]):
            with self.subTest(args=args):
                env = neutral_env(self.tmp, CI_PLATFORM="local", CI_EVENT="push",
                                  CI_REF="refs/heads/x")
                proc = subprocess.run([sys.executable, "-I", str(STAGE), "plan", *args],
                                      env=env, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


def _data(edit_legs=None, edit_pins=None) -> plan.PlanData:
    base = plan.load_data()
    legs, pins = copy.deepcopy(dict(base.legs)), copy.deepcopy(dict(base.pins))
    if edit_legs:
        edit_legs(legs)
    if edit_pins:
        edit_pins(pins)
    return plan.PlanData(legs, pins, base.publish_jobs, base.defined_stages)


class DataValidationTests(unittest.TestCase):
    """Type: invalid input. Scenario: legs/pins data the plan must refuse (exit 2)."""

    def assertRefused(self, data: plan.PlanData) -> None:
        with self.assertRaises(plan.PlanError):
            plan.matrix_for("push", "refs/heads/master", "full", data=data)

    def test_UnknownRunnerKeyInLegsJson(self) -> None:
        self.assertRefused(_data(lambda d: d["legs"]["macos"].update(runner="solaris")))

    def test_LabelMissingFromPins(self) -> None:
        self.assertRefused(_data(edit_pins=lambda p: p["runners"].pop("windows")))

    def test_MalformedPinnedLabel(self) -> None:
        for label in ("ubuntu latest", "x\ny", "", "a;b"):
            with self.subTest(label=label):
                self.assertRefused(_data(edit_pins=lambda p: p["runners"].update(linux=label)))

    def test_UnknownOrPendingTierLeg(self) -> None:
        self.assertRefused(_data(lambda d: d["tiers"]["full"].append("solaris")))
        self.assertRefused(_data(lambda d: d["tiers"]["full"].append("linux-x64")))
        self.assertRefused(_data(lambda d: d["tiers"]["push_branch"].append("linux-x64")))
        self.assertRefused(_data(lambda d: d["legs"]["linux"].update(job="deploy")))
        self.assertRefused(_data(lambda d: d["tiers"].pop("push_branch")))

    def test_MalformedPublishEntry(self) -> None:
        base = plan.load_data()
        for entry in ({"job": "x"}, {"job": "x", "tier": "prod"}, {"job": "x\n", "tier": "release"}):
            with self.subTest(entry=entry):
                data = plan.PlanData(base.legs, base.pins, (entry,), base.defined_stages)
                self.assertRefused(data)

    def test_RunMapsDataErrorsToPlanError(self) -> None:
        env = {"CI_EVENT": "push", "CI_REF": "refs/heads/master", "CI_BASE_COMMIT": "",
               "CI_COMMIT": "HEAD", "CI_PR_SAME_REPO": ""}
        data = _data(edit_pins=lambda p: p["runners"].pop("macos"))
        written: List[str] = []
        reporter = get_reporter("plain")
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(reporter, "set_output", lambda n, v: written.append(n)):
            with self.assertRaises(plan.PlanError):
                plan.run(reporter, data=data, cwd=REPO_ROOT)
        self.assertEqual(written, [])

    def test_RunnersComeFromPins(self) -> None:
        data = plan.load_data()
        result = plan.matrix_for("schedule", "refs/heads/master", "full", data=data)
        self.assertTrue(set(result["runners"].values()) <= set(data.pins["runners"].values()))
        for leg, label in result["runners"].items():
            self.assertEqual(label, data.pins["runners"][data.legs["legs"][leg]["runner"]])


class ParsePlanTests(unittest.TestCase):
    """Type: unit + invalid input. Scenario: the report reads ``CI_PLAN`` back."""

    legs = plan.load_data().legs

    def text(self, **change) -> str:
        result = plan.matrix_for("pr", "refs/pull/1/merge", "full", pr_same_repo=True)
        result.update(change)
        return json.dumps(result, separators=(",", ":"))

    def test_RoundTrip(self) -> None:
        parsed = plan.parse_plan(self.text(), self.legs)
        self.assertEqual(parsed["legs"]["linux"], plan.NOT_RUN_COVERED)
        self.assertEqual(parsed["stages"]["publish"], plan.NOT_RUN_TIER)

    def test_HostilePlansAreRejected(self) -> None:
        good = json.loads(self.text())
        bad_legs = dict(good["legs"], linux="PASS")
        extra_leg = dict(good["legs"], evil="run")
        missing_leg = {k: v for k, v in good["legs"].items() if k != "linux"}
        cases = [self.text() + "\n", "x" * (plan.PLAN_MAX_BYTES + 1), "[]", "{",
                 self.text(schema=2), self.text(legs=bad_legs), self.text(legs=extra_leg),
                 self.text(legs=missing_leg), self.text(stages={"package": "run"}),
                 self.text(stages=dict(good["stages"], publish="yes"))]
        for value in cases:
            with self.subTest(value=value[:60]), self.assertRaises(plan.PlanError):
                plan.parse_plan(value, self.legs)


class CiWorkflowTests(unittest.TestCase):
    """Type: unit (data sync). Scenario: ci.yml triggers, concurrency and the plan wiring."""

    root = gate.load_yaml(CI_YML.read_text(encoding="utf-8"))

    def test_TriggersAreTheRuledSet(self) -> None:
        on = gate._triggers(self.root)
        self.assertEqual(sorted(on), ["pull_request", "push", "schedule"])
        self.assertEqual([str(b) for b in on["pull_request"]["branches"]], ["master"])
        self.assertEqual([str(b) for b in on["push"]["branches-ignore"]], ["probe/**"])
        self.assertEqual([str(t) for t in on["push"]["tags"]], ["v*"])
        for trigger in on.values():
            if isinstance(trigger, dict):
                self.assertFalse({"paths", "paths-ignore"} & set(trigger), "Q5: no path filter")

    def test_ConcurrencyNeverCancelsMasterOrTags(self) -> None:
        conc = self.root["concurrency"]
        self.assertEqual(str(conc["group"]), "ci-" + EXPR + " github.workflow }}-" + EXPR
                         + " github.event.pull_request.number || github.ref }}")
        expr = gate._expressions(str(conc["cancel-in-progress"]))[0]
        want = {"push-master": False, "schedule": False, "push-tag-v": False,
                "push-tag-legacy": False, "push-branch": True, "pull_request": True,
                "pull_request-from-master": True}
        for name, cancel in want.items():
            with self.subTest(scenario=name):
                value = gate._Evaluator(expr, gate.SCENARIOS[name]).run()
                self.assertIs(gate._truth(value), cancel)

    def test_PlanJob(self) -> None:
        job = self.root["jobs"]["plan"]
        self.assertNotIn("needs", job)
        self.assertEqual(str(job["runs-on"]), "ubuntu-24.04")
        self.assertEqual(str(job["timeout-minutes"]), "2")
        checkout = next(s for s in job["steps"] if "actions/checkout@" in str(s.get("uses")))
        self.assertEqual(str(checkout["with"]["persist-credentials"]), "false")
        self.assertEqual(str(checkout["with"]["fetch-depth"]), "0")
        self.assertEqual([str(s["run"]).strip() for s in job["steps"] if "run" in s],
                         ["python3 scripts/ci/stage.py plan"])
        step = next(s for s in job["steps"] if "run" in s)
        self.assertEqual({str(k): str(v) for k, v in job["outputs"].items()},
                         {name: f"{EXPR} steps.{step['id']}.outputs.{name} }}}}"
                          for name in plan.OUTPUTS})
        self.assertEqual(str(job["env"]["CI_PR_SAME_REPO"]),
                         EXPR + " github.event.pull_request.head.repo.full_name == "
                         "github.repository }}")

    def test_LegJobsConsumeThePlanAndSkipWhenEmpty(self) -> None:
        jobs = self.root["jobs"]
        self.assertNotIn("if", jobs["lint"], "lint always runs")
        for name in plan.LEG_JOBS:
            with self.subTest(job=name):
                job = jobs[name]
                self.assertEqual(sorted(str(n) for n in job["needs"]), ["lint", "plan"])
                self.assertEqual(str(job["if"]), f"needs.plan.outputs.{name} != '[]'")
                self.assertEqual(str(job["with"]["legs"]),
                                 f"{EXPR} needs.plan.outputs.{name} }}}}")

    def test_ReportReadsThePlan(self) -> None:
        job = self.root["jobs"]["report"]
        self.assertIn("plan", [str(n) for n in job["needs"]])
        self.assertEqual(str(job["env"]["CI_PLAN"]), f"{EXPR} needs.plan.outputs.plan }}}}")


if __name__ == "__main__":
    unittest.main()
