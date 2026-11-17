#!/usr/bin/env python3
"""Platform-neutral stage command (milestone 0014 plan SC1; ADR 0005 D8, "Platform portability").

One workflow step is one invocation of this command::

    python3 scripts/ci/stage.py <lint|build|test|package|verify> [--leg <leg>] [--only <step>]
    python3 scripts/ci/stage.py publish --destination <name> --only <phase> | --dry-run
    python3 scripts/ci/stage.py summary [--leg <leg>] [--job-failed]
    python3 scripts/ci/stage.py report
    python3 scripts/ci/stage.py plan

Every sub-step goes through the ``ci_step`` library (RC1-RC3), so a green
sub-step prints exactly one ``ok:``/``info:``/``skip:`` line (plus the RC4
header). Milestone 0014 Story 03.0 Task 01 implemented ``lint``, ``build`` and
``test``; milestone 0012 Story 03.0 Task 01 adds ``package``; Story 04.0 Task 02
adds ``publish``. ``verify`` (Story 04.0 Task 04) exits 2 until it is defined.

Stages and their steps (``--only`` selects one; without it every step runs in
this order):

``lint`` (Linux only; stops nothing early, every check reports):
    ``env`` (RC4 header), ``self-test`` (``ci_step self-test``), ``lint``
    (``lint.py``: script tests, actionlint, policy gate), ``versions``
    (``check_versions.py``, SC3; ``--tag $CI_REF_NAME`` on a tag event),
    ``registry`` (``check_vcpkg_registry.py``: ``versions/`` match the committed
    ``ports/smbios-c``; 0012 Story 02.0), ``cversions`` (inventory C01),
    ``doc-samples`` (C02).
``build`` (stops at the first failing step):
    ``prepare`` - pinned tools into a job-local venv (``$CI_TEMP_DIR/ci-tools``,
    printed after the header), the RC4 header, ``ci_step self-test``, and the
    vcpkg cache key, exported as the job outputs ``cache`` (``{"key",
    "restore_prefix"}``) and ``cache_enabled`` (``false`` on a tag-event
    packaging leg, CR-2);
    ``compile`` - ``bootstrap_vcpkg.py`` (in-process, so ``VCPKG_ROOT`` and
    ``VCPKG_BINARY_SOURCES`` reach configure), configure (C04), the cache-health
    line (C03), build (C04). A configure/build log above 50 MiB is renamed so
    the workflow's literal-path log upload skips it, with one ``info:`` line
    (plan Q3, threat model CR-10);
    ``install`` - packaging legs only (SC2): ``cmake --install`` into
    ``$CI_ARTIFACT_DIR/install/<leg>/`` and the tree digest exported as the job
    output ``install_digest`` (CR-8); other legs print ``skip:``.
    Packaging legs (``linux-x64``, ``windows-x64``, ``macos-arm64``) build the
    shipped configuration, ``Release`` (0012 status.md 2026-11-16), in their own
    tree ``build/<leg>`` (configured with ``-DCMAKE_BUILD_TYPE=<config>``, built
    and installed with ``--config <config>``), so they never reuse the Debug
    tree of the plain leg of the same preset. That configuration differs by
    design from the ``-C Debug`` the plain Windows leg tests (inventory C06).
``test`` (every step runs; the stage fails if any did):
    ``unit`` (hard; C06 on Windows, C13 on the sanitizer legs; legs with
    ``unit_tests``), ``cli-formats`` (C07, C08, C09; ``cli_format_check.py``;
    not on sanitizer legs), ``live-table`` (best-effort, counts-only; C10 on
    Windows, plan Q4 under ``sudo -n`` on Linux, C14 on ``macos-asan``; legs
    with ``live_table_tests``, else one ``skip:`` line), ``consumer`` (hard;
    0012 Story 01.0: ``consumer_smoke.py`` installs the built tree and builds a
    clean-room consumer of the installed ``libsmbios`` package; the build job's
    legs only). ``unit`` and
    ``live-table`` run the ``test_sets.json`` sets through ``ctest_report.py``
    (Story 03.0 Task 02): the exit code decides, the counts come from
    ``ctest --output-junit``.
``package`` (milestone 0012 Story 03.0 Task 01; ADR 0005 D1/D4; packaging
legs only; it never configures or builds; stops at the first failing step):
    ``env`` (RC4 header); ``unpack``: extracts the build job's one-file
    hand-off ``$CI_ARTIFACT_DIR/install-<leg>.tar`` into ``install/<leg>/`` and
    requires its install-tree digest to equal ``CI_INSTALL_SHA256`` (the build
    job's per-leg digest output, CR-8; required on CI); a missing hand-off is
    one RC1 line naming the path and exit 2, never a fallback to a build tree;
    ``archive`` (``package_archive.py``): refuses a tree the digest refuses
    (symlink, ``Testing``), a tree without the ruled contents or with the
    ``smbios`` CLI/``smbios_gui`` (status.md 2026-11-16 ruling I2); then writes
    ``dist/libbios-<version>-<leg>.<ext>`` (reproducible ``.tar.gz``, ``.zip``
    on Windows), its ``.sha256`` and the leg's entry in ``dist/manifest.json``.
    ``<version>`` is the D5 value read by ``check_versions.check`` (the root
    ``project()`` VERSION); a version-site mismatch fails the step. The
    job outputs ``version`` and ``archive_ext`` name the files for the
    workflow's literal-path upload (P15). ``manifest`` (``--only manifest``,
    the manifest job): merges the per-leg ``parts/dist-<leg>/`` into the one
    ``dist/manifest.json``; it skips in a per-leg run.

    The manifest job also refuses a part whose archive digest, install digest
    or ``binary_cache`` differs from the package and build jobs' own outputs
    (``CI_PACKAGE_ARCHIVES``, ``CI_PACKAGE_LEGS``; required on CI), and
    exports ``manifest_sha256``, the out-of-band anchor of ``dist/manifest.json``
    (threat model 2026-11-18 RP-4). Each leg entry records the build job's
    ``binary_cache`` (``CI_BINARY_CACHE``; ``restored`` on a local run), and
    each package job exports ``archive_sha256_<leg>``. The build job's
    ``compile`` step exports ``binary_cache_<leg>`` on a packaging leg:
    ``none`` only for ``VCPKG_BINARY_SOURCES=clear`` with 0 restored packages.
``publish`` (milestone 0012 Story 04.0 Task 02; ADR 0005 D6/D9; not leg-based;
:func:`run_publish`): ``--destination <name>`` names an entry of
``publish_destinations.json``; ``--only`` is one phase per job: ``preflight``
(settings drift + tag/commit binding; writes nothing), ``draft``
(``release-draft``) or ``promote`` (``release-promote``). ``--dry-run`` lists
every read and write with no network and without reading the credential. A
real run refuses a disabled destination, a local run, an event outside the
tier, an unanchored or invalid manifest (``CI_MANIFEST_SHA256``), a leg that
is not cold-built (``CI_BUILD_BINARY_CACHE``), and a missing verify result
(``CI_VERIFIED_MANIFEST_SHA256``; verify is Task 04, so today it always
refuses). ``CI_PUBLISH_TOKEN`` leaves ``os.environ`` before anything else runs
and no child process is started. Rules: ``publish_inputs.py`` and
``publish_github_release.py``.

Sanitizer legs (Story 03.0 Task 02; legs with ``"job": "sanitizers"``,
inventory C12-C14): ``build`` configures into ``build/<preset>-asan`` with
:data:`SANITIZER_CXX_FLAGS`/:data:`SANITIZER_LINK_FLAGS` and uses the ``asan``
vcpkg cache flavor; ``test`` runs with :func:`sanitizer_env` (``ASAN_OPTIONS``
as the old workflow, fatal ``UBSAN_OPTIONS``, plan Q2/D2).

GUI legs (Story 03.0 Task 03; legs with ``"job": "gui"``, inventory C15-C20)
run their own step lists (:func:`steps_for`):
    ``build``: ``packages`` (C15, ``apt_install.py``; legs with
    ``apt_packages``, else ``skip:``; a local run never calls ``sudo apt``),
    then ``prepare`` and ``compile`` as above (``cmake --preset <os>-gui``,
    ``gui`` vcpkg cache flavor; C16).
    ``test`` (every step runs): ``gui-binary`` (C17), ``gui-hooks`` (R-7, C18),
    ``gui-ci`` (C19, the ``gui_ci`` set through ``ctest_report.py``) and
    ``gui-local`` (R-8, C20); C17, C18 and C20 are ``gui_guards.py``. No step
    executes ``smbios_gui``.
A step named with ``--only`` that is not in the leg's own list prints one
``skip:`` line.

Input validation (threat model CR-6): ``--leg`` (else ``$CI_LEG``), ``--only``,
``$CI_EVENT`` and ``$CI_REF`` are re-validated against this script's own
allowlists (``legs.json``, the stage's step names, the neutral event names);
an unknown value exits 2 with a fixed message that never echoes it. The
platform's own validation is never relied on.

Trigger tiering (plan Q1): a leg not selected for ``$CI_EVENT``/``$CI_REF``
prints one ``skip:`` line and exits 0 (the workflow does not start such legs;
this is the portable self-skip of ADR 0005 "Trigger tiering"). The packaging
legs are in ``tiers.full`` only, never ``tiers.push_branch``, so their build,
test and ``package`` stages follow ADR 0005 D7: every event except a push to a
branch other than ``master``.

Utility commands (Story 04.0 Task 01, plan E9), also one workflow line each:
    ``summary [--job-failed]`` - the last step of every job (``job_summary.py``):
    the job block of ``$CI_JOB``/``$CI_LEG`` (``--leg``) into the job summary
    and ``$CI_ARTIFACT_DIR/summary-<job>-<leg>.json`` for the run report.
    ``--job-failed`` (the workflow's ``failure() || cancelled()`` twin step)
    marks a job that failed outside any recorded step. Always exits 0 except
    for a usage error.
    ``report`` - the ``report`` job (``run_report.py``): the run table from
    the artifacts under ``$CI_TEMP_DIR/summaries``, legs and tier from
    ``legs.json`` and the plan (``$CI_PLAN``; without one, the tier of
    ``$CI_EVENT``/``$CI_REF``). Exits 1 if a leg failed or has no record.
    ``plan`` (Story 04.0 Task 02, plan E7) - the ``plan`` job (``plan.py``):
    classifies the changes (``classify_changes.py``), applies the tier table
    and sets the job outputs ``build``/``sanitizers``/``gui``/``package``/
    ``verify``/``publish``/``plan`` through ``Reporter.set_output()``. Exits
    2 on any input outside its allowlist.

Exit codes (SC1): 0 success, 1 a gate failed, 2 usage or environment error,
128+n a sub-step killed by signal n. Run locally with the defaults of
``env_names`` (``CI_REPORTER=plain``, temporary directories); the leg then
defaults to the host OS. Python 3.9+ standard library only.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argparse  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import (Callable, Dict, Final, Iterator, List, Mapping, Optional,  # noqa: E402
                    Sequence, Tuple)

import ci_step  # noqa: E402
import env_names  # noqa: E402
from reporters import Reporter, get_reporter  # noqa: E402

REPO_ROOT: Final = Path(_HERE).resolve().parents[1]
LEGS_FILE: Final = Path(_HERE) / "legs.json"
PINS_FILE: Final = Path(_HERE) / "pins.json"
REQUIREMENTS_FILE: Final = Path(_HERE) / "requirements-tools.txt"

#: Every stage name of SC1, in pipeline order.
STAGES: Final = ("lint", "build", "test", "package", "verify", "publish")
#: Steps of each implemented stage, in run order (the ``--only`` allowlist).
STEPS: Final[Dict[str, Tuple[str, ...]]] = {
    "lint": ("env", "self-test", "lint", "versions", "registry", "cversions", "doc-samples"),
    "build": ("packages", "prepare", "compile", "install"),
    "test": ("unit", "cli-formats", "live-table", "consumer", "gui-binary", "gui-hooks",
             "gui-ci", "gui-local"),
    "package": ("env", "unpack", "archive", "manifest"),
    # 0012 Story 04.0 Task 02: one phase per publish job (run_publish; not leg-based).
    "publish": ("preflight", "draft", "promote"),
}
#: Run order of each (stage, job); a stage absent here runs all of ``STEPS``.
JOB_STEPS: Final[Dict[Tuple[str, str], Tuple[str, ...]]] = {
    ("build", "build"): ("prepare", "compile", "install"),
    ("build", "sanitizers"): ("prepare", "compile", "install"),
    ("build", "gui"): ("packages", "prepare", "compile"),
    ("test", "build"): ("unit", "cli-formats", "live-table", "consumer"),
    ("test", "sanitizers"): ("unit", "cli-formats", "live-table"),
    ("test", "gui"): ("gui-binary", "gui-hooks", "gui-ci", "gui-local"),
}
#: Neutral ``CI_EVENT`` values (ADR 0005); empty means a local run.
EVENTS: Final = ("push", "pr", "schedule", "manual", "tag")
#: The only branch whose pushes run the full matrix (plan Q1).
MASTER_REF: Final = "refs/heads/master"

#: RC5 reason of the C10/C14 best-effort step (wording kept from the old workflow).
LIVE_TABLE_REASON: Final = "a virtualized runner's firmware may not expose an SMBIOS table"
#: RC5 ``skip:`` reason of the live-table step on the Linux sanitizer leg (plan Q4 ruling).
LIVE_TABLE_ASAN_SKIP: Final = ("Q4: reading the table needs root on Linux; not run under "
                               "sanitizers (build linux runs it under sudo)")
#: Jobs a leg can belong to (the workflow job that runs it).
JOBS: Final = ("build", "sanitizers", "gui")
#: Utility commands that are not SC1 stages (Story 04.0 Task 01).
COMMANDS: Final = ("summary", "report", "plan")
#: Jobs whose ``summary`` step may run (``lint`` plus every leg job).
SUMMARY_JOBS: Final = ("lint",) + JOBS

#: Inventory C12 sanitizer compile flags (the old ``ci.yml``'s), plus
#: ``-fno-sanitize-recover=undefined`` (plan Q2/D2, E11): without it UBSan
#: reports and continues, the test exits 0 and the report is never shown.
SANITIZER_CXX_FLAGS: Final = ("-fsanitize=address,undefined -fno-omit-frame-pointer -g "
                              "-fno-sanitize-recover=undefined")
#: Inventory C12 sanitizer link flags (unchanged from the old ``ci.yml``).
SANITIZER_LINK_FLAGS: Final = "-fsanitize=address,undefined"
#: Fatal UBSan at run time too (plan Q2/D2); the stack trace goes to the log only.
UBSAN_OPTIONS: Final = "halt_on_error=1:print_stacktrace=1"

#: Step labels whose logs the workflow uploads on failure (plan Q3), and the cap.
UPLOADED_LOGS: Final = ("configure", "build")
LOG_UPLOAD_CAP: Final = 50 << 20
#: Job-local venv for the pinned tools, under ``$CI_TEMP_DIR``.
TOOLS_VENV: Final = "ci-tools"
#: Time limits of the network-bound sub-steps (the workflow step budget is the outer bound).
PIP_TIMEOUT_S: Final = 300.0
VENV_TIMEOUT_S: Final = 120.0

_LEG_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_REF = re.compile(r"^refs/[A-Za-z0-9._/+-]{1,200}$")
_REF_NAME = re.compile(r"^[A-Za-z0-9._/+-]{1,200}$")
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9._/+-]{1,120}$")
_HOST_LEGS: Final = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
_HOST_ARCHES: Final = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

EXIT_OK: Final = 0
EXIT_GATE: Final = 1
EXIT_USAGE: Final = env_names.EXIT_ENV_ERROR


class UsageError(Exception):
    """A usage or environment error (exit 2). The message never echoes the bad value."""


class DigestError(Exception):
    """The install tree cannot be digested (symlink, special file, ``Testing/``, empty)."""


class ReportedUsageError(UsageError):
    """An environment error (exit 2) whose RC1 line the step already printed and recorded.

    Raised by a step whose required input is absent (the ``package`` stage's
    ``install/<leg>``), so the stage exits 2 with that one line and no second
    ``stage.py: error:`` line.
    """


# ---------------------------------------------------------------------------
# Legs, tiers and input validation (CR-6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    """One ``legs.json`` entry."""

    name: str
    runner: str
    preset: str
    config: Optional[str]
    live_table_sudo: bool = False
    sysfs_info: bool = False
    packaging: bool = False
    unit_tests: bool = False
    live_table_tests: bool = False
    pending: Optional[str] = None
    job: str = "build"
    apt_packages: bool = False

    @property
    def sanitizers(self) -> bool:
        """Whether this leg is a sanitizer leg (``build/<preset>-asan``, C12-C14)."""
        return self.job == "sanitizers"

    @property
    def gui(self) -> bool:
        """Whether this leg is a GUI leg (``build/<os>-gui``, C15-C20)."""
        return self.job == "gui"


def load_legs(path: Path = LEGS_FILE) -> Mapping[str, object]:
    """Return the parsed ``legs.json``.

    Raises:
        UsageError: The file is unreadable or not the expected shape.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UsageError(f"cannot read {path.name}") from None
    if not (isinstance(data, dict) and isinstance(data.get("legs"), dict)
            and isinstance(data.get("stages"), dict) and isinstance(data.get("tiers"), dict)):
        raise UsageError(f"{path.name}: expected 'stages', 'tiers' and 'legs' objects")
    return data


def _leg(name: str, entry: Mapping[str, object]) -> Leg:
    def flag(key: str) -> bool:
        return entry.get(key) is True

    config = entry.get("config")
    pending = entry.get("pending")
    job = entry.get("job", "build")
    if not (isinstance(entry.get("runner"), str) and isinstance(entry.get("preset"), str)
            and (config is None or isinstance(config, str))):
        raise UsageError("legs.json: a leg needs string runner/preset and a string or null config")
    if job not in JOBS:
        raise UsageError(f"legs.json: a leg's job must be one of: {', '.join(JOBS)}")
    return Leg(name, str(entry["runner"]), str(entry["preset"]), config,
               flag("live_table_sudo"), flag("sysfs_info"), flag("packaging"),
               flag("unit_tests"), flag("live_table_tests"),
               pending if isinstance(pending, str) else None, str(job),
               flag("apt_packages"))


def validate_leg(stage: str, value: Optional[str], legs: Mapping[str, object]) -> Leg:
    """Return the :class:`Leg` named ``value`` if ``stage`` declares it.

    Raises:
        UsageError: ``value`` is malformed, unknown, not a leg of ``stage``, or
            a packaging leg still ``pending`` (milestone 0012).
    """
    if value is None or not _LEG_NAME.fullmatch(value):
        raise UsageError("leg must match [a-z0-9][a-z0-9-]* (see scripts/ci/legs.json)")
    entries = legs.get("legs")
    stage_legs = legs.get("stages", {}).get(stage, [])  # type: ignore[union-attr]
    if not isinstance(entries, dict) or value not in entries or value not in stage_legs:
        raise UsageError(f"unknown leg for stage {stage} (not in scripts/ci/legs.json)")
    leg = _leg(value, entries[value])
    if leg.pending:
        raise UsageError(f"leg {value} is pending: {leg.pending}")
    return leg


def validate_only(stage: str, only: Optional[str]) -> Optional[str]:
    """Return ``only`` if it names a step of ``stage``.

    Raises:
        UsageError: Unknown step (the value is not echoed).
    """
    if only is None:
        return None
    if only not in STEPS[stage]:
        raise UsageError(f"--only must be one of: {', '.join(STEPS[stage])}")
    return only


def validate_event(event: str, ref: str, ref_name: str) -> None:
    """Check the neutral ``CI_EVENT``/``CI_REF``/``CI_REF_NAME`` values (empty is a local run).

    Raises:
        UsageError: A value is outside its allowlist.
    """
    if event and event not in EVENTS:
        raise UsageError(f"CI_EVENT must be one of: {', '.join(EVENTS)}")
    if ref and not _REF.fullmatch(ref):
        raise UsageError("CI_REF is not a well-formed ref")
    if ref_name and not _REF_NAME.fullmatch(ref_name):
        raise UsageError("CI_REF_NAME is not a well-formed ref name")


def tier_legs(event: str, ref: str, legs: Mapping[str, object]) -> Optional[Tuple[str, ...]]:
    """Return the legs the Q1 tier runs for this event, or ``None`` for a local run (all legs).

    A ``push`` to a branch other than ``master`` runs ``tiers.push_branch``;
    every other event runs ``tiers.full`` (plan Q1, ADR 0005 D7).
    """
    if not event:
        return None
    tiers = legs.get("tiers", {})
    name = "push_branch" if (event == "push" and ref.startswith("refs/heads/")
                             and ref != MASTER_REF) else "full"
    selected = tiers.get(name, []) if isinstance(tiers, dict) else []
    return tuple(str(x) for x in selected)


def leg_selected(leg: str, event: str, ref: str, legs: Mapping[str, object]) -> bool:
    """Return whether the Q1 tier runs ``leg`` for this event (always true locally)."""
    selected = tier_legs(event, ref, legs)
    return selected is None or leg in selected


def host_leg() -> str:
    """Return the leg matching this host's OS (local default)."""
    return _HOST_LEGS.get(platform.system(), "linux")


def host_packaging_leg() -> str:
    """Return the packaging leg ``<os>-<arch>`` of this host (local default of ``package``)."""
    arch = _HOST_ARCHES.get(platform.machine().lower(), "x64")
    return f"{host_leg()}-{arch}"



# ---------------------------------------------------------------------------
# Install-tree digest (SC2, CR-8, CR-9)
# ---------------------------------------------------------------------------

def _show(rel: str) -> str:
    return rel if _PATH_TOKEN.match(rel) else "<path>"


def tree_digest(root: Path) -> Tuple[str, int]:
    """Return ``(sha256 hex, file count)`` of the installed tree under ``root``.

    The digest is SHA-256 over the sorted relative POSIX paths, each followed
    by its file's SHA-256 (``<path>\\0<sha256>\\n`` per file). Directories add
    nothing on their own.

    Raises:
        DigestError: ``root`` is missing, a symlink or not a directory; the tree
            holds a symlink, a non-regular file, or a path component named
            ``Testing`` (the build tree's CTest directory must never ship,
            CR-9); or the tree has no file.
    """
    try:
        st = os.lstat(root)
    except OSError:
        raise DigestError("install tree missing") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise DigestError("install tree root is not a directory")
    files: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames) + sorted(filenames):
            path = here / name
            rel = path.relative_to(root).as_posix()
            if name == "Testing":
                raise DigestError(f"install tree contains a Testing path: {_show(rel)}")
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise DigestError(f"install tree contains a symlink: {_show(rel)}")
            if name in filenames:
                if not stat.S_ISREG(mode):
                    raise DigestError(f"install tree contains a non-regular file: {_show(rel)}")
                files.append((rel, path))
    if not files:
        raise DigestError("install tree is empty")
    digest = hashlib.sha256()
    for rel, path in sorted(files):
        file_hash = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                file_hash.update(chunk)
        digest.update(rel.encode("utf-8") + b"\0" + file_hash.hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest(), len(files)


# ---------------------------------------------------------------------------
# Stage context and step helpers
# ---------------------------------------------------------------------------

class _QuietOk:
    """Reporter proxy that suppresses ``ok:`` lines; failures render normally."""

    def __init__(self, inner: Reporter) -> None:
        self._inner = inner
        self.name = inner.name

    def step_ok(self, label: str, summary: str, elapsed_s: float) -> List[str]:  # noqa: D102
        return []

    def __getattr__(self, attr: str):
        return getattr(self._inner, attr)


@dataclass
class Context:
    """Everything a step needs; built once per invocation from the neutral names."""

    stage: str
    leg: Leg
    reporter: Reporter
    workspace: Path
    temp_dir: Path
    log_dir: Path
    results: Path
    artifact_dir: Path
    event: str = ""
    ref: str = ""
    ref_name: str = ""
    platform: str = "local"
    run_id: str = "local"
    py: str = field(default_factory=lambda: sys.executable)
    pins_file: Path = PINS_FILE
    only: Optional[str] = None

    @property
    def local(self) -> bool:
        """Whether this is a developer-machine run (``CI_PLATFORM=local``)."""
        return self.platform == "local"

    @property
    def build_dir(self) -> Path:
        """The leg's build tree.

        ``build/<preset>``; ``build/<preset>-asan`` on sanitizer legs; and
        ``build/<leg>`` on packaging legs, which build the shipped
        configuration and so never share the plain leg's Debug tree.
        """
        if self.leg.packaging:
            return self.workspace / "build" / self.leg.name
        suffix = "-asan" if self.leg.sanitizers else ""
        return self.workspace / "build" / (self.leg.preset + suffix)

    def run(self, label: str, argv: Sequence[str], *, patterns: Sequence[str],
            best_effort: Optional[str] = None, summary: Optional[str] = None,
            hygiene: ci_step.Hygiene = "full", timeout_s: Optional[float] = None,
            reporter: Optional[Reporter] = None) -> int:
        """Run one sub-step through ``ci_step.run_step``; return its exit (0 for best-effort)."""
        result = ci_step.run_step(label, argv, patterns=patterns, best_effort_reason=best_effort,
                                  summary=summary, log_dir=self.log_dir,
                                  reporter=reporter or self.reporter, results_file=self.results,
                                  timeout_s=timeout_s, hygiene=hygiene)
        return 0 if result.mode == "best-effort" else result.exit

    def emit(self, lines: List[str]) -> None:
        """Print reporter-rendered lines (the single emitter, P11)."""
        ci_step.emit(lines)

    def skip(self, label: str, reason: str) -> int:
        """Print one RC5 ``skip:`` line and record it; return 0."""
        self.emit(self.reporter.step_skip(label, reason))
        ci_step.append_record(ci_step.StepResult(label, "hard", "skip", 0, 0.0, reason, None,
                                                 None), self.results)
        return 0

    def fail(self, label: str, first: str, start: float, excerpt: Sequence[str] = ()) -> int:
        """Print one RC1 failure for a stage-level check and record it; return 1."""
        elapsed = round(time.monotonic() - start, 1)
        self.emit(self.reporter.step_fail(label, 1, elapsed, first, list(excerpt)))
        ci_step.append_record(ci_step.StepResult(label, "hard", "fail", 1, elapsed, "", first,
                                                 None), self.results)
        return EXIT_GATE


def _script(ctx: Context, rel: str, *args: str) -> List[str]:
    return [ctx.py, "-I", str(ctx.workspace / rel), *args]


def _venv_bin(temp_dir: Path) -> Path:
    return temp_dir / TOOLS_VENV / ("Scripts" if os.name == "nt" else "bin")


def activate_tools(temp_dir: Path) -> bool:
    """Put the job-local tools venv first on ``PATH`` if a previous step created it.

    Only a real directory is used (never a symlink), so a planted path cannot
    redirect ``cmake``. Returns whether the venv was activated.
    """
    venv = temp_dir / TOOLS_VENV
    bindir = _venv_bin(temp_dir)
    try:
        if venv.is_symlink() or bindir.is_symlink() or not bindir.is_dir():
            return False
    except OSError:
        return False
    path = os.environ.get("PATH", "")
    if not path.startswith(str(bindir) + os.pathsep):
        os.environ["PATH"] = str(bindir) + os.pathsep + path
    return True


# ---------------------------------------------------------------------------
# Common steps
# ---------------------------------------------------------------------------

def step_env(ctx: Context) -> int:
    """RC4 header (``print_env.py``, in-process so the summary goes to this job)."""
    import print_env
    print_env.main(["--job", ctx.stage, "--leg", ctx.leg.name, "--reporter", ctx.reporter.name])
    return 0


def step_self_test(ctx: Context) -> int:
    """``ci_step self-test`` (one ``ok:`` line)."""
    return ci_step.main(["self-test", "--reporter", ctx.reporter.name])


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

def step_lint(ctx: Context) -> int:
    """``lint.py``: script tests, actionlint and the policy gate (three ``ok:`` lines)."""
    import lint
    return lint.main([])


def step_versions(ctx: Context) -> int:
    """SC3 version consistency; on a tag event the tag name is checked too."""
    import check_versions
    tag = ctx.ref_name if ctx.event == "tag" else None
    return check_versions.run(ctx.workspace, tag=tag, reporter=ctx.reporter, results=ctx.results)


def step_registry(ctx: Context) -> int:
    """0012 Story 02.0: the vcpkg registry's ``versions/`` match ``ports/smbios-c`` at HEAD."""
    import check_vcpkg_registry
    return check_vcpkg_registry.run(ctx.workspace, reporter=ctx.reporter, results=ctx.results)


def step_cversions(ctx: Context) -> int:
    """Inventory C01: nothing builds, includes or tracks the GPL ``c-versions/``."""
    return ctx.run("c-versions isolation", _script(ctx, "scripts/check_cversions_isolation.py"),
                   patterns=["python"])


def step_doc_samples(ctx: Context) -> int:
    """Inventory C02: doc output samples equal the golden files."""
    return ctx.run("doc samples", _script(ctx, "scripts/check_doc_samples.py", "--check"),
                   patterns=["python"])


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _runner_label(ctx: Context) -> str:
    try:
        pins = json.loads(ctx.pins_file.read_text(encoding="utf-8"))
        label = pins["runners"][ctx.leg.runner]
    except (OSError, ValueError, KeyError, TypeError):
        raise UsageError("pins.json has no runner label for this leg") from None
    if not isinstance(label, str) or not re.fullmatch(r"[0-9A-Za-z._-]{1,40}", label):
        raise UsageError("pins.json runner label is malformed")
    return label


def cold_build(ctx: Context) -> bool:
    """Return whether this leg builds without any binary cache (tag-event packaging leg, CR-2)."""
    return ctx.event == "tag" and ctx.leg.packaging


def _tools(ctx: Context) -> Tuple[int, Optional[Tuple[str, float]]]:
    """Install the pinned tools into the job-local venv; return ``(exit, deferred ok line)``.

    The ``ok:`` line is printed later, after the RC4 header (which then shows
    the pinned versions). A local run uses the host tools and skips.
    """
    if ctx.local:
        return 0, None
    start = time.monotonic()
    quiet = _QuietOk(ctx.reporter)
    venv = ctx.temp_dir / TOOLS_VENV
    if venv.is_symlink():
        return ctx.fail("pinned tools", "tools venv path is a symlink", start), None
    rc = ctx.run("tools venv", [ctx.py, "-I", "-m", "venv", str(venv)], patterns=["python"],
                 timeout_s=VENV_TIMEOUT_S, reporter=quiet)
    if rc != 0:
        return rc, None
    venv_py = _venv_bin(ctx.temp_dir) / ("python.exe" if os.name == "nt" else "python")
    rc = ctx.run("pinned tools", [str(venv_py), "-I", "-m", "pip", "install", "--quiet",
                                  "--disable-pip-version-check", "--require-hashes", "--no-deps",
                                  "--only-binary", ":all:", "-r", str(REQUIREMENTS_FILE)],
                 patterns=["python"], timeout_s=PIP_TIMEOUT_S, reporter=quiet)
    if rc != 0:
        return rc, None
    activate_tools(ctx.temp_dir)
    return 0, ("cmake, ninja from requirements-tools.txt", round(time.monotonic() - start, 1))


def _set_cache_output(ctx: Context, key: Optional[Tuple[str, str]]) -> None:
    """Export ``cache`` (``{"key", "restore_prefix"}``) and ``cache_enabled`` (a JSON boolean).

    ``cache_enabled`` is separate so the workflow's guards stay plain
    comparisons the policy gate can evaluate (P15).
    """
    ctx.reporter.set_output("cache", {"key": key[0] if key else "",
                                      "restore_prefix": key[1] if key else ""})
    ctx.reporter.set_output("cache_enabled", key is not None)


def step_cache_key(ctx: Context) -> int:
    """Export the vcpkg cache key as the job output ``cache`` (Story 02.0 Task 02 scheme)."""
    import print_env
    import vcpkg_cache_report
    if cold_build(ctx):
        _set_cache_output(ctx, None)
        return ctx.skip("vcpkg cache", "tag-event packaging leg builds cold (CR-2)")
    cxx = vcpkg_cache_report.cxx_tag(*print_env.probe_cxx())
    run_id = ctx.run_id if re.fullmatch(r"[0-9A-Za-z._-]{1,40}", ctx.run_id) else "local"
    try:
        flavor = "asan" if ctx.leg.sanitizers else "gui" if ctx.leg.gui else "default"
        key, prefix = vcpkg_cache_report.cache_key(flavor, _runner_label(ctx), cxx,
                                                   ctx.workspace, run_id)
    except (OSError, ValueError) as exc:
        return ctx.fail("vcpkg cache key", f"cannot compute the key ({type(exc).__name__})",
                        time.monotonic())
    _set_cache_output(ctx, (key, prefix))
    ctx.emit(ctx.reporter.info(f"info: vcpkg cache key: {key}"))
    return 0


def step_packages(ctx: Context) -> int:
    """C15: the GUI leg's apt packages (``apt_install.py``, in-process); ``skip:`` elsewhere."""
    if not ctx.leg.apt_packages:
        return ctx.skip("apt packages", "no system packages on this leg (C15)")
    if ctx.local:
        return ctx.skip("apt packages", "local run: install scripts/ci/apt-packages-gui.txt "
                                        "yourself (no sudo apt from stage.py)")
    import apt_install
    return apt_install.main(["--reporter", ctx.reporter.name])


def step_prepare(ctx: Context) -> int:
    """Pinned tools, RC4 header, self-test, cache key (one workflow step)."""
    rc, tools_ok = _tools(ctx)
    if rc != 0:
        return rc
    step_env(ctx)
    rc = step_self_test(ctx)
    if rc != 0:
        return rc
    if tools_ok is None:
        ctx.skip("pinned tools", "local run uses the host cmake/ninja")
    else:
        summary, elapsed = tools_ok
        ctx.emit(ctx.reporter.step_ok("pinned tools", summary, elapsed))
    return step_cache_key(ctx)


def cap_upload_logs(ctx: Context) -> None:
    """Keep a configure/build log above 50 MiB out of the failure upload (Q3, CR-10).

    The workflow uploads literal paths only; an oversized log is renamed so
    that path no longer exists, and one ``info:`` line says so.
    """
    for label in UPLOADED_LOGS:
        path = ci_step.log_path_for(label, ctx.log_dir)
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            if size > LOG_UPLOAD_CAP:
                path.replace(path.with_name(path.name + ".over-cap"))
                ctx.emit(ctx.reporter.info(
                    f"info: {path.name} is {size >> 20} MiB, above the "
                    f"{LOG_UPLOAD_CAP >> 20} MiB upload cap; not uploaded"))
        except OSError:
            continue


def configure_argv(ctx: Context) -> List[str]:
    """Return the configure argv: the preset, or on a sanitizer leg the C12 configure.

    A sanitizer leg keeps its own binary directory (``-B build/<preset>-asan``)
    so it never clobbers the plain build tree of the same checkout. A
    packaging leg configures ``build/<leg>`` with ``CMAKE_BUILD_TYPE`` set to
    its shipped configuration (single-config generators; a multi-config
    generator ignores it and takes ``--config`` at build time).
    """
    if ctx.leg.packaging:
        return ["cmake", "--preset", ctx.leg.preset, "-B", str(ctx.build_dir),
                f"-DCMAKE_BUILD_TYPE={_shipped_config(ctx)}"]
    if not ctx.leg.sanitizers:
        return ["cmake", "--preset", ctx.leg.preset]
    return ["cmake", "--preset", ctx.leg.preset, "-B", str(ctx.build_dir),
            f"-DCMAKE_CXX_FLAGS={SANITIZER_CXX_FLAGS}",
            f"-DCMAKE_EXE_LINKER_FLAGS={SANITIZER_LINK_FLAGS}"]


def _shipped_config(ctx: Context) -> str:
    """Return a packaging leg's build configuration (``legs.json`` ``config``).

    Raises:
        UsageError: The leg has no configuration (a packaging leg must name one).
    """
    config = ctx.leg.config or ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,31}", config):
        raise UsageError("a packaging leg needs a build configuration in legs.json")
    return config


def build_argv(ctx: Context) -> List[str]:
    """Return the build argv (the preset's, the sanitizer tree's, or a packaging leg's)."""
    if ctx.leg.packaging:
        return ["cmake", "--build", str(ctx.build_dir), "--config", _shipped_config(ctx)]
    if not ctx.leg.sanitizers:
        return ["cmake", "--build", "--preset", ctx.leg.preset]
    return ["cmake", "--build", str(ctx.build_dir)]


def step_compile(ctx: Context) -> int:
    """vcpkg bootstrap, configure, cache health, build (inventory C03, C04, C12)."""
    import bootstrap_vcpkg
    import vcpkg_cache_report
    args = ["--reporter", ctx.reporter.name, "--repo-root", str(ctx.workspace)]
    if ctx.leg.packaging:
        args.append("--packaging-leg")
    rc = bootstrap_vcpkg.main(args)
    if rc != 0:
        return rc
    vcpkg_cache_report.main(["--reporter", ctx.reporter.name, "snapshot"])
    rc = ctx.run("configure", configure_argv(ctx), patterns=["cmake", "vcpkg"])
    if rc != 0:
        cap_upload_logs(ctx)
        return rc
    configure_log = ci_step.log_path_for("configure", ctx.log_dir)
    if ctx.leg.packaging:  # recorded in the 0012 manifest (CR-2, threat model RP-4)
        ctx.reporter.set_output(binary_cache_output_name(ctx.leg.name),
                                binary_cache_state(os.environ.get("VCPKG_BINARY_SOURCES"),
                                                   configure_log))
    vcpkg_cache_report.main(["--reporter", ctx.reporter.name, "report", "--log",
                             str(configure_log)])
    rc = ctx.run("build", build_argv(ctx), patterns=["cxx"])
    if rc != 0:
        cap_upload_logs(ctx)
    return rc


def binary_cache_output_name(leg: str) -> str:
    """The per-leg build-job output carrying the binary-cache state (``binary_cache_linux_x64``)."""
    return "binary_cache_" + leg.replace("-", "_")


def binary_cache_state(binary_sources: Optional[str], configure_log: Path) -> str:
    """Return ``none`` only for a provably cold build, else ``restored`` (threat model RP-4).

    ``none`` needs both: ``VCPKG_BINARY_SOURCES`` was exactly ``clear`` (no
    binary source configured, CR-2), **and** ``vcpkg_cache_report.parse_log``
    read the configure log and counted 0 restored packages. An unreadable or
    unparseable log counts as ``restored`` (fail closed): the value is an
    observation, not only a self-report of the configuration.
    """
    import vcpkg_cache_report
    if binary_sources != "clear":
        return "restored"
    restored, _built = vcpkg_cache_report.parse_log(configure_log)
    return "none" if restored == 0 else "restored"


def handoff_path(ctx: Context) -> Path:
    """The single-file hand-off of ``install/<leg>/``: ``$CI_ARTIFACT_DIR/install-<leg>.tar``.

    Policy P15 uploads literal file paths only, never a directory, so the build
    stage ships its tested tree to the package stage as this one uncompressed
    tar (plumbing, not a product archive).
    """
    return ctx.artifact_dir / f"install-{ctx.leg.name}.tar"


def install_output_name(leg: str) -> str:
    """The per-leg job output carrying the install-tree digest (``install_sha256_linux_x64``)."""
    return "install_sha256_" + leg.replace("-", "_")


def run_install(ctx: Context) -> int:
    """SC2: ``cmake --install`` into ``install/<leg>/``, the tree digest as job outputs, and the
    one-file hand-off ``install-<leg>.tar`` (:func:`handoff_path`)."""
    start = time.monotonic()
    prefix = ctx.artifact_dir / "install" / ctx.leg.name
    if prefix.is_symlink():
        return ctx.fail("install tree", "install prefix is a symlink", start)
    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    argv = ["cmake", "--install", str(ctx.build_dir), "--prefix", str(prefix)]
    if ctx.leg.config:
        argv += ["--config", ctx.leg.config]
    rc = ctx.run("install", argv, patterns=["cmake"], reporter=_QuietOk(ctx.reporter))
    if rc != 0:
        return rc
    try:
        digest, count = tree_digest(prefix)
    except DigestError as exc:
        return ctx.fail("install tree", str(exc), start)
    import package_archive as pa
    handoff = handoff_path(ctx)
    try:
        if handoff.is_symlink():
            raise pa.ArchiveError("hand-off archive path is a symlink")
        if handoff.exists():
            handoff.unlink()
        pa.archive_tree(prefix, handoff, "tar")
    except pa.ArchiveError as exc:
        return ctx.fail("install tree", str(exc), start)
    ctx.reporter.set_output("install_digest", {"leg": ctx.leg.name, "sha256": digest,
                                               "files": count})
    # CR-8: one uniquely named output per leg, so the matrix build job's
    # combined outputs never overwrite each other (each leg sets only its own).
    ctx.reporter.set_output(install_output_name(ctx.leg.name), digest)
    elapsed = round(time.monotonic() - start, 1)
    summary = f"{count} files, sha256 {digest[:16]}, {handoff.name}"
    ctx.emit(ctx.reporter.step_ok("install tree", summary, elapsed))
    ci_step.append_record(ci_step.StepResult("install tree", "hard", "ok", 0, elapsed, summary,
                                             None, None), ctx.results)
    return 0


def step_install(ctx: Context) -> int:
    """Packaging legs only; other legs print ``skip:``."""
    if not ctx.leg.packaging:
        return ctx.skip("install tree", "not a packaging leg")
    return run_install(ctx)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------

def sanitizer_env(leg: Leg) -> Dict[str, str]:
    """Return the sanitizer run-time options of a sanitizer leg (empty for any other leg).

    ``ASAN_OPTIONS`` is kept exactly as the old ``ci.yml`` (inventory C13):

    - ``detect_container_overflow=0``: libc++'s ``vector<string>`` container
      annotations produce a false-positive container-overflow on the ordinary
      ``push_back``-triggered reallocation in
      ``AbstractSMBiosEntry::parse_dmi_strings()`` (verified locally on
      AppleClang/libc++ - not a real out-of-bounds access); a known
      libc++/ASan interaction, not an application bug.
    - ``detect_leaks=0`` (macOS only): AddressSanitizer on macOS does not
      support LeakSanitizer (known platform limitation from milestone 0001).

    ``UBSAN_OPTIONS`` makes UBSan fatal at run time (plan Q2/D2), together
    with ``-fno-sanitize-recover=undefined`` at compile time.
    """
    if not leg.sanitizers:
        return {}
    asan = "detect_container_overflow=0"
    if leg.runner == "macos":
        asan = "detect_leaks=0:" + asan
    return {"ASAN_OPTIONS": asan, "UBSAN_OPTIONS": UBSAN_OPTIONS}


@contextlib.contextmanager
def _child_env(extra: Mapping[str, str]) -> Iterator[None]:
    """Set ``extra`` in ``os.environ`` for the duration of a step (``ci_step`` passes it on)."""
    saved = {k: os.environ.get(k) for k in extra}
    os.environ.update(extra)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_set(ctx: Context, set_name: str, label: str, best_effort: Optional[str],
             sudo: bool = False) -> int:
    """Run one ``test_sets.json`` set through ``ctest_report`` (RC1-RC3 via ``ci_step``)."""
    import ctest_report
    try:
        with _child_env(sanitizer_env(ctx.leg)):
            return ctest_report.run_set(set_name, ctx.build_dir, ctx.leg.config or None, label,
                                        best_effort, sudo=sudo, reporter=ctx.reporter,
                                        log_dir=ctx.log_dir, results=ctx.results,
                                        temp_dir=ctx.temp_dir)
    except ctest_report.UsageError as exc:
        raise UsageError(str(exc)) from None


def step_unit(ctx: Context) -> int:
    """Hard: the ``unprivileged`` set (C06 on Windows, C13 on sanitizer legs)."""
    if not ctx.leg.unit_tests:
        return ctx.skip("tests unprivileged", "run by the sanitizers job on this OS (C13)")
    return _run_set(ctx, "unprivileged", "unprivileged", None)


def step_cli_formats(ctx: Context) -> int:
    """Inventory C07, C08, C09 through ``cli_format_check.py`` (counts only)."""
    if ctx.leg.sanitizers:
        return ctx.skip("cli formats", "run by the build job on this OS (C07)")
    import cli_format_check
    sudo = ctx.leg.live_table_sudo and not ctx.local
    return cli_format_check.run(ctx.build_dir, py=ctx.py, sudo=sudo, sysfs=ctx.leg.sysfs_info,
                                ctx=cli_format_check._Ctx(ctx.reporter, ctx.log_dir, ctx.results))


def step_live_table(ctx: Context) -> int:
    """Best-effort, counts-only: the ``live_table`` set (C10, Q4 sudo on Linux, C14)."""
    if not ctx.leg.live_table_tests:
        reason = (LIVE_TABLE_ASAN_SKIP if ctx.leg.sanitizers
                  else "not run on this leg in the build job (C10, Q4)")
        return ctx.skip("tests live-table", reason)
    sudo = ctx.leg.live_table_sudo and not ctx.local
    return _run_set(ctx, "live_table", "live-table", LIVE_TABLE_REASON, sudo=sudo)


def step_consumer(ctx: Context) -> int:
    """Hard: a clean-room consumer of the installed ``libsmbios`` package (0012 Story 01.0).

    ``consumer_smoke.py`` installs the built tree into a scratch prefix and builds
    ``tests/packaging/libsmbios_consumer`` against that prefix only, proving the
    exported target carries the whole-archive link (ADR 0004 DD1).
    """
    import consumer_smoke
    start = time.monotonic()
    try:
        lines = consumer_smoke.run(ctx.build_dir, ctx.temp_dir / "consumer-smoke",
                                   ctx.leg.config or "")
    except consumer_smoke.SmokeError as exc:
        first, *tail = str(exc).splitlines() or ["failed"]
        return ctx.fail("consumer smoke", first, start, tail)
    elapsed = round(time.monotonic() - start, 1)
    summary = ", ".join(lines)
    ctx.emit(ctx.reporter.step_ok("consumer smoke", summary, elapsed))
    ci_step.append_record(ci_step.StepResult("consumer smoke", "hard", "ok", 0, elapsed, summary,
                                             None, None), ctx.results)
    return 0


def _gui_ctx(ctx: Context):
    import gui_guards
    return gui_guards.Ctx(ctx.reporter, ctx.results)


def step_gui_binary(ctx: Context) -> int:
    """C17: the shipped ``smbios_gui`` exists under ``build/<os>-gui`` (``gui_guards.py``)."""
    import gui_guards
    return gui_guards.check_find_binary(ctx.build_dir, _gui_ctx(ctx))[0]


def step_gui_hooks(ctx: Context) -> int:
    """R-7 (C18): no test-hook string in any shipped ``smbios_gui``; no binary fails closed.

    The paths come from ``gui_guards.find_binaries`` (CR-12); a search error
    fails closed too (review S3-4).
    """
    import gui_guards
    return gui_guards.check_hook_strings_in_tree(ctx.build_dir, _gui_ctx(ctx))


def step_gui_ci(ctx: Context) -> int:
    """C19: the ``gui_ci`` set (``-L gui-ci``, ``--no-tests=error``), counted from JUnit."""
    return _run_set(ctx, "gui_ci", "gui-ci", None)


def step_gui_local(ctx: Context) -> int:
    """R-8 (C20): the local-only GUI tier is absent (``gui_guards.py``, fail closed)."""
    import gui_guards
    return gui_guards.check_local_tier_absent(ctx.build_dir, ctx.leg.config or None,
                                              _gui_ctx(ctx))


# ---------------------------------------------------------------------------
# package (milestone 0012 Story 03.0 Task 01; ADR 0005 D1/D4)
# ---------------------------------------------------------------------------

def _commit(ctx: Context) -> str:
    """Return the commit the manifest records: ``CI_COMMIT`` on CI, ``git rev-parse HEAD`` locally.

    Raises:
        UsageError: On CI, ``CI_COMMIT`` is not a 40-hex commit id.
    """
    value = env_names.get("CI_COMMIT")
    if value:
        if not _COMMIT.fullmatch(value):
            raise UsageError("CI_COMMIT must be a 40-hex commit id")
        return value
    if not ctx.local:
        raise UsageError("CI_COMMIT is required by the package stage")
    import subprocess
    from reporters import child_environment
    try:
        proc = subprocess.run(["git", "-C", str(ctx.workspace), "rev-parse", "HEAD"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, env=child_environment(os.environ),
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    head = proc.stdout.decode("ascii", errors="replace").strip()
    return head if proc.returncode == 0 and _COMMIT.fullmatch(head) else "unknown"


def _fail_input(ctx: Context, label: str, first: str, start: float) -> int:
    """Print and record one RC1 failure with exit 2, then raise :class:`ReportedUsageError`."""
    elapsed = round(time.monotonic() - start, 1)
    ctx.emit(ctx.reporter.step_fail(label, EXIT_USAGE, elapsed, first, []))
    ci_step.append_record(ci_step.StepResult(label, "hard", "fail", EXIT_USAGE, elapsed, "",
                                             first, None), ctx.results)
    raise ReportedUsageError(first)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def step_unpack(ctx: Context) -> int:
    """Rebuild ``install/<leg>/`` from the build stage's hand-off and prove it is the tested tree.

    The input is ``$CI_ARTIFACT_DIR/install-<leg>.tar`` (downloaded by artifact
    ID on CI, CR-8). It is extracted with :func:`package_archive.extract_tar`
    and its install-tree digest must equal ``CI_INSTALL_SHA256``, the build
    job's own digest output for this leg; a mismatch fails the stage, because
    then the bytes about to be packaged are not the bytes the build stage
    tested. On CI the expected digest is required. A local run may skip the
    tar (an existing ``install/<leg>/`` from ``stage.py build --only install``
    is used as is) and checks the digest only if ``CI_INSTALL_SHA256`` is set.
    """
    import package_archive as pa
    start = time.monotonic()
    leg = ctx.leg.name
    label = f"unpack {leg}"
    expected = env_names.get("CI_INSTALL_SHA256")
    if expected and not _HEX64.fullmatch(expected):
        raise UsageError("CI_INSTALL_SHA256 must be 64 lower-case hex characters")
    if not expected and not ctx.local:
        raise UsageError("CI_INSTALL_SHA256 is required on CI (the build job's install digest, "
                         "CR-8)")
    tree = ctx.artifact_dir / "install" / leg
    handoff = handoff_path(ctx)
    if handoff.is_file() and not handoff.is_symlink():
        if tree.is_symlink():
            return ctx.fail(label, "install tree path is a symlink", start)
        if tree.exists():
            shutil.rmtree(tree)
        try:
            pa.extract_tar(handoff, tree)
        except pa.ArchiveError as exc:
            return ctx.fail(label, str(exc), start)
        source = handoff.name
    elif ctx.local and tree.is_dir() and not tree.is_symlink():
        source = "existing install tree (local run)"
    else:
        return _fail_input(ctx, label, f"install hand-off missing: {handoff} ($CI_ARTIFACT_DIR/"
                                       f"install-{leg}.tar, uploaded by the build job; the "
                                       "package stage never builds)", start)
    try:
        digest, count = tree_digest(tree)
    except DigestError as exc:
        return ctx.fail(label, str(exc), start)
    if expected and digest != expected:
        return ctx.fail(label, f"install tree digest: expected {expected[:16]} -> actual "
                               f"{digest[:16]} (not the tree the build stage tested)", start)
    check = "matches the build job's digest" if expected else "digest not checked (local run)"
    summary = f"{source}: {count} files, sha256 {digest[:16]}, {check}"
    elapsed = round(time.monotonic() - start, 1)
    ctx.emit(ctx.reporter.step_ok(label, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(label, "hard", "ok", 0, elapsed, summary, None,
                                             None), ctx.results)
    return 0


#: Where the manifest job downloads the per-leg ``dist-<leg>`` artifacts.
PARTS_DIR: Final = "parts"


def step_manifest(ctx: Context) -> int:
    """Merge the per-leg ``dist-<leg>`` parts into the one ``dist/manifest.json`` (ADR 0005 D4).

    Runs in the package stage's manifest job (``--only manifest``), after the
    per-leg jobs: every packaging leg of ``legs.json`` ``stages.package`` must
    have a part ``$CI_ARTIFACT_DIR/parts/dist-<leg>/`` with its archive, its
    ``.sha256`` and a one-leg ``manifest.json``. Each archive is checked
    against its part's recorded digest (transfer check; the digest itself was
    computed once, by the leg's job, and is copied, never recomputed into the
    manifest), then the archive and its ``.sha256`` are copied into ``dist/``.
    Without ``--only manifest`` and without a parts directory (a per-leg job,
    or a local run whose ``archive`` step already merged its leg) it skips.
    """
    import package_archive as pa
    start = time.monotonic()
    label = "package manifest"
    parts = ctx.artifact_dir / PARTS_DIR
    if not parts.is_dir() or parts.is_symlink():
        if ctx.only != "manifest":
            return ctx.skip(label, "no per-leg parts; the archive step merged this leg")
        return _fail_input(ctx, label, f"manifest parts missing: {parts} ($CI_ARTIFACT_DIR/"
                                       "parts/dist-<leg>/, downloaded from the package jobs)",
                           start)
    legs = load_legs().get("stages", {}).get("package", [])  # type: ignore[union-attr]
    expected = _manifest_expectations(ctx, legs)
    dist = ctx.artifact_dir / "dist"
    if dist.is_symlink():
        return ctx.fail(label, "dist directory is a symlink", start)
    manifest = dist / pa.MANIFEST_NAME
    try:
        if manifest.exists():
            manifest.unlink()
        version = ""
        for leg in legs:
            part = parts / f"dist-{leg}"
            if part.is_symlink() or not part.is_dir():
                raise pa.ArchiveError(f"no part for leg {leg}: dist-{leg}")
            version, commit, entry = pa.leg_entry(part / pa.MANIFEST_NAME)
            if entry["leg"] != leg:
                raise pa.ArchiveError(f"part dist-{leg} holds another leg")
            name = str(entry["file"])
            archive = part / name
            side = part / (name + ".sha256")
            for path in (archive, side):
                if path.is_symlink() or not path.is_file():
                    raise pa.ArchiveError(f"part dist-{leg} lacks {path.name}")
            if pa.sha256_file(archive) != entry["sha256"]:
                raise pa.ArchiveError(f"{name}: digest differs from its part's manifest entry")
            if expected is not None:
                _check_part_against_jobs(leg, entry, expected[leg])
            if side.read_text(encoding="ascii", errors="replace") != \
                    f"{entry['sha256']}  {name}\n":
                raise pa.ArchiveError(f"{side.name} differs from its part's manifest entry")
            dist.mkdir(parents=True, exist_ok=True)
            for path in (archive, side):
                target = dist / path.name
                if target.is_symlink():
                    raise pa.ArchiveError(f"{target.name} in dist/ is a symlink")
                shutil.copyfile(path, target)
            pa.merge_entry(manifest, version, commit, entry)
        manifest_sha = pa.sha256_file(manifest)
    except pa.ArchiveError as exc:
        return ctx.fail(label, str(exc), start)
    ctx.reporter.set_output("version", version)
    # RP-4 / constraint 4: the out-of-band anchor verify and publish check
    # before they parse the manifest (computed once, here, by its writer).
    ctx.reporter.set_output("manifest_sha256", manifest_sha)
    check = ("archives, install digests and binary cache match the build and package jobs"
             if expected is not None else "job outputs not checked (local run)")
    summary = (f"{pa.MANIFEST_NAME}: {len(legs)} legs, version {version}, "
               f"sha256 {manifest_sha[:16]}, {check}")
    elapsed = round(time.monotonic() - start, 1)
    ctx.emit(ctx.reporter.step_ok(label, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(label, "hard", "ok", 0, elapsed, summary, None,
                                             None), ctx.results)
    return 0


def archive_output_name(leg: str) -> str:
    """The per-leg package-job output carrying the archive digest (``archive_sha256_linux_x64``)."""
    return "archive_sha256_" + leg.replace("-", "_")


def _binary_cache_input(ctx: Context) -> str:
    """Return the leg's ``binary_cache`` for its manifest entry (``CI_BINARY_CACHE``; RP-4).

    On CI the value is required: it is the build job's own
    ``binary_cache_<leg>`` output. A local run without it records
    ``restored`` (fail closed: a local archive can never satisfy the release
    tier's cold-build rule, CR-2).

    Raises:
        UsageError: The value is missing on CI or not ``none``/``restored``.
    """
    import package_archive as pa
    value = env_names.get("CI_BINARY_CACHE")
    if not value:
        if not ctx.local:
            raise UsageError("CI_BINARY_CACHE is required on CI (the build job's binary_cache "
                             "output, threat model RP-4)")
        return "restored"
    if value not in pa.BINARY_CACHE_VALUES:
        raise UsageError("CI_BINARY_CACHE must be none or restored")
    return value


def _json_input(name: str, max_bytes: int = 8192) -> object:
    """Parse the single-line JSON value of the neutral variable ``name`` (never echoed).

    Raises:
        UsageError: The value is too long, multi-line or not JSON.
    """
    text = env_names.get(name)
    if len(text) > max_bytes or "\n" in text or "\r" in text:
        raise UsageError(f"{name} is not a single-line JSON value")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise UsageError(f"{name} is not JSON") from None


def _manifest_expectations(ctx: Context, legs: Sequence[str]
                           ) -> Optional[Dict[str, Dict[str, str]]]:
    """Return the per-leg values the build and package jobs exported, or ``None`` locally.

    Threat model RP-4: the manifest job must not trust the parts it downloads
    (any job of the run can replace an artifact, CR-8), so it compares each
    part with job outputs written by the jobs that produced them:
    ``CI_PACKAGE_LEGS`` (the build job's ``install_sha256_<leg>`` and
    ``binary_cache_<leg>``) and ``CI_PACKAGE_ARCHIVES`` (the package jobs'
    ``archive_sha256_<leg>``). Both are required on CI; a local run without
    them skips the check and says so.

    Raises:
        UsageError: A value is missing on CI, malformed, or does not name
            exactly the packaging legs.
    """
    import package_archive as pa
    raw_legs, raw_archives = env_names.get("CI_PACKAGE_LEGS"), env_names.get("CI_PACKAGE_ARCHIVES")
    if not raw_legs and not raw_archives and ctx.local:
        return None
    if not raw_legs or not raw_archives:
        raise UsageError("CI_PACKAGE_LEGS and CI_PACKAGE_ARCHIVES are required on CI (the build "
                         "and package jobs' digest outputs, threat model RP-4)")
    rows, archives = _json_input("CI_PACKAGE_LEGS"), _json_input("CI_PACKAGE_ARCHIVES")
    if not (isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
            and isinstance(archives, dict)):
        raise UsageError("CI_PACKAGE_LEGS must be a list of objects and CI_PACKAGE_ARCHIVES "
                         "an object")
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        leg = row.get("leg")
        if not (isinstance(leg, str) and leg in legs and leg not in out):
            raise UsageError("CI_PACKAGE_LEGS names a leg that is not a packaging leg, or twice")
        values = {"install_sha256": row.get("install_sha256"),
                  "binary_cache": row.get("binary_cache"), "sha256": archives.get(leg)}
        if not (isinstance(values["install_sha256"], str)
                and _HEX64.fullmatch(values["install_sha256"])
                and isinstance(values["sha256"], str) and _HEX64.fullmatch(values["sha256"])
                and values["binary_cache"] in pa.BINARY_CACHE_VALUES):
            raise UsageError("CI_PACKAGE_LEGS/CI_PACKAGE_ARCHIVES hold a malformed digest or "
                             "binary_cache value")
        out[leg] = {k: str(v) for k, v in values.items()}
    if set(out) != set(legs) or set(archives) != set(legs):
        raise UsageError("CI_PACKAGE_LEGS/CI_PACKAGE_ARCHIVES must name every packaging leg once")
    return out


def _check_part_against_jobs(leg: str, entry: Mapping[str, object],
                             expected: Mapping[str, str]) -> None:
    """Refuse a part whose archive, install digest or binary cache differs from the job outputs.

    Raises:
        package_archive.ArchiveError: The first discrepancy, in RC1 form.
    """
    import package_archive as pa
    for key, what in (("sha256", "archive digest (package job output)"),
                      ("install_sha256", "install digest (build job output)"),
                      ("binary_cache", "binary_cache (build job output)")):
        have, want = str(entry.get(key)), expected[key]
        if have != want:
            raise pa.ArchiveError(f"dist-{leg} {what}: expected {want[:16]} -> actual "
                                  f"{have[:16]}")


def step_archive(ctx: Context) -> int:
    """Archive ``install/<leg>/`` into ``dist/`` with its ``.sha256`` and manifest entry.

    Input is only the build stage's tested install tree; an absent tree exits
    2 naming the exact path (never a fallback to ``build/``). One ``ok:`` line
    on success; every refusal is one RC1 failure (exit 1).
    """
    import check_versions
    import package_archive as pa
    start = time.monotonic()
    leg = ctx.leg.name
    label = f"package {leg}"
    src = ctx.artifact_dir / "install" / leg
    if src.is_symlink() or not src.is_dir():
        return _fail_input(ctx, label, f"install tree missing: {src} ($CI_ARTIFACT_DIR/install/"
                                       f"{leg}; produced by stage.py build on this leg - the "
                                       "package stage never builds)", start)
    config = _shipped_config(ctx)
    commit = _commit(ctx)
    binary_cache = _binary_cache_input(ctx)
    try:
        outcome = check_versions.check(ctx.workspace)
    except check_versions.SitesError as exc:
        raise UsageError(str(exc)) from None
    if outcome.problems or outcome.authority is None:
        first = outcome.problems[0] if outcome.problems else "no version"
        return ctx.fail(label, f"version sites disagree ({first}); run stage.py lint "
                               "--only versions", start)
    version = outcome.authority
    try:
        digest, count = tree_digest(src)
    except DigestError as exc:
        return ctx.fail(label, str(exc), start)
    dist = ctx.artifact_dir / "dist"
    if dist.is_symlink():
        return ctx.fail(label, "dist directory is a symlink", start)
    try:
        os_name = pa.leg_os(leg)
        pa.check_contents(src, os_name)
        meta = pa.platform_meta(src, leg)
        archive = pa.archive_tree(src, dist / pa.archive_name(version, leg),
                                  pa.archive_format(leg))
        sha = pa.sha256_file(archive)
        pa.write_sha256_file(archive, sha)
        pa.update_manifest(dist / pa.MANIFEST_NAME, leg, archive,
                           {"version": version, "commit": commit, "config": config,
                            "install_sha256": digest, "files": str(count), "sha256": sha,
                            "binary_cache": binary_cache, **meta})
    except pa.ArchiveError as exc:
        return ctx.fail(label, str(exc), start)
    ctx.reporter.set_output("version", version)
    ctx.reporter.set_output("archive_ext", pa.archive_format(leg))
    ctx.reporter.set_output("package_digest", {"leg": leg, "file": archive.name, "sha256": sha})
    # RP-4: one uniquely named output per leg (the matrix job's combined
    # outputs never overwrite each other), the manifest job's out-of-band check.
    ctx.reporter.set_output(archive_output_name(leg), sha)
    key = pa.PLATFORM_KEYS[os_name]
    summary = (f"{archive.name}, {count} files, {archive.stat().st_size} bytes, "
               f"sha256 {sha[:16]}, {config}, {key} {meta[key]}, {meta['arch']}, "
               f"binary cache {binary_cache}")
    elapsed = round(time.monotonic() - start, 1)
    ctx.emit(ctx.reporter.step_ok(label, summary, elapsed))
    ci_step.append_record(ci_step.StepResult(label, "hard", "ok", 0, elapsed, summary, None,
                                             None), ctx.results)
    return 0


StepFn = Callable[[Context], int]
STEP_FUNCTIONS: Final[Dict[str, Dict[str, StepFn]]] = {
    "lint": {"env": step_env, "self-test": step_self_test, "lint": step_lint,
             "versions": step_versions, "registry": step_registry,
             "cversions": step_cversions,
             "doc-samples": step_doc_samples},
    "build": {"packages": step_packages, "prepare": step_prepare, "compile": step_compile,
              "install": step_install},
    "test": {"unit": step_unit, "cli-formats": step_cli_formats, "live-table": step_live_table,
             "consumer": step_consumer, "gui-binary": step_gui_binary,
             "gui-hooks": step_gui_hooks, "gui-ci": step_gui_ci, "gui-local": step_gui_local},
    "package": {"env": step_env, "unpack": step_unpack, "archive": step_archive,
                "manifest": step_manifest},
}
#: Stages that stop at the first failing step (later steps depend on earlier ones).
STOP_ON_FAILURE: Final = ("build", "package")


def _combine(codes: Sequence[int]) -> int:
    """Map sub-step exits to the SC1 stage exit: 0, 1, or 128+n for a signal."""
    for rc in codes:
        if 128 < rc < 160:
            return rc
    return EXIT_GATE if any(codes) else EXIT_OK


def steps_for(stage_name: str, leg: Leg) -> Tuple[str, ...]:
    """Return the steps ``stage_name`` runs on ``leg``'s job, in order."""
    return JOB_STEPS.get((stage_name, leg.job), STEPS[stage_name])


def run_stage(ctx: Context, only: Optional[str]) -> int:
    """Run ``only`` or every step of ``ctx.stage`` for the leg's job; return the SC1 exit code."""
    own = steps_for(ctx.stage, ctx.leg)
    if only and only not in own:
        return ctx.skip(f"{ctx.stage} {only}", f"not a step of the {ctx.leg.job} job")
    ctx.only = only
    names = [only] if only else list(own)
    codes: List[int] = []
    for name in names:
        rc = STEP_FUNCTIONS[ctx.stage][name](ctx)
        codes.append(rc)
        if rc != 0 and ctx.stage in STOP_ON_FAILURE:
            break
    return _combine(codes)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stage.py", description="Run one pipeline stage (SC1).")
    p.add_argument("stage", help="|".join(STAGES))
    p.add_argument("--leg", default=None, help="leg from scripts/ci/legs.json (default $CI_LEG)")
    p.add_argument("--only", default=None, help="run one step of the stage")
    p.add_argument("--reporter", default=None, help="github|teamcity|plain (default $CI_REPORTER)")
    p.add_argument("--job-failed", action="store_true",
                   help="summary only: the job failed outside any recorded step")
    p.add_argument("--destination", default=None,
                   help="publish only: a name from scripts/ci/publish_destinations.json")
    p.add_argument("--dry-run", action="store_true",
                   help="publish only: list every read and write; no network, no credential")
    return p


def run_summary(leg_value: Optional[str], reporter: Reporter, legs: Mapping[str, object],
                job_failed: bool) -> int:
    """``stage.py summary``: publish the job block and write the summary artifact (E9).

    ``$CI_JOB`` (local default ``build``) must be one of :data:`SUMMARY_JOBS`
    and the leg (``--leg``, else ``$CI_LEG``, local default the host leg) a
    leg of ``legs.json``.

    Raises:
        UsageError: The job or leg is outside its allowlist (never echoed).
    """
    import job_summary
    local = env_names.get("CI_PLATFORM") == "local"
    job = env_names.get("CI_JOB") or ("build" if local else "")
    if job not in SUMMARY_JOBS:
        raise UsageError(f"CI_JOB must be one of: {', '.join(SUMMARY_JOBS)}")
    leg = leg_value or env_names.get("CI_LEG") or (host_leg() if local else "")
    entries = legs.get("legs")
    if not _LEG_NAME.fullmatch(leg) or not isinstance(entries, dict) or leg not in entries:
        raise UsageError("leg must be a leg of scripts/ci/legs.json")
    rc, lines = job_summary.run(
        reporter, job=job, leg=leg,
        results=Path(env_names.get("CI_RESULTS_FILE", required=True)),
        temp_dir=Path(env_names.get("CI_TEMP_DIR", required=True)),
        artifact_dir=Path(env_names.get("CI_ARTIFACT_DIR", required=True)),
        job_failed=job_failed)
    ci_step.emit(lines)
    return rc


def run_report(reporter: Reporter, legs: Mapping[str, object]) -> int:
    """``stage.py report``: the run table from ``$CI_TEMP_DIR/summaries`` (E9).

    The legs and stages come from ``$CI_PLAN`` (the ``plan`` job's output,
    re-validated by ``plan.parse_plan``); without a plan (a local run, or the
    plan job failed) the Q1 tier of ``CI_EVENT``/``CI_REF`` decides, which
    leaves every scheduled leg ``no record`` - never PASS.

    Raises:
        UsageError: ``CI_EVENT``/``CI_REF``/``CI_PLAN`` are outside their allowlists.
    """
    import plan as planning
    import run_report as report
    event, ref = env_names.get("CI_EVENT"), env_names.get("CI_REF")
    validate_event(event, ref, env_names.get("CI_REF_NAME"))
    text = env_names.get("CI_PLAN")
    try:
        parsed = planning.parse_plan(text, legs) if text else None
    except planning.PlanError as exc:
        raise UsageError(str(exc)) from None
    temp_dir = Path(env_names.get("CI_TEMP_DIR", required=True))
    rc, lines = report.run(reporter, summaries=temp_dir / report.SUMMARIES_DIR,
                           legs_data=legs, tier=tier_legs(event, ref, legs), plan=parsed)
    ci_step.emit(lines)
    return rc


def run_plan(reporter: Reporter) -> int:
    """``stage.py plan``: the run plan as job outputs (Story 04.0 Task 02, plan E7).

    Raises:
        UsageError: Any ``CI_*`` input or data file is outside its allowlist.
    """
    import plan as planning
    try:
        lines = planning.run(reporter)
    except planning.PlanError as exc:
        raise UsageError(str(exc)) from None
    ci_step.emit(lines)
    return EXIT_OK


# ---------------------------------------------------------------------------
# publish (milestone 0012 Story 04.0 Task 02; ADR 0005 D6/D9)
# ---------------------------------------------------------------------------

#: The publish destination registry (ADR 0005 D9; policy P12).
DESTINATIONS_FILE: Final = Path(_HERE) / "publish_destinations.json"
#: Destination modules, until 0012 Story 04.0 Task 05 moves them to
#: ``scripts/ci/destinations/<name>.py`` behind the D9 interface.
DESTINATION_MODULES: Final[Dict[str, str]] = {"github-release": "publish_github_release"}
_DESTINATION_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def load_destination(name: Optional[str], path: Optional[Path] = None) -> Dict[str, object]:
    """Return the registry entry of destination ``name`` (``publish_destinations.json``).

    Raises:
        UsageError: The name is malformed, unknown, has no module, or the
            entry is not well formed (never echoes the name).
    """
    if name is None or not _DESTINATION_NAME.fullmatch(name):
        raise UsageError("--destination must name an entry of scripts/ci/"
                         "publish_destinations.json")
    try:
        data = json.loads((path or DESTINATIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UsageError("cannot read publish_destinations.json") from None
    entries = data.get("destinations") if isinstance(data, dict) else None
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None) \
        if isinstance(entries, list) else None
    if entry is None or name not in DESTINATION_MODULES:
        raise UsageError("--destination must name an entry of scripts/ci/"
                         "publish_destinations.json that has a module")
    if not (isinstance(entry.get("enabled"), bool) and entry.get("tier") in ("release", "staging")
            and entry.get("environment") == entry.get("tier")):
        raise UsageError("publish_destinations.json: the entry needs enabled, a tier and an "
                         "environment equal to it")
    return entry


def _publish_record(label: str, ok: bool, start: float, text: str, results: Path) -> None:
    elapsed = round(time.monotonic() - start, 1)
    ci_step.append_record(ci_step.StepResult(label, "hard", "ok" if ok else "fail",
                                             0 if ok else EXIT_GATE, elapsed,
                                             text if ok else "", None if ok else text, None),
                          results)


def _publish_inputs(entry: Mapping[str, object], dry_run: bool, log: Callable[[str], None]):
    """Load, anchor and validate everything the destination will act on (constraints 4-6).

    A dry run accepts a missing anchor, tag, commit, run id or build
    binary-cache map, says so in one ``info:`` line each, and substitutes the
    manifest's own values; a real run refuses every one of them.

    Raises:
        publish_inputs.PublishInputError: The first refusal.
    """
    import publish_inputs as pin
    legs = load_legs().get("stages", {}).get("package", [])  # type: ignore[union-attr]
    dist = Path(env_names.get("CI_ARTIFACT_DIR", required=True)) / "dist"
    anchor = env_names.get("CI_MANIFEST_SHA256")
    if not anchor and dry_run:
        raw = pin._read_regular(dist / "manifest.json", pin.MANIFEST_MAX_BYTES, "manifest.json")
        anchor = hashlib.sha256(raw).hexdigest()
        log("info: dry run: CI_MANIFEST_SHA256 unset, manifest not anchored (a real run "
            "refuses this)")
    raw, data = pin.load_anchored_manifest(dist, anchor)
    view = pin.validate_manifest(data, anchor, len(raw), legs)
    tag, commit, run_id = (env_names.get("CI_REF_NAME"), env_names.get("CI_COMMIT"),
                           env_names.get("CI_RUN_ID"))
    if dry_run and not pin.TAG.fullmatch(tag or ""):
        tag = f"v{view.version}"
        log(f"info: dry run: CI_REF_NAME is not a release tag; showing {tag}")
    if dry_run and not commit:
        commit = view.commit
        log("info: dry run: CI_COMMIT unset; showing manifest.commit")
    if dry_run and not pin.RUN_ID.fullmatch(run_id or ""):
        run_id = "0"
        log("info: dry run: CI_RUN_ID is not a run id; showing 0")
    base, prerelease = pin.check_commit_binding(tag, view, commit)
    pin.run_marker(run_id)
    build = pin.parse_build_binary_cache(env_names.get("CI_BUILD_BINARY_CACHE"))
    tier = str(entry["tier"])
    if build is None and dry_run:
        pin.check_binary_cache(view, "staging", None)
        if tier == "release" and any(e.get("binary_cache") != "none" for e in view.legs):
            pin.check_binary_cache(view, tier, {str(e["leg"]): "none" for e in view.legs})
        log("info: dry run: CI_BUILD_BINARY_CACHE unset, build-job cross-check skipped (a real "
            "run refuses this)")
    else:
        pin.check_binary_cache(view, tier, build)
    return pin.ReleaseInputs(tag, base, prerelease, commit, run_id, view,
                             pin.local_assets(dist, view))


def _publish_gates(entry: Mapping[str, object], name: str) -> None:
    """Real-run gates before any input is trusted or any request made (fail closed).

    Raises:
        publish_inputs.PublishInputError: The destination is disabled, this is
            a local run, or the event is not the tier's.
    """
    import publish_inputs as pin
    if entry.get("enabled") is not True:
        raise pin.PublishInputError(f"{name}: expected enabled in publish_destinations.json -> "
                                    "actual disabled (threat model constraint 12: settings "
                                    "S-1..S-8 read back first)")
    if env_names.get("CI_PLATFORM") == "local":
        raise pin.PublishInputError(f"{name}: expected a CI run -> actual a local run (use "
                                    "--dry-run)")
    event, ref, ref_name = (env_names.get("CI_EVENT"), env_names.get("CI_REF"),
                            env_names.get("CI_REF_NAME"))
    if entry.get("tier") == "release" and not (event == "tag" and ref == "refs/tags/" + ref_name):
        raise pin.PublishInputError(f"{name}: expected a release tag event -> actual "
                                    f"{event or 'none'} (release tier, ADR 0005 D6)")
    if entry.get("tier") == "staging" and not (event == "push" and ref == MASTER_REF):
        raise pin.PublishInputError(f"{name}: expected a push to master -> actual "
                                    f"{event or 'none'} (staging tier, ADR 0005 D9)")


def run_publish(args: argparse.Namespace) -> int:
    """``stage.py publish --destination <name> --only preflight|draft|promote [--dry-run]``.

    The credential (``CI_PUBLISH_TOKEN``) is removed from ``os.environ`` before
    anything else runs (RP-8) and handed to the destination as a redacted
    :class:`publish_github_release.Secret`; a dry run discards it unread. No
    child process is started on this path. Exit 0 success, 1 a refusal or a
    failed request (one RC1 line), 2 a usage error, 70 an internal error
    (type name only, never its message).
    """
    secret = os.environ.pop("CI_PUBLISH_TOKEN", "")
    if args.dry_run:
        secret = ""
    try:
        if args.leg is not None or args.job_failed:
            raise UsageError("--leg and --job-failed are not valid with publish")
        entry = load_destination(args.destination)
        name = str(entry["name"])
        steps = STEPS["publish"]
        if args.only is not None and args.only not in steps:
            raise UsageError(f"--only must be one of: {', '.join(steps)}")
        if args.only is None and not args.dry_run:
            raise UsageError("publish needs --only " + "|".join(steps) + " (one phase per job, "
                             "ADR 0005 D6) or --dry-run")
        try:
            reporter = get_reporter(args.reporter)
        except ValueError:
            raise UsageError("reporter must be one of: github, teamcity, plain") from None
        validate_event(env_names.get("CI_EVENT"), env_names.get("CI_REF"),
                       env_names.get("CI_REF_NAME"))
        results = Path(env_names.get("CI_RESULTS_FILE", required=True))
    except UsageError as exc:
        ci_step.emit_error(f"stage.py: error: {exc}")
        return EXIT_USAGE
    import importlib
    import publish_inputs as pin
    module = importlib.import_module(DESTINATION_MODULES[name])
    label = f"publish {name}" + (f" {args.only}" if args.only else "") + \
        (" (dry run)" if args.dry_run else "")
    start = time.monotonic()

    def log(line: str) -> None:
        ci_step.emit(reporter.info(line))

    try:
        if not args.dry_run:
            _publish_gates(entry, name)
        inputs = _publish_inputs(entry, args.dry_run, log)
        verified = env_names.get("CI_VERIFIED_MANIFEST_SHA256")
        if args.dry_run:
            if args.only != "preflight":
                ci_step.emit([ln for line in module.dry_run_plan(
                    inputs, env_names.get("CI_REPOSITORY") or "<owner>/<repo>", args.only)
                    for ln in reporter.info(line)])
            gate = ("passes" if verified == inputs.manifest.sha256 else
                    "refuses (no verify result)" if not verified else "refuses (other manifest)")
            state = "enabled" if entry.get("enabled") is True else "disabled (real run refuses)"
            summary = (f"{inputs.tag}: {len(inputs.assets)} assets match manifest "
                       f"{inputs.manifest.sha256[:16]}, {state}, verify gate {gate}, "
                       "0 requests, credential unread")
        else:
            pin.check_verify_gate(inputs.manifest.sha256, verified)
            api = module.GitHubApi(module.Secret(secret), env_names.get("CI_REPOSITORY"))
            secret = ""
            if args.only == "preflight":
                module.settings_preflight(api, log)
                module.commit_preflight(api, inputs)
                summary = (f"preflight {inputs.tag}: tag and master ancestry bind "
                           f"{inputs.commit[:12]}, {len(inputs.assets)} assets match the "
                           "manifest")
            else:
                summary = getattr(module, args.only)(api, inputs, log)
    except (pin.PublishInputError, getattr(module, "PublishError", pin.PublishInputError)) as exc:
        text = str(exc).splitlines()[0] if str(exc) else "refused"
        elapsed = round(time.monotonic() - start, 1)
        ci_step.emit(reporter.step_fail(label, EXIT_GATE, elapsed, text, []))
        _publish_record(label, False, start, text, results)
        return EXIT_GATE
    except UsageError as exc:
        ci_step.emit_error(f"stage.py: error: {exc}")
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - RC2: never a traceback, never the message (RP-8)
        ci_step.emit([f"error: stage.py: internal error ({type(exc).__name__})"])
        _publish_record(label, False, start, f"internal error ({type(exc).__name__})", results)
        return 70
    elapsed = round(time.monotonic() - start, 1)
    ci_step.emit(reporter.step_ok(label, summary, elapsed))
    _publish_record(label, True, start, summary, results)
    return EXIT_OK


def build_context(stage: str, leg_value: Optional[str], reporter: Reporter,
                  legs: Mapping[str, object]) -> Context:
    """Validate the neutral inputs and return the :class:`Context` (CR-6).

    Raises:
        UsageError: Any input is outside its allowlist.
    """
    plat = env_names.get("CI_PLATFORM")
    if leg_value is None:
        leg_value = env_names.get("CI_LEG") or None
    if leg_value is None and plat == "local":
        leg_value = ("linux" if stage == "lint" else host_packaging_leg() if stage == "package"
                     else host_leg())
    leg = validate_leg(stage, leg_value, legs)
    event = env_names.get("CI_EVENT")
    ref = env_names.get("CI_REF")
    ref_name = env_names.get("CI_REF_NAME")
    validate_event(event, ref, ref_name)
    workspace = Path(env_names.get("CI_WORKSPACE", required=True)).resolve()
    temp_dir = Path(env_names.get("CI_TEMP_DIR", required=True))
    return Context(stage=stage, leg=leg, reporter=reporter, workspace=workspace,
                   temp_dir=temp_dir,
                   log_dir=Path(env_names.get("CI_LOG_DIR", required=True)),
                   results=Path(env_names.get("CI_RESULTS_FILE", required=True)),
                   artifact_dir=Path(env_names.get("CI_ARTIFACT_DIR", required=True)),
                   event=event, ref=ref, ref_name=ref_name, platform=plat,
                   run_id=env_names.get("CI_RUN_ID") or "local")


def _main_command(args: argparse.Namespace) -> int:
    """Run a utility command (:data:`COMMANDS`); exit 2 on a usage error."""
    try:
        if args.only is not None:
            raise UsageError(f"--only is not valid with {args.stage}")
        if args.job_failed and args.stage != "summary":
            raise UsageError("--job-failed is valid only with summary")
        if args.leg is not None and args.stage != "summary":
            raise UsageError(f"--leg is not valid with {args.stage}")
        try:
            reporter = get_reporter(args.reporter)
        except ValueError:
            raise UsageError("reporter must be one of: github, teamcity, plain") from None
        if args.stage == "plan":
            return run_plan(reporter)
        legs = load_legs()
        if args.stage == "summary":
            return run_summary(args.leg, reporter, legs, args.job_failed)
        return run_report(reporter, legs)
    except UsageError as exc:
        ci_step.emit_error(f"stage.py: error: {exc}")
        return EXIT_USAGE


def main(argv: Sequence[str]) -> int:
    """Validate the inputs, run the stage, return the SC1 exit code (never a traceback)."""
    args = _parser().parse_args(list(argv))
    if args.stage == "publish":
        return run_publish(args)
    if args.destination is not None or args.dry_run:
        ci_step.emit_error("stage.py: error: --destination and --dry-run are valid only with "
                           "publish")
        return EXIT_USAGE
    if args.stage in COMMANDS:
        return _main_command(args)
    try:
        if args.job_failed:
            raise UsageError("--job-failed is valid only with summary")
        if args.stage not in STAGES:
            raise UsageError(f"stage must be one of: {', '.join(STAGES)}")
        if args.stage not in STEPS:
            raise UsageError(f"stage {args.stage} is not defined yet (milestone 0012)")
        only = validate_only(args.stage, args.only)
        try:
            reporter = get_reporter(args.reporter)
        except ValueError:
            raise UsageError("reporter must be one of: github, teamcity, plain") from None
        legs = load_legs()
        ctx = build_context(args.stage, args.leg, reporter, legs)
    except UsageError as exc:
        ci_step.emit_error(f"stage.py: error: {exc}")
        return EXIT_USAGE
    if not leg_selected(ctx.leg.name, ctx.event, ctx.ref, legs):
        if not ctx.local:
            # Review S3-5: on CI the workflow matrix scheduled this leg, so a
            # tier that excludes it is a legs.json/matrix mismatch - a whole
            # job (e.g. every GUI check C17-C20) must not turn into a green skip.
            ci_step.emit_error(f"stage.py: error: leg {ctx.leg.name} is scheduled on CI "
                               f"but not in the {ctx.event} tier "
                               "(legs.json tiers vs workflow matrix)")
            return EXIT_USAGE
        if ctx.stage == "build" and only in (None, "prepare"):
            _set_cache_output(ctx, None)
        return ctx.skip(f"{ctx.stage} {ctx.leg.name}", f"not in the {ctx.event} tier (Q1)")
    try:
        os.chdir(ctx.workspace)
    except OSError:
        ci_step.emit_error("stage.py: error: cannot enter CI_WORKSPACE")
        return EXIT_USAGE
    activate_tools(ctx.temp_dir)
    try:
        return run_stage(ctx, only)
    except ReportedUsageError:
        return EXIT_USAGE
    except UsageError as exc:
        ci_step.emit_error(f"stage.py: error: {exc}")
        return EXIT_USAGE
    except ci_step.UnsafePathError as exc:
        ci_step.emit_error(f"stage.py: error: unsafe path ({exc})")
        return EXIT_USAGE


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - never a traceback (RC2)
        ci_step.emit([f"error: stage.py: internal error ({type(exc).__name__})"])
        sys.exit(70)
