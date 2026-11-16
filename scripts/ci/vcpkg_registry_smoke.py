#!/usr/bin/env python3
"""Clean-room install of ``smbios-c`` through this repository's own vcpkg registry.

Milestone 0012, Story 02.0 Task 03. Writes ``vcpkg.json`` and
``vcpkg-configuration.json`` into a scratch copy of
``tests/packaging/smbios_c_vcpkg_consumer`` (registry = this repository at
``HEAD``, so what is tested is what is committed), lets the vcpkg toolchain file
resolve and build the port during CMake configure, then builds and runs the
consumer and checks that the vcpkg-built shared library exports only
``smbios_*`` symbols (ADR 0004 Validation: the export allowlist survives
vcpkg's own build). No ``--overlay-ports``: the package is found only through
the registry.

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
from typing import List, Optional, Sequence

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

    symbols = exported_symbols(find_library(build / "vcpkg_installed"))
    if symbols is None:
        lines.append("symbols=skipped")
    else:
        check_symbols(symbols)
        lines.append(f"symbols={len(symbols)}")
    return lines


def _find_consumer(build: Path) -> Path:
    for name in ("consumer", "consumer.exe", "Debug/consumer.exe", "Release/consumer.exe"):
        candidate = build / name
        if candidate.is_file():
            return candidate
    raise SmokeError("consumer binary not found after build")
