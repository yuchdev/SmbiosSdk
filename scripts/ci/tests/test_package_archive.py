"""Tests for ``package_archive.py`` and ``stage.py package`` (milestone 0012 Story 03.0 Task 01).

Classification: unit tests (archive determinism, digests, manifest merge,
contents policy, binary metadata readers on synthetic ELF/Mach-O/PE bytes)
plus command-line tests of ``stage.py package`` in a subprocess against a
synthetic ``install/<leg>`` tree under a temporary ``CI_ARTIFACT_DIR``. No
network, no cmake, no firmware data, no real build tree.

Boundaries: ADR 0005 D4 (``dist/`` layout, manifest fields), the 2026-11-16
maintainer rulings in ``docs/roadmap/0012-package-manager-distribution/status.md``
(shipped configuration ``Release``; libraries only - no ``smbios`` CLI, no
``smbios_gui``), RC1 (one line per step, exit 2 for a missing input).

On failure, first check: ``package_archive.REQUIRED`` against the install
rules in ``libsmbios/CMakeLists.txt`` / ``smbios_c/CMakeLists.txt`` (a renamed
installed file), then ``scripts/ci/legs.json`` ``stages.package`` and the
packaging legs' ``config``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Dict, List

from ci_test_support import CI_DIR, neutral_env

import package_archive as pa  # noqa: E402
import stage  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]
STAGE = CI_DIR / "stage.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def fake_elf(glibc: List[str]) -> bytes:
    """An ELF-magic blob whose string table names the given ``GLIBC_x.y`` versions."""
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"".join(
        f"GLIBC_{v}".encode() + b"\x00" for v in glibc) + b"libc.so.6\x00"


def fake_macho(minos: tuple, *, legacy: bool = False) -> bytes:
    """A thin 64-bit Mach-O header with one LC_BUILD_VERSION (or LC_VERSION_MIN_MACOSX)."""
    packed = (minos[0] << 16) | (minos[1] << 8) | minos[2]
    if legacy:
        cmd = struct.pack("<IIII", 0x24, 16, packed, packed)
    else:
        cmd = struct.pack("<IIIIII", 0x32, 24, 1, packed, packed, 0)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, 1, len(cmd), 0, 0)
    return header + cmd + b"\x00" * 64


def fake_pe(major: int, minor: int) -> bytes:
    """A minimal PE image: MZ stub, PE signature, COFF header, optional-header linker version."""
    data = bytearray(0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<HBB", data, 0x80 + 24, 0x20B, major, minor)
    return bytes(data)


LIBRARY_BYTES = {"linux": fake_elf(["2.17", "2.34", "2.2.5"]),
                 "macos": fake_macho((13, 0, 0)),
                 "windows": fake_pe(14, 44)}


def make_tree(root: Path, os_name: str) -> Path:
    """Write a synthetic install tree with every ``REQUIRED`` path of ``os_name``."""
    for rel in pa.REQUIRED[os_name]:
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content of {rel}\n".encode())
    lib = {"linux": "lib/libsmbios_c.so", "macos": "lib/libsmbios_c.dylib",
           "windows": "bin/smbios_c.dll"}[os_name]
    root.joinpath(*lib.split("/")).write_bytes(LIBRARY_BYTES[os_name])
    (root / "include" / "libsmbios" / "tables").mkdir(parents=True, exist_ok=True)
    (root / "include" / "libsmbios" / "tables" / "bios.hpp").write_text("// x\n",
                                                                         encoding="utf-8")
    return root


class ArchiveTreeTests(unittest.TestCase):
    """Type: unit. Scenario: reproducible archives of one tree. Boundary: D4 determinism."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = make_tree(self.tmp / "install" / "linux-x64", "linux")
        if os.name != "nt":
            os.chmod(self.src / "lib" / "libsmbios_c.so", 0o775)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_SameTreeTwiceIsByteIdentical(self) -> None:
        for fmt in pa.FORMATS:
            with self.subTest(fmt=fmt):
                first = pa.archive_tree(self.src, self.tmp / "a" / f"x.{fmt}", fmt)
                time.sleep(1.1)  # a timestamp leak would now differ
                second = pa.archive_tree(self.src, self.tmp / "b" / f"x.{fmt}", fmt)
                self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_FileTimesAndModesDoNotLeakIntoTheArchive(self) -> None:
        for fmt in pa.FORMATS:
            with self.subTest(fmt=fmt):
                before = pa.archive_tree(self.src, self.tmp / "a" / f"x.{fmt}", fmt).read_bytes()
                header = self.src / "include" / "smbios" / "smbios.h"
                os.utime(header, (1_700_000_000, 1_700_000_000))
                if os.name != "nt":
                    os.chmod(header, 0o600)
                after = pa.archive_tree(self.src, self.tmp / "b" / f"x.{fmt}", fmt).read_bytes()
                self.assertEqual(before, after)

    def test_TarEntriesAreSortedNormalisedAndRootRelative(self) -> None:
        path = pa.archive_tree(self.src, self.tmp / "x.tar.gz", "tar.gz")
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
        names = [m.name for m in members]
        self.assertEqual(names, sorted(names))
        self.assertIn("lib/pkgconfig/smbios_c.pc", names)
        self.assertFalse(any(n.startswith(("/", "..", "install")) for n in names))
        for m in members:
            self.assertEqual((m.mtime, m.uid, m.gid, m.uname, m.gname),
                             (pa.FIXED_MTIME, 0, 0, "", ""))
            self.assertIn(m.mode, (0o755, 0o644))
        so = next(m for m in members if m.name == "lib/libsmbios_c.so")
        hpp = next(m for m in members if m.name == "include/libsmbios/smbios.hpp")
        self.assertEqual(hpp.mode, 0o644)
        if os.name != "nt":
            self.assertEqual(so.mode, 0o755, "an executable bit becomes 0755")
        raw = path.read_bytes()
        self.assertEqual(raw[4:8], b"\x00\x00\x00\x00", "gzip header mtime is 0")
        self.assertEqual(raw[3] & 0x08, 0, "gzip header carries no file name")

    def test_ZipEntriesAreSortedNormalisedAndRootRelative(self) -> None:
        path = pa.archive_tree(self.src, self.tmp / "x.zip", "zip")
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            self.assertIsNone(zf.testzip())
        names = [i.filename for i in infos]
        self.assertEqual(names, sorted(names))
        self.assertIn("include/smbios/smbios.h", names)
        for info in infos:
            self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(info.create_system, 3)
            self.assertIn((info.external_attr >> 16) & 0o777, (0o755, 0o644))

    def test_ContentRoundTrips(self) -> None:
        path = pa.archive_tree(self.src, self.tmp / "x.tar.gz", "tar.gz")
        with tarfile.open(path, "r:gz") as tar:
            data = tar.extractfile("lib/pkgconfig/smbios_c.pc").read()  # type: ignore[union-attr]
        self.assertEqual(data, (self.src / "lib" / "pkgconfig" / "smbios_c.pc").read_bytes())

    @unittest.skipIf(os.name == "nt", "symlink creation needs privileges on Windows")
    def test_SymlinkInTreeIsRefused(self) -> None:
        (self.src / "lib" / "libsmbios_c.so.1").symlink_to("libsmbios_c.so")
        with self.assertRaisesRegex(pa.ArchiveError, "symlink: lib/libsmbios_c.so.1"):
            pa.archive_tree(self.src, self.tmp / "x.tar.gz", "tar.gz")
        self.assertFalse((self.tmp / "x.tar.gz").exists())

    def test_EmptyOrMissingTreeIsRefused(self) -> None:
        empty = self.tmp / "empty"
        (empty / "lib").mkdir(parents=True)
        with self.assertRaisesRegex(pa.ArchiveError, "empty"):
            pa.archive_tree(empty, self.tmp / "x.zip", "zip")
        with self.assertRaisesRegex(pa.ArchiveError, "missing"):
            pa.archive_tree(self.tmp / "nope", self.tmp / "x.zip", "zip")

    def test_DestinationInsideTheTreeOrUnknownFormatIsRefused(self) -> None:
        with self.assertRaisesRegex(pa.ArchiveError, "inside the install tree"):
            pa.archive_tree(self.src, self.src / "x.tar.gz", "tar.gz")
        with self.assertRaisesRegex(pa.ArchiveError, "format"):
            pa.archive_tree(self.src, self.tmp / "x.7z", "7z")  # type: ignore[arg-type]


class DigestAndNameTests(unittest.TestCase):
    """Type: unit. Scenario: SHA-256 helpers and the D4 file name."""

    def test_Sha256FileMatchesHashlibAndTheSha256FileIsCheckable(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            archive = Path(name) / "libbios-1.0.0-linux-x64.tar.gz"
            archive.write_bytes(b"x" * 3_000_000)
            digest = pa.sha256_file(archive)
            self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())
            side = pa.write_sha256_file(archive, digest)
            self.assertEqual(side.name, "libbios-1.0.0-linux-x64.tar.gz.sha256")
            self.assertEqual(side.read_bytes(),
                             f"{digest}  libbios-1.0.0-linux-x64.tar.gz\n".encode())
            with self.assertRaises(pa.ArchiveError):
                pa.write_sha256_file(archive, "XYZ")

    def test_ArchiveNamePerLeg(self) -> None:
        self.assertEqual(pa.archive_name("1.0.0", "linux-x64"), "libbios-1.0.0-linux-x64.tar.gz")
        self.assertEqual(pa.archive_name("1.0.0", "macos-arm64"),
                         "libbios-1.0.0-macos-arm64.tar.gz")
        self.assertEqual(pa.archive_name("1.2.3-rc.1", "windows-x64"),
                         "libbios-1.2.3-rc.1-windows-x64.zip")
        for version, leg in (("1.0", "linux-x64"), ("1.0.0/..", "linux-x64"),
                             ("1.0.0", "linux"), ("1.0.0", "linux-asan")):
            with self.subTest(version=version, leg=leg), self.assertRaises(pa.ArchiveError):
                pa.archive_name(version, leg)


class ManifestTests(unittest.TestCase):
    """Type: unit. Scenario: merging legs into dist/manifest.json (D4 fields)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self._tmp.name) / "dist"
        self.dist.mkdir()
        self.manifest = self.dist / pa.MANIFEST_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def meta(self, os_key: str, value: str, **extra: str) -> Dict[str, str]:
        base = {"version": "1.0.0", "commit": COMMIT, "config": "Release",
                "install_sha256": "a" * 64, "files": "42", os_key: value}
        base.update(extra)
        return base

    def archive(self, leg: str, payload: bytes) -> Path:
        path = self.dist / pa.archive_name("1.0.0", leg)
        path.write_bytes(payload)
        return path

    def test_ManifestRecordsTheArchiveDigest(self) -> None:
        archive = self.archive("linux-x64", b"linux archive bytes")
        pa.update_manifest(self.manifest, "linux-x64", archive, self.meta("glibc", "2.34"))
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((data["schema"], data["version"], data["commit"]),
                         (1, "1.0.0", COMMIT))
        (entry,) = data["legs"]
        self.assertEqual(entry, {"leg": "linux-x64", "file": archive.name,
                                 "sha256": hashlib.sha256(b"linux archive bytes").hexdigest(),
                                 "bytes": len(b"linux archive bytes"), "config": "Release",
                                 "install_sha256": "a" * 64, "files": 42, "glibc": "2.34"})

    def test_LegsMergeSortedAndAReRunReplacesItsLeg(self) -> None:
        mac = self.archive("macos-arm64", b"mac")
        win = self.archive("windows-x64", b"win")
        pa.update_manifest(self.manifest, "windows-x64", win, self.meta("msvc", "14.44"))
        pa.update_manifest(self.manifest, "macos-arm64", mac, self.meta("macos_min", "13.0"))
        mac.write_bytes(b"mac again")
        pa.update_manifest(self.manifest, "macos-arm64", mac, self.meta("macos_min", "13.0"))
        legs = json.loads(self.manifest.read_text(encoding="utf-8"))["legs"]
        self.assertEqual([e["leg"] for e in legs], ["macos-arm64", "windows-x64"])
        self.assertEqual(legs[0]["sha256"], hashlib.sha256(b"mac again").hexdigest())
        self.assertEqual(legs[1]["msvc"], "14.44")

    def test_CallerDigestIsUsedAsIs(self) -> None:
        archive = self.archive("linux-x64", b"bytes")
        pa.update_manifest(self.manifest, "linux-x64", archive,
                           self.meta("glibc", "2.34", sha256="b" * 64))
        self.assertEqual(json.loads(self.manifest.read_text())["legs"][0]["sha256"], "b" * 64)

    def test_ManifestIsDeterministic(self) -> None:
        archive = self.archive("linux-x64", b"bytes")
        pa.update_manifest(self.manifest, "linux-x64", archive, self.meta("glibc", "2.34"))
        first = self.manifest.read_bytes()
        pa.update_manifest(self.manifest, "linux-x64", archive, self.meta("glibc", "2.34"))
        self.assertEqual(first, self.manifest.read_bytes())

    def test_AnotherVersionOrCommitIsRefused(self) -> None:
        archive = self.archive("linux-x64", b"bytes")
        pa.update_manifest(self.manifest, "linux-x64", archive, self.meta("glibc", "2.34"))
        with self.assertRaisesRegex(pa.ArchiveError, "another commit"):
            pa.update_manifest(self.manifest, "linux-x64", archive,
                               self.meta("glibc", "2.34", commit="f" * 40))

    def test_InvalidMetadataIsRefused(self) -> None:
        archive = self.archive("linux-x64", b"bytes")
        cases = [self.meta("macos_min", "13.0"),                  # wrong platform key
                 self.meta("glibc", "2.34", config="Rel;ease"),
                 self.meta("glibc", "2.34", commit="HEAD"),
                 self.meta("glibc", "2.34", version="2.0.0")]     # name says 1.0.0
        for meta in cases:
            with self.subTest(meta=meta), self.assertRaises(pa.ArchiveError):
                pa.update_manifest(self.manifest, "linux-x64", archive, meta)
        self.assertFalse(self.manifest.exists())

    def test_CorruptManifestIsRefused(self) -> None:
        self.manifest.write_text("{not json", encoding="utf-8")
        archive = self.archive("linux-x64", b"bytes")
        with self.assertRaisesRegex(pa.ArchiveError, "not readable JSON"):
            pa.update_manifest(self.manifest, "linux-x64", archive, self.meta("glibc", "2.34"))


class ContentsPolicyTests(unittest.TestCase):
    """Type: unit + data sync. Scenario: maintainer ruling I2 (status.md 2026-11-16).

    The tarball ships libraries only: libsmbios + smbios_c. The ``smbios`` CLI
    and ``smbios_gui`` are excluded, and the stage refuses a tree holding them.
    """

    def test_RequiredContentsAreTheTwoLibrariesOnly(self) -> None:
        for os_name, paths in pa.REQUIRED.items():
            with self.subTest(os=os_name):
                text = " ".join(paths)
                self.assertIn("lib/cmake/libsmbios/libsmbiosConfig.cmake", paths)
                self.assertIn("lib/pkgconfig/smbios_c.pc", paths)
                self.assertIn("include/smbios/smbios.h", paths)
                self.assertIn("include/libsmbios/smbios.hpp", paths)
                self.assertNotRegex(text, r"(^|/)smbios(\.exe)?( |$)")
                self.assertNotIn("smbios_gui", text)

    def test_CliAndGuiInTheTreeAreRefused(self) -> None:
        for bad in ("bin/smbios", "bin/smbios.exe", "bin/smbios_gui", "bin/smbios_gui.exe",
                    "bin/smbios_gui_autoexit"):
            with self.subTest(path=bad), tempfile.TemporaryDirectory() as name:
                root = make_tree(Path(name) / "t", "linux")
                target = root.joinpath(*bad.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x7fELF")
                self.assertEqual(pa.excluded_entries(root), [bad])
                with self.assertRaisesRegex(pa.ArchiveError, "excluded until milestone 0015"):
                    pa.check_contents(root, "linux")

    def test_TheHeaderNamedSmbiosIsNotMistakenForTheCli(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = make_tree(Path(name) / "t", "macos")
            self.assertEqual(pa.excluded_entries(root), [])
            pa.check_contents(root, "macos")

    def test_MissingLibraryIsRefused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = make_tree(Path(name) / "t", "windows")
            (root / "lib" / "pkgconfig" / "smbios_c.pc").unlink()
            with self.assertRaisesRegex(pa.ArchiveError, "lacks lib/pkgconfig/smbios_c.pc"):
                pa.check_contents(root, "windows")

    def test_NoInstallRuleShipsTheCliOrGui(self) -> None:
        """The exclusion holds by construction today: neither target has an install() rule.

        When milestone 0015 Story 01.0 adds one, this test and
        ``check_contents`` both fail, so shipping them is a recorded change.
        """
        for top in ("smbios", "smbios_gui"):
            for cmake in sorted((REPO_ROOT / top).rglob("CMakeLists.txt")):
                with self.subTest(file=str(cmake.relative_to(REPO_ROOT))):
                    text = re.sub(r"#[^\n]*", "", cmake.read_text(encoding="utf-8"))
                    self.assertNotRegex(text, r"\binstall\s*\(")


class PlatformMetaTests(unittest.TestCase):
    """Type: unit. Scenario: glibc / macOS minimum / MSVC linker read from the binaries."""

    def test_GlibcIsTheHighestRequirement(self) -> None:
        self.assertEqual(pa.glibc_requirement(fake_elf(["2.17", "2.34", "2.2.5"])), (2, 34))
        self.assertIsNone(pa.glibc_requirement(b"GLIBC_2.40\x00"), "not an ELF image")
        self.assertIsNone(pa.glibc_requirement(fake_elf([])))

    def test_MacosMinimumFromBuildVersionOrLegacyCommand(self) -> None:
        self.assertEqual(pa.macos_minimum(fake_macho((13, 0, 0))), (13, 0, 0))
        self.assertEqual(pa.macos_minimum(fake_macho((10, 15, 4), legacy=True)), (10, 15, 4))
        self.assertIsNone(pa.macos_minimum(b"\xcf\xfa\xed\xfe" + b"\xff" * 20))
        self.assertIsNone(pa.macos_minimum(b"junk"))

    def test_UniversalBinaryTakesTheHighestSliceMinimum(self) -> None:
        a, b = fake_macho((11, 0, 0)), fake_macho((10, 13, 0))
        head = struct.pack(">II", 0xCAFEBABE, 2)
        off_a = 8 + 2 * 20
        off_b = off_a + len(a)
        head += struct.pack(">IIIII", 0x0100000C, 0, off_a, len(a), 0)
        head += struct.pack(">IIIII", 0x01000007, 3, off_b, len(b), 0)
        self.assertEqual(pa.macos_minimum(head + a + b), (11, 0, 0))

    def test_PeLinkerVersion(self) -> None:
        self.assertEqual(pa.pe_linker_version(fake_pe(14, 44)), (14, 44))
        self.assertIsNone(pa.pe_linker_version(b"MZ" + b"\xff" * 100))
        self.assertIsNone(pa.pe_linker_version(fake_elf(["2.17"])))

    def test_PlatformMetaPerOs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            for os_name, want in (("linux", {"glibc": "2.34"}),
                                  ("macos", {"macos_min": "13.0"}),
                                  ("windows", {"msvc": "14.44"})):
                with self.subTest(os=os_name):
                    root = make_tree(Path(name) / os_name, os_name)
                    self.assertEqual(pa.platform_meta(root, os_name), want)

    def test_UnreadableLibraryFailsLoudly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = make_tree(Path(name) / "t", "macos")
            (root / "lib" / "libsmbios_c.dylib").write_bytes(b"not a mach-o")
            with self.assertRaisesRegex(pa.ArchiveError, "macos_min"):
                pa.platform_meta(root, "macos")


def run_package(tmp: Path, args: List[str], **extra: str) -> subprocess.CompletedProcess:
    env = neutral_env(tmp, **{"CI_WORKSPACE": str(REPO_ROOT), "CI_PLATFORM": "local",
                              "CI_ARTIFACT_DIR": str(tmp / "artifacts"), "CI_COMMIT": COMMIT,
                              **extra})
    return subprocess.run([sys.executable, "-I", str(STAGE), "package", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, env=env, cwd=str(tmp), timeout=120,
                          text=True)


class PackageStageTests(unittest.TestCase):
    """Type: integration (stage.py in a subprocess). Scenario: SC1 exits and the dist/ layout."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rc1(self, text: str) -> List[str]:
        return [ln for ln in text.splitlines() if ln.startswith(("ok:", "error:", "skip:"))]

    def test_MissingInstallTreeExits2WithOneRc1Line(self) -> None:
        proc = run_package(self.tmp, ["--leg", "macos-arm64", "--only", "archive"])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        lines = (proc.stdout + proc.stderr).splitlines()
        self.assertEqual(len(lines), 1, lines)
        expected = str(self.tmp / "artifacts" / "install" / "macos-arm64")
        self.assertTrue(lines[0].startswith("error: package macos-arm64 failed (exit 2"), lines)
        self.assertIn(f"install tree missing: {expected}", lines[0])
        self.assertFalse((self.tmp / "artifacts" / "dist").exists())
        record = json.loads((self.tmp / "results.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["outcome"], record["exit"]), ("fail", 2))

    def test_WholeStageWithMissingTreeStillHasOneRc1Line(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux-x64"])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(len(self.rc1(proc.stdout + proc.stderr)), 1, proc.stdout + proc.stderr)
        self.assertNotIn("stage.py: error:", proc.stderr)

    def test_PackagesAllThreeLegsIntoOneManifest(self) -> None:
        for leg in ("linux-x64", "windows-x64", "macos-arm64"):
            make_tree(self.tmp / "artifacts" / "install" / leg, pa.leg_os(leg))
            proc = run_package(self.tmp, ["--leg", leg])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            (ok,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("ok: package ")]
            self.assertRegex(ok, rf"^ok: package {leg}: libbios-\d+\.\d+\.\d+-{leg}\."
                                 r"(tar\.gz|zip), \d+ files, \d+ bytes, sha256 [0-9a-f]{16}, "
                                 r"Release, (glibc 2\.34|msvc 14\.44|macos_min 13\.0) ")
        dist = self.tmp / "artifacts" / "dist"
        manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual([e["leg"] for e in manifest["legs"]],
                         ["linux-x64", "macos-arm64", "windows-x64"])
        for entry in manifest["legs"]:
            with self.subTest(leg=entry["leg"]):
                archive = dist / entry["file"]
                self.assertEqual(entry["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
                self.assertEqual(entry["bytes"], archive.stat().st_size)
                self.assertEqual(entry["config"], "Release")
                self.assertEqual((dist / (entry["file"] + ".sha256")).read_text(),
                                 f"{entry['sha256']}  {entry['file']}\n")
                self.assertTrue(entry["file"].endswith(
                    ".zip" if entry["leg"].startswith("windows") else ".tar.gz"))
        self.assertEqual(sorted(p.name for p in dist.iterdir() if p.suffix != ".json"),
                         sorted([e["file"] for e in manifest["legs"]]
                                + [e["file"] + ".sha256" for e in manifest["legs"]]))

    def test_RepackagingIsByteIdentical(self) -> None:
        make_tree(self.tmp / "artifacts" / "install" / "linux-x64", "linux")
        self.assertEqual(run_package(self.tmp, ["--leg", "linux-x64"]).returncode, 0)
        dist = self.tmp / "artifacts" / "dist"
        first = {p.name: p.read_bytes() for p in dist.iterdir()}
        time.sleep(1.1)
        self.assertEqual(run_package(self.tmp, ["--leg", "linux-x64"]).returncode, 0)
        self.assertEqual(first, {p.name: p.read_bytes() for p in dist.iterdir()})

    def test_TreeWithTheCliIsAGateFailure(self) -> None:
        root = make_tree(self.tmp / "artifacts" / "install" / "linux-x64", "linux")
        (root / "bin").mkdir()
        (root / "bin" / "smbios").write_bytes(b"\x7fELF")
        proc = run_package(self.tmp, ["--leg", "linux-x64", "--only", "archive"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("bin/smbios", proc.stdout)
        self.assertFalse((self.tmp / "artifacts" / "dist" / "manifest.json").exists())

    def test_TestingDirectoryInTheTreeIsRefused(self) -> None:
        root = make_tree(self.tmp / "artifacts" / "install" / "linux-x64", "linux")
        (root / "Testing").mkdir()
        (root / "Testing" / "LastTest.log").write_text("x", encoding="utf-8")
        proc = run_package(self.tmp, ["--leg", "linux-x64", "--only", "archive"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Testing path", proc.stdout)

    def test_NonPackagingLegIsAUsageError(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown leg for stage package", proc.stderr)

    def test_BadCommitIsAUsageError(self) -> None:
        make_tree(self.tmp / "artifacts" / "install" / "linux-x64", "linux")
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_COMMIT="HEAD;rm")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("rm", proc.stderr)

    def test_PackagingLegsFollowD7(self) -> None:
        """ADR 0005 D7: build/test/package on every event but a push to a non-master branch."""
        legs = stage.load_legs()
        for leg in legs["stages"]["package"]:
            with self.subTest(leg=leg):
                self.assertIn(leg, legs["tiers"]["full"])
                self.assertNotIn(leg, legs["tiers"]["push_branch"])
                self.assertIn(leg, legs["stages"]["test"])
                self.assertFalse(stage.leg_selected(leg, "push", "refs/heads/topic", legs))
                for event, ref in (("push", "refs/heads/master"), ("pr", "refs/pull/1/merge"),
                                   ("tag", "refs/tags/v1.0.0"),
                                   ("schedule", "refs/heads/master"), ("", "")):
                    self.assertTrue(stage.leg_selected(leg, event, ref, legs), event)

    def test_PlanSchedulesPackageWithItsBuildLegs(self) -> None:
        import plan
        packaging = ["linux-x64", "windows-x64", "macos-arm64"]
        for event, ref, same in (("pr", "refs/pull/1/merge", True),
                                 ("pr", "refs/pull/1/merge", False),
                                 ("push", "refs/heads/master", False),
                                 ("tag", "refs/tags/v1.0.0", False),
                                 ("schedule", "refs/heads/master", False)):
            with self.subTest(event=event, same_repo=same):
                result = plan.matrix_for(event, ref, "full", pr_same_repo=same)
                self.assertEqual(result["stages"]["package"], plan.RUN)
                self.assertEqual(sorted(result["package"]), sorted(packaging))
                self.assertLessEqual(set(packaging), set(result["build"]),
                                     "package needs the same run's build of every leg")
        for event, ref, cls in (("push", "refs/heads/topic", "full"),
                                ("push", "refs/heads/master", "docs")):
            with self.subTest(event=event, classification=cls):
                result = plan.matrix_for(event, ref, cls)
                self.assertEqual(result["package"], [])
                self.assertFalse(set(packaging) & set(result["build"]))


class HandOffTests(unittest.TestCase):
    """Type: integration. Scenario: build -> package hand-off (one tar, digest check, CR-8)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.art = self.tmp / "artifacts"
        tree = make_tree(self.tmp / "built" / "linux-x64", "linux")
        self.digest = stage.tree_digest(tree)[0]
        pa.archive_tree(tree, self.art / "install-linux-x64.tar", "tar")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_UnpackVerifiesTheBuildDigestThenPackages(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_INSTALL_SHA256=self.digest)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"sha256 {self.digest[:16]}, matches the build job's digest", proc.stdout)
        manifest = json.loads((self.art / "dist" / "manifest.json").read_text())
        self.assertEqual(manifest["legs"][0]["install_sha256"], self.digest)
        self.assertIn('output: archive_ext="tar.gz"', proc.stdout)

    def test_DigestMismatchFailsLoudlyAndPackagesNothing(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_INSTALL_SHA256="0" * 64)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("install tree digest: expected 0000000000000000 -> actual "
                      f"{self.digest[:16]} (not the tree the build stage tested)", proc.stdout)
        self.assertFalse((self.art / "dist").exists())

    def test_TamperedHandOffIsCaught(self) -> None:
        tree = make_tree(self.tmp / "evil" / "linux-x64", "linux")
        (tree / "lib" / "pkgconfig" / "smbios_c.pc").write_text("prefix=/evil\n")
        (self.art / "install-linux-x64.tar").unlink()
        pa.archive_tree(tree, self.art / "install-linux-x64.tar", "tar")
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_INSTALL_SHA256=self.digest)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not the tree the build stage tested", proc.stdout)

    def test_CiRequiresTheExpectedDigest(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_PLATFORM="github",
                           CI_REPORTER="plain", CI_EVENT="pr", CI_REF="refs/pull/1/merge")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("CI_INSTALL_SHA256 is required on CI", proc.stderr)
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_INSTALL_SHA256="abc;rm")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("rm", proc.stderr)

    def test_CiNeverFallsBackToAnExistingTree(self) -> None:
        (self.art / "install-linux-x64.tar").unlink()
        make_tree(self.art / "install" / "linux-x64", "linux")
        proc = run_package(self.tmp, ["--leg", "linux-x64"], CI_PLATFORM="github",
                           CI_REPORTER="plain", CI_EVENT="pr", CI_REF="refs/pull/1/merge",
                           CI_INSTALL_SHA256=self.digest)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("install hand-off missing: "
                      f"{self.art / 'install-linux-x64.tar'}", proc.stdout)
        self.assertEqual(len(self.rc1(proc.stdout + proc.stderr)), 1)

    def rc1(self, text: str) -> List[str]:
        return [ln for ln in text.splitlines() if ln.startswith(("ok:", "error:", "skip:"))]


class ExtractTests(unittest.TestCase):
    """Type: invalid input. Scenario: the hand-off tar is untrusted (artifact store)."""

    def hostile(self, tmp: Path, name: str, kind: bytes = tarfile.REGTYPE) -> Path:
        path = tmp / "evil.tar"
        with tarfile.open(path, "w") as tar:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                data = b"x"
                info.size = len(data)
                import io
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_TraversalAbsoluteAndLinksAreRefused(self) -> None:
        for name, kind in (("../outside", tarfile.REGTYPE), ("/abs", tarfile.REGTYPE),
                           ("lib/../../x", tarfile.REGTYPE), ("lib/link", tarfile.SYMTYPE),
                           ("lib\\x", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                with self.assertRaises(pa.ArchiveError):
                    pa.extract_tar(self.hostile(tmp, name, kind), tmp / "out")
                self.assertFalse((tmp / "outside").exists())

    def test_RoundTripPreservesTheDigest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tree = make_tree(tmp / "t", "macos")
            pa.archive_tree(tree, tmp / "h.tar", "tar")
            self.assertGreater(pa.extract_tar(tmp / "h.tar", tmp / "out"), 0)
            self.assertEqual(stage.tree_digest(tree), stage.tree_digest(tmp / "out"))
            with self.assertRaisesRegex(pa.ArchiveError, "already exists"):
                pa.extract_tar(tmp / "h.tar", tmp / "out")


class ManifestMergeTests(unittest.TestCase):
    """Type: integration. Scenario: the manifest job merges three one-leg parts (D4)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.art = self.tmp / "artifacts"
        # Each leg packaged in its own workspace, as the per-leg CI jobs do.
        for leg in ("linux-x64", "windows-x64", "macos-arm64"):
            own = self.tmp / leg
            make_tree(own / "artifacts" / "install" / leg, pa.leg_os(leg))
            proc = run_package(own, ["--leg", leg])
            assert proc.returncode == 0, proc.stdout + proc.stderr
            part = self.art / "parts" / f"dist-{leg}"
            part.mkdir(parents=True)
            for f in (own / "artifacts" / "dist").iterdir():
                (part / f.name).write_bytes(f.read_bytes())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_MergesThreePartsIntoOneManifest(self) -> None:
        proc = run_package(self.tmp, ["--leg", "linux-x64", "--only", "manifest"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok: package manifest: manifest.json: 3 legs, version ", proc.stdout)
        dist = self.art / "dist"
        manifest = json.loads((dist / "manifest.json").read_text())
        self.assertEqual([e["leg"] for e in manifest["legs"]],
                         ["linux-x64", "macos-arm64", "windows-x64"])
        for entry in manifest["legs"]:
            self.assertEqual(entry["sha256"],
                             hashlib.sha256((dist / entry["file"]).read_bytes()).hexdigest())
        self.assertEqual(len(list(dist.iterdir())), 7)

    def test_CorruptedPartArchiveIsRefused(self) -> None:
        part = self.art / "parts" / "dist-windows-x64"
        archive = next(part.glob("*.zip"))
        archive.write_bytes(archive.read_bytes() + b"x")
        proc = run_package(self.tmp, ["--leg", "linux-x64", "--only", "manifest"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("digest differs from its part's manifest entry", proc.stdout)

    def test_MissingLegPartIsRefused(self) -> None:
        import shutil
        shutil.rmtree(self.art / "parts" / "dist-macos-arm64")
        proc = run_package(self.tmp, ["--leg", "linux-x64", "--only", "manifest"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("no part for leg macos-arm64", proc.stdout)

    def test_OnlyManifestWithoutPartsExits2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            proc = run_package(Path(d), ["--leg", "linux-x64", "--only", "manifest"])
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("manifest parts missing", proc.stdout)


if __name__ == "__main__":
    unittest.main()
