#!/usr/bin/env python3
"""Clean-room install of ``smbios-c`` through this repository's own vcpkg registry.

Milestone 0012, Story 02.0 Task 03. Writes ``vcpkg.json`` and
``vcpkg-configuration.json`` into a scratch copy of
``tests/packaging/smbios_c_vcpkg_consumer`` (registry = this repository at
``HEAD``, so what is tested is what is committed), lets the vcpkg toolchain file
resolve and build the port during CMake configure, then builds and runs the
consumer and checks that the vcpkg-built shared library exports only
``smbios_*`` symbols (ADR 0004 Validation: the export allowlist survives
vcpkg's own build), and checks the **exact** set of files the package
installed (:func:`check_installed_files`, 0012 Story 04.0 Task 03: the portfile
prunes a fixed list, so a new root ``install()`` rule would otherwise ship
silently). No ``--overlay-ports``: the package is found only through the
registry. Used by the publish stage's ``registry-prepare`` job
(``registry_prepare.py``) before any registry write.

A library, not a command: it prints nothing (policy P11); a caller reports the
returned lines or the :class:`consumer_smoke.SmokeError`. Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import consumer_smoke  # noqa: E402
from consumer_smoke import SmokeError  # noqa: E402,F401

REPO_ROOT = Path(_HERE).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "packaging" / "smbios_c_vcpkg_consumer"
PORT = "smbios-c"
#: A vcpkg port install builds Boost on a cold cache; the bound is generous.
consumer_smoke.PHASE_TIMEOUT_S.update({"git head": 30, "configure vcpkg consumer": 3600,
                                       "build vcpkg consumer": 300, "run vcpkg consumer": 60,
                                       "symbols": 60})


def head_commit(repo_root: Path) -> str:
    """The full SHA of the repository's ``HEAD`` (the registry baseline under test)."""
    sha = consumer_smoke._run("git head", ["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    sha = sha.strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise SmokeError("git head did not return a commit SHA")
    return sha


def write_consumer_files(consumer: Path, repo_root: Path, baseline: str) -> None:
    """``vcpkg.json`` + ``vcpkg-configuration.json``: this repository is the registry."""
    root_cfg = json.loads((repo_root / "vcpkg-configuration.json").read_text(encoding="utf-8"))
    config = {
        "default-registry": root_cfg["default-registry"],
        "registries": [{"kind": "git", "repository": repo_root.resolve().as_uri(),
                        "baseline": baseline, "packages": [PORT]}],
    }
    manifest = {"name": "smbios-c-vcpkg-consumer", "version": "0.0.0", "dependencies": [PORT]}
    (consumer / "vcpkg-configuration.json").write_text(json.dumps(config, indent=2) + "\n",
                                                       encoding="utf-8")
    (consumer / "vcpkg.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def exported_symbols(library: Path) -> Optional[List[str]]:
    """Defined, externally visible symbols of ``library``; ``None`` where ``nm`` is unusable."""
    if library.suffix not in (".so", ".dylib"):
        return None
    flags = ["-gU"] if library.suffix == ".dylib" else ["-D", "--defined-only"]
    out = consumer_smoke._run("symbols", ["nm", *flags, str(library)])
    names = [line.split()[-1] for line in out.splitlines() if line.strip()]
    return [n.lstrip("_") if library.suffix == ".dylib" else n for n in names]


def check_symbols(symbols: Sequence[str]) -> None:
    """Every exported symbol is a ``smbios_*`` C function (and at least one exists)."""
    # A GNU ld version script can list its version node (SMBIOS_C_<ver>) as a defined symbol.
    stray = [s for s in symbols if not s.startswith(("smbios_", "SMBIOS_C_"))]
    if not symbols:
        raise SmokeError("symbols: the library exports nothing")
    if stray:
        raise SmokeError(f"symbols: {len(stray)} exported symbol(s) outside smbios_*")


#: Files every ``smbios-c`` package installs under ``vcpkg_installed/<triplet>/``.
#: Taken from vcpkg's binary packages of the port on ``arm64-osx`` (Story 02.0
#: runs, 2026-11-16). The package built from portfile ``70f268a`` also carried
#: ``lib/libsmbios.dylib``: exactly the regression this list catches.
INSTALLED_COMMON: Tuple[str, ...] = (
    "include/smbios/smbios.h", "include/smbios_c/smbios_c_export.h",
    "lib/pkgconfig/smbios_c.pc", "debug/lib/pkgconfig/smbios_c.pc",
    "share/smbios-c/copyright", "share/smbios-c/vcpkg.spdx.json",
    "share/smbios-c/vcpkg-spdx-resources.json", "share/smbios-c/vcpkg_abi_info.txt",
)
#: The library files, by the shared-library suffix found. ``.so`` follows the
#: same rule (no SOVERSION in smbios_c/CMakeLists.txt); ``.so`` and ``.dll`` are
#: unverified until a Linux/Windows registry run (0012 Story 04.0 Task 04).
INSTALLED_LIBRARIES: Dict[str, Tuple[str, ...]] = {
    ".dylib": ("lib/libsmbios_c.dylib", "debug/lib/libsmbios_c.dylib"),
    ".so": ("lib/libsmbios_c.so", "debug/lib/libsmbios_c.so"),
    ".dll": ("bin/smbios_c.dll", "debug/bin/smbios_c.dll", "lib/smbios_c.lib",
             "debug/lib/smbios_c.lib"),
}
#: Files that may be absent (``vcpkg_copy_pdbs`` copies a PDB only if the build made one).
INSTALLED_OPTIONAL: Dict[str, Tuple[str, ...]] = {
    ".dll": ("bin/smbios_c.pdb", "debug/bin/smbios_c.pdb"),
}


def installed_files(installed: Path) -> Tuple[str, List[str]]:
    """``(triplet, files)`` the ``smbios-c`` package installed, from vcpkg's own list file.

    vcpkg records every file a package installed in
    ``vcpkg_installed/vcpkg/info/smbios-c_<version>_<triplet>.list`` as
    ``<triplet>/<path>`` lines (directories end in ``/``). Dependencies (Boost)
    have their own list files and are not counted.
    """
    info = installed / "vcpkg" / "info"
    lists = sorted(info.glob(f"{PORT}_*.list")) if info.is_dir() else []
    if len(lists) != 1:
        raise SmokeError(f"installed files: expected one {PORT} list file -> actual "
                         f"{len(lists)}")
    triplet = lists[0].stem.rsplit("_", 1)[-1]
    files = []
    for line in lists[0].read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.endswith("/"):
            continue
        prefix, _, rest = line.partition("/")
        if prefix != triplet or not rest:
            raise SmokeError("installed files: expected <triplet>/<path> entries -> actual "
                             "another form")
        files.append(rest)
    return triplet, sorted(files)


def check_installed_files(installed: Path, library: Path) -> int:
    """The package installed exactly :data:`INSTALLED_COMMON` plus its library files.

    Returns:
        The number of installed files.

    Raises:
        SmokeError: One RC1 line naming the counts and the first extra or
            missing path.
    """
    _triplet, files = installed_files(installed)
    name = library.name
    suffix = ".dll" if name.endswith(".dll") else ".dylib" if name.endswith(".dylib") else \
        ".so" if ".so" in name else ""
    if suffix not in INSTALLED_LIBRARIES:
        raise SmokeError("installed files: expected a .dylib, .so or .dll library -> actual "
                         "another kind")
    want = set(INSTALLED_COMMON) | set(INSTALLED_LIBRARIES[suffix])
    optional = set(INSTALLED_OPTIONAL.get(suffix, ()))
    have = set(files)
    extra = sorted(have - want - optional)
    missing = sorted(want - have)
    if extra or missing:
        first = (extra or missing)[0]
        raise SmokeError(f"installed files: expected the {len(want)} allowlisted files -> "
                         f"actual {len(extra)} extra, {len(missing)} missing (first: "
                         f"{'+' if extra else '-'}{first})")
    return len(files)


def find_library(installed: Path) -> Path:
    """The vcpkg-installed ``smbios_c`` shared library under ``vcpkg_installed/<triplet>/``."""
    for triplet in sorted(p for p in installed.iterdir() if p.is_dir() and p.name != "vcpkg"):
        for pattern in ("libsmbios_c.dylib", "libsmbios_c.so", "smbios_c.dll", "libsmbios_c.so.*"):
            found = sorted((triplet / "lib").glob(pattern)) + sorted(
                (triplet / "bin").glob(pattern))
            if found:
                return found[0]
    raise SmokeError("vcpkg-installed smbios_c library not found")


def run(work_dir: Path, *, vcpkg_root: Path, repo_root: Path = REPO_ROOT,
        fixture: Path = FIXTURE, triplet: Optional[str] = None) -> List[str]:
    """Resolve ``smbios-c`` through the registry, build and run the consumer, check symbols."""
    if not fixture.is_dir():
        raise SmokeError("vcpkg consumer fixture missing")
    toolchain = vcpkg_root / "scripts" / "buildsystems" / "vcpkg.cmake"
    if not toolchain.is_file():
        raise SmokeError("vcpkg toolchain file not found")
    consumer = work_dir / "consumer"
    build = work_dir / "build"
    for path in (consumer, build):
        if path.is_symlink():
            raise SmokeError("scratch path is a symlink")
        if path.exists():
            shutil.rmtree(path)
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture, consumer)
    write_consumer_files(consumer, repo_root, head_commit(repo_root))

    configure = ["cmake", "-S", str(consumer), "-B", str(build),
                 f"-DCMAKE_TOOLCHAIN_FILE={toolchain}"]
    if triplet:
        configure.append(f"-DVCPKG_TARGET_TRIPLET={triplet}")
    consumer_smoke._run("configure vcpkg consumer", configure)
    consumer_smoke._run("build vcpkg consumer", ["cmake", "--build", str(build)])

    exe = _find_consumer(build)
    out = consumer_smoke._run("run vcpkg consumer", [str(exe)])
    lines = out.splitlines()

    library = find_library(build / "vcpkg_installed")
    symbols = exported_symbols(library)
    if symbols is None:
        lines.append("symbols=skipped")
    else:
        check_symbols(symbols)
        lines.append(f"symbols={len(symbols)}")
    lines.append(f"installed={check_installed_files(build / 'vcpkg_installed', library)}")
    return lines


def _find_consumer(build: Path) -> Path:
    for name in ("consumer", "consumer.exe", "Debug/consumer.exe", "Release/consumer.exe"):
        candidate = build / name
        if candidate.is_file():
            return candidate
    raise SmokeError("consumer binary not found after build")
