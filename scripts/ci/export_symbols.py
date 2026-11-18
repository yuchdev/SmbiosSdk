#!/usr/bin/env python3
"""Export allowlist of the ``smbios_c`` shared library (milestone 0012 Story 04.0 Task 04).

ADR 0004's Validation item, "the shared library exports only ``smbios_*``",
checked from the outside with the platform's own symbol tool:

- ``.so`` (ELF): ``nm -D --defined-only``;
- ``.dylib`` (Mach-O): ``nm -gU`` (leading ``_`` stripped);
- ``.dll`` (PE): ``dumpbin /exports`` (the MSVC tool; found on ``PATH``, else
  through ``vswhere`` at the Visual Studio installer's fixed path, see
  :func:`find_dumpbin`).

Two callers share this module: ``verify_release_artifact.py`` (the ``verify``
stage, on the extracted release archive) and ``vcpkg_registry_smoke.py`` (the
vcpkg-built package; until this task its ``.dll`` case printed
``symbols=skipped``). Each passes its own runner (``run(phase, argv) -> output``),
so each keeps its own environment and timeout rules; this module starts no
process itself.

A library: it prints nothing (P11). Failures are :class:`ExportError` with a
value-free first line. Python 3.9+ standard library only.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

#: Name prefixes an exported symbol may carry. ``SMBIOS_C_<ver>`` is the GNU ld
#: version-script node, which ``nm -D`` lists as a defined symbol.
ALLOWED_PREFIXES = ("smbios_", "SMBIOS_C_")
#: vswhere's fixed install path on Windows (the same constant as ``print_env.py``).
VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
#: The vswhere query for ``dumpbin.exe`` of the newest x64-hosted MSVC toolset.
_VSWHERE_FIND = r"VC\Tools\MSVC\**\bin\Hostx64\x64\dumpbin.exe"
#: A ``dumpbin /exports`` row: ordinal, hint, RVA, name (an RVA-less forwarder row
#: and an ordinal-only ``[NONAME]`` row also match; the name is group 1).
_DUMPBIN_ROW = re.compile(r"^\s+\d+\s+[0-9A-Fa-f]+\s+(?:[0-9A-Fa-f]{8}\s+)?(\S+)")
_DUMPBIN_HEADER = re.compile(r"^\s+ordinal\s+hint\s+RVA\s+name\s*$")

#: ``run(phase, argv) -> stdout``; raises on a non-zero exit.
Runner = Callable[[str, Sequence[str]], str]


class ExportError(Exception):
    """The export check failed; the message is one value-free RC1 line."""


def library_kind(path: Path) -> Optional[str]:
    """Return ``.so``, ``.dylib`` or ``.dll`` for a shared library name, else ``None``."""
    name = path.name.lower()
    if name.endswith(".dll"):
        return ".dll"
    if name.endswith(".dylib"):
        return ".dylib"
    if name.endswith(".so") or ".so." in name:
        return ".so"
    return None


def find_dumpbin(environ: Mapping[str, str], *, run: Optional[Runner] = None,
                 vswhere: str = VSWHERE) -> Optional[str]:
    """Return ``dumpbin`` from ``PATH``, else the newest one ``vswhere`` finds, else ``None``."""
    found = shutil.which("dumpbin", path=environ.get("PATH", ""))
    if found or run is None or not os.path.isfile(vswhere):
        return found
    try:
        out = run("vswhere", [vswhere, "-latest", "-products", "*", "-find", _VSWHERE_FIND])
    except Exception:  # noqa: BLE001 - absence is reported by the caller as one line
        return None
    paths = [line.strip() for line in out.splitlines() if line.strip().lower()
             .endswith("dumpbin.exe")]
    return sorted(paths)[-1] if paths else None


def parse_nm(output: str, kind: str) -> List[str]:
    """Symbol names from ``nm`` output (``<addr> <type> <name>`` lines)."""
    names = [line.split()[-1] for line in output.splitlines() if line.strip()]
    return [n[1:] if kind == ".dylib" and n.startswith("_") else n for n in names]


def parse_dumpbin(output: str) -> List[str]:
    """Exported names from ``dumpbin /exports`` output (the rows under its column header).

    Raises:
        ExportError: The output has no export table header (not a DLL, or not dumpbin).
    """
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if _DUMPBIN_HEADER.match(line)), None)
    if start is None:
        raise ExportError("symbols: dumpbin /exports printed no export table")
    names: List[str] = []
    for line in lines[start + 1:]:
        if line.strip().lower() == "summary":
            break
        match = _DUMPBIN_ROW.match(line)
        if match:
            names.append(match.group(1))
    return names


def exported_symbols(library: Path, run: Runner, environ: Mapping[str, str],
                     *, vswhere_run: Optional[Runner] = None) -> List[str]:
    """Defined, externally visible symbols of ``library`` (``.so``, ``.dylib`` or ``.dll``).

    Args:
        library: The shared library.
        run: Runs one tool and returns its standard output.
        environ: The environment whose ``PATH`` locates the tool.
        vswhere_run: Runner for the ``vswhere`` lookup of ``dumpbin`` (Windows only).

    Raises:
        ExportError: Not a shared library, or no symbol tool for it.
    """
    kind = library_kind(library)
    if kind is None:
        raise ExportError("symbols: expected a .so, .dylib or .dll -> actual another file")
    if kind == ".dll":
        tool = find_dumpbin(environ, run=vswhere_run)
        if tool is None:
            raise ExportError("symbols: dumpbin not found (needs the MSVC toolset; vswhere "
                              "found none)")
        return parse_dumpbin(run("symbols", [tool, "/nologo", "/exports", str(library)]))
    tool = shutil.which("nm", path=environ.get("PATH", ""))
    if tool is None:
        raise ExportError("symbols: nm is not on PATH")
    flags = ["-gU"] if kind == ".dylib" else ["-D", "--defined-only"]
    return parse_nm(run("symbols", [tool, *flags, str(library)]), kind)


def check_symbols(symbols: Sequence[str]) -> None:
    """Every exported symbol is a ``smbios_*`` C function (and at least one exists).

    Raises:
        ExportError: Nothing exported, or a name outside :data:`ALLOWED_PREFIXES`.
    """
    if not symbols:
        raise ExportError("symbols: the library exports nothing")
    stray = [s for s in symbols if not s.startswith(ALLOWED_PREFIXES)]
    if stray:
        raise ExportError(f"symbols: {len(stray)} exported symbol(s) outside smbios_*")


def shared_library(prefix: Path, leg_os: str) -> Path:
    """The ``smbios_c`` shared library of an installed tree (``bin/`` on Windows, else ``lib/``).

    Raises:
        ExportError: The library is missing.
    """
    rel = {"windows": "bin/smbios_c.dll", "macos": "lib/libsmbios_c.dylib",
           "linux": "lib/libsmbios_c.so"}.get(leg_os)
    path = prefix / rel if rel else None
    if path is None or path.is_symlink() or not path.is_file():
        raise ExportError(f"symbols: {rel or 'smbios_c library'} missing")
    return path


def host_os() -> str:
    """``linux``, ``macos`` or ``windows`` for this host (``other`` elsewhere)."""
    return {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system(),
                                                                          "other")
