#!/usr/bin/env python3
"""Run-level CI report (milestone 0014, Story 04.0 Task 01; plan E9).

The ``report`` job of ``ci.yml`` (``needs`` every stage job, ``if: always()``)
downloads every ``summary-<job>-<leg>`` artifact (``job_summary.py``) into
``$CI_TEMP_DIR/summaries`` and runs ``stage.py report``, which calls
:func:`run`. It publishes one table of at most 40 lines through the reporter's
``job_summary`` event::

    ## CI: <PASS|FAIL> - <n> legs passed, <f> failed, <w> with warnings, <s> not run (tier)

    | job | linux | windows | macos | first failure (once) |
    |---|---|---|---|---|
    | gui | FAIL | FAIL | FAIL | `gui-ci: ...` (linux-gui, windows-gui, macos-gui) |

    versions: `cmake <v> | ctest <v> | cxx <id> <v>` (<legs>)     <- one line per distinct set

Cells: ``PASS``, ``PASS (<k> warn)``, ``FAIL``, ``no record`` (an expected
leg with no readable artifact: cancelled, never started, or its summary step
did not run - never PASS), ``not run: tier`` (a leg of ``legs.json`` the
run's plan or Q1 tier does not run - never PASS), ``not run: covered by push
run`` (Story 04.0 Task 02: a same-repo PR leg its commit's push run already
reports - never PASS) and ``-`` (the job has no leg on that OS). ``<n>``
counts clean passes and ``<w>`` passes with best-effort warnings, so the four
counts are disjoint; ``, <m> no record`` is added when ``m > 0`` and ``, <c>
not run (covered by push run)`` when ``c > 0``, and a run with a missing
record is FAIL. When several legs of a job share one first failure it is
printed once with the legs listed.

Below the leg jobs, one row per milestone 0012 stage (``package``,
``verify``, ``publish``) carries the plan's reason in every column: ``not
run: stage not defined`` until 0012 defines the stage, ``not run: tier``, or,
for a scheduled stage, ``run: result in its own jobs`` (never PASS: the
stage's jobs publish no summary artifact yet). These rows are not legs and are
not counted.

When anything failed (or has no record) the report adds exactly one run-level
error annotation, ``<n> legs failed: <job>/<leg>, ...``, through the
reporter's ``error`` event (title ``CI``), and exits 1. That is the only
annotation this module emits (annotation budget, plan E9).

Artifacts are untrusted input: each is read only if it is a regular file (no
symlink) of at most 16 KiB of JSON naming a known job and leg, and every
record goes through ``job_summary.normalise_record``.

Python 3.9+ standard library only; no shell; never a traceback.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple  # noqa: E402

import job_summary  # noqa: E402
from job_summary import FAIL, PASS, NAME_TOKEN, inert  # noqa: E402
from reporters import Reporter, clean_text  # noqa: E402

#: Plan E9 line cap of the run table.
TABLE_MAX_LINES = 40
#: Largest artifact file read (the writer caps it at 8 KiB).
ARTIFACT_READ_MAX = 16 * 1024
#: Job rows in pipeline order; ``lint`` has the single leg ``linux``.
#: Row of the build job's packaging legs (``<os>-<arch>``, ADR 0005 D1; milestone 0012
#: Story 03.0): their own row, so a plain leg and a packaging leg never share a cell.
PACKAGING_ROW = "build (packaging)"
JOB_ORDER: Tuple[str, ...] = ("lint", "build", "sanitizers", "gui")
#: Table rows in order: one per job, the packaging legs after ``build``.
ROW_ORDER: Tuple[str, ...] = ("lint", "build", PACKAGING_ROW, "sanitizers", "gui")
_PACKAGING_LEG = re.compile(r"^(linux|windows|macos)-(x64|arm64)$")


def row_of(key: Tuple[str, str]) -> str:
    """Return the table row of a ``(job, leg)``: the job, or :data:`PACKAGING_ROW`."""
    return PACKAGING_ROW if key[0] == "build" and _PACKAGING_LEG.match(key[1]) else key[0]
LINT_LEG: Tuple[str, str] = ("lint", "linux")
#: Table columns; a leg's column is the OS its name starts with.
OS_COLUMNS: Tuple[str, ...] = ("linux", "windows", "macos")
#: Directory under ``$CI_TEMP_DIR`` the report job downloads the artifacts to.
SUMMARIES_DIR = "summaries"

NO_RECORD = "no record"
NOT_RUN_TIER = "not run: tier"
NOT_RUN_COVERED = "not run: covered by push run"
NOT_RUN_UNDEFINED = "not run: stage not defined"
#: Cell of a scheduled 0012 stage (its result is in its own jobs, not summarised here).
STAGE_RUN = "run: result in its own jobs"
#: Milestone 0012 stages shown as reason-only rows (``plan.PIPELINE_STAGES``).
PIPELINE_STAGES: Tuple[str, ...] = ("package", "verify", "publish")
NO_LEG = "-"
_CELL_MAX = 300
_VERSION_LINE_MAX = 400
_VERSION_FIELDS = ("cmake ", "ctest ", "cxx ")

LegKey = Tuple[str, str]


def leg_os(leg: str) -> Optional[str]:
    """Return the table column of ``leg`` (its leading OS name), or ``None``."""
    head = leg.split("-", 1)[0]
    return head if head in OS_COLUMNS else None


def dedupe_failures(firsts: Mapping[LegKey, str]) -> List[Tuple[str, List[str]]]:
    """Group legs by identical first failure, in first-seen order.

    Args:
        firsts: ``(job, leg) -> "<label>: <first_diagnostic>"``.

    Returns:
        ``[(first failure, [leg, ...]), ...]``: each distinct text once, with
        the leg names (the ``leg`` part of each key) that share it, in input
        order. Callers group by job, so the leg name alone is unambiguous.
    """
    groups: Dict[str, List[str]] = {}
    for (_job, leg), text in firsts.items():
        groups.setdefault(text, [])
        if leg not in groups[text]:
            groups[text].append(leg)
    return list(groups.items())


def _cell(records: Optional[Sequence[Mapping[str, object]]]) -> Tuple[str, str]:
    """Return ``(state, cell text)``; state is pass / warn / fail / missing."""
    if records is None:
        return "missing", NO_RECORD
    status, warnings = job_summary.leg_status(records)
    if status == FAIL:
        return "fail", FAIL
    if warnings:
        return "warn", f"PASS ({warnings} warn)"
    return "pass", PASS


def _version_key(env_line: str) -> Optional[str]:
    fields = [f.strip() for f in env_line.split(" | ")]
    picked = [next((f for f in fields if f.startswith(name)), "") for name in _VERSION_FIELDS]
    picked = [p for p in picked if p]
    return " | ".join(picked) if picked else None


def _versions(env_lines: Mapping[LegKey, str]) -> List[str]:
    groups: Dict[str, List[str]] = {}
    for (job, leg), line in env_lines.items():
        key = _version_key(line)
        if key is not None:
            groups.setdefault(key, []).append(f"{job}/{leg}")
    out = []
    for key, legs in groups.items():
        text = f"versions: {inert(key, 200, in_table=False)} ({', '.join(legs)})"
        out.append(clean_text(text, _VERSION_LINE_MAX))
    return out


def classify(job_records: Mapping[LegKey, Sequence[dict]], expected_legs: Sequence[LegKey],
             not_run: Sequence[LegKey] = (),
             covered: Sequence[LegKey] = ()) -> Dict[LegKey, str]:
    """Return each leg's state: ``pass``, ``warn``, ``fail``, ``missing``, ``tier`` or ``covered``."""
    states: Dict[LegKey, str] = {}
    for key in expected_legs:
        states[key] = _cell(job_records.get(key))[0]
    for key in not_run:
        if key not in states:
            states[key] = "tier"
    for key in covered:
        if key not in states:
            states[key] = "covered"
    return states


def render_run_table(job_records: Mapping[LegKey, Sequence[dict]],
                     expected_legs: Sequence[LegKey], max_lines: int = TABLE_MAX_LINES, *,
                     not_run: Sequence[LegKey] = (),
                     env_lines: Optional[Mapping[LegKey, str]] = None,
                     covered: Sequence[LegKey] = (),
                     stages: Optional[Mapping[str, str]] = None) -> str:
    """Return the run table (at most ``max_lines`` lines, newline-terminated).

    Args:
        job_records: ``(job, leg) -> records`` read from the artifacts.
        expected_legs: Legs this run should have run (the event's tier, plus
            ``lint``/``linux``); one without records shows ``no record``.
        max_lines: Line cap of the whole table.
        not_run: Legs of ``legs.json`` outside the tier: ``not run: tier``.
            Records for such a leg are ignored (it is never PASS).
        env_lines: ``(job, leg) -> RC4 line`` for the ``versions:`` lines.
        covered: Same-repo PR legs the push run reports:
            ``not run: covered by push run``; records are ignored (never PASS).
        stages: ``stage -> reason`` rows for the 0012 stages (none if ``None``).
    """
    states = classify(job_records, expected_legs, not_run, covered)
    counts = {s: sum(1 for v in states.values() if v == s)
              for s in ("pass", "warn", "fail", "missing", "tier", "covered")}
    ok = counts["fail"] == 0 and counts["missing"] == 0
    head = (f"## CI: {PASS if ok else FAIL} - {counts['pass']} legs passed, {counts['fail']} "
            f"failed, {counts['warn']} with warnings, {counts['tier']} not run (tier)")
    if counts["missing"]:
        head += f", {counts['missing']} no record"
    if counts["covered"]:
        head += f", {counts['covered']} not run (covered by push run)"
    jobs = [j for j in ROW_ORDER if any(row_of(k) == j for k in states)]
    jobs += sorted({row_of(k) for k in states} - set(jobs))
    rows = ["| job | " + " | ".join(OS_COLUMNS) + " | first failure (once) |",
            "|---|" + "---|" * len(OS_COLUMNS) + "---|"]
    for job in jobs:
        cells: List[str] = []
        for column in OS_COLUMNS:
            legs = [k for k in states if row_of(k) == job and leg_os(k[1]) == column]
            texts = []
            for key in legs:
                state = states[key]
                texts.append(NOT_RUN_TIER if state == "tier"
                             else NOT_RUN_COVERED if state == "covered"
                             else _cell(job_records.get(key))[1])
            cells.append(", ".join(texts) if texts else NO_LEG)
        firsts: Dict[LegKey, str] = {}
        for key, state in states.items():
            if row_of(key) == job and state == "fail":
                text = job_summary.first_failure(job_records.get(key, ()))
                if text:
                    firsts[key] = text
        rows.append(f"| {job} | " + " | ".join(cells) + f" | {_failure_cell(firsts)} |")
    for name in PIPELINE_STAGES:
        reason = (stages or {}).get(name)
        if reason in (NOT_RUN_TIER, NOT_RUN_UNDEFINED):
            rows.append(f"| {name} | " + " | ".join([reason] * len(OS_COLUMNS)) + " |  |")
        elif reason == "run":
            # Scheduled, but its jobs publish no summary artifact yet: say where the
            # result is instead of a cell that could read as PASS.
            rows.append(f"| {name} | " + " | ".join([STAGE_RUN] * len(OS_COLUMNS)) + " |  |")
    lines = [head, ""] + rows
    versions = _versions(env_lines or {})
    room = max_lines - len(lines) - 1  # one blank line before the versions
    if versions and room > 0:
        if len(versions) > room:
            shown = versions[:room - 1]
            versions = shown + [f"versions: (+{len(versions) - len(shown)} more)"]
        lines += [""] + versions
    return "\n".join(lines[:max_lines]) + "\n"


def _failure_cell(firsts: Mapping[LegKey, str]) -> str:
    """Return the ``first failure (once)`` cell: whole groups only, never a cut code span."""
    parts: List[str] = []
    groups = dedupe_failures(firsts)
    for index, (text, legs) in enumerate(groups):
        part = f"{inert(text, 200)} ({', '.join(legs)})"
        if len("; ".join(parts + [part])) > _CELL_MAX:
            parts.append(f"(+{len(groups) - index} more)")
            break
        parts.append(part)
    return "; ".join(parts)


def failed_legs(job_records: Mapping[LegKey, Sequence[dict]],
                expected_legs: Sequence[LegKey]) -> List[str]:
    """Return ``<job>/<leg>`` of every expected leg that failed, or ``... (no record)``."""
    out: List[str] = []
    for key in expected_legs:
        state = _cell(job_records.get(key))[0]
        if state == "fail":
            out.append(f"{key[0]}/{key[1]}")
        elif state == "missing":
            out.append(f"{key[0]}/{key[1]} (no record)")
    return out


def expected_legs(legs_data: Mapping[str, object],
                  tier: Optional[Sequence[str]]) -> Tuple[List[LegKey], List[LegKey]]:
    """Return ``(expected, not run)`` legs from ``legs.json`` and the event's tier.

    Every leg named by any tier is a table cell; ``tier`` (``None`` for a
    local run: the ``full`` tier) selects the expected ones. ``lint/linux``
    is always expected.
    """
    tiers = legs_data.get("tiers", {})
    entries = legs_data.get("legs", {})
    tiers = tiers if isinstance(tiers, dict) else {}
    entries = entries if isinstance(entries, dict) else {}
    selected = set(tier) if tier is not None else set(tiers.get("full", []))
    names: List[str] = []
    for members in tiers.values():
        for name in members if isinstance(members, list) else []:
            if isinstance(name, str) and name not in names:
                names.append(name)
    expected: List[LegKey] = [LINT_LEG]
    not_run: List[LegKey] = []
    for name in names:
        entry = entries.get(name)
        job = entry.get("job", "build") if isinstance(entry, dict) else "build"
        (expected if name in selected else not_run).append((str(job), name))
    return expected, not_run


def legs_from_plan(legs_data: Mapping[str, object], plan: Mapping[str, object]
                   ) -> Tuple[List[LegKey], List[LegKey], List[LegKey]]:
    """Return ``(expected, not run: tier, covered by push run)`` legs from a parsed plan.

    ``plan`` is ``plan.parse_plan``'s result (``{"legs": {leg: reason}}``);
    ``lint/linux`` is always expected.
    """
    entries = legs_data.get("legs", {})
    entries = entries if isinstance(entries, dict) else {}
    expected: List[LegKey] = [LINT_LEG]
    not_run: List[LegKey] = []
    covered: List[LegKey] = []
    reasons = plan.get("legs", {})
    for name, reason in (reasons.items() if isinstance(reasons, dict) else []):
        entry = entries.get(name)
        key = (str(entry.get("job", "build") if isinstance(entry, dict) else "build"), str(name))
        if reason == "run":
            expected.append(key)
        elif reason == NOT_RUN_COVERED:
            covered.append(key)
        else:
            not_run.append(key)
    return expected, not_run, covered


def load_artifacts(directory: Path, allowed: Set[LegKey]) -> Tuple[Dict[LegKey, List[dict]],
                                                                   Dict[LegKey, str], int]:
    """Read every ``summary-<job>-<leg>.json`` under ``directory`` (and one level below).

    Returns:
        ``(records per leg, RC4 line per leg, number of files rejected)``. A
        file is rejected when it is a symlink or not a regular file, larger
        than 16 KiB, not JSON, of another schema, for a leg not in ``allowed``,
        or named differently from its own ``job``/``leg``.
    """
    records: Dict[LegKey, List[dict]] = {}
    env_lines: Dict[LegKey, str] = {}
    rejected = 0
    if not directory.is_dir() or directory.is_symlink():
        return records, env_lines, 0
    candidates = sorted(directory.glob("summary-*.json")) + \
        sorted(directory.glob("summary-*/summary-*.json"))
    for path in candidates:
        try:
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                raise ValueError("not a regular file")
            data = job_summary._read_bounded(path, ARTIFACT_READ_MAX + 1)
            if len(data) > ARTIFACT_READ_MAX:
                raise ValueError("too large")
            obj = json.loads(data.decode("utf-8"))
            if not isinstance(obj, dict) or obj.get("schema") != job_summary.ARTIFACT_SCHEMA:
                raise ValueError("schema")
            job, leg = obj.get("job"), obj.get("leg")
            if not (isinstance(job, str) and isinstance(leg, str) and NAME_TOKEN.match(job)
                    and NAME_TOKEN.match(leg) and (job, leg) in allowed
                    and path.stem == job_summary.artifact_name(job, leg)):
                raise ValueError("leg")
            raw = obj.get("records")
            clean = [r for r in (job_summary.normalise_record(x)
                                 for x in (raw if isinstance(raw, list) else [])[:200]) if r]
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            rejected += 1
            continue
        records[(job, leg)] = clean
        line = obj.get("env_line")
        if isinstance(line, str) and line:
            env_lines[(job, leg)] = clean_text(line, 240)
    return records, env_lines, rejected


def run(reporter: Reporter, *, summaries: Path, legs_data: Mapping[str, object],
        tier: Optional[Sequence[str]],
        plan: Optional[Mapping[str, object]] = None) -> Tuple[int, List[str]]:
    """Publish the run table; return ``(exit code, lines to print)``.

    ``plan`` (``plan.parse_plan``'s result, from ``CI_PLAN``) decides the
    expected, ``not run: tier`` and ``covered by push run`` legs and the 0012
    stage rows; without it ``tier`` decides and the stage rows say ``not run:
    stage not defined``. Exit 1 when any expected leg failed or has no record
    (then exactly one run-level error annotation is among the lines), else 0.
    """
    if plan is not None:
        expected, not_run, covered = legs_from_plan(legs_data, plan)
        stages = plan.get("stages")
        stages = dict(stages) if isinstance(stages, dict) else {}
    else:
        (expected, not_run), covered = expected_legs(legs_data, tier), []
        stages = {name: NOT_RUN_UNDEFINED for name in PIPELINE_STAGES}
    allowed = set(expected) | set(not_run) | set(covered)
    records, env_lines, rejected = load_artifacts(summaries, allowed)
    table = render_run_table(records, expected, not_run=not_run, env_lines=env_lines,
                             covered=covered, stages=stages)
    lines: List[str] = []
    try:
        reporter.job_summary(table)
    except (OSError, SystemExit) as exc:
        lines += reporter.info(f"info: run report: not published ({type(exc).__name__})")
    if rejected:
        lines += reporter.info(f"info: run report: {rejected} summary artifacts rejected")
    failed = failed_legs(records, expected)
    lines += reporter.info(f"info: run report: {table.splitlines()[0].lstrip('# ')}")
    if failed:
        lines += reporter.error("CI", f"{len(failed)} legs failed: {', '.join(failed)}")
        return 1, lines
    return 0, lines
