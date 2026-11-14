#!/usr/bin/env python3
"""Per-job summary block and summary artifact (milestone 0014, Story 04.0 Task 01; plan E9).

The last project-owned step of every job (``stage.py summary``, ``if: always()``
in the workflow) reads the job's RC3 records (``$CI_RESULTS_FILE``) and its RC4
line (the ``ci-env-line.txt`` hand-off file that ``print_env.py`` writes under
``$CI_TEMP_DIR``), and

- publishes one Markdown block of at most 30 lines and 4 KiB through the
  reporter backend's ``job_summary`` event (ADR 0005)::

      ### <job> (<leg>) - PASS | FAIL | PASS with <k> best-effort warnings
      `<RC4 line>`

      | step | result | detail |      <- one row per RC3 record; detail <= 120 chars
      |---|---|---|

      first failure: `<label>: <first_diagnostic>`   <- only when FAIL

- writes the same records as ``$CI_ARTIFACT_DIR/summary-<job>-<leg>.json``
  (at most 8 KiB, mode 0600, never through a symlink), which the workflow
  uploads as the artifact ``summary-<job>-<leg>`` for the run-level report
  (``run_report.py``); matrix job outputs collapse to one leg, so they cannot
  carry this.

A job that failed outside any recorded step (a step timeout, a cancel, a
platform step such as the cache restore, a stage usage error) is passed in as
``job_failed``; it then gets one synthetic ``job`` record with outcome
``fail``, so the job is never reported as PASS.

Hygiene (RC1, threat model CR-4): every text taken from a record goes through
``reporters.clean_text`` again (records are already escaped and cut by
``ci_step``) and is rendered inside a Markdown code span with backticks and
pipes neutralised, so Markdown or HTML in tool output is inert. Records hold no
log content: only labels, outcomes, exit codes, durations, the ``ok:`` summary,
``first_diagnostic`` and counts.

Python 3.9+ standard library only; no shell; never a traceback.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

from reporters import ANNOTATION_MAX, INFO_MAX, LABEL_MAX, Reporter, clean_text  # noqa: E402

#: Plan E9 bounds of one job block.
BLOCK_MAX_LINES = 30
BLOCK_MAX_BYTES = 4096
#: Longest ``detail`` cell, code-span backticks included.
DETAIL_MAX = 120
#: Ceiling of the ``summary-<job>-<leg>.json`` artifact.
ARTIFACT_MAX_BYTES = 8192
#: Schema version of the artifact.
ARTIFACT_SCHEMA = 1
#: Most bytes read from the RC3 results file; a longer file is read up to here.
RESULTS_READ_MAX = 1 << 20
#: Longest RC3 line considered; a longer line is skipped as malformed.
RECORD_LINE_MAX = 8192
#: The ``print_env.py`` hand-off file under ``$CI_TEMP_DIR`` (kept equal to
#: ``print_env.ENV_LINE_FILE`` by the tests).
ENV_LINE_FILE = "ci-env-line.txt"
#: Most bytes read from :data:`ENV_LINE_FILE`.
ENV_LINE_READ_MAX = 1024

#: Job and leg names that may appear in a header or an artifact name.
NAME_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_COUNT_KEY = re.compile(r"^[a-z_]{1,16}$")

OUTCOMES = ("ok", "fail", "warn", "skip")
MODES = ("hard", "best-effort", "info")

#: Label and text of the synthetic record of a job that failed outside any step.
JOB_FAILED_LABEL = "job"
JOB_FAILED_TEXT = "job failed outside a recorded step (timeout, cancel or a platform step)"

PASS = "PASS"
FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _text(value: object, width: int) -> str:
    return clean_text(value, width) if isinstance(value, str) else ""


def normalise_record(raw: object) -> Optional[Dict[str, object]]:
    """Return a clean copy of one RC3 record, or ``None`` if it is not one.

    Every string is re-sanitised and cut (label 80, summary and diagnostic
    160 characters), the outcome and mode must be known values, numbers must
    be finite, and ``counts`` keeps at most 8 ``name: int`` pairs.
    """
    if not isinstance(raw, dict):
        return None
    outcome = raw.get("outcome")
    label = _text(raw.get("label"), LABEL_MAX)
    if outcome not in OUTCOMES or not label:
        return None
    mode = raw.get("mode") if raw.get("mode") in MODES else "hard"
    rc = raw.get("exit")
    rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else 0
    elapsed = raw.get("elapsed_s")
    elapsed = float(elapsed) if isinstance(elapsed, (int, float)) and not isinstance(
        elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0 else 0.0
    diag = raw.get("first_diagnostic")
    counts = raw.get("counts")
    clean_counts: Optional[Dict[str, int]] = None
    if isinstance(counts, dict):
        clean_counts = {k: v for k, v in list(counts.items())[:8]
                        if isinstance(k, str) and _COUNT_KEY.match(k)
                        and isinstance(v, int) and not isinstance(v, bool)}
    return {"label": label, "mode": mode, "outcome": outcome, "exit": rc,
            "elapsed_s": round(elapsed, 1), "summary": _text(raw.get("summary"), ANNOTATION_MAX),
            "first_diagnostic": _text(diag, ANNOTATION_MAX) if isinstance(diag, str) else None,
            "counts": clean_counts}


def _open_nofollow(path: Path) -> int:
    if path.is_symlink():  # O_NOFOLLOW does not exist on Windows; refuse there too
        raise OSError("refusing symlink")
    return os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                   | getattr(os, "O_BINARY", 0))


def _read_bounded(path: Path, limit: int) -> bytes:
    fd = _open_nofollow(path)
    try:
        chunks: List[bytes] = []
        left = limit
        while left > 0:
            chunk = os.read(fd, min(65536, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_records(path: Path, limit: int = RESULTS_READ_MAX) -> Tuple[List[Dict[str, object]], int]:
    """Return the normalised RC3 records in ``path`` and the number of lines skipped.

    A missing file is no records (the job failed before its first step). A
    malformed or overlong line is skipped and counted; at most ``limit``
    bytes are read and a symlink is refused (CR-11).

    Raises:
        OSError: The file exists but cannot be read (or is a symlink).
    """
    if not path.exists() and not path.is_symlink():
        return [], 0
    data = _read_bounded(path, limit)
    records: List[Dict[str, object]] = []
    skipped = 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        if len(raw) > RECORD_LINE_MAX:
            skipped += 1
            continue
        try:
            record = normalise_record(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError):
            record = None
        if record is None:
            skipped += 1
        else:
            records.append(record)
    return records, skipped


def read_env_line(temp_dir: Path) -> str:
    """Return the RC4 line handed off by ``print_env.py``, sanitised, or ``""`` if absent."""
    path = temp_dir / ENV_LINE_FILE
    try:
        data = _read_bounded(path, ENV_LINE_READ_MAX)
    except OSError:
        return ""
    return clean_text(data.decode("utf-8", errors="replace"), INFO_MAX)


def effective_records(records: Sequence[Mapping[str, object]],
                      job_failed: bool) -> List[Dict[str, object]]:
    """Return ``records`` plus the synthetic ``job`` failure record when needed.

    The synthetic record is added only when ``job_failed`` is true and no
    record already failed, so a failing step is never reported twice.
    """
    out = [dict(r) for r in records]
    if job_failed and not any(r.get("outcome") == "fail" for r in out):
        out.append({"label": JOB_FAILED_LABEL, "mode": "hard", "outcome": "fail", "exit": 1,
                    "elapsed_s": 0.0, "summary": "", "first_diagnostic": JOB_FAILED_TEXT,
                    "counts": None})
    return out


def leg_status(records: Sequence[Mapping[str, object]]) -> Tuple[str, int]:
    """Return ``(PASS|FAIL, best-effort warning count)`` for one leg's records."""
    failed = any(r.get("outcome") == "fail" for r in records)
    warnings = sum(1 for r in records if r.get("outcome") == "warn")
    return (FAIL if failed else PASS), warnings


def status_text(status: str, warnings: int) -> str:
    """Return ``PASS``, ``FAIL`` or ``PASS with <k> best-effort warning(s)``."""
    if status == PASS and warnings:
        return f"PASS with {warnings} best-effort warning{'s' if warnings != 1 else ''}"
    return status


def first_failure(records: Sequence[Mapping[str, object]]) -> Optional[str]:
    """Return ``<label>: <first_diagnostic>`` of the first failing record, or ``None``."""
    for r in records:
        if r.get("outcome") == "fail":
            diag = r.get("first_diagnostic") or r.get("summary") or f"exit {r.get('exit')}"
            return f"{r.get('label')}: {diag}"
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def inert(text: str, width: int, *, in_table: bool = True) -> str:
    """Return ``text`` as one Markdown code span of at most ``width`` characters.

    The text is sanitised (``clean_text``: one line, no control or bidi
    characters, service-message prefixes broken). Inside the span a backtick
    becomes ``'`` (so the span cannot be closed early) and, when ``in_table``,
    ``|`` becomes ``\\|`` (so a table cell cannot be split; GitHub renders it
    as ``|``). Markdown and HTML inside a code span are shown literally. An
    empty text gives ``""``.
    """
    clean = clean_text(text, max(width, 1) * 2)
    if not clean or width < 3:
        return ""
    out: List[str] = []
    used = 2
    for ch in clean:
        piece = "'" if ch == "`" else ("\\|" if ch == "|" and in_table else ch)
        if used + len(piece) > width:
            break
        out.append(piece)
        used += len(piece)
    body = "".join(out).strip()
    return f"`{body}`" if body else ""


def _name(value: str) -> str:
    return value if NAME_TOKEN.match(value or "") else "?"


def _result_cell(record: Mapping[str, object]) -> str:
    outcome = record.get("outcome")
    rc = record.get("exit")
    elapsed = record.get("elapsed_s") or 0.0
    if outcome == "ok":
        return f"ok ({elapsed:.1f}s)"
    if outcome == "fail":
        return f"FAIL (exit {rc})"
    if outcome == "warn":
        return f"warn (exit {rc})"
    return "skip"


def _detail(record: Mapping[str, object]) -> str:
    if record.get("outcome") in ("fail", "warn"):
        text = record.get("first_diagnostic") or record.get("summary") or ""
    else:
        text = record.get("summary") or ""
    return inert(str(text), DETAIL_MAX)


def _row(record: Mapping[str, object]) -> str:
    return f"| {inert(str(record.get('label', '')), LABEL_MAX + 2)} | {_result_cell(record)} | " \
           f"{_detail(record)} |"


def _bytes(lines: Sequence[str]) -> int:
    return len("\n".join(lines).encode("utf-8")) + 1


def render_job_block(records: Sequence[dict], env_line: str, job: str, leg: str,
                     max_lines: int = BLOCK_MAX_LINES, max_bytes: int = BLOCK_MAX_BYTES) -> str:
    """Return the Markdown block of one job (plan E9: at most ``max_lines`` and ``max_bytes``).

    Args:
        records: The job's RC3 records (normalised; a failure outside any step
            is the synthetic record of :func:`effective_records`).
        env_line: The RC4 line, or ``""`` when the job never printed it.
        job: Job name (``lint``, ``build``, ...); anything else prints ``?``.
        leg: Leg name; same rule.
        max_lines: Line cap of the whole block.
        max_bytes: Byte cap of the whole block (UTF-8, trailing newline included).

    Returns:
        The block, newline-terminated. When rows do not fit, failing and
        best-effort rows are kept first and one ``(+k more)`` row stands for
        the rest.
    """
    status, warnings = leg_status(records)
    head = [f"### {_name(job)} ({_name(leg)}) - {status_text(status, warnings)}",
            inert(env_line, INFO_MAX + 2, in_table=False) or "env: (no RC4 line)",
            "",
            "| step | result | detail |",
            "|---|---|---|"]
    tail: List[str] = []
    first = first_failure(records) if status == FAIL else None
    if first is not None:
        tail = ["", f"first failure: "
                f"{inert(first, ANNOTATION_MAX + LABEL_MAX + 4, in_table=False)}"]
    rows = [_row(r) for r in records] or ["| (no step records) | - | |"]
    # Keep failing and best-effort rows first, the rest in run order.
    priority = [i for i, r in enumerate(records) if r.get("outcome") in ("fail", "warn")]
    order = priority + [i for i in range(len(rows)) if i not in priority]

    def build(keep: Sequence[int]) -> List[str]:
        kept = [rows[i] for i in sorted(keep)]
        hidden = len(rows) - len(kept)
        more = [f"| (+{hidden} more) | | |"] if hidden else []
        return head + kept + more + tail

    keep = list(order)
    lines = build(keep)
    while keep and (len(lines) > max_lines or _bytes(lines) > max_bytes):
        keep.pop()  # drop the lowest-priority row
        lines = build(keep)
    while lines and (len(lines) > max_lines or _bytes(lines) > max_bytes):
        lines.pop()  # cannot happen with the default caps; stays bounded anyway
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

def artifact_name(job: str, leg: str) -> str:
    """Return the artifact (and file stem) name ``summary-<job>-<leg>``."""
    return f"summary-{_name(job)}-{_name(leg)}"


def artifact_payload(records: Sequence[Mapping[str, object]], env_line: str, job: str, leg: str,
                     max_bytes: int = ARTIFACT_MAX_BYTES) -> Dict[str, object]:
    """Return the summary artifact object, at most ``max_bytes`` once serialised.

    Records that do not fit are dropped (passing ones first) and counted in
    ``omitted``; failing and best-effort records are dropped last.
    """
    kept = [dict(r) for r in records]
    important = [i for i, r in enumerate(kept) if r.get("outcome") in ("fail", "warn")]
    drop_order = [i for i in reversed(range(len(kept))) if i not in important] \
        + list(reversed(important))
    payload: Dict[str, object] = {"schema": ARTIFACT_SCHEMA, "job": _name(job),
                                  "leg": _name(leg), "env_line": clean_text(env_line, INFO_MAX),
                                  "records": kept, "omitted": 0}
    removed: set = set()
    while len(serialise(payload)) > max_bytes and drop_order:
        removed.add(drop_order.pop(0))
        payload["records"] = [r for i, r in enumerate(kept) if i not in removed]
        payload["omitted"] = len(removed)
    return payload


def serialise(payload: Mapping[str, object]) -> bytes:
    """Return ``payload`` as compact ASCII JSON plus a newline."""
    return (json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            + "\n").encode("ascii")


def write_artifact(payload: Mapping[str, object], directory: Path) -> Path:
    """Write ``<directory>/summary-<job>-<leg>.json``: mode 0600, never through a symlink.

    Raises:
        OSError: The file cannot be written, or the path is a symlink.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{artifact_name(str(payload['job']), str(payload['leg']))}.json"
    if path.is_symlink():
        raise OSError("refusing symlink")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0))
    fd = os.open(str(path), flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        data = serialise(payload)
        if os.write(fd, data) != len(data):
            raise OSError("short write")
    finally:
        os.close(fd)
    return path


# ---------------------------------------------------------------------------
# Entry point (called by stage.py summary)
# ---------------------------------------------------------------------------

def run(reporter: Reporter, *, job: str, leg: str, results: Path, temp_dir: Path,
        artifact_dir: Path, job_failed: bool = False) -> Tuple[int, List[str]]:
    """Publish the job block and write the artifact; return ``(0, lines to print)``.

    Informational: every problem becomes one ``info:`` line and the exit code
    stays 0, so the summary step never changes a job's result.
    """
    lines: List[str] = []
    try:
        records, skipped = load_records(results)
    except OSError as exc:
        records, skipped = [], 0
        lines += reporter.info(f"info: job summary: results not read ({type(exc).__name__})")
    records = effective_records(records, job_failed)
    env_line = read_env_line(temp_dir)
    block = render_job_block(records, env_line, job, leg)
    status, warnings = leg_status(records)
    try:
        reporter.job_summary(block)
    except (OSError, SystemExit) as exc:
        lines += reporter.info(f"info: job summary: not published ({type(exc).__name__})")
    try:
        write_artifact(artifact_payload(records, env_line, job, leg), artifact_dir)
    except OSError as exc:
        lines += reporter.info(f"info: job summary: artifact not written ({type(exc).__name__})")
    note = f", {skipped} malformed records skipped" if skipped else ""
    lines += reporter.info(f"info: job summary: {_name(job)} ({_name(leg)}) "
                           f"{status_text(status, warnings)}, {len(records)} records{note}")
    return 0, lines
