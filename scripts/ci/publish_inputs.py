#!/usr/bin/env python3
"""Inputs of the ``publish`` stage, checked before any destination sees them (0012 Story 04.0 Task 02).

Destination-neutral (ADR 0005 D9): every publish destination receives a
:class:`ReleaseInputs` built here, never a path to parse on its own. The rules
are the threat model's standing constraints for implementers
(``docs/security/2026-11-18-release-and-publish-automation.md``):

- **Constraint 4, the manifest is anchored out of band (RP-4).**
  :func:`load_anchored_manifest` reads ``dist/manifest.json`` (no symlink,
  size-capped), compares ``sha256(bytes)`` with ``CI_MANIFEST_SHA256`` (the
  package stage's manifest job output, which travels outside the artifact)
  **before** it parses a single byte as JSON. Then every file the release
  will carry is checked against the manifest (:func:`local_assets`). Nothing
  recomputes a digest into anything published: the ``.sha256`` sidecars must
  equal the text the manifest implies, byte for byte.
- **Constraint 5, one release, one commit (local half; RP-5).** The tag must
  fully match :data:`TAG` (no leading zeros, an optional ``-<pre>`` suffix), its
  base must equal ``manifest.version``, and ``manifest.commit`` must equal
  ``CI_COMMIT``. The remote half (tag dereference, ancestry) is the
  destination's, because it needs the network.
- **Constraint 6, the release tier is cold-built only (CR-2, RP-4).**
  :func:`check_binary_cache` refuses a leg whose ``binary_cache`` is not
  exactly ``none`` (a missing field included) and cross-checks every leg
  against the build job's own ``binary_cache_<leg>`` output
  (``CI_BUILD_BINARY_CACHE``).
- **ADR 0005 D6, publish only after verify in the same run.**
  :func:`check_verify_gate` requires ``CI_VERIFIED_MANIFEST_SHA256`` (the
  verify stage's output, set by its ``verify-gate`` job only when every
  packaging leg verified this manifest; 0012 Story 04.0 Task 04) to equal the
  anchored digest. Empty or different, publish refuses.
- **RP-10, no untrusted text in what is published.** Every manifest field is
  matched against a strict allowlist regex before :func:`release_body` uses
  it; an unknown key, a ``|``, a backtick or a link is refused.

A library: it prints nothing (P11) and never echoes a rejected value; every
refusal is a :class:`PublishInputError` whose message is one RC1 line.
Python 3.9+ standard library only; no network, no child process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import package_archive as pa

#: A release tag (threat model constraint 5, with RP-5's rejections: ``v1.0.0x``,
#: ``v1.0``, ``v1.0.0-`` and ``v01.0.0`` all fail). Group 1 is the base version,
#: group 2 the optional pre-release suffix.
TAG = re.compile(r"^v((?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5}))"
                 r"(?:-([0-9A-Za-z.]{1,40}))?$")
#: ``manifest.version`` of a release: the D5 value, never with a suffix (Task 01 ruling).
RELEASE_VERSION = re.compile(r"^(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RUN_ID = re.compile(r"^[0-9]{1,20}$")
#: Largest manifest the stage reads (a three-leg manifest is about 1.5 KiB).
MANIFEST_MAX_BYTES = 256 * 1024
#: Largest single release asset (an archive is about 1 MiB today).
ASSET_MAX_BYTES = 512 << 20
#: Every key a leg entry may carry: ``LEG_FIELDS`` plus exactly one platform key.
_PLATFORM_KEYS = frozenset(pa.PLATFORM_KEYS.values())
_TOP_KEYS = frozenset({"schema", "version", "commit", "legs"})
_FILES = re.compile(r"^[0-9]{1,9}$")
#: The run marker of a draft this run created (RP-6, RP-12): the first line of its body.
MARKER_PREFIX = "<!-- ci-run-id: "
MARKER_SUFFIX = " -->"
#: Destination tiers (ADR 0005 D9).
TIERS: Tuple[str, ...] = ("release", "staging")


class PublishInputError(Exception):
    """An input the publish stage refuses; the message is one value-free RC1 line."""


@dataclass(frozen=True)
class Asset:
    """One file a release carries, with the size and digest it must have.

    Attributes:
        name: The asset (and file) name.
        path: The verified local file under ``dist/``.
        size: Its size in bytes (the manifest's ``bytes``, or the manifest's own length).
        sha256: Its digest: the manifest's ``sha256`` for an archive, the anchor
            for ``manifest.json``, and for a sidecar the digest of the exact text
            the manifest implies (``<sha256>  <file>\\n``).
    """

    name: str
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class ManifestView:
    """A validated manifest (every field matched its allowlist)."""

    version: str
    commit: str
    legs: Tuple[Mapping[str, object], ...]
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseInputs:
    """Everything a destination may act on, all validated.

    Attributes:
        tag: The release tag (``CI_REF_NAME``), matching :data:`TAG`; ``""``
            for a ``staging``-tier run (a push to ``master``, no tag).
        base: The tag's ``X.Y.Z`` (equals ``manifest.version``).
        prerelease: Whether the tag carries a ``-<pre>`` suffix.
        commit: ``CI_COMMIT`` (equals ``manifest.commit``).
        run_id: ``CI_RUN_ID`` (decimal), the draft marker.
        manifest: The anchored manifest.
        assets: Exactly the files to publish, in upload order.
    """

    tag: str
    base: str
    prerelease: bool
    commit: str
    run_id: str
    manifest: ManifestView
    assets: Tuple[Asset, ...]


# ---------------------------------------------------------------------------
# Tag and commit (constraint 5, local half)
# ---------------------------------------------------------------------------

def parse_tag(ref_name: str) -> Tuple[str, Optional[str]]:
    """Return ``(base, pre)`` of a release tag; ``pre`` is ``None`` for a final release.

    Raises:
        PublishInputError: ``ref_name`` does not fully match :data:`TAG`.
    """
    m = TAG.fullmatch(ref_name) if isinstance(ref_name, str) else None
    if m is None:
        raise PublishInputError("release tag: expected v<X.Y.Z>[-<pre>] (no leading zeros) -> "
                                "actual <rejected>")
    return m.group(1), m.group(2)


def check_commit_binding(tag: str, view: ManifestView, commit: str) -> Tuple[str, bool]:
    """The local half of constraint 5: tag base == ``manifest.version``, ``manifest.commit`` == commit.

    Returns:
        ``(base, prerelease)``.

    Raises:
        PublishInputError: The first discrepancy.
    """
    base, pre = parse_tag(tag)
    if base != view.version:
        raise PublishInputError(f"tag {tag} version: expected {view.version} (manifest) -> "
                                f"actual {base}")
    if not COMMIT.fullmatch(commit or ""):
        raise PublishInputError("CI_COMMIT: expected a 40-hex commit id -> actual <rejected>")
    if view.commit != commit:
        raise PublishInputError(f"manifest.commit: expected {commit[:12]} (CI_COMMIT) -> actual "
                                f"{view.commit[:12]}")
    return base, pre is not None


def check_staging_binding(view: ManifestView, commit: str) -> None:
    """The staging tier's binding (ADR 0005 D9; 0012 Story 04.0 Task 05): ``manifest.commit`` == commit.

    A ``staging`` destination runs on a push to ``master``, so there is no tag
    to bind; the manifest must still name the commit this run built.

    Raises:
        PublishInputError: ``CI_COMMIT`` is malformed or differs from ``manifest.commit``.
    """
    if not COMMIT.fullmatch(commit or ""):
        raise PublishInputError("CI_COMMIT: expected a 40-hex commit id -> actual <rejected>")
    if view.commit != commit:
        raise PublishInputError(f"manifest.commit: expected {commit[:12]} (CI_COMMIT) -> actual "
                                f"{view.commit[:12]}")


# ---------------------------------------------------------------------------
# The anchored manifest (constraint 4)
# ---------------------------------------------------------------------------

def _read_regular(path: Path, max_bytes: int, what: str) -> bytes:
    """Read a regular, non-symlink file of at most ``max_bytes`` without following a link."""
    try:
        st = os.lstat(path)
    except OSError:
        raise PublishInputError(f"{what}: expected a file in dist/ -> actual missing") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PublishInputError(f"{what}: expected a regular file -> actual symlink or special "
                                "file")
    if st.st_size > max_bytes:
        raise PublishInputError(f"{what}: expected at most {max_bytes} bytes -> actual "
                                f"{st.st_size}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        raise PublishInputError(f"{what}: expected a readable file -> actual unreadable") from None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PublishInputError(f"{what}: expected at most {max_bytes} bytes -> actual more")
    return data


def load_anchored_manifest(dist: Path, expected_sha256: str) -> Tuple[bytes, object]:
    """Read ``dist/manifest.json``, check it against the out-of-band digest, then parse it.

    The digest comparison happens on the raw bytes **before** any JSON
    parsing (constraint 4), so a substituted manifest is never interpreted.

    Raises:
        PublishInputError: The anchor is malformed, the file is missing, a
            link or too large, its digest differs, or it is not JSON.
    """
    if not HEX64.fullmatch(expected_sha256 or ""):
        raise PublishInputError("CI_MANIFEST_SHA256: expected 64 lower-case hex (the package "
                                "stage's manifest_sha256 output) -> actual <rejected>")
    if dist.is_symlink():
        raise PublishInputError("dist: expected a directory -> actual symlink")
    raw = _read_regular(dist / pa.MANIFEST_NAME, MANIFEST_MAX_BYTES, pa.MANIFEST_NAME)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise PublishInputError(f"{pa.MANIFEST_NAME} sha256: expected {expected_sha256[:16]} "
                                f"(manifest job output) -> actual {actual[:16]}")
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PublishInputError(f"{pa.MANIFEST_NAME}: expected JSON -> actual unparseable") \
            from None


def validate_manifest(data: object, digest: str, size: int,
                      packaging_legs: Sequence[str]) -> ManifestView:
    """Return a :class:`ManifestView` if every field matches its allowlist (RP-10).

    Raises:
        PublishInputError: Unknown or missing keys, a value outside its
            allowlist, or a leg set that is not exactly ``packaging_legs``.
    """
    if not (isinstance(data, dict) and set(data) == _TOP_KEYS
            and data.get("schema") == pa.MANIFEST_SCHEMA):
        raise PublishInputError(f"{pa.MANIFEST_NAME}: expected schema {pa.MANIFEST_SCHEMA} with "
                                "keys commit, legs, schema, version -> actual other keys")
    version, commit, legs = data["version"], data["commit"], data["legs"]
    if not (isinstance(version, str) and RELEASE_VERSION.fullmatch(version)):
        raise PublishInputError("manifest.version: expected X.Y.Z -> actual <rejected>")
    if not (isinstance(commit, str) and COMMIT.fullmatch(commit)):
        raise PublishInputError("manifest.commit: expected a 40-hex commit id -> actual "
                                "<rejected>")
    if not (isinstance(legs, list) and all(isinstance(e, dict) for e in legs)):
        raise PublishInputError("manifest.legs: expected a list of objects -> actual <rejected>")
    names = [e.get("leg") for e in legs]
    if sorted(str(n) for n in names) != sorted(packaging_legs) or len(set(map(str, names))) \
            != len(names):
        raise PublishInputError(f"manifest.legs: expected {','.join(sorted(packaging_legs))} -> "
                                f"actual {len(legs)} entries of another leg set")
    for entry in legs:
        _validate_leg(entry, version)
    return ManifestView(version, commit, tuple(sorted(legs, key=lambda e: str(e["leg"]))),
                        digest, size)


def _validate_leg(entry: Mapping[str, object], version: str) -> None:
    leg = str(entry.get("leg"))
    if not pa.LEG.fullmatch(leg):
        raise PublishInputError("manifest leg: expected <os>-<arch> -> actual <rejected>")
    platform_key = pa.PLATFORM_KEYS[pa.leg_os(leg)]
    allowed = set(pa.LEG_FIELDS) | {platform_key}
    present = set(entry)
    if present - allowed:
        raise PublishInputError(f"manifest leg {leg}: expected keys {','.join(sorted(allowed))} "
                                f"-> actual {len(present - allowed)} unknown key(s)")
    missing = sorted(allowed - present - {"binary_cache"})
    if missing:  # binary_cache is checked by the tier rule, which names it (constraint 6)
        raise PublishInputError(f"manifest leg {leg}: expected field {missing[0]} -> actual "
                                "missing")
    checks = (
        ("file", lambda v: v == pa.archive_name(version, leg)),
        ("sha256", lambda v: isinstance(v, str) and bool(HEX64.fullmatch(v))),
        ("bytes", lambda v: type(v) is int and 0 < v <= ASSET_MAX_BYTES),
        ("config", lambda v: isinstance(v, str) and bool(pa.CONFIG.fullmatch(v))),
        ("install_sha256", lambda v: isinstance(v, str) and bool(HEX64.fullmatch(v))),
        ("files", lambda v: type(v) is int and 0 < v < 10 ** 9),
        ("arch", lambda v: isinstance(v, str) and bool(pa.ARCH.fullmatch(v))
         and leg.split("-", 1)[1] in v.split("+")),
        (platform_key, lambda v: isinstance(v, str) and bool(re.fullmatch(r"[0-9.]{1,16}", v))),
    )
    for key, ok in checks:
        if not ok(entry.get(key)):
            raise PublishInputError(f"manifest leg {leg} {key}: expected an allowlisted value "
                                    "-> actual <rejected>")
    if "binary_cache" in entry and entry["binary_cache"] not in pa.BINARY_CACHE_VALUES:
        raise PublishInputError(f"manifest leg {leg} binary_cache: expected none or restored -> "
                                "actual <rejected>")


# ---------------------------------------------------------------------------
# Cold-build rule (constraint 6)
# ---------------------------------------------------------------------------

def parse_build_binary_cache(text: str) -> Optional[Dict[str, str]]:
    """Parse ``CI_BUILD_BINARY_CACHE`` (JSON object leg -> ``none``/``restored``); ``None`` if empty.

    Raises:
        PublishInputError: Not a single-line JSON object of allowlisted values.
    """
    if not text:
        return None
    if len(text) > 4096 or "\n" in text or "\r" in text:
        raise PublishInputError("CI_BUILD_BINARY_CACHE: expected single-line JSON -> actual "
                                "<rejected>")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise PublishInputError("CI_BUILD_BINARY_CACHE: expected a JSON object (the build job's "
                                "binary_cache_<leg> outputs) -> actual unparseable") from None
    if not (isinstance(data, dict) and all(isinstance(k, str) and pa.LEG.fullmatch(k)
                                           and v in pa.BINARY_CACHE_VALUES
                                           for k, v in data.items())):
        raise PublishInputError("CI_BUILD_BINARY_CACHE: expected leg -> none|restored -> actual "
                                "<rejected>")
    return data


def check_binary_cache(view: ManifestView, tier: str,
                       build: Optional[Mapping[str, str]]) -> None:
    """Refuse a leg the tier does not accept, and any disagreement with the build job (CR-2).

    ``release``: every leg's ``binary_cache`` must be exactly ``none`` (a
    missing field is refused) **and** ``build`` (required) must say ``none``
    for it too. ``staging``: ``restored`` is accepted (a disposable copy), but
    a field that disagrees with ``build`` is still refused.

    Raises:
        PublishInputError: The first leg that fails, naming its value.
    """
    if tier not in TIERS:
        raise PublishInputError("destination tier: expected release or staging -> actual "
                                "<rejected>")
    if tier == "release" and build is None:
        raise PublishInputError("CI_BUILD_BINARY_CACHE: expected the build job's "
                                "binary_cache_<leg> outputs (release tier) -> actual unset")
    for entry in view.legs:
        leg = str(entry["leg"])
        have = entry.get("binary_cache")
        if have is None:
            raise PublishInputError(f"manifest leg {leg} binary_cache: expected none -> actual "
                                    "missing (cannot prove a cold build, CR-2)")
        if tier == "release" and have != "none":
            raise PublishInputError(f"manifest leg {leg} binary_cache: expected none (release "
                                    f"tier is cold-built only, CR-2) -> actual {have}")
        if build is not None:
            want = build.get(leg)
            if want != have:
                raise PublishInputError(f"manifest leg {leg} binary_cache: expected {want} "
                                        f"(build job output) -> actual {have}")


# ---------------------------------------------------------------------------
# Verify gate (ADR 0005 D6)
# ---------------------------------------------------------------------------

def check_verify_gate(manifest_sha256: str, verified_sha256: str) -> None:
    """Require the same run's verify stage to have passed for exactly this manifest.

    ``CI_VERIFIED_MANIFEST_SHA256`` is set only by the verify stage's
    ``verify-gate`` job (0012 Story 04.0 Task 04), which runs only when every
    verify leg succeeded and itself requires every packaging leg to report
    this digest. A skipped, cancelled or partly failed verify leaves it empty.

    Raises:
        PublishInputError: No verify result, or one for another manifest.
    """
    if not verified_sha256:
        raise PublishInputError(f"verify gate: expected verify passed for manifest "
                                f"{manifest_sha256[:16]} -> actual no verify result (every "
                                "packaging leg must pass the same run's verify stage)")
    if verified_sha256 != manifest_sha256:
        raise PublishInputError(f"verify gate: expected {manifest_sha256[:16]} -> actual "
                                f"{verified_sha256[:16] if HEX64.fullmatch(verified_sha256) else '<rejected>'}"
                                " (verify passed for another manifest)")


# ---------------------------------------------------------------------------
# Local files (constraint 4)
# ---------------------------------------------------------------------------

def sidecar_text(sha256: str, name: str) -> bytes:
    """The exact ``.sha256`` sidecar content the manifest implies (``package_archive`` form)."""
    return f"{sha256}  {name}\n".encode("ascii")


def _verified_file(dist: Path, name: str, size: int, sha256: str) -> Asset:
    path = dist / name
    try:
        if path.resolve(strict=True).parent != dist.resolve(strict=True):
            raise PublishInputError(f"{name}: expected a file directly in dist/ -> actual "
                                    "elsewhere")
    except OSError:
        raise PublishInputError(f"{name}: expected a file in dist/ -> actual missing") from None
    data = _read_regular(path, ASSET_MAX_BYTES, name)
    if len(data) != size:
        raise PublishInputError(f"{name} bytes: expected {size} (manifest) -> actual {len(data)}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise PublishInputError(f"{name} sha256: expected {sha256[:16]} (manifest) -> actual "
                                f"{actual[:16]}")
    return Asset(name, path, size, sha256)


def local_assets(dist: Path, view: ManifestView) -> Tuple[Asset, ...]:
    """Return exactly the files to publish, each checked against the manifest (constraint 4).

    Per leg: the archive (size and digest from the manifest) and its sidecar
    (content must equal :func:`sidecar_text`); then ``manifest.json`` itself
    (its anchored digest). Any other file in ``dist/`` is ignored, never
    published.

    Raises:
        PublishInputError: The first file that is missing, a link, outside
            ``dist/``, or differs from the manifest.
    """
    out: List[Asset] = []
    for entry in view.legs:
        name, sha, size = str(entry["file"]), str(entry["sha256"]), int(entry["bytes"])
        out.append(_verified_file(dist, name, size, sha))
        text = sidecar_text(sha, name)
        out.append(_verified_file(dist, name + ".sha256", len(text),
                                  hashlib.sha256(text).hexdigest()))
    out.append(_verified_file(dist, pa.MANIFEST_NAME, view.size, view.sha256))
    return tuple(out)


# ---------------------------------------------------------------------------
# Release text (RP-10)
# ---------------------------------------------------------------------------

def run_marker(run_id: str) -> str:
    """The first body line of a draft this run created (RP-6, RP-12)."""
    if not RUN_ID.fullmatch(run_id or ""):
        raise PublishInputError("CI_RUN_ID: expected a decimal run id -> actual <rejected>")
    return f"{MARKER_PREFIX}{run_id}{MARKER_SUFFIX}"


def release_body(inputs: ReleaseInputs) -> str:
    """The fixed, script-authored release body (RP-10): marker, tag, commit, digest table.

    Built only from validated fields; generated notes (if any) follow it on
    the platform side and are never echoed into logs or records.
    """
    lines = [run_marker(inputs.run_id), "",
             f"libbios `{inputs.tag}` (version `{inputs.manifest.version}`), built from commit "
             f"`{inputs.commit}`.", "",
             "Verify a download with the digests below or the `.sha256` file next to it.", "",
             "| File | Bytes | SHA-256 |", "|---|---|---|"]
    lines += [f"| `{a.name}` | {a.size} | `{a.sha256}` |" for a in inputs.assets]
    return "\n".join(lines) + "\n"


def build_inputs(*, dist: Path, manifest_sha256: str, ref_name: str, commit: str, run_id: str,
                 packaging_legs: Sequence[str]) -> ReleaseInputs:
    """Load, anchor and validate everything; return the :class:`ReleaseInputs`.

    Raises:
        PublishInputError: The first refusal (anchor, manifest, tag, commit, files).
    """
    raw, data = load_anchored_manifest(dist, manifest_sha256)
    view = validate_manifest(data, manifest_sha256, len(raw), packaging_legs)
    base, prerelease = check_commit_binding(ref_name, view, commit)
    run_marker(run_id)
    return ReleaseInputs(ref_name, base, prerelease, commit, run_id, view,
                         local_assets(dist, view))
