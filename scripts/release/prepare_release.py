#!/usr/bin/env python3
"""Prepare a release: set the one version everywhere it is hand-edited (milestone 0012 Story 04.0 Task 01).

Implements the edit half of ADR 0005 D5. The check half is milestone 0014's
``scripts/ci/check_versions.py``, which this script runs after editing. One
argument, ``X.Y.Z``, is written to:

- the **authoritative** site of ``scripts/ci/version_sites.json`` (the root
  ``CMakeLists.txt`` ``project(... VERSION X.Y.Z ...)``);
- every site whose rule is ``equal`` (today the port's
  ``ports/smbios-c/vcpkg.json`` ``"version"`` and the registry's
  ``versions/baseline.json`` ``default.smbios-c.baseline``).

``derived`` sites (``SMBIOS_C_VERSION``, ``LIBSMBIOS_VERSION``) are never
touched: they reference ``${PROJECT_VERSION}`` and follow the authoritative
value at configure time. A derived site holding a literal is reported by the
check, not repaired here.

**What it never does** (threat model 2026-11-18, Task 01 requirement 3): it
never commits, creates or pushes a tag, runs ``vcpkg x-add-version``, or
edits ``versions/s-/smbios-c.json``, ``port-version`` or the portfile
``REF``. Tagging is a human action; the registry entry is Story 04.0 Task 03
(until then, ``docs/dev/vcpkg-registry.md``'s manual procedure), and the lint
``registry`` step reports the drift until that entry exists. The only child
process it starts is ``git status`` (read-only).

**Pre-releases are refused** (threat-model RP-5, constraint 8). The argument
is validated against the constraint-5 tag regex without its ``v``,
``^\\d{1,6}\\.\\d{1,6}\\.\\d{1,6}(-[0-9A-Za-z.]{1,40})?$``, and a value with
a ``-<pre>`` suffix exits 2 instead of being truncated to ``X.Y.Z``. Reasons:

1. CMake rejects it: ``project(p VERSION 1.0.0-rc.1)`` fails with
   ``VERSION "1.0.0-rc.1" format invalid``, so the authoritative site cannot
   hold a suffix, and ``equal`` sites must equal the authoritative value.
2. Writing the suffix-less base instead would put the base into the port and
   the registry baseline from a release candidate, which constraint 8 forbids.
3. A pre-release is a property of the **tag** only: prepare ``X.Y.Z`` with
   this script, then a maintainer tags ``vX.Y.Z-rc.N``, which
   ``check_versions.py --tag`` accepts. The D4 ``<version>`` in archive names
   therefore never carries the suffix (RP-5's accepted consequence).

**Safety rules:**

- Every edit is computed before any file is written; a site that cannot be
  edited exactly (missing value, a ``${...}`` reference in an ``equal`` site,
  JSON that would not re-serialise byte-identically) aborts with no writes.
- **Idempotent.** When every site already holds the version, nothing is
  written, the dirty check is skipped and only the check runs. A second run
  after a successful one is therefore a no-op, and a run interrupted midway
  can simply be repeated.
- **Refuses a dirty tree for the files it would write**: a file with
  uncommitted (staged or unstaged) changes is never overwritten, so a
  hand-edit is never mixed into a release bump. Outside a git work tree the
  script refuses.
- Refuses to move the version backwards.
- Line endings and file modes are preserved; writes are atomic per file.

Usage::

    python3 scripts/release/prepare_release.py X.Y.Z [--repo-root DIR] [--sites FILE]

Output: ``ok: prepare release: <old> -> <new>, <n> files (...)`` (or
``already <new>``), then ``check_versions.py``'s own ``ok: versions: ...``
line or its discrepancy lines. Exit codes: 0 consistent, 1 the check failed
after editing, 2 refused (bad or pre-release version, dirty file, downgrade,
uneditable site, unreadable sites file, not a git work tree). Python 3
standard library only.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CI = os.path.join(_REPO, "scripts", "ci")
if _CI not in sys.path:
    sys.path.insert(0, _CI)

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, Dict, Final, List, Optional, Sequence, Tuple  # noqa: E402

import check_versions as cv  # noqa: E402

REPO_ROOT: Final = Path(_REPO)
#: Threat-model constraint 5's tag regex without the leading ``v``.
RELEASE_VERSION: Final = re.compile(r"^(\d{1,6})\.(\d{1,6})\.(\d{1,6})(-[0-9A-Za-z.]{1,40})?$")
LABEL: Final = "prepare release"
EXIT_OK: Final = 0
EXIT_CHECK_FAILED: Final = 1
EXIT_REFUSED: Final = 2

#: ``(repo_root, relative paths) -> those with uncommitted changes``.
DirtyLookup = Callable[[Path, Sequence[str]], List[str]]


def _show(value: str) -> str:
    """Return ``value`` if safe to print, else ``<invalid>`` (CR-4; a full match, so no newline)."""
    return value if re.fullmatch(r"[0-9A-Za-z.+_-]{1,40}", value) else "<invalid>"


class RefusedError(Exception):
    """The release preparation is refused; nothing has been written (exit 2)."""


def parse_version(arg: str) -> Tuple[int, int, int]:
    """Return ``(major, minor, patch)`` of a release version.

    Raises:
        RefusedError: ``arg`` does not match :data:`RELEASE_VERSION`, or it is a
            pre-release (see the module docstring for why).
    """
    match = RELEASE_VERSION.fullmatch(arg)
    if match is None:
        raise RefusedError(f"version must be X.Y.Z, got {_show(arg)}")
    if match.group(4):
        raise RefusedError(
            f"pre-release {_show(arg)} refused: the version sites hold X.Y.Z only. "
            f"Prepare {match.group(1)}.{match.group(2)}.{match.group(3)}, "
            f"then tag v{_show(arg)}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def mask_cmake_comments(text: str) -> str:
    """Return ``text`` with every CMake comment character replaced by a space.

    Same comment rules as :func:`check_versions.strip_cmake_comments`, but
    offsets are preserved (newlines are kept), so a match in the masked text
    can be applied to the original.
    """
    def blank(match: "re.Match[str]") -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    text = re.sub(r"#\[(=*)\[.*?\]\1\]", blank, text, flags=re.DOTALL)
    out: List[str] = []
    for line in text.split("\n"):
        quoted = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                quoted = not quoted
            elif ch == "#" and not quoted:
                cut = i
                break
        out.append(line[:cut] + " " * (len(line) - cut))
    return "\n".join(out)


def _replace_span(text: str, span: Tuple[int, int], value: str) -> Tuple[str, str]:
    return text[:span[0]] + value + text[span[1]:], text[span[0]:span[1]]


def _edit_cmake_project_version(text: str, value: str) -> Tuple[str, str]:
    masked = mask_cmake_comments(text)
    for call in re.finditer(r"\bproject\s*\(([^)]*)\)", masked, flags=re.IGNORECASE):
        match = re.search(r"\bVERSION\s+\"?([^\s\")]+)\"?", call.group(1))
        if match:
            start = call.start(1) + match.start(1)
            return _replace_span(text, (start, start + len(match.group(1))), value)
    raise RefusedError("no project(... VERSION ...) call")


def _edit_cmake_set(text: str, var: str, value: str) -> Tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", var):
        raise RefusedError("bad cmake_set variable")
    masked = mask_cmake_comments(text)
    match = re.search(r"\bset\s*\(\s*" + re.escape(var) + r"\s+\"?([^\s\")]+)\"?", masked,
                      flags=re.IGNORECASE)
    if match is None:
        raise RefusedError(f"no set({var} ...)")
    if match.group(1).startswith("${"):
        raise RefusedError(f"set({var} ...) holds a variable reference, not a literal")
    return _replace_span(text, match.span(1), value)


def _edit_json_key(text: str, path: str, value: str) -> Tuple[str, str]:
    try:
        data = json.loads(text)
    except ValueError:
        raise RefusedError("not valid JSON") from None
    if json.dumps(data, indent=2, ensure_ascii=False) + "\n" != text:
        raise RefusedError("JSON is not in the canonical 2-space form; cannot edit it exactly")
    keys = path.split(".")
    node = data
    for key in keys[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict) or not isinstance(node.get(keys[-1]), str):
        raise RefusedError(f"no string at key {path}")
    old = node[keys[-1]]
    node[keys[-1]] = value
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", old


def edit_site(text: str, extractor: str, value: str) -> Tuple[str, str]:
    """Return ``(new_text, old_value)`` with the site named by ``extractor`` set to ``value``.

    ``text`` uses ``\\n`` line endings. ``extractor`` is a
    ``version_sites.json`` extractor (``cmake_project_version``,
    ``cmake_set:<VAR>``, ``json_key:<dotted.path>``).

    Raises:
        RefusedError: The site is missing, holds no literal, or cannot be edited exactly.
    """
    kind, _, arg = extractor.partition(":")
    if kind == "cmake_project_version":
        return _edit_cmake_project_version(text, value)
    if kind == "cmake_set":
        return _edit_cmake_set(text, arg, value)
    if kind == "json_key":
        return _edit_json_key(text, arg, value)
    raise RefusedError(f"unknown extractor {_show(kind)}")


@dataclass(frozen=True)
class FileEdit:
    """The planned new content of one file (bytes as they will be written)."""

    file: str
    old: bytes
    new: bytes


@dataclass(frozen=True)
class Plan:
    """What :func:`plan` decided: the current version and the files to rewrite."""

    current: str
    version: str
    edits: List[FileEdit]


def _read(repo_root: Path, rel: str) -> bytes:
    try:
        return (repo_root / rel).read_bytes()
    except OSError:
        raise RefusedError(f"{rel}: cannot read") from None


def plan(repo_root: Path, version: str, sites_file: Path = cv.SITES_FILE) -> Plan:
    """Compute every edit that sets ``version``; write nothing.

    Raises:
        RefusedError: Bad or pre-release version, a downgrade, no authoritative
            value, an uneditable site, or an invalid sites file.
    """
    wanted = parse_version(version)
    try:
        authoritative, _, sites = cv.load_sites(sites_file)
    except cv.SitesError as exc:
        raise RefusedError(str(exc)) from None
    current = cv.extract(repo_root, authoritative)
    if current is None or not cv.VERSION.match(current):
        raise RefusedError(f"{authoritative.site}: no X.Y.Z value in {authoritative.file}")
    current_tuple = tuple(int(x) for x in current.split("-", 1)[0].split("."))
    if wanted < current_tuple:
        raise RefusedError(f"{version} is lower than the current {current}")

    targets = [authoritative] + [s for s in sites if s.rule == "equal"]
    texts: Dict[str, str] = {}
    originals: Dict[str, bytes] = {}
    for site in targets:
        if site.file not in texts:
            raw = _read(repo_root, site.file)
            try:
                texts[site.file] = raw.decode("utf-8").replace("\r\n", "\n")
            except UnicodeDecodeError:
                raise RefusedError(f"{site.file}: not UTF-8") from None
            originals[site.file] = raw
        try:
            texts[site.file], _ = edit_site(texts[site.file], site.extractor, version)
        except RefusedError as exc:
            raise RefusedError(f"{site.site} ({site.file}): {exc}") from None

    edits = []
    for rel, text in texts.items():
        raw = originals[rel]
        if b"\r\n" in raw:
            text = text.replace("\n", "\r\n")
        new = text.encode("utf-8")
        if new != raw:
            edits.append(FileEdit(rel, raw, new))
    return Plan(current, version, edits)


def git_dirty(repo_root: Path, files: Sequence[str]) -> List[str]:
    """Return those of ``files`` with uncommitted changes (staged, unstaged or untracked).

    Runs one read-only ``git status --porcelain`` per file; any output means
    dirty, so no path in git's output is ever parsed.

    Raises:
        RefusedError: ``repo_root`` is not a git work tree, or git is unavailable.
    """
    dirty = []
    for rel in files:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "status", "--porcelain=v1",
                 "--untracked-files=all", "--", rel],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            raise RefusedError("cannot run git status") from None
        if proc.returncode != 0:
            raise RefusedError("not a git work tree; refusing to edit untracked state")
        if proc.stdout.strip():
            dirty.append(rel)
    return dirty


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(str(path), tmp)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def apply(repo_root: Path, the_plan: Plan, dirty: DirtyLookup = git_dirty) -> None:
    """Write ``the_plan``'s edits, after refusing any target file with uncommitted changes.

    Raises:
        RefusedError: A target file is dirty, or the tree is not a git work tree.
    """
    if not the_plan.edits:
        return
    files = [e.file for e in the_plan.edits]
    unclean = dirty(repo_root, files)
    if unclean:
        raise RefusedError("uncommitted changes in " + ", ".join(unclean)
                           + "; commit or revert them first")
    for edit in the_plan.edits:
        if _read(repo_root, edit.file) != edit.old:
            raise RefusedError(f"{edit.file} changed while preparing; nothing more written")
        _write_atomic(repo_root / edit.file, edit.new)


def run(repo_root: Path, version: str, *, sites_file: Path = cv.SITES_FILE,
        dirty: DirtyLookup = git_dirty, out=None) -> int:
    """Plan, apply and check; print the result lines; return 0, 1 or 2."""
    out = out or sys.stdout
    try:
        the_plan = plan(repo_root, version, sites_file)
        apply(repo_root, the_plan, dirty)
    except RefusedError as exc:
        print(f"error: {LABEL}: {exc}", file=out)
        return EXIT_REFUSED
    if the_plan.edits:
        names = ", ".join(e.file for e in the_plan.edits)
        print(f"ok: {LABEL}: {the_plan.current} -> {version}, {len(the_plan.edits)} files "
              f"({names})", file=out)
    else:
        print(f"ok: {LABEL}: already {version}, nothing written", file=out)
    try:
        outcome = cv.check(repo_root, sites_file)
    except cv.SitesError as exc:
        print(f"error: {LABEL}: {exc}", file=out)
        return EXIT_REFUSED
    if outcome.problems:
        print(f"fail: versions: {outcome.problems[0]}", file=out)
        for line in outcome.problems[1:]:
            print(f"  {line}", file=out)
        return EXIT_CHECK_FAILED
    print(f"ok: versions: {outcome.authority} ({outcome.authority_site}), {outcome.sites} sites",
          file=out)
    if the_plan.edits:
        print("next: review and commit; the registry entry and the v-tag are separate steps "
              "(docs/dev/vcpkg-registry.md)", file=out)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prepare_release.py",
                                description="Set the release version X.Y.Z at every hand-edited "
                                            "version site, then run check_versions.")
    p.add_argument("version", help="the new release version, X.Y.Z (no v, no pre-release)")
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p.add_argument("--sites", type=Path, default=cv.SITES_FILE)
    return p


def main(argv: Sequence[str]) -> int:
    """Command-line entry point."""
    args = _parser().parse_args(list(argv))
    return run(args.repo_root.resolve(), args.version, sites_file=args.sites)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never a traceback
        print(f"error: prepare_release.py: internal error ({type(exc).__name__})")
        sys.exit(70)
