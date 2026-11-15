"""Tests for scripts/ci/consumer_smoke.py (milestone 0012, Story 01.0 Task 03)."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_test_support import CI_DIR  # noqa: F401  (puts scripts/ci on sys.path)

import consumer_smoke  # noqa: E402


class VcpkgPrefixesTest(unittest.TestCase):
    def test_ListsTripletDirsAndSkipsVcpkgMetadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vcpkg_installed"
            for name in ("x64-osx", "vcpkg"):
                (root / name).mkdir(parents=True)
            (root / "stray.txt").write_text("x", encoding="utf-8")
            self.assertEqual(consumer_smoke.vcpkg_prefixes(Path(tmp)), [root / "x64-osx"])

    def test_NoVcpkgTreeIsEmpty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(consumer_smoke.vcpkg_prefixes(Path(tmp)), [])


class FindExecutableTest(unittest.TestCase):
    def test_FindsSingleConfigBinary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "consumer"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(consumer_smoke.find_executable(Path(tmp), ""), exe)

    def test_FindsMultiConfigBinaryUnderTheConfig(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Debug" / "consumer.exe"
            exe.parent.mkdir()
            exe.write_text("", encoding="utf-8")
            self.assertEqual(consumer_smoke.find_executable(Path(tmp), "Debug"), exe)

    def test_MissingBinaryIsAFailure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(consumer_smoke.SmokeError):
            consumer_smoke.find_executable(Path(tmp), "")


class RunTest(unittest.TestCase):
    def test_MissingFixtureFailsBeforeAnyCommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run:
            with self.assertRaises(consumer_smoke.SmokeError):
                consumer_smoke.run(Path(tmp), Path(tmp) / "w", "", fixture=Path(tmp) / "nope")
            run.assert_not_called()

    def test_FailingPhaseNamesThePhaseAndKeepsATail(self) -> None:
        proc = mock.Mock(returncode=1, stdout="\n".join(f"line{i}" for i in range(40)))
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                consumer_smoke.run(Path(tmp), Path(tmp) / "w", "", fixture=Path(tmp))
            text = str(cm.exception)
            self.assertIn("install failed (exit 1)", text)
            self.assertIn("line39", text)
            self.assertNotIn("line0\n", text)

    def test_PhasesRunInOrderAndPrefixPathCarriesTheInstallPrefix(self) -> None:
        calls = []

        def fake(argv, **_kw):
            calls.append(list(argv))
            if argv[0] == "cmake" and "--build" in argv:
                build = Path(argv[argv.index("--build") + 1])
                exe = build / "consumer"
                build.mkdir(parents=True, exist_ok=True)
                exe.write_text("", encoding="utf-8")
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            return mock.Mock(returncode=0, stdout="formatters=4\n")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run", side_effect=fake):
            lines = consumer_smoke.run(Path(tmp), Path(tmp) / "w", "Debug", fixture=Path(tmp))
        self.assertEqual(lines, ["formatters=4"])
        install, configure, build, run = calls
        self.assertIn("--install", install)
        self.assertIn("--build", build)
        self.assertEqual(install[install.index("--config") + 1], "Debug")
        configure = calls[1]
        self.assertIn("-S", configure)
        prefix_arg = next(a for a in configure if a.startswith("-DCMAKE_PREFIX_PATH="))
        self.assertIn(str(Path(tmp) / "w" / "prefix"), prefix_arg)
        self.assertIn("-DCMAKE_BUILD_TYPE=Debug", configure)


class HardeningTest(unittest.TestCase):
    """CR-5 (environment strip), CR-4 (cleaned output), bounded time, config validation."""

    def _capture_kwargs(self):
        seen = {}

        def fake(argv, **kw):
            seen.update(kw)
            return mock.Mock(returncode=1, stdout="x\n")
        return seen, fake

    def test_ChildEnvironmentDropsDeniedNamesAndStdinIsClosed(self) -> None:
        seen, fake = self._capture_kwargs()
        env = {"PATH": os.environ.get("PATH", ""), "GITHUB_ENV": "/tmp/e",
               "ACTIONS_RUNTIME_TOKEN": "t", "CI_OUTPUT_FILE": "/tmp/o"}
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", side_effect=fake):
            with self.assertRaises(consumer_smoke.SmokeError):
                consumer_smoke._run("install", ["cmake"])
        for name in ("GITHUB_ENV", "ACTIONS_RUNTIME_TOKEN", "CI_OUTPUT_FILE"):
            self.assertNotIn(name, seen["env"])
        self.assertIn("PATH", seen["env"])
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        self.assertGreater(seen["timeout"], 0)

    def test_FailureTailIsCleanedOfControlAndBidiCharacters(self) -> None:
        dirty = "ok\x1b[31mred\x1b[0m\u202eevil\x07"
        proc = mock.Mock(returncode=1, stdout=dirty + "\n")
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                consumer_smoke._run("build consumer", ["cmake"])
        text = str(cm.exception)
        for bad in ("\x1b", "\u202e", "\x07"):
            self.assertNotIn(bad, text)
        self.assertIn("build consumer failed (exit 1)", text)

    def test_TimeoutIsReportedWithThePhaseName(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmake", 1)):
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                consumer_smoke._run("configure consumer", ["cmake"])
        self.assertIn("configure consumer timed out", str(cm.exception))

    def test_OptionLookingConfigIsRejectedBeforeAnyCommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run:
            for bad in ("--prefix", "Deb ug", "Debug;x", "1Debug"):
                with self.subTest(config=bad), self.assertRaises(consumer_smoke.SmokeError):
                    consumer_smoke.run(Path(tmp), Path(tmp) / "w", bad, fixture=Path(tmp))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
