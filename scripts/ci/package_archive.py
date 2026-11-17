#!/usr/bin/env python3
"""Deterministic archive, SHA-256 and ``manifest.json`` of one packaging leg (milestone 0012 Story 03.0 Task 01).

The ``package`` stage of the pipeline (ADR 0005 D1/D4): ``stage.py package
--leg <leg>`` calls this module in-process on the build stage's tested
``cmake --install`` tree, ``$CI_ARTIFACT_DIR/install/<leg>/``, and writes::

    $CI_ARTIFACT_DIR/dist/libbios-<version>-<leg>.<ext>          # .zip on windows-*, else .tar.gz
    $CI_ARTIFACT_DIR/dist/libbios-<version>-<leg>.<ext>.sha256   # "<hex>  <file>" (sha256sum -c form)
    $CI_ARTIFACT_DIR/dist/manifest.json                          # D4: {version, commit, legs: [...]}

It never configures, builds or edits the tree: the archive holds exactly the
installed files (ADR 0004 option 3, ADR 0005 DD1). It **refuses** a tree that
is not what the maintainer ruled ships (status.md 2026-11-16, ruling I2):

- required: ``libsmbios`` (static archive, headers, ``libsmbiosConfig.cmake``;
  Story 01.0) and ``smbios_c`` (shared library, header, ``smbios_c.pc``;
  milestone 0007) - :data:`REQUIRED`;
- excluded: the ``smbios`` CLI and ``smbios_gui`` (no ``install()`` rule yet;
  milestone 0015 Story 01.0) - :func:`excluded_entries`. When 0015 adds those
  rules this check fails on purpose, so shipping them is a recorded decision.

Determinism (the same tree archived twice is byte-identical): entries in
sorted POSIX-path order with their directories, a fixed timestamp
(:data:`FIXED_MTIME`, 1980-01-01, the earliest a zip can store), owner
``0:0`` with empty names, and mode ``0755`` for directories and for files with
any execute bit, ``0644`` otherwise. The gzip header carries no file name and
mtime 0; zip entries are always marked as made on Unix so the modes survive.

Platform metadata for the manifest is read from the installed shared library
itself (:func:`platform_meta`), so it describes the tested binary, not the
packaging runner: the highest ``GLIBC_x.y`` symbol-version requirement of the
ELF objects (``glibc``), the Mach-O minimum macOS (``macos_min``,
``LC_BUILD_VERSION``/``LC_VERSION_MIN_MACOSX``), or the PE linker version of
the DLL (``msvc``, e.g. ``14.44``; the MSVC toolset's linker).

Digests are computed once, here. ``verify`` and ``publish`` (Story 04.0) read
them from ``manifest.json`` and refuse any mismatch (D4); they never recompute
a digest to decide what a file *should* be.

A library, not a command: it prints nothing (policy P11, the single emitter is
``ci_step``); every failure is an :class:`ArchiveError` whose message is
value-free apart from relative tree paths. Python 3.9+ standard library only.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
import struct
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Literal, Mapping, Optional, Sequence, Tuple

#: Archive base name (ADR 0005 D4 / status.md 2026-11-12: ``libbios-<version>-<os>-<arch>``).
ARCHIVE_PREFIX = "libbios"
#: ``manifest.json`` schema version.
MANIFEST_SCHEMA = 1
MANIFEST_NAME = "manifest.json"
#: Fixed entry timestamp: 1980-01-01T00:00:00Z, the earliest DOS date a zip can hold.
FIXED_MTIME = 315532800
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
#: Normalised modes.
DIR_MODE = 0o755
EXEC_MODE = 0o755
FILE_MODE = 0o644
#: Compression level of both formats (fixed so the output is stable).
COMPRESS_LEVEL = 9
_CHUNK = 1 << 20

Format = Literal["zip", "tar.gz", "tar"]
#: ``zip``/``tar.gz`` are the product formats (D4); ``tar`` (uncompressed) is
#: only the build -> package stage hand-off of ``install/<leg>/`` (one literal
#: file, as policy P15 requires), never a published artifact.
FORMATS: Tuple[str, ...] = ("zip", "tar.gz", "tar")

#: A packaging leg (``<os>-<arch>``, ADR 0005 D1) and a D5 version value.
LEG = re.compile(r"^(linux|windows|macos)-(x64|arm64)$")
VERSION = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z.]{1,40})?$")
#: A commit id as recorded in the manifest (40-hex, or ``unknown`` on a local run without git).
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|unknown)$")
#: A build configuration name (``Release``, ...).
CONFIG = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
#: Manifest fields of one leg entry, in D4 order, plus the platform-minimum key per OS.
LEG_FIELDS: Tuple[str, ...] = ("leg", "file", "sha256", "bytes", "config", "install_sha256",
                               "files")
PLATFORM_KEYS: Dict[str, str] = {"linux": "glibc", "macos": "macos_min", "windows": "msvc"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_META_VALUE = re.compile(r"^[0-9A-Za-z._+-]{1,64}$")

#: Installed paths every archive must hold (status.md 2026-11-16 ruling I2), per OS.
#: Names are the ones the install rules in ``libsmbios/CMakeLists.txt`` and
#: ``smbios_c/CMakeLists.txt`` produce.
_COMMON_REQUIRED: Tuple[str, ...] = (
    "include/libsmbios/smbios.hpp",
    "lib/cmake/libsmbios/libsmbiosConfig.cmake",
    "lib/cmake/libsmbios/libsmbiosConfigVersion.cmake",
    "include/smbios/smbios.h",
    "include/smbios_c/smbios_c_export.h",
    "lib/pkgconfig/smbios_c.pc",
)
REQUIRED: Dict[str, Tuple[str, ...]] = {
    "linux": _COMMON_REQUIRED + ("lib/libsmbios.a", "lib/libsmbios_c.so"),
    "macos": _COMMON_REQUIRED + ("lib/libsmbios.a", "lib/libsmbios_c.dylib"),
    "windows": _COMMON_REQUIRED + ("lib/libsmbios.lib", "lib/smbios_c.lib", "bin/smbios_c.dll"),
}
#: File names that must never ship (ruling I2): the CLI and every GUI binary
#: (``smbios_gui``, the test-only ``smbios_gui_autoexit``, their ``.exe``/``.pdb``).
_EXCLUDED_NAMES: Tuple[str, ...] = ("smbios", "smbios.exe", "smbios.pdb")
_EXCLUDED_PREFIX = "smbios_gui"


class ArchiveError(Exception):
    """The tree, the archive or the manifest is not acceptable; the message names the reason."""


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

def _show(rel: str) -> str:
    """Return a relative tree path if it is plain, else ``<path>`` (never echo a hostile name)."""
    return rel if re.fullmatch(r"[A-Za-z0-9._/+-]{1,160}", rel) else "<path>"


def tree_entries(src: Path) -> List[Tuple[str, Path, bool]]:
    """Return ``(relative POSIX path, path, is_dir)`` for every entry under ``src``, sorted.

    Raises:
        ArchiveError: ``src`` is missing, a symlink or not a directory; the
            tree holds a symlink or a non-regular file; or it holds no file.
    """
    try:
        st = os.lstat(src)
    except OSError:
        raise ArchiveError("install tree missing") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ArchiveError("install tree root is not a directory")
    entries: List[Tuple[str, Path, bool]] = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            path = here / name
            rel = path.relative_to(src).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise ArchiveError(f"install tree contains a symlink: {_show(rel)}")
            if stat.S_ISDIR(mode):
                entries.append((rel, path, True))
            elif stat.S_ISREG(mode):
                entries.append((rel, path, False))
            else:
                raise ArchiveError(f"install tree contains a non-regular file: {_show(rel)}")
    if not any(not is_dir for _, _, is_dir in entries):
        raise ArchiveError("install tree is empty")
    return sorted(entries, key=lambda e: e[0])


def _file_mode(path: Path) -> int:
    return EXEC_MODE if os.stat(path).st_mode & 0o111 else FILE_MODE


# ---------------------------------------------------------------------------
# Archive, digest
# ---------------------------------------------------------------------------

def archive_tree(src: Path, dest: Path, fmt: Format) -> Path:
    """Archive the tree ``src`` into the file ``dest`` (reproducibly); return ``dest``.

    Entry names are paths relative to ``src`` (no top-level directory), so the
    archive extracts to exactly the installed layout (``lib/``, ``include/``,
    ...). The file is written next to ``dest`` and renamed into place, so a
    failure never leaves a partial archive under the final name.

    Raises:
        ArchiveError: ``fmt`` is unknown, the tree is refused
            (:func:`tree_entries`), ``dest`` is a symlink or lies inside ``src``.
    """
    if fmt not in FORMATS:
        raise ArchiveError(f"archive format must be one of: {', '.join(FORMATS)}")
    entries = tree_entries(src)
    src_real = Path(os.path.realpath(src))
    dest_real = Path(os.path.realpath(dest.parent)) / dest.name
    if src_real == dest_real or src_real in dest_real.parents:
        raise ArchiveError("archive destination is inside the install tree")
    if dest.is_symlink():
        raise ArchiveError("archive destination is a symlink")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    try:
        if fmt == "zip":
            _write_zip(entries, tmp)
        elif fmt == "tar":
            _write_tar(entries, tmp)
        else:
            _write_tar_gz(entries, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest


def _write_tar_gz(entries: Sequence[Tuple[str, Path, bool]], out: Path) -> None:
    with open(out, "xb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0,
                          compresslevel=COMPRESS_LEVEL) as gz:
        _write_tar_entries(entries, gz)


def _write_tar(entries: Sequence[Tuple[str, Path, bool]], out: Path) -> None:
    with open(out, "xb") as raw:
        _write_tar_entries(entries, raw)


def _write_tar_entries(entries: Sequence[Tuple[str, Path, bool]], fileobj) -> None:
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, path, is_dir in entries:
            info = tarfile.TarInfo(rel)
            info.mtime = FIXED_MTIME
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = DIR_MODE
                tar.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.mode = _file_mode(path)
            info.size = os.stat(path).st_size
            with open(path, "rb") as fh:
                tar.addfile(info, fh)


def _write_zip(entries: Sequence[Tuple[str, Path, bool]], out: Path) -> None:
    with open(out, "xb") as raw, \
            zipfile.ZipFile(raw, mode="w", compression=zipfile.ZIP_DEFLATED,
                            compresslevel=COMPRESS_LEVEL) as zf:
        for rel, path, is_dir in entries:
            info = zipfile.ZipInfo(rel + ("/" if is_dir else ""), date_time=_ZIP_DATE)
            info.create_system = 3  # Unix: the mode bits below are honoured on extraction
            if is_dir:
                info.external_attr = ((stat.S_IFDIR | DIR_MODE) << 16) | 0x10
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, b"")
                continue
            info.external_attr = (stat.S_IFREG | _file_mode(path)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.file_size = os.stat(path).st_size
            with open(path, "rb") as fh, zf.open(info, mode="w") as target:
                for chunk in iter(lambda: fh.read(_CHUNK), b""):
                    target.write(chunk)


#: Most members and bytes a hand-off tar may expand to (an install tree is ~100 files, a few MiB).
_EXTRACT_MAX_MEMBERS = 10_000
_EXTRACT_MAX_BYTES = 2 << 30


def extract_tar(archive: Path, dest: Path) -> int:
    """Extract the hand-off ``archive`` (an uncompressed tar) into the new directory ``dest``.

    The tar came through a platform artifact store, so it is untrusted input:
    only regular files and directories are written, every member name must be
    a plain relative path (no absolute path, no ``..``, no backslash), member
    count and total size are bounded, and ``dest`` must not exist yet. Modes
    are the normalised ones of :func:`archive_tree`, never the member's own.
    Returns the number of files written. Whether the result is the tree the
    build stage tested is the caller's check (the install-tree digest).

    Raises:
        ArchiveError: ``archive`` is missing, a symlink or not a tar; ``dest``
            exists; or a member is refused.
    """
    if archive.is_symlink() or not archive.is_file():
        raise ArchiveError("hand-off archive missing")
    if dest.exists() or dest.is_symlink():
        raise ArchiveError("extraction target already exists")
    files = 0
    total = 0
    try:
        with tarfile.open(archive, mode="r:") as tar:
            members = tar.getmembers()
            if len(members) > _EXTRACT_MAX_MEMBERS:
                raise ArchiveError("hand-off archive has too many members")
            dest.mkdir(parents=True)
            for member in members:
                name = member.name
                parts = name.split("/")
                if (not name or name.startswith("/") or "\\" in name or ":" in parts[0]
                        or any(p in ("", ".", "..") for p in parts)):
                    raise ArchiveError(f"hand-off archive member refused: {_show(name)}")
                target = dest.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isreg():
                    raise ArchiveError(f"hand-off archive member is not a regular file: "
                                       f"{_show(name)}")
                total += member.size
                if total > _EXTRACT_MAX_BYTES:
                    raise ArchiveError("hand-off archive is too large")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ArchiveError(f"hand-off archive member unreadable: {_show(name)}")
                with source, open(target, "xb") as out:
                    for chunk in iter(lambda: source.read(_CHUNK), b""):
                        out.write(chunk)
                os.chmod(target, EXEC_MODE if member.mode & 0o111 else FILE_MODE)
                files += 1
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"hand-off archive unreadable ({type(exc).__name__})") from None
    return files


def sha256_file(path: Path) -> str:
    """Return the lower-case hex SHA-256 of the file at ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_file(archive: Path, digest: str) -> Path:
    """Write ``<archive>.sha256`` as ``<hex>  <basename>\\n`` (``sha256sum -c`` form); return it.

    Raises:
        ArchiveError: ``digest`` is not 64 lower-case hex characters.
    """
    if not _HEX64.match(digest):
        raise ArchiveError("sha256 must be 64 lower-case hex characters")
    out = archive.with_name(archive.name + ".sha256")
    _write_text_atomic(out, f"{digest}  {archive.name}\n")
    return out


def archive_name(version: str, leg: str) -> str:
    """Return ``libbios-<version>-<leg>.<ext>`` (``.zip`` on ``windows-*``, else ``.tar.gz``).

    Raises:
        ArchiveError: ``version`` or ``leg`` is outside its allowlist.
    """
    if not VERSION.match(version):
        raise ArchiveError("version must be X.Y.Z[-pre] (ADR 0005 D5)")
    if not LEG.match(leg):
        raise ArchiveError("leg must be <linux|windows|macos>-<x64|arm64>")
    return f"{ARCHIVE_PREFIX}-{version}-{leg}.{archive_format(leg)}"


def archive_format(leg: str) -> Format:
    """Return the archive format of a packaging leg: ``zip`` on Windows, ``tar.gz`` elsewhere."""
    return "zip" if leg.startswith("windows-") else "tar.gz"


def leg_os(leg: str) -> str:
    """Return the OS part of a packaging leg name (``linux``, ``windows``, ``macos``)."""
    match = LEG.match(leg)
    if match is None:
        raise ArchiveError("leg must be <linux|windows|macos>-<x64|arm64>")
    return match.group(1)


# ---------------------------------------------------------------------------
# Contents policy (status.md 2026-11-16, ruling I2)
# ---------------------------------------------------------------------------

def missing_entries(src: Path, os_name: str) -> List[str]:
    """Return the :data:`REQUIRED` paths of ``os_name`` that ``src`` does not hold as regular files."""
    missing = []
    for rel in REQUIRED[os_name]:
        path = src.joinpath(*rel.split("/"))
        if path.is_symlink() or not path.is_file():
            missing.append(rel)
    return missing


def excluded_entries(src: Path) -> List[str]:
    """Return every file under ``src`` that must not ship: the ``smbios`` CLI or a GUI binary."""
    found = []
    for rel, _path, is_dir in tree_entries(src):
        name = rel.rsplit("/", 1)[-1]
        if not is_dir and (name in _EXCLUDED_NAMES or name.startswith(_EXCLUDED_PREFIX)):
            found.append(rel)
    return found


def check_contents(src: Path, os_name: str) -> None:
    """Refuse a tree that does not match the shipped-contents ruling.

    Raises:
        ArchiveError: A required path is missing, or the CLI/GUI is present.
    """
    missing = missing_entries(src, os_name)
    if missing:
        more = f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""
        raise ArchiveError(f"install tree lacks {missing[0]}{more}")
    excluded = excluded_entries(src)
    if excluded:
        raise ArchiveError(f"install tree ships {_show(excluded[0])}: the CLI and GUI are "
                           "excluded until milestone 0015 Story 01.0 (status.md 2026-11-16)")


# ---------------------------------------------------------------------------
# Platform metadata read from the installed binaries
# ---------------------------------------------------------------------------

_GLIBC = re.compile(rb"GLIBC_(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}))?\x00")
_MH_MAGIC_64 = 0xFEEDFACF
_MH_MAGIC = 0xFEEDFACE
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_LC_BUILD_VERSION = 0x32
_LC_VERSION_MIN_MACOSX = 0x24
_PLATFORM_MACOS = 1
#: Largest binary read for metadata (the installed libraries are a few MiB).
_META_MAX_BYTES = 256 << 20


def _read_binary(path: Path) -> bytes:
    if os.stat(path).st_size > _META_MAX_BYTES:
        raise ArchiveError("installed library is too large to inspect")
    return path.read_bytes()


def glibc_requirement(data: bytes) -> Optional[Tuple[int, ...]]:
    """Return the highest ``GLIBC_x.y[.z]`` version an ELF object names, or ``None``.

    The names are the symbol-version requirements in the dynamic string table
    (``.gnu.version_r``); the highest one is the oldest glibc that can load it.
    """
    if not data.startswith(b"\x7fELF"):
        return None
    found = [tuple(int(g) for g in m.groups() if g is not None) for m in _GLIBC.finditer(data)]
    return max(found) if found else None


def _macho_slice_min(data: bytes, offset: int) -> Optional[Tuple[int, int, int]]:
    if offset + 28 > len(data):
        return None
    (magic,) = struct.unpack_from("<I", data, offset)
    if magic not in (_MH_MAGIC_64, _MH_MAGIC):
        return None
    ncmds, sizeofcmds = struct.unpack_from("<II", data, offset + 16)
    cursor = offset + (32 if magic == _MH_MAGIC_64 else 28)
    end = min(len(data), cursor + sizeofcmds)
    for _ in range(ncmds):
        if cursor + 8 > end:
            return None
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > end:
            return None
        packed = None
        if cmd == _LC_BUILD_VERSION and cmdsize >= 16:
            platform, packed = struct.unpack_from("<II", data, cursor + 8)
            if platform != _PLATFORM_MACOS:
                packed = None
        elif cmd == _LC_VERSION_MIN_MACOSX and cmdsize >= 12:
            (packed,) = struct.unpack_from("<I", data, cursor + 8)
        if packed is not None:
            return (packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF)
        cursor += cmdsize
    return None


def macos_minimum(data: bytes) -> Optional[Tuple[int, int, int]]:
    """Return the minimum macOS of a Mach-O (thin or universal) image, or ``None``.

    For a universal binary the highest slice minimum is returned: the image
    as a whole needs at least that.
    """
    if len(data) < 8:
        return None
    (fat,) = struct.unpack_from(">I", data, 0)
    if fat not in (_FAT_MAGIC, _FAT_MAGIC_64):
        return _macho_slice_min(data, 0)
    (count,) = struct.unpack_from(">I", data, 4)
    size = 32 if fat == _FAT_MAGIC_64 else 20
    found = []
    for i in range(min(count, 32)):
        at = 8 + i * size
        if at + size > len(data):
            return None
        if fat == _FAT_MAGIC_64:
            (offset,) = struct.unpack_from(">Q", data, at + 8)
        else:
            (offset,) = struct.unpack_from(">I", data, at + 8)
        value = _macho_slice_min(data, offset)
        if value is not None:
            found.append(value)
    return max(found) if found else None


def pe_linker_version(data: bytes) -> Optional[Tuple[int, int]]:
    """Return ``(major, minor)`` of the linker that produced a PE image, or ``None``."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    (pe,) = struct.unpack_from("<I", data, 0x3C)
    if pe + 28 > len(data) or data[pe:pe + 4] != b"PE\x00\x00":
        return None
    magic, major, minor = struct.unpack_from("<HBB", data, pe + 24)
    if magic not in (0x10B, 0x20B):
        return None
    return major, minor


def _version_text(parts: Sequence[int], keep: int = 2) -> str:
    shown = list(parts[:keep]) + [p for p in parts[keep:] if p]
    return ".".join(str(p) for p in shown)


def platform_meta(src: Path, os_name: str) -> Dict[str, str]:
    """Return ``{glibc|macos_min|msvc: <version>}`` read from the installed ``smbios_c`` library.

    Raises:
        ArchiveError: The library is missing or no version could be read from it.
    """
    lib = {"linux": "lib/libsmbios_c.so", "macos": "lib/libsmbios_c.dylib",
           "windows": "bin/smbios_c.dll"}[os_name]
    path = src.joinpath(*lib.split("/"))
    if path.is_symlink() or not path.is_file():
        raise ArchiveError(f"install tree lacks {lib}")
    data = _read_binary(path)
    key = PLATFORM_KEYS[os_name]
    if os_name == "linux":
        found: Optional[Sequence[int]] = glibc_requirement(data)
        value = _version_text(found) if found else None
    elif os_name == "macos":
        found = macos_minimum(data)
        value = _version_text(found) if found else None
    else:
        found = pe_linker_version(data)
        value = f"{found[0]}.{found[1]:02d}" if found else None
    if value is None:
        raise ArchiveError(f"cannot read the {key} requirement from {lib}")
    return {key: value}


# ---------------------------------------------------------------------------
# manifest.json
# ---------------------------------------------------------------------------

def _write_text_atomic(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ArchiveError(f"{path.name} is a symlink")
    tmp = path.with_name(path.name + ".partial")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    with open(tmp, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_manifest(manifest: Path) -> Dict[str, object]:
    """Return the manifest at ``manifest`` (an empty shell if absent), shape-checked.

    Raises:
        ArchiveError: The file is a symlink, unreadable, or not a schema-1 manifest.
    """
    if manifest.is_symlink():
        raise ArchiveError(f"{MANIFEST_NAME} is a symlink")
    if not manifest.exists():
        return {"schema": MANIFEST_SCHEMA, "legs": []}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ArchiveError(f"{MANIFEST_NAME} is not readable JSON") from None
    if not (isinstance(data, dict) and data.get("schema") == MANIFEST_SCHEMA
            and isinstance(data.get("legs"), list)
            and all(isinstance(e, dict) and isinstance(e.get("leg"), str)
                    for e in data["legs"])):
        raise ArchiveError(f"{MANIFEST_NAME} is not a schema-{MANIFEST_SCHEMA} manifest")
    return data


def update_manifest(manifest: Path, leg: str, archive: Path, meta: Mapping[str, str]) -> None:
    """Merge ``leg``'s entry into ``manifest`` (ADR 0005 D4), creating the file if needed.

    ``meta`` must hold ``version``, ``commit``, ``config``, ``install_sha256``
    and ``files``, and the platform key of the leg's OS
    (:data:`PLATFORM_KEYS`); it may hold ``sha256`` (the archive digest already
    computed by the caller, so it is computed once). Otherwise the digest is
    computed here. The entry of the same leg is replaced (a re-run); every
    other leg is kept. Legs are sorted by name and the JSON is written with
    sorted keys, so the file is deterministic. ``version`` and ``commit``
    must equal those already in the manifest: one manifest describes one
    commit at one version.

    Raises:
        ArchiveError: A value is outside its allowlist, a required key is
            missing, the archive is missing, or the manifest belongs to another
            version or commit.
    """
    os_name = leg_os(leg)
    platform_key = PLATFORM_KEYS[os_name]
    for key in ("version", "commit", "config", "install_sha256", "files", platform_key):
        if not isinstance(meta.get(key), str):
            raise ArchiveError(f"manifest metadata lacks {key}")
    checks = {"version": VERSION, "commit": COMMIT, "config": CONFIG, "install_sha256": _HEX64,
              "files": re.compile(r"^\d{1,9}$"), platform_key: _META_VALUE}
    for key, rx in checks.items():
        if not rx.match(meta[key]):
            raise ArchiveError(f"manifest metadata {key} is malformed")
    if archive.name != archive_name(meta["version"], leg):
        raise ArchiveError("archive name does not match libbios-<version>-<leg>.<ext>")
    if archive.is_symlink() or not archive.is_file():
        raise ArchiveError(f"archive missing: {archive.name}")
    digest = meta.get("sha256") or sha256_file(archive)
    if not _HEX64.match(digest):
        raise ArchiveError("manifest metadata sha256 is malformed")

    data = read_manifest(manifest)
    for key in ("version", "commit"):
        have = data.get(key)
        if have is not None and have != meta[key]:
            raise ArchiveError(f"{MANIFEST_NAME} is for another {key}; clear dist/ first")
        data[key] = meta[key]
    entry: Dict[str, object] = {
        "leg": leg, "file": archive.name, "sha256": digest, "bytes": archive.stat().st_size,
        "config": meta["config"], "install_sha256": meta["install_sha256"],
        "files": int(meta["files"]), platform_key: meta[platform_key],
    }
    legs = [e for e in data["legs"] if e.get("leg") != leg] + [entry]  # type: ignore[union-attr]
    data["legs"] = sorted(legs, key=lambda e: str(e["leg"]))
    data["schema"] = MANIFEST_SCHEMA
    manifest.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(manifest, json.dumps(data, indent=2, sort_keys=True) + "\n")


def leg_entry(manifest: Path) -> Tuple[str, str, Dict[str, object]]:
    """Return ``(version, commit, entry)`` of a one-leg manifest part (a per-leg package job's).

    Raises:
        ArchiveError: The file is not a schema-1 manifest with exactly one
            well-formed leg entry.
    """
    data = read_manifest(manifest)
    legs = data["legs"]
    version, commit = data.get("version"), data.get("commit")
    if not (isinstance(legs, list) and len(legs) == 1 and isinstance(version, str)
            and VERSION.match(version) and isinstance(commit, str) and COMMIT.match(commit)):
        raise ArchiveError(f"{MANIFEST_NAME} part is not one leg of one version and commit")
    entry = legs[0]
    leg = entry.get("leg")
    if not (isinstance(leg, str) and LEG.match(leg)
            and entry.get("file") == archive_name(version, leg)
            and isinstance(entry.get("sha256"), str) and _HEX64.match(entry["sha256"])):
        raise ArchiveError(f"{MANIFEST_NAME} part has a malformed leg entry")
    return version, commit, entry


def merge_entry(manifest: Path, version: str, commit: str, entry: Mapping[str, object]) -> None:
    """Merge one already-validated leg ``entry`` (:func:`leg_entry`) into ``manifest``.

    The digest is copied, never recomputed: it was computed once, by the leg's
    own package job. Same version/commit rule as :func:`update_manifest`.

    Raises:
        ArchiveError: The manifest is for another version or commit.
    """
    data = read_manifest(manifest)
    for key, value in (("version", version), ("commit", commit)):
        have = data.get(key)
        if have is not None and have != value:
            raise ArchiveError(f"{MANIFEST_NAME} is for another {key}; clear dist/ first")
        data[key] = value
    legs = [e for e in data["legs"] if e.get("leg") != entry["leg"]]  # type: ignore[union-attr]
    data["legs"] = sorted(legs + [dict(entry)], key=lambda e: str(e["leg"]))
    data["schema"] = MANIFEST_SCHEMA
    manifest.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(manifest, json.dumps(data, indent=2, sort_keys=True) + "\n")
