"""Tests for scripts/ci/vcpkg_registry_smoke.py (milestone 0012, Story 02.0 Task 03).

Classification: unit tests with subprocess mocked and temporary trees; the real
end-to-end install is a manual/CI smoke (it builds Boost on a cold cache).
On failure, first check: the registry-consumer files the driver writes
(vcpkg.json, vcpkg-configuration.json) and docs/dev/vcpkg-registry.md.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_test_support import CI_DIR  # noqa: F401  (puts scripts/ci on sys.path)

import consumer_smoke  # noqa: E402
import vcpkg_registry_smoke as smoke  # noqa: E402

SHA = "a" * 40


def make_repo(root: Path) -> None:
    (root / "vcpkg-configuration.json").write_text(json.dumps({
        "default-registry": {"kind": "git", "repository": "https://example.invalid/vcpkg",
                             "baseline": "b" * 40},
        "registries": []}), encoding="utf-8")


class WriteConsumerFilesTest(unittest.TestCase):
    def test_RegistryIsThisRepoAtTheBaselineAndOnlyForThePort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, consumer = Path(tmp) / "repo", Path(tmp) / "c"
            repo.mkdir()
            consumer.mkdir()
            make_repo(repo)
            smoke.write_consumer_files(consumer, repo, SHA)
            cfg = json.loads((consumer / "vcpkg-configuration.json").read_text())
            manifest = json.loads((consumer / "vcpkg.json").read_text())
        (registry,) = cfg["registries"]
        self.assertEqual((registry["kind"], registry["baseline"], registry["packages"]),
                         ("git", SHA, ["smbios-c"]))
        self.assertTrue(registry["repository"].startswith("file://"))
        self.assertEqual(cfg["default-registry"]["baseline"], "b" * 40)
        self.assertEqual(manifest["dependencies"], ["smbios-c"])
        self.assertNotIn("overrides", manifest)


class SymbolTest(unittest.TestCase):
    def test_OnlySmbiosSymbolsPass(self) -> None:
        smoke.check_symbols(["smbios_open", "smbios_close"])

    def test_StraySymbolFails(self) -> None:
        with self.assertRaises(consumer_smoke.SmokeError) as cm:
            smoke.check_symbols(["smbios_open", "_ZN6smbios4SMBiosC1Ev"])
        self.assertIn("1 exported symbol(s) outside smbios_*", str(cm.exception))

    def test_EmptyExportFails(self) -> None:
        with self.assertRaises(consumer_smoke.SmokeError):
            smoke.check_symbols([])

    def test_DylibLeadingUnderscoreIsStripped(self) -> None:
        with mock.patch.object(consumer_smoke, "_run", return_value="0000 T _smbios_open\n"):
            self.assertEqual(smoke.exported_symbols(Path("libsmbios_c.dylib")), ["smbios_open"])

    def test_WindowsDllIsNotInspected(self) -> None:
        self.assertIsNone(smoke.exported_symbols(Path("smbios_c.dll")))


class FindLibraryTest(unittest.TestCase):
    def test_FindsTheTripletLibrary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "x64-linux" / "lib" / "libsmbios_c.so"
            lib.parent.mkdir(parents=True)
            (Path(tmp) / "vcpkg").mkdir()
            lib.write_text("", encoding="utf-8")
            self.assertEqual(smoke.find_library(Path(tmp)), lib)

    def test_MissingLibraryIsAFailure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(consumer_smoke.SmokeError):
            smoke.find_library(Path(tmp))


class RunTest(unittest.TestCase):
    def test_MissingToolchainFailsBeforeAnyCommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run:
            with self.assertRaises(consumer_smoke.SmokeError):
                smoke.run(Path(tmp) / "w", vcpkg_root=Path(tmp), fixture=Path(tmp))
            run.assert_not_called()

    def test_MissingFixtureFailsBeforeAnyCommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run:
            with self.assertRaises(consumer_smoke.SmokeError):
                smoke.run(Path(tmp) / "w", vcpkg_root=Path(tmp), fixture=Path(tmp) / "nope")
            run.assert_not_called()

    def test_ConfigureUsesTheVcpkgToolchainAndNoOverlay(self) -> None:
        calls = []

        def fake(argv, **_kw):
            calls.append(list(argv))
            return mock.Mock(returncode=1, stdout="boom\n")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts" / "buildsystems").mkdir(parents=True)
            (root / "scripts" / "buildsystems" / "vcpkg.cmake").write_text("", encoding="utf-8")
            repo = root / "repo"
            repo.mkdir()
            make_repo(repo)
            fixture = root / "fixture"
            fixture.mkdir()
            with mock.patch.object(smoke, "head_commit", return_value=SHA), \
                    mock.patch("subprocess.run", side_effect=fake):
                with self.assertRaises(consumer_smoke.SmokeError) as cm:
                    smoke.run(root / "w", vcpkg_root=root, repo_root=repo, fixture=fixture,
                              triplet="x64-linux")
        (configure,) = calls
        joined = " ".join(configure)
        self.assertIn("-DCMAKE_TOOLCHAIN_FILE=", joined)
        self.assertIn("-DVCPKG_TARGET_TRIPLET=x64-linux", joined)
        self.assertNotIn("overlay", joined.lower())
        self.assertIn("configure vcpkg consumer failed", str(cm.exception))


class HeadCommitTest(unittest.TestCase):
    def test_RejectsNonShaOutput(self) -> None:
        with mock.patch.object(consumer_smoke, "_run", return_value="not-a-sha\n"):
            with self.assertRaises(consumer_smoke.SmokeError):
                smoke.head_commit(Path("."))

    def test_AcceptsFullSha(self) -> None:
        with mock.patch.object(consumer_smoke, "_run", return_value=SHA + "\n"):
            self.assertEqual(smoke.head_commit(Path(".")), SHA)


if __name__ == "__main__":
    unittest.main()
