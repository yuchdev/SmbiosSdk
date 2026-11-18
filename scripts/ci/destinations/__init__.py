"""Publish destinations behind one interface (ADR 0005 D9; milestone 0012 Story 04.0 Task 05).

A destination is where verified artifacts go: a GitHub Release
(:mod:`destinations.github_release`), this repository's vcpkg registry
(:mod:`destinations.vcpkg_registry`), an S3 staging area
(:mod:`destinations.s3_staging`). Each is one module here, named after its
registry key with ``-`` replaced by ``_``, and one entry of
``scripts/ci/publish_destinations.json``. ``stage.py publish`` drives a
destination **only** through the members of :data:`INTERFACE`; it holds no
per-destination logic, so adding a destination is a module, a registry entry,
tests and a doc line (``docs/dev/adding-a-publish-destination.md``).

The interface (the contract is milestone 0014 SC5):

``NAME``
    The registry key, e.g. ``github-release``.
``REQUIRED_ENV``
    The neutral names the destination reads. ``CI_PUBLISH_*`` names among them
    are credentials: ``stage.py`` removes every ``CI_PUBLISH_*`` variable from
    its own environment before anything else runs and hands the destination
    only those, wrapped in :class:`Secret` (threat model 2026-11-18 RP-8).
``validate(manifest, env)``
    Checks the destination's own inputs; no network, prints nothing secret.
    Runs in ``--dry-run`` and before any write.
``publish(manifest, artifact_dir, env, dry_run)``
    Uploads exactly the files of the anchored manifest and returns a
    :class:`Result` with the remote locations. With ``dry_run`` it makes no
    request and reads no credential, and lists the exact files and keys.
``check_published(manifest, env)``
    Re-reads the remote copy and compares it with the manifest (the
    destination's post-publish check).

``manifest`` is always a :class:`publish_inputs.ReleaseInputs`: the manifest
anchored against the package stage's ``manifest_sha256`` and every local file
already checked against it (threat model constraint 4). A destination never
builds, re-archives or recomputes a digest into anything it publishes.

Optional members, for destinations whose work spans several jobs (D6):
``PHASES`` (one per job, selected with ``stage.py publish --only <phase>``)
and ``UNPRIVILEGED_PHASES`` (phases that run toolchain or anonymous code and
so must never share a job with a credential, RP-2; ``stage.py`` refuses them
when any credential is set). A destination without ``PHASES`` has a single
write phase and runs in its permission class's matrix job
(``publish-<tier>-<class>`` in ``stage-publish.yml``).

Python 3.9+ standard library only. A library: it prints nothing (P11).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Optional, Tuple

#: The members every destination module defines (ADR 0005 D9, 0014 SC5).
INTERFACE: Tuple[str, ...] = ("NAME", "REQUIRED_ENV", "validate", "publish", "check_published")
#: Permission classes (ADR 0005 D9); policy P12 maps each to its workflow permissions.
PERMISSION_CLASSES: Tuple[str, ...] = ("repo-write", "oidc", "token-only")
#: Destination tiers (ADR 0005 D9).
TIERS: Tuple[str, ...] = ("release", "staging")
#: Prefix of every credential name a destination may read (policy P8/P12).
CREDENTIAL_PREFIX = "CI_PUBLISH_"
#: A registry key: lower-case, digits and ``-``.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
#: A phase name (``--only``).
PHASE_RE = re.compile(r"^[a-z][a-z-]{0,19}$")
#: Where the destination modules live.
PACKAGE_DIR = Path(__file__).resolve().parent


class DestinationError(Exception):
    """A destination module that does not satisfy :data:`INTERFACE`; one value-free line."""


class Secret:
    """A credential that never renders: ``repr``/``str`` are ``<redacted>`` (RP-8)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def reveal(self) -> str:
        """Return the value (only for the one header or file that needs it)."""
        return self._value


Log = Callable[[str], None]


@dataclass(frozen=True)
class Env:
    """What ``stage.py`` hands a destination besides the manifest.

    Attributes:
        values: Non-secret neutral names (``CI_REPOSITORY``, ``CI_RUN_ID``, ...),
            already read through ``env_names``.
        credentials: The ``CI_PUBLISH_*`` names of the destination's
            ``REQUIRED_ENV``, each a :class:`Secret`. Empty in a dry run and in
            an unprivileged phase.
        phase: The ``--only`` phase, or ``None`` (a single-phase destination,
            or a dry run of every phase).
        dry_run: ``stage.py publish --dry-run``: no request, no child process,
            ``credentials`` and ``oidc`` empty.
        log: Renders one ``info:`` line (the destination prints nothing itself).
        workspace: The checkout root (read-only use: a dry run's local preview).
        temp_dir: ``CI_TEMP_DIR`` (private scratch files).
        log_dir: ``CI_LOG_DIR`` (step logs of a child process, if any).
        results: ``CI_RESULTS_FILE`` (RC3 records of a child process, if any).
        reporter: The reporter backend (only for a child process's step runner).
        oidc: For the ``oidc`` class: ``(request_url, request_token)`` of the
            platform's OIDC token service, the token a :class:`Secret`; ``None``
            elsewhere, in a dry run, or when the platform provides none.
    """

    values: Mapping[str, str]
    credentials: Mapping[str, Secret] = field(default_factory=dict)
    phase: Optional[str] = None
    dry_run: bool = False
    log: Log = lambda _line: None  # noqa: E731 - a no-op default
    workspace: Path = Path(".")
    temp_dir: Path = Path(".")
    log_dir: Path = Path(".")
    results: Path = Path(".")
    reporter: object = None
    oidc: Optional[Tuple[str, Secret]] = None

    def __repr__(self) -> str:
        return (f"Env(phase={self.phase}, dry_run={self.dry_run}, "
                f"credentials={sorted(self.credentials)})")

    def get(self, name: str) -> str:
        """Return a non-secret value, ``""`` if unset."""
        return str(self.values.get(name, ""))

    def secret(self, name: str) -> Secret:
        """Return a credential, an empty :class:`Secret` if unset."""
        return self.credentials.get(name) or Secret("")


@dataclass(frozen=True)
class Result:
    """The record ``publish`` returns (RC3 summary text plus the remote locations).

    Attributes:
        summary: One value-free line for the ``ok:`` line (counts and the base
            location only; never a credential, role, policy or presigned URL).
        locations: The remote locations written (or, in a dry run, that would be).
        lines: ``info:`` lines a dry run lists (exact files and keys).
        requests: Network requests made (0 in a dry run).
    """

    summary: str
    locations: Tuple[str, ...] = ()
    lines: Tuple[str, ...] = ()
    requests: int = 0


def module_name(name: str) -> str:
    """The module of registry key ``name``: ``s3-staging`` -> ``s3_staging``."""
    return name.replace("-", "_")


def load(name: str, package: str = __name__) -> ModuleType:
    """Import the destination module of ``name`` and check it against :data:`INTERFACE`.

    Args:
        name: A registry key.
        package: The package to import from (tests pass a fake package).

    Raises:
        DestinationError: The name is malformed, the module is missing, a
            member is missing or of the wrong kind, ``NAME`` differs from
            ``name``, or ``PHASES``/``UNPRIVILEGED_PHASES`` are malformed.
    """
    if not NAME_RE.fullmatch(name or ""):
        raise DestinationError("destination: expected a registry key [a-z0-9-] -> actual "
                               "<rejected>")
    try:
        module = importlib.import_module(f"{package}.{module_name(name)}")
    except ImportError:
        raise DestinationError(f"destination {name}: expected module destinations/"
                               f"{module_name(name)}.py -> actual missing") from None
    check_interface(module, name)
    return module


def check_interface(module: ModuleType, name: str) -> None:
    """Raise :class:`DestinationError` unless ``module`` implements D9 for ``name``."""
    missing = [m for m in INTERFACE if not hasattr(module, m)]
    if missing:
        raise DestinationError(f"destination {name}: expected member {missing[0]} -> actual "
                               "missing")
    if getattr(module, "NAME") != name:
        raise DestinationError(f"destination {name}: expected NAME {name} -> actual another "
                               "name")
    required = getattr(module, "REQUIRED_ENV")
    if not (isinstance(required, tuple)
            and all(isinstance(n, str) and re.fullmatch(r"CI_[A-Z0-9_]+", n) for n in required)):
        raise DestinationError(f"destination {name}: expected REQUIRED_ENV a tuple of CI_* "
                               "names -> actual other")
    for member in ("validate", "publish", "check_published"):
        if not callable(getattr(module, member)):
            raise DestinationError(f"destination {name}: expected {member} callable -> actual "
                                   "not callable")
    phases = getattr(module, "PHASES", ())
    unprivileged = getattr(module, "UNPRIVILEGED_PHASES", ())
    if not (isinstance(phases, tuple) and all(isinstance(p, str) and PHASE_RE.fullmatch(p)
                                               for p in phases)
            and isinstance(unprivileged, tuple) and set(unprivileged) <= set(phases)
            and "preflight" not in phases):
        raise DestinationError(f"destination {name}: expected PHASES and UNPRIVILEGED_PHASES "
                               "as tuples of phase names -> actual other")


def credential_names(module: ModuleType) -> Tuple[str, ...]:
    """The ``CI_PUBLISH_*`` names of the module's ``REQUIRED_ENV``."""
    return tuple(n for n in module.REQUIRED_ENV if n.startswith(CREDENTIAL_PREFIX))


def phases(module: ModuleType) -> Tuple[str, ...]:
    """The ``--only`` values of ``module``: ``preflight`` plus its ``PHASES``, or ``()``."""
    own = tuple(getattr(module, "PHASES", ()))
    return ("preflight",) + own if own else ()


def unprivileged_phases(module: ModuleType) -> Tuple[str, ...]:
    """Phases of ``module`` that must never see a credential (RP-2, constraint 11)."""
    return tuple(getattr(module, "UNPRIVILEGED_PHASES", ()))
