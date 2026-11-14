#!/usr/bin/env python3
"""Change classification for trigger tiering (milestone 0014, Story 04.0 Task 02; plan E7).

Decides whether a run's changes are ``docs`` (lint only) or ``full`` (every
leg of the tier)::

    python3 scripts/ci/classify_changes.py [--base <rev> | --against-master] [--head <rev>]

It runs ``git diff --name-only -z --no-renames <base>...<head>``. The base is
``$CI_BASE_COMMIT`` (the PR base, or the commit before a push to ``master``
or of a tag; ADR 0005 neutral names), the head ``$CI_COMMIT`` (else
``HEAD``). For a push to any other branch the plan uses :func:`branch_base`
instead (``--against-master`` here): the merge base of the head with
``refs/remotes/origin/master``, so a branch with any code change relative to
``master`` is ``full`` on every push, and a later docs-only push cannot leave
its head commit without the Linux legs (a cancelled code-push run is not a
coverage hole). The result is
``docs`` only when every changed path is under ``docs/`` or a root-level
``*.md`` file; anything else - including ``tests/data/`` (goldens),
``scripts/``, ``examples/`` and ``.github/`` - is ``full``, because the
inventory checks C02 and C07 depend on them.

Fail-safe: the result is ``full`` when the base is empty or all zeros (a new
branch), not in the checkout, not an ancestor of the head (a force-push), when
git fails, or when the diff is empty (an empty commit pushed to re-run CI).
``--no-renames`` lists both sides of a rename, so moving a source file under
``docs/`` is never ``docs``. :func:`branch_base` is ``None`` (so ``full``) when
``origin/master`` is missing and one credential-less fetch of it fails, or
when the branch shares no history with it.

Input validation (threat model CR-6): ``--base``/``--head`` (and the
neutral names they default to) must be a 7-64 digit lower-case hex SHA or a
ref name from a conservative allowlist (no leading ``-``, no ``..``, no
``@{``); anything else exits 2 without being echoed. git is run with an argv
list, never a shell, with the step runner's child environment (CR-5).

Output: one ``ok: changes: <docs|full> (<n> paths)`` line (RC1; ``(no diff
base)`` instead of the count when there is nothing to diff). Exit 0, or 2 on a
usage error. Python 3.9+ standard library only; never a traceback.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Sequence, Tuple  # noqa: E402

import ci_step  # noqa: E402
import env_names  # noqa: E402
from reporters import Reporter, child_environment, get_reporter  # noqa: E402

DOCS = "docs"
FULL = "full"
#: The two classifications, in the order a plan consumes them.
CLASSIFICATIONS: Tuple[str, ...] = (DOCS, FULL)
#: RC1 label of the single output line.
LABEL = "changes"
#: Upper bound of one git call; the plan job's step budget is the outer bound.
GIT_TIMEOUT_S = 30.0
#: Paths beyond this count make the diff ``full`` (no need to look further).
MAX_PATHS = 20000
#: The remote-tracking ref a branch push is diffed against (its merge base).
MASTER_REMOTE_REF = "refs/remotes/origin/master"
#: The one fetch :func:`branch_base` may run when that ref is missing.
MASTER_FETCH_REFSPEC = "+refs/heads/master:" + MASTER_REMOTE_REF
#: Upper bound of that fetch.
FETCH_TIMEOUT_S = 60.0

EXIT_OK = 0
EXIT_USAGE = env_names.EXIT_ENV_ERROR

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_ZERO_SHA = re.compile(r"^0+$")
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")


class UsageError(Exception):
    """A malformed input (exit 2). The message never echoes the bad value."""


def valid_rev(value: str) -> bool:
    """Return whether ``value`` is a hex SHA or an allowlisted ref name (CR-6).

    Ref names may not contain ``..``, ``//``, ``@{`` or end in ``/``, ``.`` or
    ``.lock``, so git never reads them as a range, a reflog selector or an
    option (a leading ``-`` is excluded by the pattern itself).
    """
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        return False
    if _SHA.fullmatch(value):
        return True
    if not _REF_NAME.fullmatch(value):
        return False
    if any(bad in value for bad in ("..", "//", "@{")):
        return False
    return not value.endswith(("/", ".", ".lock"))


def _git(args: Sequence[str], cwd: Optional[Path],
         timeout_s: float = GIT_TIMEOUT_S) -> Optional[subprocess.CompletedProcess]:
    """Run ``git <args>`` (argv list, no shell); ``None`` if git cannot be started or times out.

    No credential helper and no terminal prompt: the plan job's checkout does
    not persist credentials (P14), and nothing here may ask for any.
    """
    env = child_environment(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(["git", "-c", "core.quotepath=off", "-c", "credential.helper=",
                               *args],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd else None,
                              env=env, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _commit_exists(rev: str, cwd: Optional[Path]) -> bool:
    proc = _git(["rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}"], cwd)
    return proc is not None and proc.returncode == 0


def branch_base(head: str, *, cwd: Optional[Path] = None,
                fetch: bool = True) -> Optional[str]:
    """Return the merge base of ``head`` and ``origin/master``, or ``None`` (means ``full``).

    Used for a push to a branch other than ``master``. When
    :data:`MASTER_REMOTE_REF` is missing (the checkout did not fetch it) and
    ``fetch`` is true, one ``git fetch --no-tags origin`` of
    :data:`MASTER_FETCH_REFSPEC` is tried, without credentials or a prompt.

    Raises:
        UsageError: ``head`` is not a valid SHA or ref name.
    """
    if not head or not valid_rev(head):
        raise UsageError("head must be a hex SHA or a ref name")
    if not _commit_exists(MASTER_REMOTE_REF, cwd):
        if not fetch:
            return None
        _git(["fetch", "--no-tags", "--quiet", "origin", MASTER_FETCH_REFSPEC], cwd,
             FETCH_TIMEOUT_S)
        if not _commit_exists(MASTER_REMOTE_REF, cwd):
            return None
    proc = _git(["merge-base", "--end-of-options", MASTER_REMOTE_REF, head], cwd)
    if proc is None or proc.returncode != 0:
        return None
    base = proc.stdout.decode("ascii", errors="replace").strip()
    return base if _SHA.fullmatch(base) else None


def changed_paths(base: Optional[str], head: str, *,
                  cwd: Optional[Path] = None) -> Optional[List[str]]:
    """Return the paths changed between ``base`` and ``head``, or ``None`` (means ``full``).

    Args:
        base: The diff base (PR base or the commit before a push). Empty,
            ``None`` or all zeros means "no base" (a new branch).
        head: The commit under test.
        cwd: The checkout (default: the current directory).

    Returns:
        ``git diff --name-only --no-renames base...head`` as a list, or
        ``None`` when the base is missing, not in the checkout, not an
        ancestor of ``head`` (force-push), or git fails.

    Raises:
        UsageError: ``base`` or ``head`` is not a valid SHA or ref name.
    """
    if not head or not valid_rev(head):
        raise UsageError("head must be a hex SHA or a ref name")
    if base is None or base == "" or _ZERO_SHA.fullmatch(base):
        return None
    if not valid_rev(base):
        raise UsageError("base must be a hex SHA or a ref name")
    if not (_commit_exists(base, cwd) and _commit_exists(head, cwd)):
        return None
    ancestor = _git(["merge-base", "--is-ancestor", base, head], cwd)
    if ancestor is None or ancestor.returncode != 0:
        return None
    proc = _git(["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff",
                 f"{base}...{head}", "--"], cwd)
    if proc is None or proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    paths = [p for p in text.split("\0") if p]
    return paths if len(paths) <= MAX_PATHS else None


def is_docs_path(path: str) -> bool:
    """Return whether ``path`` is a docs-only path: under ``docs/`` or a root-level ``*.md``."""
    if path.startswith("docs/") and len(path) > len("docs/"):
        return True
    return "/" not in path and "\\" not in path and path.endswith(".md") and len(path) > 3


def classify(paths: Optional[Sequence[str]]) -> str:
    """Return ``docs`` when every path is a docs path, else ``full``.

    ``None`` (no usable diff) and an empty list (nothing changed: an empty
    commit pushed to re-run CI) are ``full`` - the fail-safe direction.
    """
    if not paths:
        return FULL
    return DOCS if all(is_docs_path(p) for p in paths) else FULL


def summary_text(classification: str, paths: Optional[Sequence[str]]) -> str:
    """Return the ``ok:`` summary: ``<docs|full> (<n> paths)`` or ``full (no diff base)``."""
    if paths is None:
        return f"{classification} (no diff base)"
    return f"{classification} ({len(paths)} paths)"


def run(base: Optional[str], head: str, reporter: Reporter, *,
        cwd: Optional[Path] = None) -> Tuple[str, List[str]]:
    """Classify ``base...head``; return ``(classification, lines to print)``.

    Raises:
        UsageError: ``base`` or ``head`` is malformed.
    """
    start = time.monotonic()
    paths = changed_paths(base, head, cwd=cwd)
    result = classify(paths)
    lines = reporter.step_ok(LABEL, summary_text(result, paths),
                             round(time.monotonic() - start, 1))
    return result, lines


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="classify_changes.py",
                                description="Classify a run's changes as docs or full.")
    p.add_argument("--base", default=None, help="diff base (default $CI_BASE_COMMIT)")
    p.add_argument("--against-master", action="store_true",
                   help="diff against the merge base with origin/master (branch pushes)")
    p.add_argument("--head", default=None, help="commit under test (default $CI_COMMIT, else HEAD)")
    p.add_argument("--reporter", default=None, help="github|teamcity|plain (default $CI_REPORTER)")
    return p


def main(argv: Sequence[str]) -> int:
    """Print the one ``ok: changes:`` line; exit 0, or 2 on a usage error."""
    args = _parser().parse_args(list(argv))
    try:
        try:
            reporter = get_reporter(args.reporter)
        except ValueError:
            raise UsageError("reporter must be one of: github, teamcity, plain") from None
        head = args.head if args.head is not None else (env_names.get("CI_COMMIT") or "HEAD")
        if args.against_master and args.base is not None:
            raise UsageError("--base and --against-master are exclusive")
        if args.against_master:
            base = branch_base(head)
        else:
            base = args.base if args.base is not None else env_names.get("CI_BASE_COMMIT")
        _, lines = run(base, head, reporter)
    except UsageError as exc:
        ci_step.emit_error(f"classify_changes.py: error: {exc}")
        return EXIT_USAGE
    ci_step.emit(lines)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - never a traceback (RC2)
        ci_step.emit([f"error: classify_changes.py: internal error ({type(exc).__name__})"])
        sys.exit(70)
