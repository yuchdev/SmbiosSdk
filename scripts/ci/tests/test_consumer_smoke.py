"""Tests for scripts/ci/consumer_smoke.py (milestone 0012, Story 01.0 Task 03)."""
from __future__ import annotations

import stat
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


if __name__ == "__main__":
    unittest.main()
