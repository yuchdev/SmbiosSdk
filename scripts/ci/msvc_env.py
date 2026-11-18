#!/usr/bin/env python3
"""The MSVC developer environment for the Windows ``verify`` leg (milestone 0012 Story 04.0 Task 04).

The clean-room C check compiles ``main.c`` with ``cl`` from the raw
``pkg-config`` flags (``verify_release_artifact.c_smoke``), and the export
check runs ``dumpbin /exports``. Both need what ``vcvars64.bat`` sets up
(``PATH`` to the toolset, ``INCLUDE``, ``LIB``, ``LIBPATH``...). A hosted
Windows runner has Visual Studio but does not run that script for a job.

:func:`developer_environment` finds the newest Visual Studio with the x64 C++
toolset through ``vswhere`` at the installer's fixed path (never a ``PATH``
lookup), runs its ``vcvars64.bat`` in ``cmd.exe`` followed by ``set``, and
returns the resulting environment. The ``.bat`` path comes from ``vswhere`` and
is refused unless it is a plain path (no quote, ``&``, ``|``, ``%``, ``^``...),
because it is placed on a ``cmd.exe`` command line.

The caller (``stage.py verify``) hands the result to the verifier, whose
clean-room allowlist keeps only the documented MSVC names and asserts none of
them points into this repository or a vcpkg root.

A library: it prints nothing (P11). Python 3.9+ standard library only.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import PureWindowsPath
from typing import Callable, Dict, Mapping, Optional

from reporters import child_environment

#: vswhere's fixed install path on Windows (the same constant as ``print_env.py``).
VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
#: The component every usable installation must have (the x64 C++ toolset).
VC_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
#: ``vcvars64.bat`` relative to an installation path.
VCVARS = PureWindowsPath("VC") / "Auxiliary" / "Build" / "vcvars64.bat"
TIMEOUT_S = 120.0
#: A path that may appear on a ``cmd.exe`` command line unquoted-safe inside quotes.
_PLAIN_PATH = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 _.()\\-]{1,240}$")
_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_()]{0,63})=(.*)$")

#: ``run(argv_or_command_line) -> stdout``; raises :class:`MsvcError` on failure.
Runner = Callable[[object], str]


class MsvcError(Exception):
    """The developer environment could not be set up; the message is value-free."""


def _run(command: object) -> str:
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, env=child_environment(os.environ),
                              timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MsvcError(f"msvc environment: could not run ({type(exc).__name__})") from None
    if proc.returncode != 0:
        raise MsvcError(f"msvc environment: exit {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


def installation_path(run: Runner = _run, vswhere: str = VSWHERE) -> PureWindowsPath:
    """The newest Visual Studio installation with :data:`VC_COMPONENT`.

    Raises:
        MsvcError: vswhere is missing or reports no such installation.
    """
    if not os.path.isfile(vswhere):
        raise MsvcError("msvc environment: vswhere not found at the installer's fixed path")
    out = run([vswhere, "-latest", "-products", "*", "-requires", VC_COMPONENT,
               "-property", "installationPath"]).strip().splitlines()
    path = out[0].strip() if out else ""
    if not _PLAIN_PATH.fullmatch(path):
        raise MsvcError("msvc environment: vswhere reported no installation with the x64 "
                        "C++ toolset")
    return PureWindowsPath(path)


def parse_set_output(text: str) -> Dict[str, str]:
    """``NAME=value`` lines of ``set`` into a mapping (``=C:``-style entries are skipped)."""
    env: Dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match:
            env[match.group(1)] = match.group(2)
    return env


def developer_environment(run: Runner = _run, vswhere: str = VSWHERE,
                          base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment after ``vcvars64.bat`` (merged over ``base``, default ``os.environ``).

    Raises:
        MsvcError: No installation, an unsafe path, or the script failed / set no ``INCLUDE``.
    """
    bat = installation_path(run, vswhere) / VCVARS
    text = str(bat)
    if not _PLAIN_PATH.fullmatch(text) or not os.path.isfile(text):
        raise MsvcError("msvc environment: vcvars64.bat missing from the installation")
    # cmd /s: the outer quotes are stripped and the rest is run verbatim.
    command = f'cmd.exe /d /s /c ""{text}" >nul 2>&1 && set"'
    env = parse_set_output(run(command))
    if not env.get("INCLUDE") or not env.get("LIB"):
        raise MsvcError("msvc environment: vcvars64.bat set no INCLUDE/LIB")
    # Windows names are case-insensitive and Python's os.environ upper-cases them;
    # ``set`` prints ``Path``. Upper-case the captured names and let them replace the
    # inherited ones: otherwise the child gets both ``PATH`` and ``Path``, and every
    # ``env.get("PATH")`` lookup (cl, cmake, pkg-config, dumpbin) reads the
    # pre-vcvars value, or nothing.
    captured = {k.upper(): v for k, v in env.items()}
    merged = {k.upper(): v for k, v in (os.environ if base is None else base).items()
              if k.upper() not in captured}
    merged.update(captured)
    return merged
