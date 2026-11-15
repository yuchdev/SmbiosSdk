#!/usr/bin/env python3
"""Clean-room consumer smoke test of the installed ``libsmbios`` package.

Milestone 0012, Story 01.0 Task 03. ``cmake --install`` the already-built
tree into a scratch prefix, then configure, build and run
``tests/packaging/libsmbios_consumer`` against that prefix only
(``CMAKE_PREFIX_PATH``). The fixture carries no whole-archive flag of its own;
it proves the exported ``libsmbios::libsmbios`` target does (ADR 0004 DD1).

A library, not a command: ``stage.py test --only consumer`` calls :func:`run`
in-process and reports through ``ci_step`` (policy P11: the single emitter), so
this module prints nothing. :class:`SmokeError` carries a value-free reason: the
failing phase on the first line, then the tail of its output. Standard library
only.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "packaging" / "libsmbios_consumer"
TAIL_LINES = 12


class SmokeError(Exception):
    """A phase failed; the message is the value-free reason."""


def vcpkg_prefixes(build_dir: Path) -> List[Path]:
    """The vcpkg triplet install dirs of the build (``vcpkg_installed/<triplet>``).

    The installed package finds Boost through ``find_dependency``; a build
    that resolved Boost through vcpkg needs that tree on ``CMAKE_PREFIX_PATH``.
    """
    root = build_dir / "vcpkg_installed"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name != "vcpkg")


def _run(phase: str, argv: Sequence[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-TAIL_LINES:])
        raise SmokeError(f"{phase} failed (exit {proc.returncode})\n{tail}")
    return proc.stdout


def find_executable(build: Path, config: str) -> Path:
    """The consumer binary: ``build/consumer`` or ``build/<config>/consumer[.exe]``."""
    for name in ("consumer", "consumer.exe"):
        for base in ([build / config] if config else []) + [build]:
            candidate = base / name
            if candidate.is_file():
                return candidate
    raise SmokeError("consumer binary not found after build")


def run(build_dir: Path, work_dir: Path, config: str, extra_prefixes: Sequence[Path] = (),
        fixture: Path = FIXTURE) -> List[str]:
    """Install, configure, build, run; return the program's stdout lines."""
    if not fixture.is_dir():
        raise SmokeError("consumer fixture missing")
    prefix = work_dir / "prefix"
    build = work_dir / "build"
    for path in (prefix, build):
        if path.is_symlink():
            raise SmokeError("scratch path is a symlink")
        if path.exists():
            shutil.rmtree(path)
    work_dir.mkdir(parents=True, exist_ok=True)

    install = ["cmake", "--install", str(build_dir), "--prefix", str(prefix)]
    if config:
        install += ["--config", config]
    _run("install", install)

    prefixes = [prefix] + list(extra_prefixes) + vcpkg_prefixes(build_dir)
    configure = ["cmake", "-S", str(fixture), "-B", str(build),
                 "-DCMAKE_PREFIX_PATH=" + ";".join(str(p) for p in prefixes)]
    if config:
        configure.append("-DCMAKE_BUILD_TYPE=" + config)
    _run("configure consumer", configure)

    build_cmd = ["cmake", "--build", str(build)]
    if config:
        build_cmd += ["--config", config]
    _run("build consumer", build_cmd)

    out = _run("run consumer", [str(find_executable(build, config))])
    return out.splitlines()
