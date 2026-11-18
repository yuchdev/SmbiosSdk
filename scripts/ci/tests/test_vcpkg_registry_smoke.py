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

    def test_LinkerVersionNodeIsNotAStraySymbol(self) -> None:
        smoke.check_symbols(["smbios_open", "SMBIOS_C_1.0"])

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


class InstalledFilesTest(unittest.TestCase):
    """Installed-file allowlist (0012 Story 04.0 Task 03, carried forward from Story 02.0).

    Type: unit + invalid input. Scenario: vcpkg's own list file for the
    ``smbios-c`` package names exactly the allowlisted files; a new root
    ``install()`` rule (here the ``lib/libsmbios.dylib`` that the build from
    portfile ``70f268a`` really shipped) or a missing file fails with one line.
    On failure first check: ``vcpkg_installed/vcpkg/info/smbios-c_*.list`` of a
    real run against ``INSTALLED_COMMON``/``INSTALLED_LIBRARIES``.
    """

    def write_list(self, root: Path, files, triplet: str = "arm64-osx") -> Path:
        info = root / "vcpkg" / "info"
        info.mkdir(parents=True, exist_ok=True)
        dirs = sorted({f.rsplit("/", 1)[0] + "/" for f in files if "/" in f})
        lines = [f"{triplet}/"] + [f"{triplet}/{d}" for d in dirs] + \
            [f"{triplet}/{f}" for f in files]
        (info / f"smbios-c_1.0.1_{triplet}.list").write_text("\n".join(lines) + "\n")
        # A dependency's list file is not counted.
        (info / f"boost-test_1.88.0_{triplet}.list").write_text(f"{triplet}/include/x.hpp\n")
        return root

    def expected(self, suffix: str):
        return list(smoke.INSTALLED_COMMON) + list(smoke.INSTALLED_LIBRARIES[suffix])

    def test_ExactSetPasses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_list(Path(tmp), self.expected(".dylib"))
            self.assertEqual(smoke.check_installed_files(root, Path("libsmbios_c.dylib")), 10)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_list(Path(tmp), self.expected(".so"), "x64-linux")
            self.assertEqual(smoke.check_installed_files(root, Path("libsmbios_c.so")), 10)

    def test_StrayLibsmbiosDylibFails(self) -> None:
        """Regression: the package built from portfile 70f268a shipped lib/libsmbios.dylib."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_list(Path(tmp), self.expected(".dylib") + ["lib/libsmbios.dylib"])
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                smoke.check_installed_files(root, Path("libsmbios_c.dylib"))
        self.assertEqual(str(cm.exception), "installed files: expected the 10 allowlisted "
                                            "files -> actual 1 extra, 0 missing (first: "
                                            "+lib/libsmbios.dylib)")

    def test_MissingFileFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.write_list(Path(tmp), self.expected(".so")[1:], "x64-linux")
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                smoke.check_installed_files(root, Path("libsmbios_c.so"))
        self.assertIn("0 extra, 1 missing (first: -include/smbios/smbios.h)", str(cm.exception))

    def test_WindowsPdbsAreOptionalOnly(self) -> None:
        for extra, ok in (([], True), (["bin/smbios_c.pdb", "debug/bin/smbios_c.pdb"], True),
                          (["bin/smbios.dll"], False)):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = self.write_list(Path(tmp), self.expected(".dll") + extra, "x64-windows")
                if ok:
                    smoke.check_installed_files(root, Path("smbios_c.dll"))
                else:
                    with self.assertRaises(consumer_smoke.SmokeError):
                        smoke.check_installed_files(root, Path("smbios_c.dll"))

    def test_MissingOrMalformedListFileFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(consumer_smoke.SmokeError) as cm:
                smoke.check_installed_files(Path(tmp), Path("libsmbios_c.so"))
            self.assertIn("expected one smbios-c list file -> actual 0", str(cm.exception))
            root = self.write_list(Path(tmp), self.expected(".so"), "x64-linux")
            listfile = next((root / "vcpkg" / "info").glob("smbios-c_*.list"))
            listfile.write_text("other-triplet/lib/x.so\n")
            with self.assertRaises(consumer_smoke.SmokeError):
                smoke.check_installed_files(root, Path("libsmbios_c.so"))


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
