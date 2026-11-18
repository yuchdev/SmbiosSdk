#!/usr/bin/env python3
"""Consistency and append-only history of the self-hosted vcpkg registry.

Milestone 0012, Story 02.0 Task 02; append-only rules added by Story 04.0 Task 03
(threat model ``docs/security/2026-11-18-release-and-publish-automation.md``
RP-3, standing constraint 9).

The registry is this repository: ``ports/smbios-c/`` is the port and
``versions/`` is vcpkg's version database (``docs/dev/vcpkg-registry.md``).
The lint stage checks:

**The current tree.**

1. ``ports/smbios-c/vcpkg.json`` names the port ``smbios-c`` with a valid version;
2. ``versions/baseline.json`` names an entry that exists in
   ``versions/s-/smbios-c.json`` and is the highest one, and the entries are
   listed newest first;
3. either the port's ``(version, port-version)`` is the newest entry, and that
   entry's ``git-tree`` equals the git tree of ``ports/smbios-c`` at ``HEAD``
   (what ``vcpkg x-add-version`` records; it reads git, not the working tree),
   **or** the port is strictly newer than every entry: a release **pending
   registration**. That is the state of a release commit between its tag and
   the merge of its ``registry/v<version>`` branch (the entry's ``REF`` is the
   release commit itself, so the entry can only land after it). Nothing a
   consumer resolves points at a pending port tree;
4. the portfile's ``REF`` is a full, non-zero commit SHA and its ``URL`` is this
   repository (a SHA-addressed fetch from the parent URL can also resolve a
   commit that exists only in a fork).

**The history (append-only, constraint 9).** Needs the full history: a shallow
checkout fails closed (``fetch-depth: 0`` in the lint job).

5. every ``(version, port-version)`` that **any** commit reachable from ``HEAD``
   recorded in ``versions/s-/smbios-c.json`` still exists with the same
   ``git-tree``, so a rewrite (a hand edit, or ``x-add-version``'s overwrite
   switch) can never change what a consumer pinned to that version receives.
   :data:`SUPERSEDED` lists, by exact value, the trees replaced before the first
   publication; nothing else is exempt;
6. the ``REF`` inside every registered ``git-tree`` (read from that tree, not from
   the working copy) is an ancestor of ``master`` (``refs/remotes/origin/master``,
   else ``refs/heads/master``); a squash merge would orphan it;
7. where a ``v<version>`` tag exists, that ``REF`` equals the tag's commit.

Reports through ``ci_step`` (policy P11), one RC1 line. Exit 0 ok, 1
inconsistent, 2 usage/environment. Standard library only.
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
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import (Any, Callable, Dict, Final, FrozenSet, List, Optional, Sequence,  # noqa: E402
                    Tuple)

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
_URL: Final = re.compile(r"^\s*URL\s+(\S+)\s*$", re.MULTILINE)
#: The only source repository the port may fetch (a SHA-addressed fetch from the parent URL
#: can also return a commit that exists only in a fork, so the URL is pinned too).
PORT_URL: Final = "https://github.com/yuchdev/SmbiosSdk.git"
_MAX_LINES: Final = 10
#: Master refs tried in order when the caller names none.
MASTER_REFS: Final = ("refs/remotes/origin/master", "refs/heads/master")
GIT_TIMEOUT_S: Final = 30

#: Git trees that were recorded for an entry and replaced **before that version
#: was ever published**, by exact value (the history check's only exemption).
#: Story 02.0 regenerated ``versions/`` twice while ``1.0.0`` was still under
#: review (commits ``e63c659`` and ``e1e3add``, 2026-11-16, both later merged by
#: the merge commit of PR #17); ``483562d4...`` (``ff56d77``) is the published
#: value and is not listed. Adding a line here changes what a consumer may
#: have received: it needs a dated status.md entry and a security review
#: (milestone 0012 status.md, 2026-11-18, Story 04.0 Task 03).
SUPERSEDED: Final[Dict[Tuple[str, int], FrozenSet[str]]] = {
    ("1.0.0", 0): frozenset({"06cb80b5f6e24f2e6da8b9b529e21d83f106a726",
                             "6a4f002069e70b4dc28846125d0c406f883d9c17"}),
}

GitTree = Callable[[Path, str], Optional[str]]
#: ``git(repo_root, *args) -> (exit code, stdout)``; swappable in tests.
Git = Callable[..., Tuple[int, str]]


def _git(repo_root: Path, *args: str) -> Tuple[int, str]:
    """Run one read-only git command (argv, no shell, no stdin); ``(exit, stdout)``."""
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
                              errors="replace", check=False, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return 128, ""
    return proc.returncode, proc.stdout


def git_tree(repo_root: Path, path: str) -> Optional[str]:
    """The git tree hash of ``path`` at ``HEAD`` (``None`` if git cannot say)."""
    rc, out = _git(repo_root, "rev-parse", f"HEAD:{path}")
    out = out.strip()
    return out if rc == 0 and _SHA1.match(out) else None


def _load(path: Path, problems: List[str]) -> Optional[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        problems.append(f"{path.name}: missing or unreadable")
    except ValueError:
        problems.append(f"{path.name}: not valid JSON")
    else:
        if not isinstance(value, dict):
            problems.append(f"{path.name}: root is not a JSON object")
            return None
        return value
    return None


def version_key(version: Any, port_version: Any) -> Optional[Tuple[int, int, int, int, int]]:
    """Sort key of ``(version, port-version)``; ``None`` when either is malformed.

    ``X.Y.Z`` compares numerically; a pre-release suffix sorts before its
    release (only the base is compared beyond that).
    """
    if not (isinstance(version, str) and _VERSION.match(version)) or \
            type(port_version) is not int or port_version < 0:
        return None
    base, _, pre = version.partition("-")
    x, y, z = (int(p) for p in base.split("."))
    return x, y, z, 0 if pre else 1, port_version


def _show_key(key: Tuple[str, int]) -> str:
    """``smbios-c <version>#<port-version>`` (vcpkg's own notation; keeps RC1 lines short)."""
    return f"{PORT} {key[0]}#{key[1]}"


@dataclass
class Report:
    """Result of :func:`check_report`: problems (each one RC1 line) and notes for the ok line."""

    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _rows(versions: Optional[dict], problems: List[str]) -> List[dict]:
    rows = versions.get("versions") if isinstance(versions, dict) else None
    if versions is not None and not (isinstance(rows, list) and rows
                                     and all(isinstance(r, dict) for r in rows)):
        problems.append(f"{VERSION_FILE}: no versions listed")
        return []
    return rows or []


def check_tree(repo_root: Path, tree_of: GitTree, report: Report) -> List[dict]:
    """Checks 1-4 (the current tree); return the registered rows read from the working copy."""
    problems = report.problems
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
        url = _URL.search(portfile)
        if url is None or url.group(1) != PORT_URL:
            problems.append(f"{PORT_DIR}/portfile.cmake: URL is not {PORT_URL}")
        match = _REF.search(portfile)
        ref = match.group(1) if match else ""
        if not _SHA1.match(ref) or set(ref) == {"0"}:
            problems.append(f"{PORT_DIR}/portfile.cmake: REF is not a real 40-hex commit SHA")

    baseline = _load(repo_root / BASELINE_FILE, problems)
    default = baseline.get("default") if isinstance(baseline, dict) else None
    entry = default.get(PORT) if isinstance(default, dict) else None
    if baseline is not None and not isinstance(entry, dict):
        problems.append(f"{BASELINE_FILE}: no default entry for {PORT}")
        entry = None

    versions = _load(repo_root / VERSION_FILE, problems)
    rows = _rows(versions, problems)
    if not rows:
        return rows
    keys = [version_key(r.get("version"), r.get("port-version", 0)) for r in rows]
    if any(k is None for k in keys):
        problems.append(f"{VERSION_FILE}: an entry has no valid version and port-version")
        return rows
    if any(a <= b for a, b in zip(keys, keys[1:])):  # type: ignore[operator]
        problems.append(f"{VERSION_FILE}: entries are not unique and newest first")
    newest = rows[0]
    newest_key = keys[0]

    if isinstance(entry, dict):
        named = (entry.get("baseline"), entry.get("port-version", 0))
        if named not in [(r.get("version"), r.get("port-version", 0)) for r in rows]:
            problems.append(f"{BASELINE_FILE}: baseline names no registered entry")
        elif named != (newest.get("version"), newest.get("port-version", 0)):
            problems.append(f"{BASELINE_FILE}: baseline is not the highest registered entry")

    if version is None:
        return rows
    port_key = version_key(version, port_version)
    if port_key is not None and newest_key is not None and port_key > newest_key:
        report.notes.append(f"{version} pending registration (newest registered "
                            f"{newest.get('version')})")
        return rows
    if newest.get("version") != version:
        problems.append(f"{VERSION_FILE}: newest version differs from the port version")
    if newest.get("port-version", 0) != port_version:
        problems.append(f"{VERSION_FILE}: newest port-version differs from the port")
    if newest.get("version") == version and newest.get("port-version", 0) == port_version:
        actual = tree_of(repo_root, PORT_DIR)
        if actual is None:
            problems.append(f"git cannot read the {PORT_DIR} tree at HEAD (commit the port first)")
        elif newest.get("git-tree") != actual:
            problems.append(f"{VERSION_FILE}: git-tree is stale for {PORT_DIR} "
                            "(run vcpkg x-add-version after committing the port)")
    return rows


def resolve_master(repo_root: Path, git: Git, master_ref: Optional[str]) -> Optional[str]:
    """The commit of ``master_ref`` (or the first of :data:`MASTER_REFS` that exists)."""
    for ref in ([master_ref] if master_ref else list(MASTER_REFS)):
        rc, out = git(repo_root, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
        if rc == 0 and _SHA1.fullmatch(out.strip()):
            return out.strip()
    return None


def recorded_history(repo_root: Path, git: Git, problems: List[str]
                     ) -> Dict[Tuple[str, int], Dict[str, str]]:
    """Every ``(version, port-version) -> {git-tree: first commit}`` any commit reachable from HEAD recorded."""
    # Parents before children (--topo-order --reverse), independent of commit dates.
    rc, out = git(repo_root, "log", "--full-history", "--topo-order", "--reverse", "--format=%H",
                  "HEAD", "--", VERSION_FILE)
    if rc != 0:
        problems.append(f"git cannot list the history of {VERSION_FILE}")
        return {}
    recorded: Dict[Tuple[str, int], Dict[str, str]] = {}
    for commit in [c for c in out.split() if _SHA1.fullmatch(c)]:
        rc, text = git(repo_root, "show", f"{commit}:{VERSION_FILE}")
        if rc != 0:
            continue  # the file was deleted by this commit: a disappearance, judged on HEAD
        try:
            data = json.loads(text)
        except ValueError:
            problems.append(f"{VERSION_FILE} at {commit[:12]}: not valid JSON (history cannot "
                            "be proven append-only)")
            continue
        rows = data.get("versions") if isinstance(data, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            key = (row.get("version"), row.get("port-version", 0))
            tree = row.get("git-tree")
            if isinstance(key[0], str) and isinstance(key[1], int) and isinstance(tree, str):
                recorded.setdefault(key, {}).setdefault(tree, commit)  # type: ignore[arg-type]
    return recorded


def check_history(repo_root: Path, rows: Sequence[dict], report: Report, *,
                  git: Git = _git, master_ref: Optional[str] = None) -> None:
    """Checks 5-7: append-only history, ``REF`` on ``master``, ``REF`` == tag commit."""
    problems = report.problems
    rc, out = git(repo_root, "rev-parse", "--is-shallow-repository")
    if rc != 0:
        problems.append("git cannot read the repository history (the append-only check needs "
                        "a git checkout)")
        return
    if out.strip() == "true":
        problems.append("git history is shallow: the append-only check needs the full history "
                        "(fetch-depth: 0)")
        return
    master = resolve_master(repo_root, git, master_ref)
    if master is None:
        problems.append("master cannot be resolved (refs/remotes/origin/master or "
                        "refs/heads/master; fetch-depth: 0)")
        return

    current: Dict[Tuple[str, int], str] = {}
    for row in rows:
        key = (row.get("version"), row.get("port-version", 0))
        if isinstance(key[0], str) and isinstance(key[1], int) \
                and isinstance(row.get("git-tree"), str):
            current[key] = row["git-tree"]  # type: ignore[index]

    recorded = recorded_history(repo_root, git, problems)
    for key in sorted(recorded, key=lambda k: (str(k[0]), k[1])):
        trees = {t: c for t, c in recorded[key].items() if t not in SUPERSEDED.get(key, ())}
        if not trees:
            continue
        if len(trees) > 1:
            # Insertion order is history order (oldest first): the first tree
            # recorded is the one consumers got, the next one the rewrite.
            (a, ca), (b, cb) = list(trees.items())[:2]
            problems.append(f"{_show_key(key)} git-tree: expected {a[:12]} (recorded "
                            f"{ca[:12]}) -> actual {b[:12]} (rewritten {cb[:12]}; append-only, "
                            "constraint 9)")
            continue
        (tree, first), = trees.items()
        if key not in current:
            problems.append(f"{_show_key(key)}: expected the entry recorded {first[:12]} -> "
                            "actual removed (append-only, constraint 9)")
        elif current[key] != tree:
            problems.append(f"{_show_key(key)} git-tree: expected {tree[:12]} (recorded "
                            f"{first[:12]}) -> actual {current[key][:12]} (append-only, "
                            "constraint 9)")

    tags_checked = 0
    for key, tree in current.items():
        rc, text = git(repo_root, "cat-file", "blob", f"{tree}:portfile.cmake")
        if rc != 0:
            problems.append(f"{_show_key(key)} git-tree: expected {tree[:12]} in this "
                            "repository -> actual missing")
            continue
        match = _REF.search(text)
        ref = match.group(1) if match else ""
        if not _SHA1.fullmatch(ref):
            problems.append(f"{_show_key(key)} portfile REF: expected a 40-hex commit -> actual "
                            "<rejected>")
            continue
        rc, _ = git(repo_root, "merge-base", "--is-ancestor", ref, master)
        if rc != 0:
            problems.append(f"{_show_key(key)} REF: expected an ancestor of master -> actual "
                            f"{ref[:12]} {'not on master' if rc == 1 else 'not in the history'}")
        rc, out = git(repo_root, "rev-parse", "-q", "--verify", f"refs/tags/v{key[0]}^{{commit}}")
        tag = out.strip()
        if rc == 0 and _SHA1.fullmatch(tag):
            tags_checked += 1
            if tag != ref:
                problems.append(f"{_show_key(key)} REF: expected {tag[:12]} (tag v{key[0]}) -> "
                                f"actual {ref[:12]}")
    report.notes.append(f"history: {len(recorded)} recorded, {len(current)} REF on master, "
                        f"{tags_checked} tagged")


def check_report(repo_root: Path, tree_of: GitTree = git_tree, *, history: bool = True,
                 git: Git = _git, master_ref: Optional[str] = None) -> Report:
    """Run checks 1-4, and 5-7 unless ``history`` is false; return the :class:`Report`."""
    report = Report()
    rows = check_tree(repo_root, tree_of, report)
    if history:
        check_history(repo_root, rows, report, git=git, master_ref=master_ref)
    return report


def check(repo_root: Path, tree_of: GitTree = git_tree, *, history: bool = True,
          git: Git = _git, master_ref: Optional[str] = None) -> List[str]:
    """Return the list of inconsistencies (empty when the registry is consistent)."""
    return check_report(repo_root, tree_of, history=history, git=git,
                        master_ref=master_ref).problems


def run(repo_root: Path, *, reporter: Optional[Reporter] = None,
        results: Optional[Path] = None, tree_of: GitTree = git_tree, history: bool = True,
        master_ref: Optional[str] = None) -> int:
    """Run :func:`check_report`, print the RC1 line, append the RC3 record; return 0 or 1."""
    reporter = reporter or get_reporter(None)
    results = results or Path(env_names.get("CI_RESULTS_FILE", required=True))
    start = time.monotonic()
    report = check_report(repo_root, tree_of, history=history, master_ref=master_ref)
    problems = report.problems
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
    summary = "; ".join([f"{PORT} registry consistent"] + report.notes)
    ci_step.emit(reporter.step_ok(LABEL, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(LABEL, "hard", "ok", 0, elapsed, summary, None,
                                             None), results)
    return 0


def main(argv: Sequence[str]) -> int:
    """Command-line entry point."""
    p = argparse.ArgumentParser(prog="check_vcpkg_registry.py", description=__doc__.splitlines()[0])
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p.add_argument("--master-ref", default=None,
                   help="ref the REF ancestry is checked against (default: origin/master, "
                        "else master)")
    p.add_argument("--no-history", action="store_true",
                   help="skip the append-only history checks 5-7 (never on CI)")
    p.add_argument("--reporter", default=None)
    args = p.parse_args(list(argv))
    try:
        reporter = get_reporter(args.reporter)
    except ValueError as exc:
        ci_step.emit_error(f"check_vcpkg_registry.py: error: {exc}")
        return env_names.EXIT_ENV_ERROR
    if args.master_ref is not None and not re.fullmatch(r"refs/[A-Za-z0-9._/-]{1,100}",
                                                        args.master_ref):
        ci_step.emit_error("check_vcpkg_registry.py: error: --master-ref must be a full ref")
        return env_names.EXIT_ENV_ERROR
    return run(args.repo_root, reporter=reporter, history=not args.no_history,
               master_ref=args.master_ref)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
