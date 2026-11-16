#!/usr/bin/env python3
"""Consistency of the self-hosted vcpkg registry (milestone 0012, Story 02.0 Task 02).

The registry is this repository: ``ports/smbios-c/`` is the port and
``versions/`` is vcpkg's version database (``docs/dev/vcpkg-registry.md``).
A port edit that does not also refresh ``versions/`` is silent breakage for
every consumer who pins the registry baseline, so the lint stage checks:

1. ``ports/smbios-c/vcpkg.json`` names the port ``smbios-c`` with a valid version;
2. ``versions/baseline.json`` carries that version and ``port-version``;
3. the newest entry of ``versions/s-/smbios-c.json`` carries that version and
   ``port-version``, and its ``git-tree`` equals the git tree hash of
   ``ports/smbios-c`` at ``HEAD`` (what ``vcpkg x-add-version`` records; it reads
   git, not the working tree, so the port must be committed);
4. the portfile's ``REF`` is a full, non-zero commit SHA.

Reports through ``ci_step`` (policy P11), one RC1 line. Exit 0 ok, 1 inconsistent,
2 usage/environment. Standard library only.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Final, List, Optional, Sequence  # noqa: E402

import ci_step  # noqa: E402
import env_names  # noqa: E402
from reporters import Reporter, get_reporter  # noqa: E402

REPO_ROOT: Final = Path(_HERE).resolve().parents[1]
LABEL: Final = "vcpkg registry"
PORT: Final = "smbios-c"
PORT_DIR: Final = f"ports/{PORT}"
VERSION_FILE: Final = f"versions/s-/{PORT}.json"
BASELINE_FILE: Final = "versions/baseline.json"
_VERSION: Final = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z.]{1,40})?$")
_SHA1: Final = re.compile(r"^[0-9a-f]{40}$")
_REF: Final = re.compile(r"^\s*REF\s+([0-9A-Za-z]+)\s*$", re.MULTILINE)
_MAX_LINES: Final = 10

GitTree = Callable[[Path, str], Optional[str]]


def git_tree(repo_root: Path, path: str) -> Optional[str]:
    """The git tree hash of ``path`` at ``HEAD`` (``None`` if git cannot say)."""
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "rev-parse", f"HEAD:{path}"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and _SHA1.match(out) else None


def _load(path: Path, problems: List[str]) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        problems.append(f"{path.name}: missing or unreadable")
    except ValueError:
        problems.append(f"{path.name}: not valid JSON")
    return None


def check(repo_root: Path, tree_of: GitTree = git_tree) -> List[str]:
    """Return the list of inconsistencies (empty when the registry is consistent)."""
    problems: List[str] = []
    port = _load(repo_root / PORT_DIR / "vcpkg.json", problems)
    version: Optional[str] = None
    port_version = 0
    if isinstance(port, dict):
        version = port.get("version")
        port_version = port.get("port-version", 0)
        if port.get("name") != PORT:
            problems.append(f"{PORT_DIR}/vcpkg.json: name is not {PORT}")
        if not isinstance(version, str) or not _VERSION.match(version):
            problems.append(f"{PORT_DIR}/vcpkg.json: version is not X.Y.Z")
            version = None
        if not isinstance(port_version, int) or port_version < 0:
            problems.append(f"{PORT_DIR}/vcpkg.json: port-version is not a non-negative integer")
            port_version = 0

    try:
        portfile = (repo_root / PORT_DIR / "portfile.cmake").read_text(encoding="utf-8")
    except OSError:
        problems.append(f"{PORT_DIR}/portfile.cmake: missing or unreadable")
    else:
        match = _REF.search(portfile)
        ref = match.group(1) if match else ""
        if not _SHA1.match(ref) or set(ref) == {"0"}:
            problems.append(f"{PORT_DIR}/portfile.cmake: REF is not a real 40-hex commit SHA")

    baseline = _load(repo_root / BASELINE_FILE, problems)
    entry = (baseline or {}).get("default", {}).get(PORT) if isinstance(baseline, dict) else None
    if baseline is not None and not isinstance(entry, dict):
        problems.append(f"{BASELINE_FILE}: no default entry for {PORT}")
    elif isinstance(entry, dict) and version is not None:
        if entry.get("baseline") != version:
            problems.append(f"{BASELINE_FILE}: baseline differs from the port version")
        if entry.get("port-version", 0) != port_version:
            problems.append(f"{BASELINE_FILE}: port-version differs from the port")

    versions = _load(repo_root / VERSION_FILE, problems)
    rows = versions.get("versions") if isinstance(versions, dict) else None
    if versions is not None and not (isinstance(rows, list) and rows
                                     and isinstance(rows[0], dict)):
        problems.append(f"{VERSION_FILE}: no versions listed")
    elif isinstance(rows, list) and rows:
        newest = rows[0]
        if version is not None and newest.get("version") != version:
            problems.append(f"{VERSION_FILE}: newest version differs from the port version")
        if newest.get("port-version", 0) != port_version:
            problems.append(f"{VERSION_FILE}: newest port-version differs from the port")
        actual = tree_of(repo_root, PORT_DIR)
        if actual is None:
            problems.append(f"git cannot read the {PORT_DIR} tree at HEAD (commit the port first)")
        elif newest.get("git-tree") != actual:
            problems.append(f"{VERSION_FILE}: git-tree is stale for {PORT_DIR} "
                            "(run vcpkg x-add-version after committing the port)")
    return problems


def run(repo_root: Path, *, reporter: Optional[Reporter] = None,
        results: Optional[Path] = None, tree_of: GitTree = git_tree) -> int:
    """Run :func:`check`, print the RC1 line, append the RC3 record; return 0 or 1."""
    reporter = reporter or get_reporter(None)
    results = results or Path(env_names.get("CI_RESULTS_FILE", required=True))
    start = time.monotonic()
    problems = check(repo_root, tree_of)
    elapsed = round(time.monotonic() - start, 1)
    if problems:
        rest = problems[1:_MAX_LINES]
        more = len(problems) - 1 - len(rest)
        if more > 0:
            rest.append(f"(+{more} more)")
        ci_step.emit(reporter.step_fail(LABEL, 1, elapsed, problems[0], rest))
        ci_step.append_record(ci_step.StepResult(LABEL, "hard", "fail", 1, elapsed, "",
                                                 problems[0], None), results)
        return 1
    summary = f"{PORT} registry consistent"
    ci_step.emit(reporter.step_ok(LABEL, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(LABEL, "hard", "ok", 0, elapsed, summary, None,
                                             None), results)
    return 0


def main(argv: Sequence[str]) -> int:
    """Command-line entry point."""
    p = argparse.ArgumentParser(prog="check_vcpkg_registry.py", description=__doc__.splitlines()[0])
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p.add_argument("--reporter", default=None)
    args = p.parse_args(list(argv))
    try:
        reporter = get_reporter(args.reporter)
    except ValueError as exc:
        ci_step.emit_error(f"check_vcpkg_registry.py: error: {exc}")
        return env_names.EXIT_ENV_ERROR
    return run(args.repo_root, reporter=reporter)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
