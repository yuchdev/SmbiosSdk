"""Tests for the ``verify`` stage and its gate (milestone 0012 Story 04.0 Task 04).

Classification: unit + integration (in-process ``stage.main``) + mock (the
clean-room verifier, the host leg, vcpkg, vswhere and the GitHub API stub of
``test_publish_github_release``). Nothing here builds, downloads or publishes.

What is pinned, and where to look first on failure:

- :class:`ArtifactStepTests` - task criterion 1: a corrupted asset fails
  ``verify`` before anything is extracted (``publish_inputs.local_assets``);
  the manifest is anchored before parsing (constraint 4); ``install_sha256`` is
  compared with the build job's output (RP-4); a runner of another leg fails on
  CI; ``verified_<leg>`` is set only after every check passed and never on a
  partial (local, other-host) run. First check: ``stage.step_artifact``.
- :class:`GateTests` and :class:`FailingLegLeavesPublishRefusingTests` - the
  gate the publish stage waits on: a skipped, cancelled or failed leg (no
  output), a leg that verified another manifest, or an unknown leg yields no
  ``verified_manifest_sha256``, and publish then refuses; all three legs
  passing unblocks it. First check: ``stage.verify_gate``.
- :class:`WorkflowWiringTests` - the YAML half of the same guarantee:
  ``verify-gate`` needs ``verify`` with no ``if:`` (implicit ``success()``), is
  the only source of the workflow output, and ci.yml passes it unchanged.
- :class:`DepsTests`, :class:`ExportSymbolsTests`, :class:`MsvcEnvTests`,
  :class:`RegistryConsumerTests` - the helpers (Boost prefix, ``nm``/``dumpbin``
  allowlist, vcvars, the published-registry consumer).

Run: ``python3 scripts/ci/run_tests.py``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from ci_test_support import CI_DIR

import check_workflow_policy as gate  # noqa: E402
import export_symbols  # noqa: E402
import msvc_env  # noqa: E402
import stage  # noqa: E402
import vcpkg_registry_smoke as smoke  # noqa: E402
import verify_deps  # noqa: E402
import verify_release_artifact as vra  # noqa: E402
from test_publish_github_release import (COMMIT, LEGS, _FakeCase, make_dist,  # noqa: E402
                                         run_stage as run_publish_stage)

REPO_ROOT = CI_DIR.parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
INSTALL = "c" * 64  # make_dist's install_sha256
_OUTPUT = re.compile(r"^output: ([a-z0-9_]+)=(.*)$", re.MULTILINE)


def verify_env(tmp: Path, leg: str, anchor: str, **extra: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    env.update({"CI_PLATFORM": "github", "CI_REPORTER": "plain", "CI_EVENT": "pr",
                "CI_REF": "refs/pull/7/merge", "CI_REF_NAME": "7/merge", "CI_COMMIT": COMMIT,
                "CI_RUN_ID": "4242", "CI_RUN_ATTEMPT": "1", "CI_JOB": "verify", "CI_LEG": leg,
                "CI_TEMP_DIR": str(tmp), "CI_ARTIFACT_DIR": str(tmp),
                "CI_LOG_DIR": str(tmp / "logs"), "CI_RESULTS_FILE": str(tmp / "results.jsonl"),
                "CI_WORKSPACE": str(REPO_ROOT), "CI_MANIFEST_SHA256": anchor,
                "CI_INSTALL_SHA256": INSTALL})
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def run_verify(tmp: Path, args: List[str], leg: str, anchor: str,
               **extra: str) -> Tuple[int, str, Dict[str, str]]:
    """``stage.main(["verify", *args])`` in-process; ``(rc, output text, job outputs)``."""
    out = io.StringIO()
    cwd = os.getcwd()
    try:
        with mock.patch.dict(os.environ, verify_env(tmp, leg, anchor, **extra), clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = stage.main(["verify", *args])
    finally:
        os.chdir(cwd)
    text = out.getvalue()
    outputs = {m.group(1): json.loads(m.group(2)) for m in _OUTPUT.finditer(text)}
    return rc, text, outputs


class _DistCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-verify-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.anchor = make_dist(self.tmp)
        self.calls: List[Dict[str, Any]] = []

    def fake_run(self, fail: Optional[str] = None):
        def run(archive: Path, work: Path, **kw: Any) -> List[str]:
            self.calls.append(dict(kw, archive=archive, work=work))
            if fail:
                raise vra.VerifyError(fail + "\ntool tail line")
            return ["macos-arm64 1.0.1", "sha256 ok", "78 files, install_sha256 ok", "pkg-config ok",
                    "exports ok (12 smbios_* symbols)", "c: smbios_open=no-table",
                    "cxx: formatters=4"]
        return run

    def artifact(self, leg: str = "macos-arm64", host: Optional[str] = None,
                 fail: Optional[str] = None, deps: bool = True,
                 **extra: str) -> Tuple[int, str, Dict[str, str]]:
        if deps:
            verify_deps.installed_prefix(self.tmp, leg).mkdir(parents=True, exist_ok=True)
        with mock.patch.object(vra, "host_leg", return_value=host or leg), \
                mock.patch.object(vra, "run", side_effect=self.fake_run(fail)), \
                mock.patch.object(msvc_env, "developer_environment",
                                  return_value=dict(os.environ, INCLUDE="i", LIB="l",
                                                    PATH="C:\\VS\\bin;C:\\vcpkg")):
            return run_verify(self.tmp, ["--only", "artifact"], leg, self.anchor, **extra)


class ArtifactStepTests(_DistCase):
    """Type: integration (in-process stage) + invalid input. Task criterion 1, constraint 4, RP-4."""

    def test_PassingLegSetsItsVerifiedOutputLast(self) -> None:
        rc, text, outputs = self.artifact()
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs, {"verified_macos_arm64": self.anchor})
        self.assertIn(f"ok: verify macos-arm64: 7 files = manifest {self.anchor[:12]}; tree 78 "
                      "= install; pkg-config ok; exports 12 ok; c: no-table; "
                      "cxx: formatters=4 (", text)
        (call,) = self.calls
        self.assertTrue(call["build"])
        self.assertTrue(call["require_relocatable_pc"])
        self.assertEqual(call["dependency_prefixes"],
                         [verify_deps.installed_prefix(self.tmp, "macos-arm64")])
        self.assertEqual(call["archive"].name, "libbios-1.0.1-macos-arm64.tar.gz")

    def test_CorruptedAssetFailsBeforeAnythingIsExtracted(self) -> None:
        """Task criterion 1: a digest mismatch of ANY leg's file fails every leg's verify."""
        target = self.tmp / "dist" / "libbios-1.0.1-windows-x64.zip"
        data = bytearray(target.read_bytes())
        data[100] ^= 0x01
        target.write_bytes(bytes(data))
        rc, text, outputs = self.artifact()
        self.assertEqual(rc, 1, text)
        self.assertIn("libbios-1.0.1-windows-x64.zip sha256: expected", text)
        self.assertEqual(self.calls, [])  # never extracted
        self.assertEqual(outputs, {})

    def test_ArchiveAndManifestReplacedTogetherFailOnTheAnchor(self) -> None:
        """Constraint 4: a consistent substitution still fails, before the manifest is parsed."""
        make_dist(self.tmp, commit="d" * 40)  # new archives AND a matching new manifest
        import publish_inputs
        never = mock.Mock(loads=mock.Mock(side_effect=AssertionError("parsed before anchor")))
        with mock.patch.object(publish_inputs, "json", never):
            rc, text, outputs = self.artifact()
        self.assertEqual(rc, 1, text)
        self.assertIn(f"manifest.json sha256: expected {self.anchor[:16]} (manifest job output)",
                      text)
        self.assertEqual((self.calls, outputs), ([], {}))

    def test_InstallDigestMustEqualTheBuildJobOutput(self) -> None:
        rc, text, outputs = self.artifact(CI_INSTALL_SHA256="e" * 64)
        self.assertEqual(rc, 1, text)
        self.assertIn("install_sha256: expected eeeeeeeeeeeeeeee (build job output) -> actual "
                      "cccccccccccccccc (manifest)", text)
        self.assertEqual((self.calls, outputs), ([], {}))

    def test_CleanRoomFailureSetsNoOutput(self) -> None:
        rc, text, outputs = self.artifact(fail="cxx consumer failed (exit 1)")
        self.assertEqual(rc, 1, text)
        self.assertIn("cxx consumer failed (exit 1)", text)
        self.assertEqual(outputs, {})

    def test_RunnerOfAnotherLegFailsOnCI(self) -> None:
        rc, text, outputs = self.artifact(host="linux-x64")
        self.assertEqual(rc, 1, text)
        self.assertIn("runner: expected a macos-arm64 host -> actual linux-x64", text)
        self.assertEqual((self.calls, outputs), ([], {}))

    def test_LocalRunOnAnotherHostIsPartialAndSetsNoOutput(self) -> None:
        rc, text, outputs = self.artifact(host="macos-x64", deps=False, CI_PLATFORM="local",
                                          CI_EVENT="", CI_REF="", CI_REF_NAME="")
        self.assertEqual(rc, 0, text)
        self.assertFalse(self.calls[0]["build"])
        self.assertEqual(outputs, {})
        self.assertIn("no verify result is set (a partial verify never counts)", text)

    def test_RequiredInputsOnCI(self) -> None:
        for name, needle in (("CI_MANIFEST_SHA256", "CI_MANIFEST_SHA256 is required on CI"),
                             ("CI_INSTALL_SHA256", "CI_INSTALL_SHA256 is required on CI")):
            with self.subTest(name=name):
                rc, text, outputs = self.artifact(**{name: None})
                self.assertEqual(rc, 2, text)
                self.assertIn(needle, text)
                self.assertEqual(outputs, {})

    def test_MissingDependencyPrefixOnCIExits2(self) -> None:
        rc, text, _ = self.artifact(deps=False)
        self.assertEqual(rc, 2, text)
        self.assertIn("Boost dependency prefix missing", text)

    def test_WindowsLegGetsTheMsvcEnvironment(self) -> None:
        rc, text, outputs = self.artifact(leg="windows-x64")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.calls[0]["environ"].get("INCLUDE"), "i")
        # Ordering pin: the step hands the verifier the CAPTURED environment unfiltered
        # (vcvars output, vcpkg entry included); the verifier filters it (its own tests).
        self.assertIn("vcpkg", self.calls[0]["environ"].get("PATH", ""))
        self.assertEqual(outputs, {"verified_windows_x64": self.anchor})


class CompactSummaryTests(unittest.TestCase):
    """Type: unit + edge. The 160-character ``ok:`` line never hides a partial run."""

    def test_PartialAndFallbackNotesSurviveCompaction(self) -> None:
        for line in ("build/run NOT EXECUTED on macos-x64",
                     "exports NOT EXECUTED (a windows symbol tool is needed; host macos-x64)",
                     "pkg-config NOT EXECUTED (not on PATH)",
                     "pkg-config needs --define-prefix (non-relocatable .pc)"):
            with self.subTest(line=line):
                out = stage._compact_verify_line(line)
                self.assertTrue("NOT EXECUTED" in out or "--define-prefix" in out, out)

    def test_FullRunFitsTheSummaryCap(self) -> None:
        lines = ["clean room: PATH -2 (the vcpkg root)",
                 "78 files, install_sha256 ok", "pkg-config ok", "exports ok (13 smbios_* symbols)",
                 "c: smbios_open=ok structures=32",
                 "cxx: formatters=4 type17=Memory Device fields=15"]
        summary = (f"7 files = manifest {'a' * 12}; "
                   + "; ".join(stage._compact_verify_line(x) for x in lines))
        self.assertLessEqual(len("verify windows-x64: " + summary), 160, summary)


class GateTests(unittest.TestCase):
    """Type: unit + edge + invalid input. ``stage.verify_gate``: all legs or nothing."""

    A = "a" * 64

    def test_EveryLegVerifiedTheAnchor(self) -> None:
        self.assertEqual(stage.verify_gate(self.A, {leg: self.A for leg in LEGS}, LEGS), self.A)

    def test_AnyIncompleteOrForeignResultIsRefused(self) -> None:
        cases = (({"linux-x64": self.A, "macos-arm64": self.A, "windows-x64": ""},
                  "verify gate windows-x64: expected verified aaaaaaaaaaaaaaaa -> actual no "
                  "result (leg skipped, cancelled or failed)"),
                 ({"linux-x64": self.A, "macos-arm64": self.A},
                  "verify gate windows-x64"),
                 ({"linux-x64": self.A, "macos-arm64": "b" * 64, "windows-x64": self.A},
                  "verify gate macos-arm64: expected verified aaaaaaaaaaaaaaaa -> actual "
                  "bbbbbbbbbbbbbbbb"),
                 ({"linux-x64": self.A, "macos-arm64": "x\n", "windows-x64": self.A},
                  "actual <rejected>"),
                 (dict({leg: self.A for leg in LEGS}, **{"linux-arm64": self.A}),
                  "1 unknown leg(s)"),
                 ({}, "verify gate linux-x64"))
        for results, needle in cases:
            with self.subTest(results=results):
                with self.assertRaises(ValueError) as cm:
                    stage.verify_gate(self.A, results, LEGS)
                self.assertIn(needle, str(cm.exception))

    def test_MalformedAnchorIsAUsageError(self) -> None:
        with self.assertRaises(stage.UsageError):
            stage.verify_gate("", {leg: "" for leg in LEGS}, LEGS)


def gate_results(outputs: Dict[str, str]) -> str:
    """The ``CI_VERIFY_RESULTS`` value stage-verify.yml builds (a leg without output -> "")."""
    return json.dumps({leg: outputs.get(stage.verified_output_name(leg), "")
                       for leg in ("linux-x64", "windows-x64", "macos-arm64")})


class FailingLegLeavesPublishRefusingTests(_FakeCase):
    """Type: integration. The task's requirement (b), end to end through the real code.

    Each leg's ``artifact`` step runs (verifier mocked), the workflow's
    output plumbing is reproduced (:func:`gate_results`), the ``gate`` step
    runs, and its output - or its absence - is handed to ``stage.py publish``
    exactly as ci.yml does. The stub GitHub of ``test_publish_github_release``
    records every request.
    """

    def leg_outputs(self, failing: Tuple[str, ...]) -> Dict[str, str]:
        outputs: Dict[str, str] = {}
        for leg in LEGS:
            case = _DistCase()
            case.tmp, case.anchor, case.calls = self.tmp, self.anchor, []
            rc, text, out = case.artifact(leg=leg, fail="c consumer failed" if leg in failing
                                          else None)
            self.assertEqual(rc, 1 if leg in failing else 0, text)
            outputs.update(out)
        return outputs

    def gate(self, outputs: Dict[str, str]) -> Tuple[int, str]:
        rc, text, out = run_verify(self.tmp, ["--only", "gate"], "linux-x64", self.anchor,
                                   CI_VERIFY_RESULTS=gate_results(outputs))
        return rc, out.get("verified_manifest_sha256", "")

    def test_OneFailingLegLeavesPublishRefusing(self) -> None:
        outputs = self.leg_outputs(failing=("windows-x64",))
        self.assertEqual(sorted(outputs), ["verified_linux_x64", "verified_macos_arm64"])
        rc, verified = self.gate(outputs)
        self.assertEqual((rc, verified), (1, ""))
        run = run_publish_stage(self, ["--only", "draft"], CI_VERIFIED_MANIFEST_SHA256=verified)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("verify gate: expected verify passed for manifest", run.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_SkippedOrCancelledLegsLeavePublishRefusing(self) -> None:
        """A leg that never ran sets no output; the gate (if it ran at all) refuses."""
        outputs = {stage.verified_output_name("linux-x64"): self.anchor}
        self.assertEqual(self.gate(outputs), (1, ""))
        self.assertEqual(self.gate({}), (1, ""))

    def test_AllLegsPassingUnblocksPublish(self) -> None:
        """Positive control: the same path with every leg green creates the draft."""
        rc, verified = self.gate(self.leg_outputs(failing=()))
        self.assertEqual((rc, verified), (0, self.anchor))
        run = run_publish_stage(self, ["--only", "draft"], CI_VERIFIED_MANIFEST_SHA256=verified)
        self.assertEqual(run.rc, 0, run.text)
        self.assertEqual(len([r for r in self.fake.requests if r.method == "POST"
                              and r.path.endswith("/releases")]), 1)


class WorkflowWiringTests(unittest.TestCase):
    """Type: unit (workflow structure). The YAML half of the (b) guarantee."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.verify = gate.load_yaml((WORKFLOWS / "stage-verify.yml").read_text(encoding="utf-8"))
        cls.ci = gate.load_yaml((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))

    def test_GateJobRunsOnlyAfterEveryLegSucceeded(self) -> None:
        job = self.verify["jobs"]["verify-gate"]
        self.assertEqual(gate._needs(job), ["verify"])
        self.assertNotIn("if", job)  # implicit success(): no always()/failure()/cancelled()
        self.assertNotIn("continue-on-error", str(job))
        self.assertNotIn("continue-on-error", str(self.verify["jobs"]["verify"]))
        self.assertEqual(str(self.verify["jobs"]["verify"]["strategy"]["fail-fast"]), "false")

    def test_OnlyTheGateSetsTheWorkflowOutput(self) -> None:
        outputs = self.verify["on"]["workflow_call"]["outputs"]
        self.assertEqual(list(outputs), ["verified_manifest_sha256"])
        self.assertIn("jobs.verify-gate.outputs.verified_manifest_sha256",
                      str(outputs["verified_manifest_sha256"]["value"]))
        legs = json.loads((CI_DIR / "legs.json").read_text(encoding="utf-8"))["stages"]["package"]
        self.assertEqual(sorted(self.verify["jobs"]["verify"]["outputs"]),
                         sorted(stage.verified_output_name(leg) for leg in legs))
        results = str(self.verify["jobs"]["verify-gate"]["strategy"]["matrix"]["include"][0]
                      ["results"])
        for leg in legs:
            self.assertIn(f'"{leg}":', results)
            self.assertIn(f"needs.verify.outputs.{stage.verified_output_name(leg)} || '\"\"'",
                          results)

    def test_CiPassesTheGateOutputToPublishUnchanged(self) -> None:
        caller = self.ci["jobs"]["publish-release"]
        self.assertIn("verify", gate._needs(caller))
        self.assertNotRegex(str(caller.get("if")), r"always\(|failure\(|cancelled\(")
        value = str(caller["with"]["verified_manifest_sha256"])
        self.assertIn("fromJSON(needs.verify.outputs.verified_manifest_sha256)", value)
        self.assertNotIn("manifest_sha256)", value.replace("verified_manifest_sha256)", ""))
        verify = self.ci["jobs"]["verify"]
        self.assertEqual(sorted(gate._needs(verify)), ["build", "package", "plan"])

    def test_JobTimeoutsCoverTheirStepsAndTheColdBuildRuling(self) -> None:
        """Each job outlives its own steps; cold-build steps carry the 45-minute ruling."""
        publish = gate.load_yaml((WORKFLOWS / "stage-publish.yml").read_text(encoding="utf-8"))
        for root, job_id, cold in ((self.verify, "verify", ("deps", "deps-win")),
                                   (self.verify, "registry-consumer",
                                    ("registry", "registry-win")),
                                   (publish, "registry-prepare", ("prepare",))):
            with self.subTest(job=job_id):
                job = root["jobs"][job_id]
                steps = {str(st["id"]): int(str(st["timeout-minutes"])) for st in job["steps"]}
                one_leg = sum(v for k, v in steps.items() if not k.endswith("-win"))
                self.assertGreaterEqual(int(str(job["timeout-minutes"])), one_leg)
                for step in cold:
                    self.assertEqual(steps[step], 45)
        fast = {str(st["id"]): str(st["timeout-minutes"])
                for st in self.verify["jobs"]["verify"]["steps"]}
        self.assertEqual((fast["artifact"], fast["artifact-win"]), ("8", "8"))  # not inflated

    def test_NoCacheAndNoCredentialInVerify(self) -> None:
        text = (WORKFLOWS / "stage-verify.yml").read_text(encoding="utf-8")
        self.assertNotIn("actions/cache", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("environment:", text)


class DepsTests(unittest.TestCase):
    """Type: unit. ``verify_deps``: the Boost a consumer brings, never the archive."""

    def test_ManifestTripletAndArgv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            out = verify_deps.write_manifest(temp, REPO_ROOT)
            manifest = json.loads((out / "vcpkg.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["dependencies"], ["boost-iostreams"])
            cfg = json.loads((out / "vcpkg-configuration.json").read_text(encoding="utf-8"))
            root = json.loads((REPO_ROOT / "vcpkg-configuration.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["default-registry"], root["default-registry"])
            vcpkg = temp / "vcpkg-root"
            (vcpkg / "triplets").mkdir(parents=True)
            (vcpkg / "triplets" / "arm64-osx.cmake").write_text("set(VCPKG_TARGET_ARCHITECTURE "
                                                                "arm64)\n", encoding="utf-8")
            tdir = verify_deps.write_release_triplet(temp, vcpkg, "macos-arm64")
            self.assertTrue((tdir / "arm64-osx.cmake").read_text(encoding="utf-8").endswith(
                "set(VCPKG_BUILD_TYPE release)\n"))
            argv = verify_deps.install_argv(vcpkg / "vcpkg", temp, "macos-arm64")
            self.assertIn("--triplet=arm64-osx", argv)
            self.assertIn(f"--overlay-triplets={tdir}", argv)
            self.assertIn(f"--x-install-root={temp / 'verify-deps' / 'installed'}", argv)
            self.assertNotIn("--overlay-ports", " ".join(argv))
            with self.assertRaises(verify_deps.DepsError):
                verify_deps.write_release_triplet(temp, vcpkg, "linux-x64")  # no such triplet
            with self.assertRaises(verify_deps.DepsError):
                verify_deps.install_argv(vcpkg / "vcpkg", temp, "linux")

    def test_TripletsCoverExactlyThePackagingLegs(self) -> None:
        legs = json.loads((CI_DIR / "legs.json").read_text(encoding="utf-8"))["stages"]
        self.assertEqual(sorted(verify_deps.TRIPLETS), sorted(legs["package"]))
        self.assertEqual(legs["verify"], legs["package"])

    def test_LocalDepsStepSkips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, text, _ = run_verify(Path(tmp), ["--only", "deps", "--leg", "macos-arm64"],
                                     "macos-arm64", "a" * 64, CI_PLATFORM="local", CI_EVENT="",
                                     CI_REF="", CI_REF_NAME="")
        self.assertEqual(rc, 0, text)
        self.assertIn("skip: verify deps macos-arm64: local run uses the host's", text)


DUMPBIN = """Microsoft (R) COFF/PE Dumper Version 14.44.35207.1

Dump of file smbios_c.dll

File Type: DLL

  Section contains the following exports for smbios_c.dll

    00000000 characteristics
           2 number of functions
           2 number of names

    ordinal hint RVA      name

          1    0 00001000 smbios_close
          2    1 00001010 smbios_open
          3      00001020 [NONAME]

  Summary

        1000 .data
"""


class ExportSymbolsTests(unittest.TestCase):
    """Type: unit + invalid input. The ``nm``/``dumpbin`` export allowlist (ADR 0004)."""

    def test_ParseDumpbinAndNm(self) -> None:
        self.assertEqual(export_symbols.parse_dumpbin(DUMPBIN),
                         ["smbios_close", "smbios_open", "[NONAME]"])
        self.assertEqual(export_symbols.parse_nm("0000 T _smbios_open\n", ".dylib"),
                         ["smbios_open"])
        self.assertEqual(export_symbols.parse_nm("0000 T smbios_open\n0 A SMBIOS_C_1\n", ".so"),
                         ["smbios_open", "SMBIOS_C_1"])
        # GNU binutils >= 2.35 / llvm-nm print versioned ELF names (CI run 44253023273's .so).
        self.assertEqual(export_symbols.parse_nm(
            "0 A SMBIOS_C_1.0@@SMBIOS_C_1.0\n1 T smbios_open@@SMBIOS_C_1.0\n"
            "2 T leaked@SMBIOS_C_1.0\n", ".so"), ["SMBIOS_C_1.0", "smbios_open", "leaked"])
        with self.assertRaises(export_symbols.ExportError):
            export_symbols.check_symbols(["smbios_open", "leaked"])
        with self.assertRaises(export_symbols.ExportError):
            export_symbols.parse_dumpbin("Dump of file x.dll\n")

    def test_CheckSymbols(self) -> None:
        export_symbols.check_symbols(["smbios_open", "SMBIOS_C_1"])
        for names, needle in (([], "exports nothing"), (["smbios_open", "[NONAME]"], "1 exported"),
                              (["_ZN6smbios3fooEv"], "outside smbios_*")):
            with self.subTest(names=names), self.assertRaises(export_symbols.ExportError) as cm:
                export_symbols.check_symbols(names)
            self.assertIn(needle, str(cm.exception))

    def test_DllUsesDumpbinFromVswhereWhenNotOnPath(self) -> None:
        argvs: List[List[str]] = []

        def run(phase: str, argv: List[str]) -> str:
            argvs.append(list(argv))
            return r"C:\VS\VC\Tools\MSVC\14.44\bin\Hostx64\x64\dumpbin.exe" + "\n" \
                if phase == "vswhere" else DUMPBIN
        with mock.patch("os.path.isfile", return_value=True):
            names = export_symbols.exported_symbols(Path("smbios_c.dll"), run, {"PATH": ""},
                                                    vswhere_run=run)
        self.assertEqual(names[:2], ["smbios_close", "smbios_open"])
        self.assertEqual(argvs[1][1:3], ["/nologo", "/exports"])

    def test_LibraryKindAndShippedLibraryPath(self) -> None:
        self.assertEqual(export_symbols.library_kind(Path("libsmbios_c.so.1")), ".so")
        self.assertIsNone(export_symbols.library_kind(Path("smbios_c.lib")))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(export_symbols.ExportError) as cm:
                export_symbols.shared_library(Path(tmp), "windows")
            self.assertIn("bin/smbios_c.dll missing", str(cm.exception))


class MsvcEnvTests(unittest.TestCase):
    """Type: unit + invalid input. ``vcvars64.bat`` through vswhere (Windows verify leg)."""

    def test_ParseSetOutput(self) -> None:
        env = msvc_env.parse_set_output("=C:=C:\\x\nINCLUDE=a;b\nPath=C:\\bin\nnot a line\n")
        self.assertEqual(env, {"INCLUDE": "a;b", "Path": "C:\\bin"})

    def test_DeveloperEnvironment(self) -> None:
        commands: List[object] = []

        def run(command: object) -> str:
            commands.append(command)
            if isinstance(command, list):
                return "C:\\Program Files\\Microsoft Visual Studio\\2022\\Enterprise\n"
            return "INCLUDE=x\nLIB=y\nPATH=z\n"
        with mock.patch("os.path.isfile", return_value=True):
            env = msvc_env.developer_environment(run, base={"A": "1"})
        self.assertEqual(env, {"A": "1", "INCLUDE": "x", "LIB": "y", "PATH": "z"})
        self.assertIn('vcvars64.bat" >nul 2>&1 && set"', str(commands[1]))

    def test_VcvarsPathReplacesTheInheritedPathCaseBlind(self) -> None:
        """``set`` prints ``Path``; the inherited ``PATH`` must not survive beside it."""
        def run(command: object) -> str:
            if isinstance(command, list):
                return "C:\\VS\n"
            return "Path=C:\\VS\\bin;C:\\vcpkg\nINCLUDE=x\nLIB=y\n"
        with mock.patch("os.path.isfile", return_value=True):
            env = msvc_env.developer_environment(run, base={"PATH": "C:\\vcpkg;C:\\old"})
        self.assertEqual([k for k in env if k.upper() == "PATH"], ["PATH"])
        self.assertEqual(env["PATH"], "C:\\VS\\bin;C:\\vcpkg")  # vcvars' value, not the old one
        self.assertTrue(env["PATH"].startswith("C:\\VS"))  # what _which/find_dumpbin read

    def test_WindowsOrderingCaptureThenFilter(self) -> None:
        """CI run 44285978968: vcvars re-exports the image's C:\\vcpkg (and adds VS's own
        vcpkg); filtering must run on the captured environment, i.e. inside
        ``verify_release_artifact.run``, never before the capture."""
        captured_path = r"C:\VS\VC\Tools\bin;C:\VS\VC\vcpkg;C:\vcpkg;C:\Windows"

        def run(command: object) -> str:
            if isinstance(command, list):
                return "C:\\VS\n"
            return (f"Path={captured_path}\nINCLUDE=x\nLIB=y\n"
                    "VCPKG_ROOT=C:\\VS\\VC\\vcpkg\nVCPKG_INSTALLATION_ROOT=C:\\vcpkg\n")
        with mock.patch("os.path.isfile", return_value=True):
            env = msvc_env.developer_environment(run, base={"PATH": r"C:\vcpkg;C:\Windows"})
        # 1. The capture hands the vcpkg entries back (so filtering earlier would be undone).
        self.assertIn(r"C:\vcpkg", env["PATH"])
        # 2. The verifier filters what it is given: both vcpkg roots of the captured env.
        roots = [("this repository", Path(r"D:\a\SmbiosSdk\SmbiosSdk")),
                 ("the vcpkg root", Path(env["VCPKG_INSTALLATION_ROOT"])),
                 ("the vcpkg root", Path(env["VCPKG_ROOT"]))]
        removed: Dict[str, int] = {}
        child = vra.clean_environment(env, roots, removed, pathsep=";")
        self.assertEqual(child["PATH"], r"C:\VS\VC\Tools\bin;C:\Windows")
        self.assertEqual(removed, {"the vcpkg root": 2})
        self.assertNotIn("VCPKG_ROOT", child)

    def test_UnsafeOrMissingInstallationIsRefused(self) -> None:
        for out in ("", "C:\\VS & calc\n", "C:\\VS\"\n", "/usr/VS\n"):
            with self.subTest(out=out), mock.patch("os.path.isfile", return_value=True):
                with self.assertRaises(msvc_env.MsvcError):
                    msvc_env.developer_environment(lambda c: out, base={})
        with self.assertRaises(msvc_env.MsvcError):
            msvc_env.installation_path(lambda c: "", vswhere="/nonexistent/vswhere.exe")


class RegistryConsumerTests(unittest.TestCase):
    """Type: unit + invalid input. The nightly consumer of the published registry (D7)."""

    def test_PublishedRegistryUrlAndBaseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            consumer = Path(tmp)
            smoke.write_consumer_files(consumer, REPO_ROOT, COMMIT,
                                       "https://github.com/octo/libbios")
            cfg = json.loads((consumer / "vcpkg-configuration.json").read_text(encoding="utf-8"))
            (reg,) = cfg["registries"]
            self.assertEqual((reg["repository"], reg["baseline"], reg["packages"]),
                             ("https://github.com/octo/libbios", COMMIT, ["smbios-c"]))
            for url, base in (("file:///tmp/x", COMMIT), ("https://evil.example/x/y", COMMIT),
                              ("https://github.com/octo/libbios", "HEAD")):
                with self.subTest(url=url, base=base), self.assertRaises(smoke.SmokeError):
                    smoke.write_consumer_files(consumer, REPO_ROOT, base, url)

    def test_RegistryUrlComesFromTheRepositoryOrThePortfile(self) -> None:
        ctx = mock.Mock(local=True, workspace=REPO_ROOT)
        with mock.patch.dict(os.environ, {"CI_REPOSITORY": ""}):
            self.assertEqual(stage._registry_url(ctx), "https://github.com/yuchdev/SmbiosSdk")
        with mock.patch.dict(os.environ, {"CI_REPOSITORY": "octo/libbios"}):
            self.assertEqual(stage._registry_url(ctx), "https://github.com/octo/libbios")

    def test_NightlyStepsSkipOnOtherEvents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for only, needle in (("registry", "registry consumer linux-x64: nightly only"),
                                 ("releases", "release re-check: nightly only")):
                with self.subTest(only=only):
                    rc, text, _ = run_verify(Path(tmp), ["--only", only], "linux-x64", "a" * 64)
                    self.assertEqual(rc, 0, text)
                    self.assertIn("skip: " + needle, text)

    def test_ReleaseRecheckRefusesACredential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, text, _ = run_verify(Path(tmp), ["--only", "releases"], "linux-x64", "a" * 64,
                                     CI_EVENT="schedule", CI_REF="refs/heads/master",
                                     CI_REF_NAME="master", CI_REPOSITORY="octo/libbios",
                                     CI_PUBLISH_TOKEN="ghs_" + "x" * 36)
        self.assertEqual(rc, 1, text)
        self.assertIn("expected no credential (constraint 11", text)


if __name__ == "__main__":
    unittest.main()
