#!/usr/bin/env python3
"""Run plan: triggers, tiering and duplicate-run avoidance (milestone 0014, Story 04.0 Task 02).

The ``plan`` job of ``ci.yml`` runs ``python3 scripts/ci/stage.py plan``, which
calls :func:`run`. It classifies the run's changes (``classify_changes.py``),
applies the tier table (:func:`matrix_for`) and hands the result to the leg
jobs as job outputs, written only through ``Reporter.set_output()`` (threat
model CR-6/CR-8: single-line JSON, random per-call delimiter):

``build``, ``sanitizers``, ``gui``
    JSON lists of leg names; ``ci.yml`` passes each to its stage workflow as
    the ``legs`` input (``fromJSON`` there) and skips the job with ``if:`` when
    the list is ``[]`` (an empty matrix is an error on the platform).
``package``, ``verify``, ``publish``
    Milestone 0012's stages. ``package`` (Story 03.0) lists the packaging legs
    on every full-classified event but a branch push (ADR 0005 D7; the same
    events whose ``build`` list holds those legs, from ``tiers.full``). Until a
    stage is defined its list is ``[]`` and the plan says ``not run: stage not
    defined`` for it. ``publish`` rows come
    only from ``scripts/ci/publish_jobs.json``: ``release``-tier jobs on a
    ``tag`` event, ``staging``-tier jobs on a push to ``refs/heads/master``,
    nothing on any other event.
``plan``
    The whole plan object (per-leg run/skip reasons, per-stage reasons, the
    runner label of every scheduled leg). The ``report`` job reads it back as
    ``CI_PLAN`` (:func:`parse_plan`) so a skipped leg shows ``not run: tier`` or
    ``not run: covered by push run``, never PASS and never ``no record``.

Tier table (plan Q1 as ruled 2026-11-12; maintainer ruling: no
``workflow_dispatch``, so ``manual`` is not a plan event):

=============================  ====  ===============  ===============  ======================
Event                          lint  build            sanitizers       gui
=============================  ====  ===============  ===============  ======================
any event, ``docs`` changes    yes   -                -                -
push, branch other than master yes   linux            linux-asan       linux-gui
same-repo PR                   yes   windows, macos   macos-asan       windows-gui, macos-gui
fork PR, master push, v* tag,  yes   3 OS             linux-asan,      3 OS
nightly                                               macos-asan
=============================  ====  ===============  ===============  ======================

The packaging legs (``linux-x64``, ``windows-x64``, ``macos-arm64``; job
``build``, milestone 0012 Story 03.0) are in ``tiers.full`` only: they join the
``build`` list of every row above except the branch push (on a same-repo PR
too, since its push run did not run them; ADR 0005 D7).

Diff base of the classification: the PR base on ``pr``; the event's
``before`` (``CI_BASE_COMMIT``) on a push to ``master`` or a tag; on a push to
any other branch the merge base with ``origin/master``
(``classify_changes.branch_base``), so a branch with code changes relative to
``master`` always plans its Linux legs. Anything unresolvable is ``full``.

A same-repo PR runs only the legs the push tier did not run: its commit's
push run already reports the Linux legs (status checks attach to the SHA), so
the plan marks them ``not run: covered by push run``. A fork PR has no push
run in this repository and runs every leg. ``legs.json`` ``tiers`` stays the
single source of the leg lists.

Inputs are re-validated here (CR-6), never trusted from the platform:
``CI_EVENT`` (``push``/``pr``/``schedule``/``tag``), ``CI_REF`` (shape per
event), ``CI_BASE_COMMIT``/``CI_COMMIT`` (``classify_changes.valid_rev``),
``CI_PR_SAME_REPO`` (``true``/``false``/empty); a value outside its allowlist
or containing CR/LF exits 2 without being echoed. Only legs of ``legs.json``
and only runner labels of ``pins.json`` are ever emitted; a tier leg that is
unknown, pending, of an unknown job, or whose runner has no pinned label exits
2 too.

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
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

import classify_changes  # noqa: E402
import env_names  # noqa: E402
from reporters import Reporter  # noqa: E402

LEGS_FILE = Path(_HERE) / "legs.json"
PINS_FILE = Path(_HERE) / "pins.json"
PUBLISH_JOBS_FILE = Path(_HERE) / "publish_jobs.json"

#: Neutral ``CI_EVENT`` values a plan accepts. ``manual`` is rejected: the
#: maintainer removed ``workflow_dispatch``, so nothing may plan
#: for it and dispatch can never reach ``publish``.
EVENTS: Tuple[str, ...] = ("push", "pr", "schedule", "tag")
#: Workflow jobs that run legs (``legs.json`` ``job``), in pipeline order.
LEG_JOBS: Tuple[str, ...] = ("build", "sanitizers", "gui")
#: Milestone 0012 stages (ADR 0005 extension of the tier table).
PIPELINE_STAGES: Tuple[str, ...] = ("package", "verify", "publish")
#: Every job-output name the plan sets, in the order it sets them.
OUTPUTS: Tuple[str, ...] = LEG_JOBS + PIPELINE_STAGES + ("plan",)
#: Publish tiers (ADR 0005 D9) and the only event that reaches each.
PUBLISH_TIERS: Tuple[str, ...] = ("release", "staging")
MASTER_REF = "refs/heads/master"
PLAN_SCHEMA = 1
#: Largest ``CI_PLAN`` value :func:`parse_plan` reads.
PLAN_MAX_BYTES = 16 * 1024

RUN = "run"
NOT_RUN_TIER = "not run: tier"
NOT_RUN_COVERED = "not run: covered by push run"
NOT_RUN_UNDEFINED = "not run: stage not defined"
#: Every reason a leg can carry in a plan.
LEG_REASONS: Tuple[str, ...] = (RUN, NOT_RUN_TIER, NOT_RUN_COVERED)
#: Every reason a pipeline stage can carry in a plan.
STAGE_REASONS: Tuple[str, ...] = (RUN, NOT_RUN_TIER, NOT_RUN_UNDEFINED)

_LEG_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_JOB_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_LABEL = re.compile(r"^[0-9A-Za-z._-]{1,40}$")
_BRANCH_REF = re.compile(r"^refs/heads/[A-Za-z0-9._/+-]{1,200}$")
_REF_SHAPES: Dict[str, "re.Pattern[str]"] = {
    "push": _BRANCH_REF,
    "schedule": _BRANCH_REF,
    "pr": re.compile(r"^refs/pull/[0-9]{1,10}/merge$"),
    "tag": re.compile(r"^refs/tags/v[A-Za-z0-9._+-]{1,100}$"),
}
_BOOL_VALUES: Tuple[str, ...] = ("true", "false", "")


class PlanError(ValueError):
    """An input or data file outside its allowlist (exit 2). Never echoes the bad value."""


def _default_defined_stages() -> FrozenSet[str]:
    """Return the pipeline stages ``stage.py`` implements (``stage.STEPS``)."""
    import stage
    return frozenset(s for s in PIPELINE_STAGES if s in stage.STEPS)


@dataclass(frozen=True)
class PlanData:
    """The data files a plan reads, already parsed.

    Attributes:
        legs: ``legs.json`` (``stages``, ``tiers``, ``legs``).
        pins: ``pins.json`` (only ``runners`` is read).
        publish_jobs: ``publish_jobs.json`` ``jobs`` entries.
        defined_stages: Pipeline stages that exist (default: those in
            ``stage.STEPS``); an undefined stage is ``not run: stage not defined``.
    """

    legs: Mapping[str, Any]
    pins: Mapping[str, Any]
    publish_jobs: Sequence[Mapping[str, Any]] = ()
    defined_stages: FrozenSet[str] = field(default_factory=frozenset)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PlanError(f"cannot read {path.name}") from None


def load_data(legs_path: Path = LEGS_FILE, pins_path: Path = PINS_FILE,
              publish_path: Path = PUBLISH_JOBS_FILE,
              defined_stages: Optional[FrozenSet[str]] = None) -> PlanData:
    """Read and shape-check the plan's data files.

    Raises:
        PlanError: A file is unreadable or not the expected shape.
    """
    legs = _read_json(legs_path)
    if not (isinstance(legs, dict) and isinstance(legs.get("legs"), dict)
            and isinstance(legs.get("tiers"), dict)):
        raise PlanError(f"{legs_path.name}: expected 'tiers' and 'legs' objects")
    pins = _read_json(pins_path)
    if not (isinstance(pins, dict) and isinstance(pins.get("runners"), dict)):
        raise PlanError(f"{pins_path.name}: expected a 'runners' object")
    publish = _read_json(publish_path)
    jobs = publish.get("jobs") if isinstance(publish, dict) else None
    if not (isinstance(jobs, list) and all(isinstance(j, dict) for j in jobs)):
        raise PlanError(f"{publish_path.name}: expected a 'jobs' list of objects")
    return PlanData(legs, pins, tuple(jobs),
                    _default_defined_stages() if defined_stages is None else defined_stages)


# ---------------------------------------------------------------------------
# Validation (CR-6)
# ---------------------------------------------------------------------------

def _single_line(value: Any) -> bool:
    return isinstance(value, str) and "\r" not in value and "\n" not in value


def validate_event(event: Any, ref: Any) -> None:
    """Check ``event`` against :data:`EVENTS` and ``ref`` against that event's ref shape.

    Raises:
        PlanError: Either value is outside its allowlist (never echoed).
    """
    if not _single_line(event) or event not in EVENTS:
        raise PlanError(f"CI_EVENT must be one of: {', '.join(EVENTS)}")
    if not _single_line(ref) or not _REF_SHAPES[event].fullmatch(ref):
        raise PlanError(f"CI_REF is not a well-formed ref for a {event} event")
    if ".." in ref or "//" in ref or "@{" in ref:
        raise PlanError(f"CI_REF is not a well-formed ref for a {event} event")


def _tier_lists(data: PlanData) -> Tuple[List[str], List[str]]:
    """Return ``(full, push_branch)`` tier legs, each validated against ``legs.json`` and ``pins.json``.

    Raises:
        PlanError: A tier is missing or not a list, or a tier leg is unknown,
            pending, of an unknown job, or has no pinned runner label.
    """
    tiers = data.legs.get("tiers", {})
    entries = data.legs.get("legs", {})
    out: List[List[str]] = []
    for name in ("full", "push_branch"):
        members = tiers.get(name) if isinstance(tiers, dict) else None
        if not isinstance(members, list):
            raise PlanError(f"legs.json: tier {name} must be a list")
        for leg in members:
            if not (isinstance(leg, str) and _LEG_NAME.fullmatch(leg)
                    and isinstance(entries, dict) and isinstance(entries.get(leg), dict)):
                raise PlanError(f"legs.json: tier {name} names a leg that is not declared")
            entry = entries[leg]
            if entry.get("job", "build") not in LEG_JOBS:
                raise PlanError(f"legs.json: a leg of tier {name} has an unknown job")
            if entry.get("pending"):
                raise PlanError(f"legs.json: tier {name} names a pending leg")
            runner_label(leg, data)
        out.append(list(dict.fromkeys(members)))
    full, push_branch = out
    if not set(push_branch) <= set(full):
        raise PlanError("legs.json: every push_branch leg must also be a full-tier leg")
    return full, push_branch


def runner_label(leg: str, data: PlanData) -> str:
    """Return the pinned runner label of ``leg`` (``legs.json`` runner key -> ``pins.json``).

    Raises:
        PlanError: The leg's runner key is not in ``pins.json`` ``runners``, or
            the label is malformed.
    """
    entry = data.legs.get("legs", {}).get(leg)
    key = entry.get("runner") if isinstance(entry, dict) else None
    runners = data.pins.get("runners", {})
    label = runners.get(key) if isinstance(runners, dict) and isinstance(key, str) else None
    if not (isinstance(label, str) and _LABEL.fullmatch(label)):
        raise PlanError("pins.json has no runner label for a tier leg")
    return label


def _publish_rows(event: str, ref: str, data: PlanData) -> List[Dict[str, str]]:
    """Return the publish rows of this event: release tier on ``tag``, staging on a master push.

    Raises:
        PlanError: A ``publish_jobs.json`` entry has a malformed job or tier.
    """
    if event == "tag":
        tier: Optional[str] = "release"
    elif event == "push" and ref == MASTER_REF:
        tier = "staging"
    else:
        tier = None
    rows: List[Dict[str, str]] = []
    for entry in data.publish_jobs:
        job, entry_tier = entry.get("job"), entry.get("tier")
        if not (isinstance(job, str) and _JOB_NAME.fullmatch(job)
                and entry_tier in PUBLISH_TIERS):
            raise PlanError("publish_jobs.json: a job entry needs a job name and a known tier")
        if tier is not None and entry_tier == tier:
            rows.append({"job": job, "tier": tier})
    return rows


# ---------------------------------------------------------------------------
# The tier table
# ---------------------------------------------------------------------------

def matrix_for(event: str, ref: str, classification: str, *, pr_same_repo: bool = False,
               data: Optional[PlanData] = None) -> Dict[str, Any]:
    """Return the run plan for one event (the tier table in the module docstring).

    Args:
        event: Neutral ``CI_EVENT`` (``push``, ``pr``, ``schedule``, ``tag``);
            never a platform event name, so a TeamCity adapter can call it too.
        ref: Neutral ``CI_REF`` (``refs/heads/<b>``, ``refs/pull/<n>/merge``,
            ``refs/tags/v<x>``).
        classification: ``docs`` or ``full`` (``classify_changes.classify``).
        pr_same_repo: On a ``pr`` event, whether the head branch is in this
            repository (its push run already covered the push tier). Ignored
            on every other event.
        data: The parsed data files (default: :func:`load_data`).

    Returns:
        ``{"schema", "event", "classification", "lint", "build", "sanitizers",
        "gui", "package", "verify", "publish", "legs", "stages", "runners"}``:
        per-job leg lists in ``legs.json`` full-tier order, ``legs`` maps
        every tier leg to a :data:`LEG_REASONS` value, ``stages`` maps every
        pipeline stage to a :data:`STAGE_REASONS` value, ``runners`` maps every
        scheduled leg to its pinned label. Contains no ref or branch name.

    Raises:
        PlanError: Any argument or data file is outside its allowlist.
    """
    validate_event(event, ref)
    if classification not in classify_changes.CLASSIFICATIONS:
        raise PlanError("classification must be docs or full")
    if not isinstance(pr_same_repo, bool):
        raise PlanError("pr_same_repo must be a boolean")
    if event == "tag":
        classification = classify_changes.FULL  # a release run is never docs-only (spec: tag runs run everything)
    data = data if data is not None else load_data()
    full, push_branch = _tier_lists(data)
    entries = data.legs["legs"]

    legs: Dict[str, str] = {}
    for leg in full:
        if classification == classify_changes.DOCS:
            legs[leg] = NOT_RUN_TIER
        elif event == "push" and ref != MASTER_REF:
            legs[leg] = RUN if leg in push_branch else NOT_RUN_TIER
        elif event == "pr" and pr_same_repo:
            legs[leg] = NOT_RUN_COVERED if leg in push_branch else RUN
        else:
            legs[leg] = RUN

    plan: Dict[str, Any] = {"schema": PLAN_SCHEMA, "event": event,
                            "classification": classification, "lint": ["linux"]}
    for job in LEG_JOBS:
        plan[job] = [leg for leg in full
                     if legs[leg] == RUN and entries[leg].get("job", "build") == job]

    # ADR 0005 extension: package/verify on every full-classified event but a
    # branch push; publish only on a tag (release) or a master push (staging).
    pipeline = classification == classify_changes.FULL and not (
        event == "push" and ref != MASTER_REF)
    publish_tier = event == "tag" or (event == "push" and ref == MASTER_REF)
    wanted = {"package": pipeline, "verify": pipeline, "publish": pipeline and publish_tier}
    packaging = [name for name, entry in entries.items()
                 if isinstance(entry, dict) and entry.get("packaging") is True
                 and not entry.get("pending") and _LEG_NAME.fullmatch(str(name))]
    stages: Dict[str, str] = {}
    for name in PIPELINE_STAGES:
        if not wanted[name]:
            stages[name] = NOT_RUN_TIER
        elif name not in data.defined_stages:
            stages[name] = NOT_RUN_UNDEFINED
        else:
            stages[name] = RUN
    plan["package"] = packaging if stages["package"] == RUN else []
    plan["verify"] = packaging if stages["verify"] == RUN else []
    rows = _publish_rows(event, ref, data)
    plan["publish"] = rows if stages["publish"] == RUN else []
    plan["legs"] = legs
    plan["stages"] = stages
    plan["runners"] = {leg: runner_label(leg, data) for leg in full if legs[leg] == RUN}
    return plan


def parse_plan(text: str, legs_data: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a ``CI_PLAN`` value read back by the ``report`` job (untrusted input, CR-6).

    Returns:
        ``{"legs": {leg: reason}, "stages": {stage: reason}}``, every tier leg
        of ``legs_data`` present with a :data:`LEG_REASONS` value and every
        pipeline stage with a :data:`STAGE_REASONS` value.

    Raises:
        PlanError: The value is too large, not single-line JSON of schema
            :data:`PLAN_SCHEMA`, or names an unknown leg, stage or reason.
    """
    if not _single_line(text) or len(text.encode("utf-8", errors="replace")) > PLAN_MAX_BYTES:
        raise PlanError("CI_PLAN is not a single-line plan of at most 16 KiB")
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise PlanError("CI_PLAN is not JSON") from None
    if not isinstance(obj, dict) or obj.get("schema") != PLAN_SCHEMA:
        raise PlanError("CI_PLAN has another schema")
    tiers = legs_data.get("tiers", {})
    known: List[str] = []
    for members in (tiers.values() if isinstance(tiers, dict) else []):
        for leg in members if isinstance(members, list) else []:
            if isinstance(leg, str) and leg not in known:
                known.append(leg)
    legs, stages = obj.get("legs"), obj.get("stages")
    if not (isinstance(legs, dict) and set(legs) == set(known)
            and all(v in LEG_REASONS for v in legs.values())):
        raise PlanError("CI_PLAN legs do not match legs.json tiers")
    if not (isinstance(stages, dict) and set(stages) == set(PIPELINE_STAGES)
            and all(v in STAGE_REASONS for v in stages.values())):
        raise PlanError("CI_PLAN stages are not the pipeline stages")
    return {"legs": {k: legs[k] for k in known}, "stages": {k: stages[k] for k in PIPELINE_STAGES}}


# ---------------------------------------------------------------------------
# The plan step
# ---------------------------------------------------------------------------

def read_inputs() -> Tuple[str, str, Optional[str], str, bool]:
    """Read and validate the neutral inputs: ``(event, ref, base, head, pr_same_repo)``.

    Raises:
        PlanError: A value is outside its allowlist or contains CR/LF.
    """
    event = env_names.get("CI_EVENT")
    ref = env_names.get("CI_REF")
    validate_event(event, ref)
    base = env_names.get("CI_BASE_COMMIT")
    head = env_names.get("CI_COMMIT") or "HEAD"
    if not _single_line(base) or (base and not classify_changes.valid_rev(base)):
        raise PlanError("CI_BASE_COMMIT must be a hex SHA or a ref name")
    if not _single_line(head) or not classify_changes.valid_rev(head):
        raise PlanError("CI_COMMIT must be a hex SHA or a ref name")
    same = env_names.get("CI_PR_SAME_REPO")
    if not _single_line(same) or same not in _BOOL_VALUES:
        raise PlanError("CI_PR_SAME_REPO must be true, false or empty")
    return event, ref, base or None, head, same == "true"


def plan_summary(plan: Mapping[str, Any]) -> str:
    """Return the ``ok: plan:`` summary: ``<event>, <classification>: build <legs> | ...``."""
    parts = [f"{job} {','.join(plan[job]) or '-'}" for job in LEG_JOBS]
    covered = sum(1 for v in plan["legs"].values() if v == NOT_RUN_COVERED)
    tail = f" ({covered} covered by push run)" if covered else ""
    return f"{plan['event']}, {plan['classification']}: " + " | ".join(parts) + tail


def run(reporter: Reporter, *, data: Optional[PlanData] = None,
        cwd: Optional[Path] = None) -> List[str]:
    """The plan step: classify, plan, set the job outputs; return the lines to print.

    Outputs are set only after every input and the whole plan validated, so a
    rejected input leaves no partial output behind.

    Raises:
        PlanError: Any input or data file is outside its allowlist (the
            caller exits 2; the message never echoes the value).
    """
    start = time.monotonic()
    event, ref, base, head, same_repo = read_inputs()
    data = data if data is not None else load_data()
    try:
        if event == "push" and ref != MASTER_REF:
            # A branch push is diffed against its merge base with origin/master,
            # never the event's `before`: a branch with any code change stays
            # `full` on every push, so a docs-only push after a cancelled code
            # push cannot leave the head commit without its Linux legs.
            base = classify_changes.branch_base(head, cwd=cwd)
        classification, lines = classify_changes.run(base, head, reporter, cwd=cwd)
    except classify_changes.UsageError as exc:
        raise PlanError(str(exc)) from None
    plan = matrix_for(event, ref, classification, pr_same_repo=same_repo, data=data)
    for name in OUTPUTS:
        reporter.set_output(name, plan if name == "plan" else plan[name])
    return lines + reporter.step_ok("plan", plan_summary(plan),
                                    round(time.monotonic() - start, 1))
