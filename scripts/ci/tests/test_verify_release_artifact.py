"""Tests for scripts/ci/verify_release_artifact.py (milestone 0012, Story 03.0 Task 03).

Synthetic archives only: an install-shaped tree is archived with
``package_archive`` (the package stage's own code), so the digests, sidecar
and manifest are real; every child process (pkg-config, the compiler, cmake,
the consumers) is a mocked ``subprocess.run``. No host toolchain, network or
firmware is touched.
"""
from __future__ import annotations

import io
import os
import re
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Dict, List
from unittest import mock

from ci_test_support import CI_DIR  # noqa: F401  (puts scripts/ci on sys.path)

import package_archive as pa  # noqa: E402
import verify_release_artifact as vra  # noqa: E402

LEG = "linux-x64"
VERSION = "1.2.3"


def make_tree(root: Path, os_name: str = "linux", skip: str = "") -> Path:
    """An install tree holding every required path of ``os_name`` (minus ``skip``)."""
    for rel in pa.REQUIRED[os_name]:
        if rel == skip or rel.startswith(skip + "/") and skip:
            continue
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode("utf-8"))
    return root


def make_release(tmp: Path, tree: Path = None, leg: str = LEG) -> Path:
    """Archive a tree into ``tmp/dist`` with its sidecar and manifest; return the archive."""
    tree = tree or make_tree(tmp / "tree", pa.leg_os(leg))
    dist = tmp / "dist"
    archive = pa.archive_tree(tree, dist / pa.archive_name(VERSION, leg), pa.archive_format(leg))
    sha = pa.sha256_file(archive)
    pa.write_sha256_file(archive, sha)
    digest, count = vra.tree_digest(tree)
    key = pa.PLATFORM_KEYS[pa.leg_os(leg)]
    pa.update_manifest(dist / pa.MANIFEST_NAME, leg, archive,
                       {"version": VERSION, "commit": "unknown", "config": "Release",
                        "install_sha256": digest, "files": str(count), "sha256": sha,
                        "arch": leg.split("-")[1], key: "2.38"})
    return archive


class ArchiveIdentityTest(unittest.TestCase):
    def test_ParsesVersionAndLeg(self) -> None:
        self.assertEqual(vra.parse_archive_name("libbios-1.0.0-macos-arm64.tar.gz"),
                         ("1.0.0", "macos-arm64"))
        self.assertEqual(vra.parse_archive_name("libbios-1.0.0-rc.1-windows-x64.zip"),
                         ("1.0.0-rc.1", "windows-x64"))

    def test_WrongExtensionForTheLegFails(self) -> None:
        for bad in ("libbios-1.0.0-windows-x64.tar.gz", "libbios-1.0.0-linux-x64.zip"):
            with self.subTest(name=bad), self.assertRaises(vra.VerifyError):
                vra.parse_archive_name(bad)

    def test_NonD4NameFails(self) -> None:
        for bad in ("smbios-1.0.0-linux-x64.tar.gz", "libbios-1.0.0-linux-riscv.tar.gz",
                    "libbios-1.0-linux-x64.tar.gz", "../libbios-1.0.0-linux-x64.tar.gz"):
            with self.subTest(name=bad), self.assertRaises(vra.VerifyError):
                vra.parse_archive_name(bad)

    def test_ResolvesTheLegFromDistAndManifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            self.assertEqual(vra.resolve_archive(archive.parent, LEG), archive)

    def test_ResolveWithoutAnEntryForTheLegFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            with self.assertRaises(vra.VerifyError) as cm:
                vra.resolve_archive(archive.parent, "windows-x64")
            self.assertIn("no entry for windows-x64", str(cm.exception))


class DigestTest(unittest.TestCase):
    """Check 5: the archive against its ``.sha256`` sidecar and its manifest entry."""

    def test_MatchingSidecarAndManifestPass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            entry = vra.verify_digests(archive, archive.parent / "manifest.json", VERSION, LEG)
            self.assertEqual(entry["file"], archive.name)

    def test_TamperedArchiveFailsAgainstTheSidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            with open(archive, "ab") as fh:
                fh.write(b"\0")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.verify_digests(archive, archive.parent / "manifest.json", VERSION, LEG)
            self.assertIn("differs from its .sha256", str(cm.exception))

    def test_ManifestDigestMismatchFailsEvenWhenTheSidecarAgrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            manifest = archive.parent / "manifest.json"
            text = manifest.read_text(encoding="utf-8")
            sha = pa.sha256_file(archive)
            manifest.write_text(text.replace(sha, "0" * 64), encoding="utf-8")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.verify_digests(archive, manifest, VERSION, LEG)
            self.assertIn("differs from manifest.json", str(cm.exception))

    def test_SidecarDigestMismatchFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            pa.write_sha256_file(archive, "f" * 64)
            with self.assertRaises(vra.VerifyError):
                vra.verify_digests(archive, archive.parent / "manifest.json", VERSION, LEG)

    def test_MissingOrForeignSidecarFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            sidecar = archive.with_name(archive.name + ".sha256")
            sidecar.write_text(f"{pa.sha256_file(archive)}  other.tar.gz\n", encoding="ascii")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.read_sidecar(archive)
            self.assertIn("names another file", str(cm.exception))
            sidecar.unlink()
            with self.assertRaises(vra.VerifyError) as cm:
                vra.read_sidecar(archive)
            self.assertIn("missing", str(cm.exception))

    def test_ManifestArchOfAnotherArchitectureFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            manifest = archive.parent / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace(
                '"arch": "x64"', '"arch": "arm64"'), encoding="utf-8")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.verify_digests(archive, manifest, VERSION, LEG)
            self.assertIn("records another architecture", str(cm.exception))

    def test_ManifestWithoutArchFromBeforeTheGuardIsAccepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            manifest = archive.parent / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace(
                '"arch": "x64",', ''), encoding="utf-8")
            self.assertNotIn("arch", manifest.read_text(encoding="utf-8"))
            vra.verify_digests(archive, manifest, VERSION, LEG)

    def test_MissingManifestOrOtherVersionFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            with self.assertRaises(vra.VerifyError):
                vra.verify_digests(archive, Path(tmp) / "nope.json", VERSION, LEG)
            with self.assertRaises(vra.VerifyError) as cm:
                vra.verify_digests(archive, archive.parent / "manifest.json", "9.9.9", LEG)
            self.assertIn("version differs", str(cm.exception))


class ExtractTest(unittest.TestCase):
    """Check 2: extraction into an empty scratch directory, untrusted-input hardening."""

    def _tar(self, path: Path, members: List[tarfile.TarInfo]) -> Path:
        with tarfile.open(path, "w:gz") as tar:
            for info in members:
                tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
        return path

    def test_ExtractsTarGzAndZipWithNormalisedModes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp) / "tree", "windows")
            (tree / "bin" / "smbios_c.dll").chmod(0o755)
            for leg in ("linux-x64", "windows-x64"):
                src = tree if leg.startswith("windows") else make_tree(Path(tmp) / "t2")
                archive = pa.archive_tree(src, Path(tmp) / pa.archive_name(VERSION, leg),
                                          pa.archive_format(leg))
                dest = Path(tmp) / f"x-{leg}"
                files = vra.extract_archive(archive, dest)
                self.assertEqual(files, len(pa.REQUIRED[pa.leg_os(leg)]))
                self.assertEqual(vra.tree_digest(dest), vra.tree_digest(src))
            dll = Path(tmp) / "x-windows-x64" / "bin" / "smbios_c.dll"
            self.assertEqual(stat.S_IMODE(dll.stat().st_mode), pa.EXEC_MODE)

    def test_TraversalAndAbsoluteMembersAreRefused(self) -> None:
        for name in ("../evil", "/abs/evil", "a/../../evil", "C:/evil"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                info = tarfile.TarInfo(name)
                info.size = 1
                archive = self._tar(Path(tmp) / "a.tar.gz", [info])
                with self.assertRaises(vra.VerifyError) as cm:
                    vra.extract_archive(archive, Path(tmp) / "out")
                self.assertIn("refused", str(cm.exception))
                self.assertFalse((Path(tmp).parent / "evil").exists())

    def test_SymlinkMembersAreRefusedInTarAndZip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link = tarfile.TarInfo("lib/libsmbios_c.so")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive = self._tar(Path(tmp) / "a.tar.gz", [link])
            with self.assertRaises(vra.VerifyError):
                vra.extract_archive(archive, Path(tmp) / "out")
            zpath = Path(tmp) / "a.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                info = zipfile.ZipInfo("lib/link")
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "/etc/passwd")
            with self.assertRaises(vra.VerifyError):
                vra.extract_archive(zpath, Path(tmp) / "out2")

    def test_ExistingDestinationIsRefused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            (Path(tmp) / "out").mkdir()
            with self.assertRaises(vra.VerifyError):
                vra.extract_archive(archive, Path(tmp) / "out")

    def test_CorruptArchiveIsAValueFreeFailure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "libbios-1.0.0-linux-x64.tar.gz"
            bad.write_bytes(b"not a gzip")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.extract_archive(bad, Path(tmp) / "out")
            self.assertIn("unreadable", str(cm.exception))


class LayoutTest(unittest.TestCase):
    """Required paths named on failure; the extracted tree is the tested tree."""

    def _entry(self, tree: Path) -> Dict[str, object]:
        digest, count = vra.tree_digest(tree)
        return {"files": count, "install_sha256": digest}

    def test_MissingPkgConfigFileFailsNamingThePath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), skip="lib/pkgconfig/smbios_c.pc")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.check_layout(tree, LEG, self._entry(tree))
            self.assertIn("lib/pkgconfig/smbios_c.pc", str(cm.exception))

    def test_MissingCMakePackageDirFailsNamingThePath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp), skip="lib/cmake/libsmbios")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.check_layout(tree, LEG, self._entry(tree))
            self.assertIn("lib/cmake/libsmbios/", str(cm.exception))

    def test_ShippedCliFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp))
            (tree / "bin").mkdir()
            (tree / "bin" / "smbios").write_bytes(b"x")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.check_layout(tree, LEG, self._entry(tree))
            self.assertIn("bin/smbios", str(cm.exception))

    def test_TreeDifferentFromTheTestedInstallTreeFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp))
            entry = self._entry(tree)
            (tree / "include" / "smbios" / "smbios.h").write_bytes(b"changed")
            with self.assertRaises(vra.VerifyError) as cm:
                vra.check_layout(tree, LEG, entry)
            self.assertIn("install_sha256", str(cm.exception))
            entry = self._entry(tree)
            entry["files"] = int(entry["files"]) + 1
            with self.assertRaises(vra.VerifyError) as cm:
                vra.check_layout(tree, LEG, entry)
            self.assertIn("file count", str(cm.exception))

    def test_TreeDigestEqualsTheStageDigest(self) -> None:
        import stage
        with tempfile.TemporaryDirectory() as tmp:
            tree = make_tree(Path(tmp))
            self.assertEqual(vra.tree_digest(tree), stage.tree_digest(tree))


class CleanRoomTest(unittest.TestCase):
    """The clean room is enforced: scrubbed names, then asserted leak-free."""

    REPO = Path("/work/SmbiosSdk")
    VCPKG = Path("/home/u/.vcpkg")

    def roots(self):
        return vra.forbidden_roots(self.REPO, {"VCPKG_ROOT": str(self.VCPKG)})

    def test_ScrubsSearchPathsAndPackageHints(self) -> None:
        environ = {"PATH": "/usr/bin", "HOME": "/home/u",
                   "VCPKG_ROOT": str(self.VCPKG), "PKG_CONFIG_PATH": "/x",
                   "PKG_CONFIG_LIBDIR": "/x", "CMAKE_PREFIX_PATH": "/x",
                   "CMAKE_TOOLCHAIN_FILE": "/x", "CPATH": "/x", "LIBRARY_PATH": "/x",
                   "C_INCLUDE_PATH": "/x", "LD_LIBRARY_PATH": "/x", "DYLD_LIBRARY_PATH": "/x",
                   "CFLAGS": "-I/x", "LDFLAGS": "-L/x", "BOOST_ROOT": "/x",
                   "libsmbios_DIR": "/x", "Boost_DIR": "/x", "SOME_TOOL_HOME": "/x",
                   "ACTIONS_RUNTIME_TOKEN": "t"}
        env = vra.clean_environment(environ, self.roots())
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/u"})

    def test_LeakedRepoPathInAnAllowedVariableIsDetected(self) -> None:
        for name, value in (("PATH", f"/usr/bin{os.pathsep}{self.REPO}/build/macos/bin"),
                            ("CC", f"{self.REPO}/build/cc"),
                            ("INCLUDE", f"{self.VCPKG}/installed/x64-windows/include")):
            with self.subTest(name=name), self.assertRaises(vra.VerifyError) as cm:
                vra.clean_environment({"PATH": "/usr/bin", name: value}, self.roots())
            text = str(cm.exception)
            self.assertIn(f"environment variable {name} points into", text)
            self.assertNotIn(value, text)

    def test_LeakedPathUnderAnUnlistedNameNeverReachesTheChild(self) -> None:
        env = vra.clean_environment({"PATH": "/usr/bin", "MY_SDK": str(self.REPO)}, self.roots())
        self.assertNotIn("MY_SDK", env)

    def test_LeakMatchingIsBoundaryAnchoredAndSeparatorAndCaseBlind(self) -> None:
        roots = self.roots()
        self.assertEqual(vra.find_leak(f"-I{self.REPO}/smbios_c/include", roots),
                         "this repository")
        self.assertEqual(vra.find_leak(f"-I{self.REPO}", roots), "this repository")
        self.assertEqual(vra.find_leak("-I\\WORK\\smbiossdk\\include", roots), "this repository")
        self.assertEqual(vra.find_leak(f"{self.VCPKG}/installed", roots), "the vcpkg root")
        self.assertIsNone(vra.find_leak("/work/SmbiosSdk2/include /work/other", roots))

    def test_FilesystemRootIsNeverAForbiddenRoot(self) -> None:
        roots = vra.forbidden_roots(self.REPO, {"VCPKG_ROOT": "/"})
        self.assertTrue(all(str(p) != "/" for _l, p in roots))
        self.assertIsNone(vra.find_leak("/usr/include", roots))

    def test_PkgConfigOutputPointingIntoTheRepoFails(self) -> None:
        out = f"-I{self.REPO}/smbios_c/include -L{self.REPO}/build -lsmbios_c\n"
        with mock.patch.object(vra, "_which", return_value="/usr/bin/pkg-config"), \
                mock.patch("subprocess.run",
                           return_value=mock.Mock(returncode=0, stdout=out)) as run:
            with self.assertRaises(vra.VerifyError) as cm:
                vra.resolve_pkg_config(Path("/scratch/prefix"), {"PATH": "/usr/bin"},
                                       self.roots(), False)
        self.assertIn("pkg-config output references this repository", str(cm.exception))
        self.assertEqual(run.call_count, 1)  # caught on the literal ADR command itself

    def test_CompileCommandLineIntoTheRepoFails(self) -> None:
        with self.assertRaises(vra.VerifyError) as cm:
            vra.assert_no_leak("the C compile/link command line",
                               f"cc main.c -I{self.REPO}/include", self.roots())
        self.assertIn("command line references this repository", str(cm.exception))


class PkgConfigTest(unittest.TestCase):
    PREFIX = Path("/scratch/prefix")

    def _fake(self, outputs: List[str], calls: List[dict]):
        it = iter(outputs)

        def fake(argv, **kw):
            calls.append({"argv": list(argv), "env": kw["env"]})
            return mock.Mock(returncode=0, stdout=next(it))
        return fake

    def test_RelocatablePcResolvesWithTheLiteralCommandOnly(self) -> None:
        calls: List[dict] = []
        out = f"-I{self.PREFIX}/include -L{self.PREFIX}/lib -lsmbios_c\n"
        environ = {"PATH": "/usr/bin"}
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake([out], calls)):
            incs, libdirs, libs, mode = vra.resolve_pkg_config(self.PREFIX, environ, [], False)
        self.assertEqual(mode, "relocatable")
        self.assertEqual((incs, libdirs, libs),
                         ([f"{self.PREFIX}/include"], [f"{self.PREFIX}/lib"], ["smbios_c"]))
        self.assertEqual(calls[0]["argv"], ["pkg-config", "--cflags", "--libs", "smbios_c"])
        self.assertEqual(calls[0]["env"]["PKG_CONFIG_LIBDIR"],
                         str(self.PREFIX / "lib" / "pkgconfig"))
        self.assertNotIn("PKG_CONFIG_PATH", calls[0]["env"])

    def test_PcfiledirRelativePrefixResolvesWithTheLiteralCommand(self) -> None:
        """H1 fix: ``prefix=${pcfiledir}/../..`` yields ``<prefix>/lib/pkgconfig/../../...``."""
        calls: List[dict] = []
        rel = f"{self.PREFIX}/lib/pkgconfig/../.."
        out = f"-I{rel}/include -L{rel}/lib -lsmbios_c\n"
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake([out], calls)):
            *_rest, mode = vra.resolve_pkg_config(self.PREFIX, {}, [], True)
        self.assertEqual(mode, "relocatable")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--define-prefix", calls[0]["argv"])

    def test_NonRelocatablePcFallsBackToDefinePrefixAndSaysSo(self) -> None:
        calls: List[dict] = []
        outs = ["-I/usr/local/include -L/usr/local/lib -lsmbios_c\n",
                f"-I{self.PREFIX}/include -L{self.PREFIX}/lib -lsmbios_c\n"]
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake(outs, calls)):
            *_rest, mode = vra.resolve_pkg_config(self.PREFIX, {}, [], False)
        self.assertEqual(mode, "define-prefix")
        self.assertIn("--define-prefix", calls[1]["argv"])

    def test_SpacedAbsolutePrefixAlsoFallsBack(self) -> None:
        calls: List[dict] = []
        outs = ["-IC:/Program Files (x86)/smbios_sdk/include "
                "-LC:/Program Files (x86)/smbios_sdk/lib -lsmbios_c\n",
                f"-I{self.PREFIX}/include -L{self.PREFIX}/lib -lsmbios_c\n"]
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake(outs, calls)):
            *_rest, mode = vra.resolve_pkg_config(self.PREFIX, {}, [], False)
        self.assertEqual(mode, "define-prefix")

    def test_RequireRelocatableFailsOnANonRelocatablePc(self) -> None:
        calls: List[dict] = []
        outs = ["-I/usr/local/include -L/usr/local/lib -lsmbios_c\n"]
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake(outs, calls)):
            with self.assertRaises(vra.VerifyError) as cm:
                vra.resolve_pkg_config(self.PREFIX, {}, [], True)
        self.assertIn("not relocatable", str(cm.exception))
        self.assertEqual(len(calls), 1)

    def test_OutsideTheArchiveEvenWithDefinePrefixFails(self) -> None:
        calls: List[dict] = []
        outs = ["-I/usr/local/include -L/usr/local/lib -lsmbios_c\n"] * 2
        with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                mock.patch("subprocess.run", side_effect=self._fake(outs, calls)):
            with self.assertRaises(vra.VerifyError):
                vra.resolve_pkg_config(self.PREFIX, {}, [], False)

    def test_UnexpectedFlagOrMissingLibraryIsRefused(self) -> None:
        for out in (f"-I{self.PREFIX}/include -L{self.PREFIX}/lib -lsmbios_c -Wl,-rpath,/evil",
                    f"-I{self.PREFIX}/include -L{self.PREFIX}/lib -lother"):
            with self.subTest(out=out), self.assertRaises(vra.VerifyError):
                vra.parse_pkg_config(out, self.PREFIX)

    def test_MissingPkgConfigToolFails(self) -> None:
        with mock.patch.object(vra, "_which", return_value=None), \
                mock.patch("subprocess.run") as run, self.assertRaises(vra.VerifyError):
            vra.resolve_pkg_config(self.PREFIX, {}, [], False)
        run.assert_not_called()


class RelocatablePcSourceTest(unittest.TestCase):
    """H1 (resolved 2026-11-17): the installed ``smbios_c.pc`` derives its prefix from its own path."""

    def test_PcTemplatePrefixIsPcfiledirRelative(self) -> None:
        text = (vra.REPO_ROOT / "smbios_c" / "smbios_c.pc.in").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0], "prefix=${pcfiledir}/../..")
        self.assertNotIn("CMAKE_INSTALL_PREFIX", text)

    def test_PcTemplateIsConfiguredWithAtOnly(self) -> None:
        """Without @ONLY, CMake expands ${pcfiledir} to empty and writes ``prefix=/../..``."""
        cmake = (vra.REPO_ROOT / "smbios_c" / "CMakeLists.txt").read_text(encoding="utf-8")
        match = re.search(r"configure_file\(\s*[^)]*smbios_c\.pc\.in[^)]*\)", cmake)
        self.assertIsNotNone(match)
        self.assertIn("@ONLY", match.group(0))


class CConsumerTest(unittest.TestCase):
    """Check 3 (build half) with the reused ``smbios_c_vcpkg_consumer/main.c``."""

    def test_GnuCommandUsesOnlyThePkgConfigResultPlusRpath(self) -> None:
        with mock.patch("platform.system", return_value="Linux"):
            argv = vra.c_compile_argv("cc", Path("/s/main.c"), Path("/s/c"), ["/p/include"],
                                      ["/p/lib"], ["smbios_c"])
        self.assertEqual(argv, ["cc", "/s/main.c", "-o", "/s/c", "-I/p/include", "-L/p/lib",
                                "-lsmbios_c", "-Wl,-rpath,/p/lib"])

    def test_MsvcCommandIsTranslated(self) -> None:
        argv = vra.c_compile_argv("cl.exe", Path("m.c"), Path("c.exe"), ["P/include"],
                                  ["P/lib"], ["smbios_c"])
        self.assertEqual(argv, ["cl.exe", "/nologo", "m.c", "/IP/include", "/Fec.exe", "/link",
                                "/LIBPATH:P/lib", "smbios_c.lib"])

    def _smoke(self, tmp: Path, run_output: str, rc: int = 0) -> str:
        def fake(argv, **_kw):
            if argv[0] == "cc":
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=rc, stdout=run_output)
        with mock.patch("subprocess.run", side_effect=fake), \
                mock.patch("platform.system", return_value="Linux"):
            return vra.c_smoke(tmp / "prefix", tmp, {"PATH": "/usr/bin", "CC": "cc"}, [],
                               [str(tmp / "prefix/include")], [str(tmp / "prefix/lib")],
                               ["smbios_c"])

    def test_NoTableIsAPassPerSmbiosOpensDocumentedDegradation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._smoke(Path(tmp), "smbios_open=no-table\n"),
                             "smbios_open=no-table")

    def test_PopulatedTableIsAPass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._smoke(Path(tmp), "smbios_open=ok structures=32\n"),
                             "smbios_open=ok structures=32")

    def test_OtherOpenFailureOrUnexpectedOutputFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(vra.VerifyError) as cm:
            self._smoke(Path(tmp), "smbios_open failed: 3\n", rc=1)
        self.assertIn("run c consumer failed (exit 1)", str(cm.exception))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(vra.VerifyError):
            self._smoke(Path(tmp), "hello\n")

    def test_TheReusedFixtureIsCopiedNotCompiledInPlace(self) -> None:
        seen: List[List[str]] = []

        def fake(argv, **_kw):
            seen.append(list(argv))
            return mock.Mock(returncode=0, stdout="smbios_open=no-table\n")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("subprocess.run", side_effect=fake), \
                mock.patch("platform.system", return_value="Linux"):
            roots = vra.forbidden_roots(vra.REPO_ROOT, {})
            vra.c_smoke(Path(tmp) / "prefix", Path(tmp), {"CC": "cc"}, roots, ["/p/i"],
                        ["/p/l"], ["smbios_c"])
            self.assertEqual(seen[0][1], str(Path(tmp) / "c-src" / "main.c"))
            self.assertTrue((Path(tmp) / "c-src" / "main.c").is_file())


class CxxConsumerTest(unittest.TestCase):
    """Check 4: ``find_package(libsmbios CONFIG)`` against the extracted prefix only."""

    def test_TheReusedFixtureCarriesNoWholeArchiveFlag(self) -> None:
        vra.assert_no_whole_archive(vra.CXX_FIXTURE / "CMakeLists.txt")

    def test_AWholeArchiveFlagInTheConsumerIsRefusedButCommentsAreNot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CMakeLists.txt"
            path.write_text("# no force_load here\nproject(x)\n", encoding="utf-8")
            vra.assert_no_whole_archive(path)
            path.write_text("target_link_options(c PRIVATE -Wl,-force_load,x.a)\n",
                            encoding="utf-8")
            with self.assertRaises(vra.VerifyError):
                vra.assert_no_whole_archive(path)

    def _fake_cmake(self, libsmbios_dir: str, build_out: str = "", calls=None):
        def fake(argv, **_kw):
            if calls is not None:
                calls.append(list(argv))
            if "-S" in argv:
                build = Path(argv[argv.index("-B") + 1])
                build.mkdir(parents=True, exist_ok=True)
                (build / "CMakeCache.txt").write_text(
                    f"libsmbios_DIR:PATH={libsmbios_dir}\n", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="-- Configuring done\n")
            if "--build" in argv:
                build = Path(argv[argv.index("--build") + 1])
                exe = build / "consumer"
                exe.write_text("", encoding="utf-8")
                return mock.Mock(returncode=0, stdout=build_out)
            return mock.Mock(returncode=0,
                             stdout="formatters=4\ntype17=Memory Device\nfields=15\n")
        return fake

    def test_ResolvesToTheExtractedPackageAndRunsTheConsumer(self) -> None:
        calls: List[List[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            fake = self._fake_cmake(str(prefix / "lib/cmake/libsmbios"), calls=calls)
            with mock.patch.object(vra, "_which", return_value="cmake"), \
                    mock.patch("subprocess.run", side_effect=fake), \
                    mock.patch("platform.system", return_value="Linux"):
                lines = vra.cxx_smoke(prefix, Path(tmp), {}, [], "Release",
                                      [Path("/opt/boost")])
        self.assertEqual(lines, ["formatters=4", "type17=Memory Device", "fields=15"])
        configure = calls[0]
        self.assertEqual(configure[configure.index("-S") + 1], str(Path(tmp) / "cxx-src"))
        self.assertIn(f"-DCMAKE_PREFIX_PATH={prefix};/opt/boost", configure)
        self.assertIn("-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF", configure)
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", configure)
        self.assertIn("--verbose", calls[1])

    def test_PackageFoundOutsideTheExtractedPrefixFails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_cmake("/usr/local/lib/cmake/libsmbios")
            with mock.patch.object(vra, "_which", return_value="cmake"), \
                    mock.patch("subprocess.run", side_effect=fake):
                with self.assertRaises(vra.VerifyError) as cm:
                    vra.cxx_smoke(Path(tmp) / "prefix", Path(tmp), {}, [], "Release")
        self.assertIn("did not resolve to the extracted", str(cm.exception))

    def test_RepoPathInTheBuildCommandLinesFails(self) -> None:
        roots = [("this repository", Path("/work/SmbiosSdk"))]
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            fake = self._fake_cmake(str(prefix / "lib/cmake/libsmbios"),
                                    build_out="c++ -I/work/SmbiosSdk/libsmbios/include x.cpp\n")
            with mock.patch.object(vra, "_which", return_value="cmake"), \
                    mock.patch("subprocess.run", side_effect=fake):
                with self.assertRaises(vra.VerifyError) as cm:
                    vra.cxx_smoke(prefix, Path(tmp), {}, roots, "Release")
        self.assertIn("compile/link command lines references this repository",
                      str(cm.exception))


class RunTest(unittest.TestCase):
    """The ``run()`` record and the checks it refuses before touching anything."""

    def test_LayoutModeReportsBuildChecksAsNotExecuted(self) -> None:
        out = "-I{p}/include -L{p}/lib -lsmbios_c\n"
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            work = Path(tmp) / "work"

            def fake(argv, **_kw):
                return mock.Mock(returncode=0, stdout=out.format(p=work / "prefix"))
            with mock.patch.object(vra, "_which", return_value="pkg-config"), \
                    mock.patch("subprocess.run", side_effect=fake):
                lines = vra.run(archive, work, build=False, environ={"PATH": "/usr/bin"})
        self.assertEqual(lines[0], f"{LEG} {VERSION}")
        self.assertIn("sha256 ok", lines)
        self.assertIn("pkg-config ok", lines)
        self.assertTrue(lines[-1].startswith("build/run NOT EXECUTED"))

    def test_FullModeRunsEveryCheckOnAMatchingHost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            work = Path(tmp) / "work"
            prefix = work / "prefix"
            cxx = CxxConsumerTest()._fake_cmake(str(prefix / "lib/cmake/libsmbios"))

            def fake(argv, **kw):
                if argv[0] == "pkg-config":
                    return mock.Mock(returncode=0, stdout=f"-I{prefix}/include "
                                     f"-L{prefix}/lib -lsmbios_c\n")
                if argv[0] == "cc":
                    return mock.Mock(returncode=0, stdout="")
                if argv[0].endswith("c-consumer"):
                    return mock.Mock(returncode=0, stdout="smbios_open=no-table\n")
                return cxx(argv, **kw)
            with mock.patch.object(vra, "host_leg", return_value=LEG), \
                    mock.patch.object(vra, "_which", side_effect=lambda n, e: n[0]), \
                    mock.patch("platform.system", return_value="Linux"), \
                    mock.patch("subprocess.run", side_effect=fake):
                lines = vra.run(archive, work, environ={"PATH": "/usr/bin", "CC": "cc"})
        self.assertIn("c: smbios_open=no-table", lines)
        self.assertEqual(lines[-1], "cxx: formatters=4 type17=Memory Device fields=15")

    def test_HostOfAnotherLegFailsBeforeAnyCommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run, \
                mock.patch.object(vra, "host_leg", return_value="macos-x64"):
            archive = make_release(Path(tmp))
            with self.assertRaises(vra.VerifyError) as cm:
                vra.run(archive, Path(tmp) / "w", environ={})
            self.assertIn("cannot be built and run on this host", str(cm.exception))
            run.assert_not_called()

    def test_LeakedRepoPathInTheEnvironmentIsDetectedBeforeAnyCommand(self) -> None:
        repo = Path("/work/SmbiosSdk")
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as run:
            archive = make_release(Path(tmp))
            with self.assertRaises(vra.VerifyError) as cm:
                vra.run(archive, Path(tmp) / "w", build=False, repo_root=repo,
                        environ={"PATH": f"/usr/bin{os.pathsep}{repo}/build/macos/bin"})
            self.assertIn("environment variable PATH points into this repository",
                          str(cm.exception))
            run.assert_not_called()
            self.assertFalse((Path(tmp) / "w").exists())

    def test_ScratchOrDependencyPrefixInsideAForbiddenTreeIsRefused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            with self.assertRaises(vra.VerifyError) as cm:
                vra.run(archive, Path(tmp) / "w", build=False, repo_root=Path(tmp),
                        environ={})
            self.assertIn("scratch directory is inside this repository", str(cm.exception))
            with self.assertRaises(vra.VerifyError) as cm:
                vra.run(archive, Path(tmp) / "w", build=False,
                        dependency_prefixes=[Path("/home/u/.vcpkg/installed/x64-osx")],
                        environ={"VCPKG_ROOT": "/home/u/.vcpkg"})
            self.assertIn("dependency prefix is inside the vcpkg root", str(cm.exception))

    def test_DigestFailureStopsBeforeExtraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_release(Path(tmp))
            pa.write_sha256_file(archive, "a" * 64)
            with self.assertRaises(vra.VerifyError):
                vra.run(archive, Path(tmp) / "w", build=False, environ={})
            self.assertFalse((Path(tmp) / "w" / "prefix").exists())


if __name__ == "__main__":
    unittest.main()
