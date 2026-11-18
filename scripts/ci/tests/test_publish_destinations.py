"""Tests for the publish destination plug-in contract (milestone 0012 Story 04.0 Task 05; ADR 0005 D9).

Classification: unit + integration (in-process ``stage.py``, ``plan.py`` and the
policy gate against the real ``.github/workflows``). No network, no child
process, no real credential.

Scenarios:

- **Success criterion 1** - a fourth destination in an existing permission
  class needs no workflow edit: a fake ``repo-write`` and a fake ``oidc``
  destination are registered (registry entry + module only); ``plan.py`` puts
  them in their class matrix, the policy gate passes on the **unchanged**
  workflows, and ``stage.py`` runs the fake module through the D9 interface
  in its class job. Negative controls prove the test can fail: a class with no
  matrix job, an unmapped credential, and a child process in a ``repo-write``
  module each fail P12.
- **Success criterion 2** - ``--dry-run`` of every registered destination lists
  its exact files and keys with no network, no child process and no
  credential read (``destinations.Secret`` is never constructed).
- **Success criterion 3** - P12 fails on an unknown class, a missing module,
  and a ``release`` tier reachable from a non-tag event.
- The D9 interface check (``destinations.load``) and the job binding
  (``stage.check_job_binding``).

On failure first check: ``scripts/ci/destinations/__init__.py`` (interface),
``plan._publish_rows``/``class_matrices``, ``check_workflow_policy.check_publish_registry``
and ``stage.check_job_binding``.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from ci_test_support import CI_DIR, DESTINATIONS_DIR

import check_workflow_policy as gate  # noqa: E402
import destinations  # noqa: E402
import plan  # noqa: E402
import stage  # noqa: E402
from test_publish_github_release import (CANARY, LEAK_FORMS, TAG, _FakeCase,  # noqa: E402
                                         stage_env)

REPO_ROOT = CI_DIR.parents[1]
REAL_WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REGISTRY = CI_DIR / "publish_destinations.json"
JOBS = CI_DIR / "publish_jobs.json"

#: A fake single-phase destination module; ``{name}``/``{cls}`` are filled in.
FAKE_MODULE = '''"""Fake destination for test_publish_destinations (never shipped)."""
from destinations import Result
NAME = "{name}"
REQUIRED_ENV = ({creds}, "CI_RUN_ID")
CALLS = []
def validate(manifest, env):
    CALLS.append(("validate", env.dry_run))
def publish(manifest, artifact_dir, env, dry_run):
    CALLS.append(("publish", dry_run, sorted(env.credentials),
                  [type(v).__name__ for v in env.credentials.values()]))
    lines = tuple("info: dry run: would copy " + a.name for a in manifest.assets)
    return Result(str(len(manifest.assets)) + " files to the fake", (), lines, 0)
def check_published(manifest, env):
    return "checked"
{extra}
'''


def full(v: gate.Violation) -> str:
    """A violation with untruncated fields (``render()`` cuts them to the RC1 width)."""
    return f"{v.location}: {v.rule}: expected {v.expected} -> actual {v.actual}"


def fake_entry(name: str, tier: str, cls: str, creds: List[str], enabled: bool = True
               ) -> Dict[str, Any]:
    return {"name": name, "enabled": enabled, "tier": tier, "permission_class": cls,
            "environment": tier, "credentials": creds,
            "binary_cache_policy": gate.BINARY_CACHE_POLICY[tier]}


class _Fakes(unittest.TestCase):
    """A temp copy of the real registry and destination modules, plus fakes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-dest-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest_dir = self.tmp / "fakedest"
        shutil.copytree(DESTINATIONS_DIR, self.dest_dir,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.registry = self.tmp / "publish_destinations.json"
        shutil.copy(REGISTRY, self.registry)

    def add(self, entry: Dict[str, Any], extra: str = "") -> None:
        creds = ", ".join(json.dumps(c) for c in entry["credentials"]) or '"CI_COMMIT"'
        (self.dest_dir / (entry["name"].replace("-", "_") + ".py")).write_text(
            FAKE_MODULE.format(name=entry["name"], creds=creds, extra=extra), encoding="utf-8")
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["destinations"].append(entry)
        self.registry.write_text(json.dumps(data), encoding="utf-8")

    def policy(self) -> gate.Policy:
        return gate.load_policy(gate.PolicyFiles(publish_jobs=JOBS,
                                                 publish_destinations=self.registry,
                                                 destinations_dir=self.dest_dir))

    def gate(self) -> List[str]:
        workflows = sorted(REAL_WORKFLOWS.glob("*.yml"))
        found = gate.run_gate(workflows, policy=self.policy(), workflows_dir=REAL_WORKFLOWS,
                              scripts_dir=None)
        return [full(v) for v in found]

    def plan(self, event: str, ref: str) -> Dict[str, Any]:
        base = plan.load_data()
        data = plan.PlanData(base.legs, base.pins, base.publish_jobs, base.defined_stages,
                             tuple(json.loads(self.registry.read_text())["destinations"]))
        return plan.matrix_for(event, ref, "full", data=data)


# ---------------------------------------------------------------------------
# Success criterion 1: a fourth destination in an existing class, no workflow edit
# ---------------------------------------------------------------------------

class FakeDestinationRegistrationTests(_Fakes):
    """Type: integration. ADR 0005 D9 / SC5: a module + a registry entry, nothing else."""

    MIRROR = fake_entry("fake-mirror", "release", "repo-write", ["CI_PUBLISH_TOKEN"],
                        enabled=False)
    BUCKET = fake_entry("fake-bucket", "staging", "oidc",
                        ["CI_PUBLISH_S3_BUCKET", "CI_PUBLISH_S3_ROLE"])

    def setUp(self) -> None:
        super().setUp()
        self.workflow_bytes = {p.name: p.read_bytes() for p in REAL_WORKFLOWS.glob("*.yml")}
        self.add(self.MIRROR)
        self.add(self.BUCKET)

    def tearDown(self) -> None:
        after = {p.name: p.read_bytes() for p in REAL_WORKFLOWS.glob("*.yml")}
        self.assertEqual(after, self.workflow_bytes, "no workflow file may change")

    def test_PlanPutsEachFakeInItsClassMatrix(self) -> None:
        tag = self.plan("tag", "refs/tags/v1.0.1")
        self.assertEqual(tag["publish_repo_write"], ["fake-mirror"])
        self.assertEqual(tag["publish_oidc"], [])
        rows = [r for r in tag["publish"] if r["destination"] == "fake-mirror"]
        self.assertEqual(rows, [{"job": "publish-release-repo-write", "tier": "release",
                                 "class": "repo-write", "destination": "fake-mirror"}])
        # The phased destinations keep their D6 jobs; nothing else moves.
        self.assertEqual([r["job"] for r in tag["publish"] if r["destination"] != "fake-mirror"],
                         ["release-draft", "release-promote", "registry-prepare", "registry-pr"])
        master = self.plan("push", "refs/heads/master")
        self.assertEqual(master["publish_oidc"], ["fake-bucket"])
        self.assertEqual(master["publish_repo_write"], [])
        self.assertEqual([r["job"] for r in master["publish"]], ["publish-staging-oidc"])
        for event, ref in (("pr", "refs/pull/3/merge"), ("schedule", "refs/heads/master"),
                           ("push", "refs/heads/topic")):
            with self.subTest(event=event):
                result = self.plan(event, ref)
                self.assertEqual((result["publish"], result["publish_oidc"],
                                  result["publish_repo_write"]), ([], [], []))

    def test_PolicyGatePassesOnTheUnchangedWorkflows(self) -> None:
        self.assertEqual(self.gate(), [])

    def test_StageRunsTheFakeThroughTheInterfaceInItsClassJob(self) -> None:
        case = _StageCase(self, self.dest_dir)
        run = case.run(["publish"], CI_DESTINATION="fake-mirror",
                       CI_JOB="publish-release-repo-write", enabled=True)
        self.assertEqual(run["rc"], 0, run["text"])
        self.assertIn("ok: publish fake-mirror: 7 files to the fake", run["text"])
        module = sys.modules["fakedest.fake_mirror"]
        self.assertEqual(module.CALLS[0], ("validate", False))
        self.assertEqual(module.CALLS[1], ("publish", False, ["CI_PUBLISH_TOKEN"], ["Secret"]))
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, run["text"])

    def test_StageBindsTheFakeToItsClassJobOnly(self) -> None:
        case = _StageCase(self, self.dest_dir)
        for job, needle in (("release-draft", "expected the job publish_jobs.json assigns"),
                            ("publish-staging-oidc", "expected a release-tier repo-write job"),
                            ("post-publish", "expected a publish_jobs.json job"),
                            ("", "expected a publish_jobs.json job (CI_JOB) -> actual none")):
            with self.subTest(job=job):
                run = case.run(["publish"], CI_DESTINATION="fake-mirror", CI_JOB=job,
                               enabled=True)
                self.assertEqual(run["rc"], 1, run["text"])
                self.assertIn(needle, run["text"])

    def test_NegativeControlsFailP12(self) -> None:
        """The same registration fails when it would need a workflow edit or breaks RP-2."""
        for entry, extra, needle in (
                (fake_entry("fake-crates", "release", "token-only", ["CI_PUBLISH_CRATES"]), "",
                 "expected a release-tier token-only job in publish_jobs.json"),
                (fake_entry("fake-pypi", "release", "repo-write", ["CI_PUBLISH_PYPI"]), "",
                 "expected credentials mapped in publish-release-repo-write's publish step "
                 "-> actual CI_PUBLISH_PYPI"),
                (fake_entry("fake-shell", "release", "repo-write", ["CI_PUBLISH_TOKEN"]),
                 "import subprocess", "no child process in a repo-write destination (RP-2) -> "
                                      "actual fake_shell.py: subprocess")):
            with self.subTest(entry=entry["name"]):
                self.setUp()
                self.add(entry, extra)
                found = self.gate()
                self.assertTrue(any(needle in v for v in found), found)


class _StageCase:
    """Runs ``stage.main`` in-process against a fake destination package."""

    def __init__(self, test: _Fakes, package_dir: Path) -> None:
        self.test = test
        self.fake = _FakeCase("run")
        self.fake.setUp()
        test.addCleanup(self.fake.doCleanups)
        sys.path.insert(0, str(package_dir.parent))
        test.addCleanup(sys.path.remove, str(package_dir.parent))
        for mod in [m for m in sys.modules if m == "fakedest" or m.startswith("fakedest.")]:
            del sys.modules[mod]
        importlib.invalidate_caches()  # the package directory was written just now

    def run(self, argv: List[str], *, enabled: bool, **env: str) -> Dict[str, Any]:
        data = json.loads(self.test.registry.read_text())
        for entry in data["destinations"]:
            entry["enabled"] = enabled
        registry = self.test.tmp / "enabled.json"
        registry.write_text(json.dumps(data), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, stage_env(self.fake, **env), clear=True), \
                mock.patch.object(stage, "DESTINATIONS_FILE", registry), \
                mock.patch.object(stage, "DESTINATIONS_PACKAGE", "fakedest"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = stage.main(argv)
        return {"rc": rc, "text": out.getvalue()}


# ---------------------------------------------------------------------------
# Success criterion 2: --dry-run of every destination
# ---------------------------------------------------------------------------

class EveryDestinationDryRunTests(_FakeCase):
    """Type: integration. D9 ``--dry-run``: exact files and keys; no network, child or credential."""

    EXPECT = {"github-release": (r"would upload (\S+) \(\d+ bytes, sha256 [0-9a-f]{16}\)", 7),
              "vcpkg-registry": (r"would POST 3 blobs, a tree on the base tree, the commit", 1),
              "s3-staging": (r"would put-object dist/(\S+) \(\d+ bytes, sha256 [0-9a-f]{16}\) "
                             r"-> s3://<bucket>/<prefix>/1\.0\.1/\d+-\d+/\S+", 7)}

    def test_EveryRegisteredDestinationDryRuns(self) -> None:
        names = [e["name"] for e in json.loads(REGISTRY.read_text())["destinations"]]
        self.assertEqual(sorted(names), sorted(self.EXPECT), "a new destination needs a row here")

        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("network, child process or credential in a dry run")
        canaries = {"CI_PUBLISH_TOKEN": CANARY, "CI_PUBLISH_S3_BUCKET": CANARY + "b",
                    "CI_PUBLISH_S3_PREFIX": "staging/" + CANARY, "CI_PUBLISH_S3_ROLE": CANARY,
                    "CI_PUBLISH_S3_REGION": CANARY, "ACTIONS_ID_TOKEN_REQUEST_URL":
                    "https://x.actions.githubusercontent.com/" + CANARY,
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": CANARY}
        for name in names:
            with self.subTest(destination=name):
                out = io.StringIO()
                env = stage_env(self, CI_WORKSPACE=str(REPO_ROOT), **canaries)
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=boom), \
                        mock.patch.object(subprocess, "Popen", side_effect=boom), \
                        mock.patch.object(destinations, "Secret", side_effect=boom), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    rc = stage.main(["publish", "--destination", name, "--dry-run"])
                text = out.getvalue()
                self.assertEqual(rc, 0, text)
                pattern, count = self.EXPECT[name]
                self.assertEqual(len(re.findall(pattern, text)), count, text)
                self.assertIn("0 requests, credential unread", text)
                self.assertNotIn(CANARY, text)
                self.assertNotIn(CANARY[10:26], text)


# ---------------------------------------------------------------------------
# Success criterion 3: P12 on the registry
# ---------------------------------------------------------------------------

class RegistryPolicyTests(_Fakes):
    """Type: unit + invalid input. P12: unknown class, missing module, release tier off a tag."""

    def edit_registry(self, edit) -> None:
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        edit(data["destinations"])
        self.registry.write_text(json.dumps(data), encoding="utf-8")

    def test_RealRegistryPasses(self) -> None:
        self.assertEqual(self.gate(), [])

    def test_UnknownClass(self) -> None:
        self.edit_registry(lambda d: d[2].update(permission_class="admin"))
        found = self.gate()
        self.assertIn("P12: expected known permission class -> actual admin", "\n".join(found))

    def test_MissingModuleEvenWhenDisabled(self) -> None:
        (self.dest_dir / "s3_staging.py").unlink()
        found = self.gate()
        self.assertEqual(len(found), 1, found)
        self.assertIn("s3-staging: P12: expected existing module -> actual "
                      "destinations/s3_staging.py missing", found[0])

    def test_ReleaseTierReachableFromANonTagEvent(self) -> None:
        workflows = self.tmp / "workflows"
        shutil.copytree(REAL_WORKFLOWS, workflows)
        path = workflows / "stage-publish.yml"
        text = path.read_text(encoding="utf-8")
        guard = ("    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')\n"
                 "    runs-on: ubuntu-24.04\n    timeout-minutes: 10\n    environment: release\n"
                 "    # permissions: create the draft")
        self.assertEqual(text.count(guard), 1)
        path.write_text(text.replace(guard, guard.replace(
            "startsWith(github.ref, 'refs/tags/v')",
            "(startsWith(github.ref, 'refs/tags/v') || github.ref == 'refs/heads/master')")),
            encoding="utf-8")
        found = [full(v) for v in gate.run_gate(sorted(workflows.glob("*.yml")),
                                                   policy=self.policy(),
                                                   workflows_dir=workflows, scripts_dir=None)]
        p12 = [v for v in found if ": P12: " in v and "release destination reachable" in v]
        self.assertEqual(len(p12), 1, found)
        self.assertIn("github-release: P12: expected release destination reachable from v* tag "
                      "push only -> actual release-draft: reachable from push-master", p12[0])

    def test_BinaryCachePolicyMustMatchTheTier(self) -> None:
        self.edit_registry(lambda d: d[0].update(binary_cache_policy="any"))
        found = self.gate()
        self.assertEqual(len(found), 1, found)
        self.assertIn("expected binary_cache_policy none-only (release tier, CR-2) -> actual any",
                      found[0])

    def test_ChildProcessInARepoWriteModuleTransitively(self) -> None:
        (self.tmp / "evil_helper.py").write_text("import subprocess\n", encoding="utf-8")
        path = self.dest_dir / "github_release.py"
        path.write_text(path.read_text(encoding="utf-8") + "\nimport evil_helper\n",
                        encoding="utf-8")
        found = self.gate()
        self.assertTrue(any("github-release: P12: expected no child process in a repo-write "
                            "destination (RP-2) -> actual evil_helper.py: subprocess" in v
                            for v in found), found)

    def test_OidcModuleMayUseTheStepRunner(self) -> None:
        source = (self.dest_dir / "s3_staging.py").read_text(encoding="utf-8")
        self.assertIn("import ci_step", source)
        self.assertEqual(self.gate(), [])


# ---------------------------------------------------------------------------
# The interface check
# ---------------------------------------------------------------------------

class InterfaceTests(unittest.TestCase):
    """Type: unit + invalid input. ``destinations.load`` enforces D9 before stage.py uses a module."""

    def test_ShippedModulesImplementTheInterface(self) -> None:
        for name in ("github-release", "vcpkg-registry", "s3-staging"):
            with self.subTest(name=name):
                module = destinations.load(name)
                self.assertEqual(module.NAME, name)
                for member in destinations.INTERFACE:
                    self.assertTrue(hasattr(module, member))

    def test_BrokenModulesAreRefused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            pkg = Path(tmp_name) / "brokendest"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            body = ('NAME = "{n}"\nREQUIRED_ENV = ("CI_RUN_ID",)\n'
                    "def validate(m, e): pass\ndef publish(m, a, e, d): pass\n"
                    "def check_published(m, e): pass\n")
            cases = {"no-check": body.replace("def check_published(m, e): pass\n", ""),
                     "wrong-name": body.replace('"{n}"', '"other"'),
                     "bad-env": body.replace('("CI_RUN_ID",)', '("GITHUB_TOKEN",)'),
                     "bad-phases": body + 'PHASES = ("preflight",)\n',
                     "bad-unpriv": body + 'PHASES = ("a",)\nUNPRIVILEGED_PHASES = ("b",)\n'}
            for name, text in cases.items():
                (pkg / (name.replace("-", "_") + ".py")).write_text(text.format(n=name),
                                                                    encoding="utf-8")
            sys.path.insert(0, tmp_name)
            try:
                for name, needle in (("no-check", "member check_published"),
                                     ("wrong-name", "expected NAME"),
                                     ("bad-env", "REQUIRED_ENV"),
                                     ("bad-phases", "PHASES"), ("bad-unpriv", "PHASES"),
                                     ("missing", "actual missing"), ("Bad_Name", "<rejected>")):
                    with self.subTest(name=name):
                        with self.assertRaisesRegex(destinations.DestinationError, needle):
                            destinations.load(name, "brokendest")
            finally:
                sys.path.remove(tmp_name)
                for mod in [m for m in sys.modules if m.startswith("brokendest")]:
                    del sys.modules[mod]

    def test_SecretAndEnvNeverRender(self) -> None:
        secret = destinations.Secret(CANARY)
        env = destinations.Env(values={}, credentials={"CI_PUBLISH_TOKEN": secret},
                               oidc=("https://x", secret))
        for text in (repr(secret), str(secret), repr(env), str(env), str([secret])):
            self.assertNotIn(CANARY, text)


class JobBindingTests(_FakeCase):
    """Type: unit + invalid input. ``stage.check_job_binding`` (D9 defence in depth)."""

    def check(self, job: str, name: str, phase: Any) -> None:
        entry = next(e for e in json.loads(REGISTRY.read_text())["destinations"]
                     if e["name"] == name)
        with mock.patch.dict(os.environ, {"CI_JOB": job}):
            stage.check_job_binding(entry, name, phase, destinations.load(name))

    def test_BoundJobsAcceptTheirPhaseAndPreflightOnly(self) -> None:
        import publish_inputs as pin
        self.check("release-draft", "github-release", "draft")
        self.check("release-draft", "github-release", "preflight")
        self.check("registry-pr", "vcpkg-registry", "branch")
        self.check("publish-staging-oidc", "s3-staging", None)
        for job, name, phase in (("release-draft", "github-release", "promote"),
                                 ("release-promote", "github-release", "draft"),
                                 ("registry-pr", "github-release", "draft"),
                                 ("release-draft", "vcpkg-registry", "branch"),
                                 ("publish-release-repo-write", "github-release", "draft"),
                                 ("publish-release-repo-write", "s3-staging", None),
                                 ("registry-prepare", "vcpkg-registry", "prepare")):
            with self.subTest(job=job, name=name, phase=phase):
                with self.assertRaises(pin.PublishInputError):
                    self.check(job, name, phase)


if __name__ == "__main__":
    unittest.main()
