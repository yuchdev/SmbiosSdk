#!/usr/bin/env python3
"""Clean-room verification of one release archive (milestone 0012, Story 03.0 Task 03).

Re-runs ADR 0004's Validation/Rollout item against a **release archive**
(``libbios-<version>-<leg>.{tar.gz,zip}``, the ``package`` stage's output), never
against this repository's build or install tree:

1. take the archive by path (offline), or resolve it from a ``dist/`` directory
   through its ``manifest.json`` (:func:`resolve_archive`); this module never
   downloads - fetching is the caller's (the ``verify`` stage's) job;
2. verify it against its ``.sha256`` sidecar **and** its ``manifest.json``
   entry (check 5 of the task, run *first*: nothing is extracted before its
   bytes are known to be the published ones);
3. extract it into an empty scratch directory (regular files and directories
   only, plain relative names, bounded), then check the layout
   (``package_archive.REQUIRED``, no CLI/GUI) and that the extracted tree's
   digest equals the manifest's ``install_sha256`` - the bytes extracted are
   the bytes the build stage tested;
4. ``pkg-config --cflags --libs smbios_c`` against the extracted
   ``lib/pkgconfig/smbios_c.pc`` only, then compile, link and run
   ``tests/packaging/smbios_c_vcpkg_consumer/main.c`` from those flags alone
   (a host without a readable SMBIOS table passes when ``smbios_open()``
   reports ``SMBIOS_ERR_NO_TABLE_FOUND``, its documented degradation);
5. configure, build and run ``tests/packaging/libsmbios_consumer`` with
   ``find_package(libsmbios CONFIG)`` resolved through ``CMAKE_PREFIX_PATH``
   to the extracted ``lib/cmake/libsmbios/``. The fixture has no whole-archive
   flag of its own (asserted here), so this re-proves ADR 0004 DD1 from a
   release archive;
6. the export allowlist of the extracted ``smbios_c`` shared library
   (:mod:`export_symbols`: ``nm`` for ``.so``/``.dylib``, ``dumpbin /exports``
   for ``.dll``): every exported symbol is ``smbios_*`` (ADR 0004 Validation;
   0012 Story 04.0 Task 04). It needs the archive's own OS on the host, not
   its architecture.

**The clean room is enforced, not assumed.** Children get an allowlisted
environment (:func:`clean_environment`): ``VCPKG_*``, ``PKG_CONFIG_*``,
``CMAKE_*``, ``CPATH``, ``LIBRARY_PATH``, the ``DYLD_*``/``LD_*`` loader
variables, ``*FLAGS`` and every package hint (``BOOST_ROOT``,
``libsmbios_DIR``...) never reach them. ``pkg-config`` searches only the
extracted ``lib/pkgconfig`` (``PKG_CONFIG_LIBDIR``), CMake's package
registries are off, and both fixtures are copied into the scratch directory
first. Then it is **asserted**: a surviving environment value, a compile or
link command line, the resolved ``pkg-config`` output, the consumer's
``CMakeCache.txt`` or generated build files that mention this repository or
the vcpkg root fails the run (:class:`VerifyError`), and the include/library
directories and ``libsmbios_DIR`` must lie inside the extracted archive.

``smbios_c.pc`` is relocatable (``prefix=${pcfiledir}/../..``, H1 resolved
2026-11-17), so the literal ADR command is the expected path and must resolve
inside the extracted archive. An archive packaged before that fix carries the
build's configure-time prefix; for it the check falls back to ``pkg-config
--define-prefix`` and says so in the returned summary (a pass never hides a
non-relocatable ``.pc``), unless ``require_relocatable_pc`` makes that a failure.

The compile/link/run checks need a host of the archive's own leg; with
``build=False`` only the platform-independent checks (1, 2, 5, layout,
``pkg-config`` resolution) run and the summary says ``not executed`` for the
rest - never a pass.

A library first: ``stage.py verify`` (milestone 0012 Story 04.0 Task 04,
step ``artifact``) calls :func:`run` in-process and reports through ``ci_step`` (policy
P11: the single emitter), so :func:`run` prints nothing. :class:`VerifyError`
carries a value-free reason: the failing check on the first line, then the
cleaned tail of the tool's output. :func:`main` is a thin manual front end
that reports through ``ci_step`` too. Python 3.9+ standard library only.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse  # noqa: E402
import hashlib  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

import ci_step  # noqa: E402
import consumer_smoke  # noqa: E402
import env_names  # noqa: E402
import export_symbols  # noqa: E402
import package_archive as pa  # noqa: E402
from reporters import child_environment  # noqa: E402

REPO_ROOT = Path(_HERE).resolve().parents[1]
#: The reused fixtures (task instruction: no new fixture).
C_FIXTURE = REPO_ROOT / "tests" / "packaging" / "smbios_c_vcpkg_consumer"
CXX_FIXTURE = REPO_ROOT / "tests" / "packaging" / "libsmbios_consumer"
LABEL = "verify release artifact"
TAIL_LINES = 12
#: Seconds each phase may take (a cold Windows configure is the slowest).
PHASE_TIMEOUT_S: Dict[str, float] = {"pkg-config": 30, "compile c consumer": 120,
                                     "run c consumer": 60, "configure cxx consumer": 300,
                                     "build cxx consumer": 300, "run cxx consumer": 60,
                                     "symbols": 60, "vswhere": 30}
#: Parent-environment variables whose values name a tree the clean room must not touch.
VCPKG_ROOT_NAMES: Tuple[str, ...] = ("VCPKG_ROOT", "VCPKG_INSTALLATION_ROOT")

_ARCHIVE = re.compile(r"^libbios-(?P<version>[0-9A-Za-z.-]{1,60})-"
                      r"(?P<leg>(?:linux|windows|macos)-(?:x64|arm64))\.(?:tar\.gz|zip)$")
_SIDECAR = re.compile(r"^([0-9a-f]{64}) [ *]([^\s/\\]{1,200})\r?\n?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_C_OUTPUT = re.compile(r"^smbios_open=(?:no-table|ok structures=\d{1,6})$")
_CXX_OUTPUT = re.compile(r"^formatters=[1-9]\d{0,3}$")
#: A whole-archive link flag in a fixture's CMake code (comments stripped first).
_WHOLE_ARCHIVE = re.compile(r"whole-archive|force_load|WHOLEARCHIVE", re.IGNORECASE)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]{0,63}$")

#: Most members and bytes an archive may expand to (one is ~100 files, ~20 MiB).
_EXTRACT_MAX_MEMBERS = 10_000
_EXTRACT_MAX_BYTES = 2 << 30
_CHUNK = 1 << 20
_SCAN_MAX_BYTES = 64 << 20

# ---------------------------------------------------------------------------
# Clean-room environment
# ---------------------------------------------------------------------------

#: Exact names a child may inherit (case-insensitive). Everything else is dropped.
_ENV_ALLOW: frozenset = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TMP", "TEMP",
    # Compiler choice (the value is still checked for leaks).
    "CC", "CXX",
    # macOS toolchain discovery.
    "DEVELOPER_DIR", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET",
    # Windows process basics.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    # MSVC developer environment (vcvars): cl needs INCLUDE/LIB/LIBPATH.
    "INCLUDE", "LIB", "LIBPATH", "PLATFORM", "UCRTVERSION", "UNIVERSALCRTSDKDIR",
    "WINDOWSLIBPATH", "WINDOWSSDKBINPATH", "WINDOWSSDKDIR", "WINDOWSSDKLIBVERSION",
    "WINDOWSSDKVERBINPATH", "WINDOWSSDKVERSION", "EXTENSIONSDKDIR", "FRAMEWORKDIR",
    "FRAMEWORKDIR64", "FRAMEWORKVERSION", "FRAMEWORKVERSION64", "DEVENVDIR",
    "VCIDEINSTALLDIR", "VCINSTALLDIR", "VCTOOLSINSTALLDIR", "VCTOOLSREDISTDIR",
    "VCTOOLSVERSION", "VISUALSTUDIOVERSION", "VSINSTALLDIR", "VSCMD_ARG_HOST_ARCH",
    "VSCMD_ARG_TGT_ARCH", "VSCMD_VER",
})
_ENV_ALLOW_PREFIXES: Tuple[str, ...] = ("LC_",)
#: Never passed even if a future allowlist edit would admit them: the names the
#: task requires scrubbed, and every search-path or package-hint variable.
_ENV_DENY_PREFIXES: Tuple[str, ...] = ("VCPKG_", "PKG_CONFIG_", "CMAKE_", "DYLD_", "LD_",
                                       "BOOST", "LIBSMBIOS", "SMBIOS")
_ENV_DENY: frozenset = frozenset({
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH",
    "OBJCPLUS_INCLUDE_PATH", "LIBRARY_PATH", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
    "LDLIBS", "CL", "_CL_", "LINK", "_LINK_", "PWD", "OLDPWD",
})


class VerifyError(Exception):
    """A check failed; the message is the value-free reason (first line names the check)."""


def _denied(name: str) -> bool:
    upper = name.upper()
    return upper in _ENV_DENY or upper.startswith(_ENV_DENY_PREFIXES)


def _allowed(name: str) -> bool:
    upper = name.upper()
    return upper in _ENV_ALLOW or upper.startswith(_ENV_ALLOW_PREFIXES)


def _show_name(name: str) -> str:
    return name if _ENV_NAME.match(name) else "<name>"


def forbidden_roots(repo_root: Path, environ: Mapping[str, str]) -> List[Tuple[str, Path]]:
    """Return ``(label, path)`` of every tree the clean room must not reach.

    This repository (source and, by containment, every in-tree build
    directory) and each vcpkg root named by the *parent* environment.
    A filesystem root is never returned (it would match everything).
    """
    roots: List[Tuple[str, Path]] = [("this repository", repo_root)]
    for name in VCPKG_ROOT_NAMES:
        value = environ.get(name, "")
        if value and os.path.isabs(value):
            roots.append(("the vcpkg root", Path(value)))
    out: List[Tuple[str, Path]] = []
    for label, root in roots:
        for candidate in {root, Path(os.path.realpath(root))}:
            if candidate != Path(candidate.anchor):
                out.append((label, candidate))
    return out


def _root_patterns(roots: Sequence[Tuple[str, Path]]) -> List[Tuple[str, "re.Pattern[str]"]]:
    """One case-insensitive, separator-agnostic, boundary-anchored regex per root."""
    patterns = []
    for label, root in roots:
        parts = [re.escape(p) for p in str(root).replace("\\", "/").split("/")]
        body = r"[\\/]+".join(parts)
        patterns.append((label, re.compile(body + r"(?=$|[\\/\s\"';:,)=])", re.IGNORECASE)))
    return patterns


def find_leak(text: str, roots: Sequence[Tuple[str, Path]]) -> Optional[str]:
    """Return the label of the first forbidden root ``text`` mentions, else ``None``."""
    for label, pattern in _root_patterns(roots):
        if pattern.search(text):
            return label
    return None


def _inside(path: Path, roots: Sequence[Tuple[str, Path]]) -> Optional[str]:
    return find_leak(os.path.realpath(path), roots) or find_leak(str(path), roots)


def clean_environment(environ: Mapping[str, str],
                      roots: Sequence[Tuple[str, Path]]) -> Dict[str, str]:
    """Return the child environment: allowlisted names only, then asserted leak-free.

    Raises:
        VerifyError: An allowlisted variable that survived (``PATH``, ``CC``,
            ``INCLUDE``...) still mentions a forbidden root. The message names
            the variable and the tree, never the value.
    """
    env = child_environment({k: v for k, v in environ.items()
                             if _allowed(k) and not _denied(k)})
    for name, value in sorted(env.items()):
        leak = find_leak(value, roots)
        if leak is not None:
            raise VerifyError(f"clean room: environment variable {_show_name(name)} points into "
                              f"{leak}; unset it or run from a clean shell")
    return env


def assert_no_leak(what: str, text: str, roots: Sequence[Tuple[str, Path]]) -> None:
    """Fail when ``text`` (a command line, tool output or generated file) mentions a forbidden root."""
    leak = find_leak(text, roots)
    if leak is not None:
        raise VerifyError(f"clean room: {what} references {leak}; the check would not prove "
                          "the archive alone")


# ---------------------------------------------------------------------------
# Archive identity, checksums, manifest
# ---------------------------------------------------------------------------

def parse_archive_name(name: str) -> Tuple[str, str]:
    """Return ``(version, leg)`` of a ``libbios-<version>-<leg>.<ext>`` file name.

    Raises:
        VerifyError: The name is not the D4 form, or its extension is not the leg's format.
    """
    match = _ARCHIVE.match(name)
    if match is None:
        raise VerifyError("archive name is not libbios-<version>-<leg>.<ext>")
    version, leg = match.group("version"), match.group("leg")
    try:
        expected = pa.archive_name(version, leg)
    except pa.ArchiveError as exc:
        raise VerifyError(f"archive name: {exc}") from None
    if expected != name:
        raise VerifyError("archive extension does not match its leg (.zip on windows-*)")
    return version, leg


def host_leg() -> Optional[str]:
    """Return this host's packaging leg (``macos-arm64``...), or ``None`` if it has none."""
    os_name = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64",
            "aarch64": "arm64"}.get(platform.machine().lower())
    return f"{os_name}-{arch}" if os_name and arch else None


def resolve_archive(dist_dir: Path, leg: str) -> Path:
    """Return ``dist_dir/<file>`` of ``leg``'s entry in ``dist_dir/manifest.json``.

    Raises:
        VerifyError: No manifest, no entry for ``leg``, or a malformed file name.
    """
    if not pa.LEG.match(leg):
        raise VerifyError("leg must be <linux|windows|macos>-<x64|arm64>")
    try:
        data = pa.read_manifest(dist_dir / pa.MANIFEST_NAME)
    except pa.ArchiveError as exc:
        raise VerifyError(f"manifest: {exc}") from None
    for entry in data["legs"]:  # type: ignore[union-attr]
        if entry.get("leg") == leg:
            name = entry.get("file")
            if not isinstance(name, str) or _ARCHIVE.match(name) is None:
                raise VerifyError("manifest entry has a malformed file name")
            return dist_dir / name
    raise VerifyError(f"manifest has no entry for {leg}")


def read_sidecar(archive: Path) -> str:
    """Return the digest in ``<archive>.sha256`` (``<hex>  <name>``, ``sha256sum -c`` form).

    Raises:
        VerifyError: The sidecar is missing, malformed or names another file.
    """
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise VerifyError(f"checksum: {sidecar.name} missing next to the archive")
    try:
        text = sidecar.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise VerifyError(f"checksum: {sidecar.name} unreadable") from None
    match = _SIDECAR.match(text)
    if match is None:
        raise VerifyError(f"checksum: {sidecar.name} is not '<sha256>  <file>'")
    if match.group(2) != archive.name:
        raise VerifyError(f"checksum: {sidecar.name} names another file")
    return match.group(1)


def manifest_entry(manifest: Path, version: str, leg: str) -> Dict[str, object]:
    """Return ``leg``'s entry of ``manifest`` after checking version and shape.

    Raises:
        VerifyError: Missing/malformed manifest, another version, no or a malformed entry.
    """
    if manifest.is_symlink() or not manifest.is_file():
        raise VerifyError(f"manifest: {pa.MANIFEST_NAME} missing")
    try:
        data = pa.read_manifest(manifest)
    except pa.ArchiveError as exc:
        raise VerifyError(f"manifest: {exc}") from None
    if data.get("version") != version:
        raise VerifyError("manifest: version differs from the archive name")
    entries = [e for e in data["legs"] if e.get("leg") == leg]  # type: ignore[union-attr]
    if len(entries) != 1:
        raise VerifyError(f"manifest: no single entry for {leg}")
    entry = entries[0]
    if not (isinstance(entry.get("sha256"), str) and _HEX64.match(entry["sha256"])
            and isinstance(entry.get("bytes"), int) and isinstance(entry.get("files"), int)
            and isinstance(entry.get("config"), str) and pa.CONFIG.match(entry["config"])
            and isinstance(entry.get("install_sha256"), str)
            and _HEX64.match(entry["install_sha256"])):
        raise VerifyError(f"manifest: entry for {leg} is malformed")
    # Manifests from before the package stage's architecture guard carry no
    # ``arch``; one that does must name the leg's own architecture.
    arch = entry.get("arch")
    if arch is not None and not (isinstance(arch, str) and pa.ARCH.match(arch)
                                 and leg.split("-", 1)[1] in arch.split("+")):
        raise VerifyError(f"manifest: entry for {leg} records another architecture")
    return entry


def verify_digests(archive: Path, manifest: Path, version: str, leg: str) -> Dict[str, object]:
    """Check 5: the archive's SHA-256 equals its sidecar's and its manifest entry's.

    The digest is computed once, here, from the archive bytes; the sidecar and
    the manifest are the two published claims it is compared with (ADR 0005
    D4: they are never recomputed to decide what the archive should be).

    Returns:
        The manifest entry (for the later file-count and install-digest checks).

    Raises:
        VerifyError: Either claim differs, or the entry names another file or size.
    """
    if archive.is_symlink() or not archive.is_file():
        raise VerifyError(f"archive missing: {archive.name}")
    claimed_sidecar = read_sidecar(archive)
    entry = manifest_entry(manifest, version, leg)
    if entry.get("file") != archive.name:
        raise VerifyError("manifest: entry names another file")
    actual = pa.sha256_file(archive)
    if actual != claimed_sidecar:
        raise VerifyError("checksum: archive sha256 differs from its .sha256 file")
    if actual != entry["sha256"]:
        raise VerifyError("checksum: archive sha256 differs from manifest.json")
    if archive.stat().st_size != entry["bytes"]:
        raise VerifyError("manifest: archive size differs from manifest.json")
    return entry


# ---------------------------------------------------------------------------
# Extraction and layout
# ---------------------------------------------------------------------------

def _member_parts(name: str) -> List[str]:
    parts = name.rstrip("/").split("/")
    if (not name or name.startswith("/") or "\\" in name or ":" in parts[0]
            or any(p in ("", ".", "..") for p in parts)):
        raise VerifyError(f"extract: archive member refused: {pa._show(name)}")
    return parts


def _write_member(source, target: Path, total: int, size_cap: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, open(target, "xb") as out:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            total += len(chunk)
            if total > size_cap:
                raise VerifyError("extract: archive is too large")
            out.write(chunk)
    return total


def extract_archive(archive: Path, dest: Path) -> int:
    """Check 2: extract ``archive`` (``.tar.gz`` or ``.zip``) into the new, empty ``dest``.

    The archive is untrusted until verified and still treated as such after:
    only regular files and directories are written, every member name is a
    plain relative path, member count and expanded size are bounded, and
    modes are normalised (``0755`` if any execute bit, else ``0644``).

    Returns:
        The number of files written.

    Raises:
        VerifyError: ``dest`` exists, or the archive is unreadable or holds a refused member.
    """
    if dest.exists() or dest.is_symlink():
        raise VerifyError("extract: scratch prefix already exists")
    files = 0
    total = 0
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                infos = zf.infolist()
                if len(infos) > _EXTRACT_MAX_MEMBERS:
                    raise VerifyError("extract: archive has too many members")
                dest.mkdir(parents=True)
                for info in infos:
                    parts = _member_parts(info.filename)
                    target = dest.joinpath(*parts)
                    mode = info.external_attr >> 16
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if mode and not stat.S_ISREG(mode):
                        raise VerifyError("extract: archive member is not a regular file: "
                                          f"{pa._show(info.filename)}")
                    total = _write_member(zf.open(info), target, total, _EXTRACT_MAX_BYTES)
                    os.chmod(target, pa.EXEC_MODE if mode & 0o111 else pa.FILE_MODE)
                    files += 1
        else:
            with tarfile.open(archive, mode="r:gz") as tar:
                members = tar.getmembers()
                if len(members) > _EXTRACT_MAX_MEMBERS:
                    raise VerifyError("extract: archive has too many members")
                dest.mkdir(parents=True)
                for member in members:
                    parts = _member_parts(member.name)
                    target = dest.joinpath(*parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isreg():
                        raise VerifyError("extract: archive member is not a regular file: "
                                          f"{pa._show(member.name)}")
                    source = tar.extractfile(member)
                    if source is None:
                        raise VerifyError(f"extract: member unreadable: {pa._show(member.name)}")
                    total = _write_member(source, target, total, _EXTRACT_MAX_BYTES)
                    os.chmod(target, pa.EXEC_MODE if member.mode & 0o111 else pa.FILE_MODE)
                    files += 1
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise VerifyError(f"extract: archive unreadable ({type(exc).__name__})") from None
    return files


def tree_digest(root: Path) -> Tuple[str, int]:
    """Return ``(sha256 hex, file count)`` of ``root`` exactly as ``stage.tree_digest`` does.

    SHA-256 over the sorted relative POSIX paths, each followed by its file's
    SHA-256 (``<path>\\0<sha256>\\n``). Restated rather than imported because
    ``stage.py`` is the command that will import this module; a unit test pins
    the two functions to the same value.
    """
    files: List[Tuple[str, Path]] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            files.append((path.relative_to(root).as_posix(), path))
    digest = hashlib.sha256()
    for rel, path in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0" + pa.sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest(), len(files)


def check_layout(prefix: Path, leg: str, entry: Mapping[str, object]) -> None:
    """The extracted tree holds the ruled contents and is the tree the build stage tested.

    Raises:
        VerifyError: A required path (``lib/pkgconfig/smbios_c.pc``,
            ``lib/cmake/libsmbios/...``) is missing - named in the message - the
            CLI/GUI ships, or the file count / install digest differs from the manifest.
    """
    os_name = pa.leg_os(leg)
    missing = pa.missing_entries(prefix, os_name)
    if missing:
        more = f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""
        raise VerifyError(f"layout: archive lacks {missing[0]}{more}")
    excluded = pa.excluded_entries(prefix)
    if excluded:
        raise VerifyError(f"layout: archive ships {pa._show(excluded[0])} (CLI/GUI are excluded)")
    digest, count = tree_digest(prefix)
    if count != entry["files"]:
        raise VerifyError("layout: extracted file count differs from manifest.json")
    if digest != entry["install_sha256"]:
        raise VerifyError("layout: extracted tree differs from the tested install tree "
                          "(install_sha256)")


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------

def _run(phase: str, argv: Sequence[str], env: Mapping[str, str],
         cwd: Optional[Path] = None) -> str:
    """Run one phase: given environment, no stdin, bounded time, cleaned failure tail."""
    timeout = PHASE_TIMEOUT_S.get(phase, 300)
    try:
        proc = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                              errors="replace", env=dict(env), check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VerifyError(f"{phase} timed out after {timeout}s") from None
    except OSError as exc:
        raise VerifyError(f"{phase} could not start ({type(exc).__name__})") from None
    if proc.returncode != 0:
        tail = "\n".join(ci_step.safe_line(line.encode("utf-8", "replace"))
                         for line in proc.stdout.splitlines()[-TAIL_LINES:])
        raise VerifyError(f"{phase} failed (exit {proc.returncode})\n{tail}")
    return proc.stdout


def _which(names: Iterable[str], env: Mapping[str, str]) -> Optional[str]:
    for name in names:
        found = shutil.which(name, path=env.get("PATH", ""))
        if found:
            return found
    return None


def _fresh(path: Path) -> None:
    if path.is_symlink():
        raise VerifyError("scratch path is a symlink")
    if path.exists():
        shutil.rmtree(path)


def _copy_fixture(fixture: Path, dest: Path, names: Sequence[str]) -> None:
    """Copy the fixture's files into the scratch tree, so no source path is the repository's."""
    dest.mkdir(parents=True)
    for name in names:
        src = fixture / name
        if src.is_symlink() or not src.is_file():
            raise VerifyError(f"fixture file missing: {name}")
        shutil.copyfile(src, dest / name)


# ---------------------------------------------------------------------------
# Check 3: pkg-config + C consumer
# ---------------------------------------------------------------------------

def _split_flags(text: str) -> List[str]:
    if os.name == "nt":
        return [t.strip('"') for t in shlex.split(text, posix=False)]
    return shlex.split(text)


def _under(path: str, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        return False
    return True


def parse_pkg_config(output: str, prefix: Path) -> Tuple[List[str], List[str], List[str], bool]:
    """Return ``(include dirs, library dirs, libraries, inside prefix)`` of ``pkg-config`` output.

    Only ``-I``, ``-L`` and ``-l`` are accepted: ``smbios_c.pc`` declares
    nothing else, and an unexpected flag from an archive is refused rather
    than passed to a compiler. ``inside prefix`` is whether every directory
    lies in the extracted archive.

    Raises:
        VerifyError: Another flag, or no include/library directory or no ``-lsmbios_c``.
    """
    incs: List[str] = []
    libdirs: List[str] = []
    libs: List[str] = []
    for token in _split_flags(output.strip()):
        if token.startswith("-I") and len(token) > 2:
            incs.append(token[2:])
        elif token.startswith("-L") and len(token) > 2:
            libdirs.append(token[2:])
        elif token.startswith("-l") and re.fullmatch(r"-l[A-Za-z0-9_.+-]{1,64}", token):
            libs.append(token[2:])
        else:
            raise VerifyError("pkg-config: smbios_c.pc yields an unexpected flag")
    if not incs or not libdirs or "smbios_c" not in libs:
        raise VerifyError("pkg-config: smbios_c.pc yields no include dir, library dir or "
                          "-lsmbios_c")
    inside = all(_under(d, prefix) for d in incs + libdirs)
    return incs, libdirs, libs, inside


def resolve_pkg_config(prefix: Path, env: Mapping[str, str],
                       roots: Sequence[Tuple[str, Path]], require_relocatable: bool
                       ) -> Tuple[List[str], List[str], List[str], str]:
    """Run ``pkg-config --cflags --libs smbios_c`` against the extracted ``.pc`` only.

    The literal ADR 0004 command is the expected path. If its directories are
    not inside the extracted archive (an archive packaged before H1's fix, with
    a configure-time ``prefix``), ``--define-prefix`` unless ``require_relocatable``.

    Returns:
        ``(include dirs, library dirs, libraries, mode)`` where ``mode`` is
        ``relocatable`` or ``define-prefix``.

    Raises:
        VerifyError: ``pkg-config`` is missing or fails, the output leaks a
            forbidden root, or no mode resolves inside the archive.
    """
    tool = _which(("pkg-config", "pkgconf"), env)
    if tool is None:
        raise VerifyError("pkg-config: neither pkg-config nor pkgconf is on PATH")
    pc_env = dict(env)
    # Only the extracted directory: no system or user .pc can satisfy the lookup.
    pc_env["PKG_CONFIG_LIBDIR"] = str(prefix / "lib" / "pkgconfig")
    out = _run("pkg-config", [tool, "--cflags", "--libs", "smbios_c"], pc_env)
    assert_no_leak("pkg-config output", out, roots)
    try:
        incs, libdirs, libs, inside = parse_pkg_config(out, prefix)
    except VerifyError:
        # An absolute configure-time prefix with a space (the Windows leg's
        # "C:/Program Files (x86)/...") does not even split into flags: not relocatable.
        inside = False
    if inside:
        return incs, libdirs, libs, "relocatable"
    if require_relocatable:
        raise VerifyError("pkg-config: smbios_c.pc resolves outside the extracted archive "
                          "(its prefix is not relocatable; packaged before the H1 fix?)")
    out = _run("pkg-config", [tool, "--define-prefix", "--cflags", "--libs", "smbios_c"], pc_env)
    assert_no_leak("pkg-config output", out, roots)
    incs, libdirs, libs, inside = parse_pkg_config(out, prefix)
    if not inside:
        raise VerifyError("pkg-config: smbios_c.pc resolves outside the extracted archive even "
                          "with --define-prefix")
    return incs, libdirs, libs, "define-prefix"


def _is_msvc(compiler: str) -> bool:
    return Path(compiler).name.lower() in ("cl", "cl.exe", "clang-cl", "clang-cl.exe")


def c_compile_argv(compiler: str, source: Path, output: Path, incs: Sequence[str],
                   libdirs: Sequence[str], libs: Sequence[str]) -> List[str]:
    """Return the one compile-and-link command built from the ``pkg-config`` result alone.

    GNU-style drivers get the flags as resolved plus an ``-rpath`` per library
    directory (the run needs no ``LD_LIBRARY_PATH``/``DYLD_*``); MSVC-style
    drivers get them translated (``/I``, ``/LIBPATH:``, ``<name>.lib``).
    """
    if _is_msvc(compiler):
        return ([compiler, "/nologo", str(source)] + [f"/I{d}" for d in incs]
                + [f"/Fe{output}", "/link"] + [f"/LIBPATH:{d}" for d in libdirs]
                + [f"{name}.lib" for name in libs])
    argv = [compiler, str(source), "-o", str(output)] + [f"-I{d}" for d in incs]
    argv += [f"-L{d}" for d in libdirs] + [f"-l{name}" for name in libs]
    if platform.system() != "Windows":
        argv += [f"-Wl,-rpath,{d}" for d in libdirs]
    return argv


def c_smoke(prefix: Path, work: Path, env: Mapping[str, str],
            roots: Sequence[Tuple[str, Path]], incs: Sequence[str], libdirs: Sequence[str],
            libs: Sequence[str], fixture: Path = C_FIXTURE) -> str:
    """Check 3 (build half): compile, link and run the reused ``main.c``; return its output line."""
    src_dir = work / "c-src"
    _copy_fixture(fixture, src_dir, ("main.c",))
    windows = platform.system() == "Windows"
    exe = work / ("c-consumer.exe" if windows else "c-consumer")
    compiler = env.get("CC") or _which(("cl",) if windows else ("cc",), env) or ""
    if not compiler:
        raise VerifyError("compile c consumer: no C compiler on PATH (set CC)")
    argv = c_compile_argv(compiler, src_dir / "main.c", exe, incs, libdirs, libs)
    assert_no_leak("the C compile/link command line", " ".join(argv), roots)
    _run("compile c consumer", argv, env, cwd=work)
    run_env = dict(env)
    if windows:
        run_env["PATH"] = str(prefix / "bin") + os.pathsep + run_env.get("PATH", "")
    out = _run("run c consumer", [str(exe)], run_env, cwd=work).strip().splitlines()
    if len(out) != 1 or not _C_OUTPUT.match(out[0]):
        raise VerifyError("run c consumer: unexpected output (expected smbios_open=...)")
    return out[0]


# ---------------------------------------------------------------------------
# Check 4: find_package(libsmbios CONFIG) consumer
# ---------------------------------------------------------------------------

def assert_no_whole_archive(cmake_lists: Path) -> None:
    """The consumer must carry no whole-archive flag of its own (ADR 0004 DD1)."""
    code = "\n".join(line.split("#", 1)[0] for line in
                     cmake_lists.read_text(encoding="utf-8").splitlines())
    if _WHOLE_ARCHIVE.search(code):
        raise VerifyError("cxx consumer fixture carries its own whole-archive flag; it would "
                          "not prove the exported target's")


def _generated_files(build: Path) -> List[Path]:
    """The consumer build's files that record compile/link command lines and found packages."""
    names = {"CMakeCache.txt", "build.ninja", "link.txt", "flags.make", "build.make"}
    out = []
    for dirpath, _dirs, filenames in os.walk(build):
        for name in filenames:
            if name in names or name.endswith(".vcxproj"):
                out.append(Path(dirpath) / name)
    return sorted(out)


def _cache_value(cache: Path, key: str) -> Optional[str]:
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(key + ":"):
            return line.split("=", 1)[1] if "=" in line else None
    return None


def cxx_smoke(prefix: Path, work: Path, env: Mapping[str, str],
              roots: Sequence[Tuple[str, Path]], config: str,
              dependency_prefixes: Sequence[Path] = (), fixture: Path = CXX_FIXTURE) -> List[str]:
    """Check 4: configure, build and run the reused ``libsmbios_consumer``; return its output."""
    src = work / "cxx-src"
    build = work / "cxx-build"
    _copy_fixture(fixture, src, ("CMakeLists.txt", "main.cpp"))
    assert_no_whole_archive(src / "CMakeLists.txt")
    cmake = _which(("cmake",), env)
    if cmake is None:
        raise VerifyError("configure cxx consumer: cmake is not on PATH")
    prefixes = [prefix] + list(dependency_prefixes)
    configure = [cmake, "-S", str(src), "-B", str(build),
                 "-DCMAKE_PREFIX_PATH=" + ";".join(str(p) for p in prefixes),
                 "-DCMAKE_BUILD_TYPE=" + config,
                 "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
                 "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF"]
    assert_no_leak("the C++ configure command line", " ".join(configure), roots)
    out = _run("configure cxx consumer", configure, env, cwd=work)
    assert_no_leak("the C++ configure output", out, roots)
    cache = build / "CMakeCache.txt"
    found = _cache_value(cache, "libsmbios_DIR") if cache.is_file() else None
    if not found or not _under(found, prefix / "lib" / "cmake" / "libsmbios"):
        raise VerifyError("configure cxx consumer: find_package(libsmbios) did not resolve to "
                          "the extracted lib/cmake/libsmbios/")
    out = _run("build cxx consumer",
               [cmake, "--build", str(build), "--config", config, "--verbose"], env, cwd=work)
    assert_no_leak("the C++ compile/link command lines", out, roots)
    for path in _generated_files(build):
        if path.stat().st_size <= _SCAN_MAX_BYTES:
            assert_no_leak(f"generated {path.name}",
                           path.read_text(encoding="utf-8", errors="replace"), roots)
    try:
        exe = consumer_smoke.find_executable(build, config)
    except consumer_smoke.SmokeError:
        raise VerifyError("build cxx consumer: consumer binary not found") from None
    run_env = dict(env)
    if platform.system() == "Windows":
        extra = [str(p / "bin") for p in dependency_prefixes]
        run_env["PATH"] = os.pathsep.join(extra + [run_env.get("PATH", "")])
    lines = _run("run cxx consumer", [str(exe)], run_env, cwd=work).strip().splitlines()
    if not lines or not _CXX_OUTPUT.match(lines[0]):
        raise VerifyError("run cxx consumer: unexpected output (expected formatters=<n>)")
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check_exports(prefix: Path, leg_os: str, env: Mapping[str, str]) -> str:
    """Check 6: the extracted ``smbios_c`` library exports only ``smbios_*``; return the summary.

    Raises:
        VerifyError: The library is missing, no symbol tool is found, it exports
            nothing, or a symbol outside the allowlist.
    """
    def run_tool(phase: str, argv: Sequence[str]) -> str:
        return _run(phase, argv, env)

    try:
        library = export_symbols.shared_library(prefix, leg_os)
        names = export_symbols.exported_symbols(library, run_tool, env, vswhere_run=run_tool)
        export_symbols.check_symbols(names)
    except export_symbols.ExportError as exc:
        raise VerifyError(str(exc)) from None
    return f"exports ok ({len(names)} smbios_* symbols)"


def run(archive: Path, work_dir: Path, *, manifest: Optional[Path] = None, build: bool = True,
        exports: bool = True,
        dependency_prefixes: Sequence[Path] = (), require_relocatable_pc: bool = False,
        repo_root: Path = REPO_ROOT, environ: Optional[Mapping[str, str]] = None,
        c_fixture: Path = C_FIXTURE, cxx_fixture: Path = CXX_FIXTURE) -> List[str]:
    """Verify one release archive in a clean room; return the value-free summary lines.

    Args:
        archive: ``libbios-<version>-<leg>.<ext>``; its ``.sha256`` must sit next to it.
        work_dir: Scratch directory, outside this repository and the vcpkg root.
            Its ``prefix``/``c-src``/``cxx-src``/``cxx-build`` children are recreated.
        manifest: The release's ``manifest.json`` (default: next to the archive).
        build: Run the compile/link/run checks (3 and 4). They need a host of the
            archive's own leg; ``False`` runs only checks 1, 2, 5, the layout and
            the ``pkg-config`` resolution, and reports the rest ``not executed``.
        exports: Run check 6 (the export allowlist). It runs when ``build`` is
            set, or when the host has the archive's OS (its symbol tool reads
            any architecture); otherwise it is reported ``NOT EXECUTED``.
        dependency_prefixes: Where the consumer's own Boost (``find_dependency(Boost
            COMPONENTS iostreams)`` in ``libsmbiosConfig.cmake``) is installed, if
            not in CMake's default search path. Must lie outside the forbidden trees.
        require_relocatable_pc: Fail instead of falling back to ``--define-prefix``.
        repo_root, environ, c_fixture, cxx_fixture: Seams for tests.

    Raises:
        VerifyError: The first failing check; nothing is reported here.
    """
    environ = os.environ if environ is None else environ
    roots = forbidden_roots(repo_root, environ)
    version, leg = parse_archive_name(archive.name)
    manifest = manifest or archive.parent / pa.MANIFEST_NAME
    here = host_leg()
    if build and here != leg:
        raise VerifyError(f"host: a {leg} archive cannot be built and run on this host "
                          f"({here or 'unknown'}); use build=False for the layout checks")
    for path, what in [(work_dir, "scratch directory")] + [
            (p, "dependency prefix") for p in dependency_prefixes]:
        leak = _inside(path, roots)
        if leak is not None:
            raise VerifyError(f"clean room: the {what} is inside {leak}")
    env = clean_environment(environ, roots)

    entry = verify_digests(archive, manifest, version, leg)
    lines = [f"{leg} {version}", "sha256 ok"]

    if work_dir.is_symlink():
        raise VerifyError("scratch path is a symlink")
    work_dir.mkdir(parents=True, exist_ok=True)
    prefix = work_dir / "prefix"
    for path in (prefix, work_dir / "c-src", work_dir / "cxx-src", work_dir / "cxx-build"):
        _fresh(path)
    files = extract_archive(archive, prefix)
    check_layout(prefix, leg, entry)
    lines.append(f"{files} files, install_sha256 ok")

    if not build and _which(("pkg-config", "pkgconf"), env) is None:
        lines.append("pkg-config NOT EXECUTED (not on PATH)")
        incs: List[str] = []
        libdirs: List[str] = []
        libs: List[str] = []
    else:
        incs, libdirs, libs, mode = resolve_pkg_config(prefix, env, roots, require_relocatable_pc)
        lines.append("pkg-config ok" if mode == "relocatable" else
                     "pkg-config needs --define-prefix (non-relocatable .pc)")

    leg_os = pa.leg_os(leg)
    if exports and (build or export_symbols.host_os() == leg_os):
        lines.append(check_exports(prefix, leg_os, env))
    else:
        lines.append(f"exports NOT EXECUTED (a {leg_os} symbol tool is needed; host "
                     f"{here or 'unknown'})")

    if not build:
        lines.append(f"build/run NOT EXECUTED on {here or 'unknown'}")
        return lines
    lines.append("c: " + c_smoke(prefix, work_dir, env, roots, incs, libdirs, libs,
                                         c_fixture))
    cxx = cxx_smoke(prefix, work_dir, env, roots, str(entry["config"]), dependency_prefixes,
                    cxx_fixture)
    lines.append("cxx: " + " ".join(cxx))
    return lines


def main(argv: Sequence[str]) -> int:
    """Manual front end; reports through ``ci_step`` (policy P11). Exit 0 ok, 1 failed, 2 usage."""
    p = argparse.ArgumentParser(prog="verify_release_artifact.py",
                                description=(__doc__ or "").splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("archive", nargs="?", type=Path, help="libbios-<version>-<leg>.<ext>")
    src.add_argument("--dist", type=Path, help="a dist/ directory holding manifest.json")
    p.add_argument("--leg", help="with --dist: the leg to verify (default: this host's)")
    p.add_argument("--manifest", type=Path, help="default: manifest.json next to the archive")
    p.add_argument("--work-dir", type=Path, help="scratch directory (default: a new temp dir)")
    p.add_argument("--layout-only", action="store_true",
                   help="skip the compile/link/run checks (any host)")
    p.add_argument("--dependency-prefix", type=Path, action="append", default=[],
                   help="where Boost is installed for the C++ consumer (repeatable)")
    p.add_argument("--require-relocatable-pc", action="store_true",
                   help="fail on a non-relocatable smbios_c.pc instead of falling back to "
                        "pkg-config --define-prefix")
    p.add_argument("--reporter", default=None)
    args = p.parse_args(list(argv))
    try:
        reporter = ci_step.get_reporter(args.reporter)
    except ValueError as exc:
        ci_step.emit_error(f"verify_release_artifact.py: error: {exc}")
        return env_names.EXIT_ENV_ERROR
    results = Path(env_names.get("CI_RESULTS_FILE", required=True))
    start = time.monotonic()
    try:
        archive = args.archive or resolve_archive(args.dist, args.leg or host_leg() or "")
        work = args.work_dir or Path(tempfile.mkdtemp(prefix="verify-release-"))
        lines = run(archive, work, manifest=args.manifest, build=not args.layout_only,
                    dependency_prefixes=args.dependency_prefix,
                    require_relocatable_pc=args.require_relocatable_pc)
    except VerifyError as exc:
        first, *tail = str(exc).splitlines() or ["failed"]
        elapsed = round(time.monotonic() - start, 1)
        ci_step.emit(reporter.step_fail(LABEL, 1, elapsed, first, tail))
        ci_step.append_record(ci_step.StepResult(LABEL, "hard", "fail", 1, elapsed, "", first,
                                                 None), results)
        return 1
    elapsed = round(time.monotonic() - start, 1)
    summary = "; ".join(lines)
    ci_step.emit(reporter.step_ok(LABEL, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(LABEL, "hard", "ok", 0, elapsed, summary, None,
                                             None), results)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
