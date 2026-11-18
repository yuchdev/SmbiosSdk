#!/usr/bin/env python3
"""The rules of one registry update, shared by both registry jobs (0012 Story 04.0 Task 03).

A release registers exactly one new ``(version, port-version)`` of the
``smbios-c`` port in this repository's vcpkg registry
(``docs/dev/vcpkg-registry.md``). Threat model
``docs/security/2026-11-18-release-and-publish-automation.md`` RP-2 splits the
work into two jobs, and both apply the rules in this module:

- ``registry-prepare`` (no credential) edits the port ``REF``, runs ``vcpkg
  x-add-version`` and the consumer test, and calls :func:`check_registry_change`
  on its own result before it hands the three files over;
- ``registry-pr`` (``contents: write``, REST only) calls the same function on the
  downloaded files against the base commit's files **read through the API**, so
  a compromised prepare job can hand over nothing but the one change these rules
  allow: the ``REF`` line set to ``manifest.commit`` (constraint 5), one new
  entry prepended to ``versions/s-/smbios-c.json`` whose ``git-tree`` is the tree
  this module computes (:func:`port_tree_sha`), and the baseline moved to it.
  Every other byte must equal the base. Nothing already registered may change
  (constraint 9, append-only).

Pure functions: no network, no child process, no file I/O, nothing printed
(P11). Git object ids are computed here in Python (``blob``/``tree`` headers, SHA-1)
so the privileged job can check a tree without running ``git``. Every refusal
is a :class:`RegistryError` whose message is one value-free RC1 line.
Python 3.9+ standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PORT = "smbios-c"
PORT_DIR = f"ports/{PORT}"
PORTFILE = f"{PORT_DIR}/portfile.cmake"
PORT_MANIFEST = f"{PORT_DIR}/vcpkg.json"
BASELINE = "versions/baseline.json"
VERSIONS = f"versions/s-/{PORT}.json"
#: The only files a registry update changes, in the order they are written (RP-2).
CHANGED_FILES: Tuple[str, ...] = (PORTFILE, BASELINE, VERSIONS)
#: The branch the update arrives on (RP-9, constraint 10): a human opens the PR.
BRANCH_PREFIX = "registry/v"
#: Largest registry file either job reads (the version file is ~150 bytes per release).
MAX_FILE_BYTES = 256 * 1024
#: The git mode of a regular file; the port directory holds only regular files.
FILE_MODE = "100644"

COMMIT = re.compile(r"^[0-9a-f]{40}$")
#: A release version as the port records it (``manifest.version``, never a suffix).
VERSION = re.compile(r"^(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$")
RUN_ID = re.compile(r"^[0-9]{1,20}$")
#: The portfile's ``REF`` line (one per portfile).
_REF_LINE = re.compile(rb"^([ \t]*REF[ \t]+)([0-9A-Za-z]+)([ \t]*\r?\n?)$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RegistryError(Exception):
    """A registry update the rules refuse; the message is one value-free RC1 line."""


# ---------------------------------------------------------------------------
# Git object ids (computed, never asked of git)
# ---------------------------------------------------------------------------

def git_blob_sha(data: bytes) -> str:
    """The git object id of a blob holding ``data`` (what ``git hash-object`` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_tree_sha(entries: Iterable[Tuple[str, str, str]]) -> str:
    """The git object id of a tree of ``(mode, name, sha)`` entries (what ``git mktree`` prints).

    Entries are sorted the way git sorts them: by name, with a subtree compared
    as ``name/``.

    Raises:
        RegistryError: A name, mode or object id is malformed, or a name repeats.
    """
    items = list(entries)
    names = [name for _mode, name, _sha in items]
    if len(set(names)) != len(names):
        raise RegistryError("tree entries: expected unique names -> actual a repeated name")
    body = b""

    def order(item: Tuple[str, str, str]) -> bytes:
        mode, name, _sha = item
        return name.encode("utf-8") + (b"/" if mode == "40000" else b"")

    for mode, name, sha in sorted(items, key=order):
        if mode not in ("100644", "100755", "40000", "120000") or not _NAME.fullmatch(name) \
                or not COMMIT.fullmatch(sha):
            raise RegistryError("tree entry: expected mode, name and object id -> actual "
                                "<rejected>")
        body += mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha)
    return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()


def port_tree_sha(base_entries: Sequence[Tuple[str, str, str]], portfile: bytes) -> str:
    """The ``git-tree`` of ``ports/smbios-c`` once its portfile is ``portfile``.

    ``base_entries`` are the base commit's ``ports/smbios-c`` entries
    ``(mode, name, sha)``; every entry must be a regular file, and only
    ``portfile.cmake``'s blob is replaced. This is the value ``vcpkg
    x-add-version`` records, computed without git.

    Raises:
        RegistryError: The base port directory is not two-or-more regular files
            including ``portfile.cmake`` and ``vcpkg.json``.
    """
    names = {name for _m, name, _s in base_entries}
    if not {"portfile.cmake", "vcpkg.json"} <= names:
        raise RegistryError(f"{PORT_DIR} at the base: expected portfile.cmake and vcpkg.json "
                            "-> actual another layout")
    if any(mode != FILE_MODE for mode, _n, _s in base_entries):
        raise RegistryError(f"{PORT_DIR} at the base: expected regular files only -> actual a "
                            "subtree, link or executable")
    new = git_blob_sha(portfile)
    return git_tree_sha([(m, n, new if n == "portfile.cmake" else s) for m, n, s in base_entries])


# ---------------------------------------------------------------------------
# The portfile REF (constraint 5: always manifest.commit)
# ---------------------------------------------------------------------------

def _ref_lines(text: bytes) -> List[int]:
    return [i for i, line in enumerate(text.splitlines(keepends=True)) if _REF_LINE.match(line)]


def portfile_ref(text: bytes) -> str:
    """The 40-hex ``REF`` of a portfile.

    Raises:
        RegistryError: Not exactly one ``REF`` line, or its value is not 40 hex.
    """
    lines = text.splitlines(keepends=True)
    hits = _ref_lines(text)
    if len(hits) != 1:
        raise RegistryError(f"{PORTFILE}: expected exactly one REF line -> actual {len(hits)}")
    value = _REF_LINE.match(lines[hits[0]]).group(2).decode("ascii")  # type: ignore[union-attr]
    if not COMMIT.fullmatch(value):
        raise RegistryError(f"{PORTFILE} REF: expected a 40-hex commit id -> actual <rejected>")
    return value


def set_portfile_ref(text: bytes, commit: str) -> bytes:
    """Return ``text`` with its one ``REF`` value replaced by ``commit``; every other byte kept.

    Raises:
        RegistryError: ``commit`` is not 40 hex, or the portfile has no single REF line.
    """
    if not COMMIT.fullmatch(commit or ""):
        raise RegistryError("REF: expected manifest.commit (40 hex) -> actual <rejected>")
    portfile_ref(text)
    lines = text.splitlines(keepends=True)
    (index,) = _ref_lines(text)
    m = _REF_LINE.match(lines[index])
    lines[index] = m.group(1) + commit.encode("ascii") + m.group(3)  # type: ignore[union-attr]
    return b"".join(lines)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def version_key(version: str, port_version: int) -> Tuple[int, int, int, int]:
    """Order of ``(version, port-version)`` pairs (numeric ``X.Y.Z``, then ``port-version``).

    Raises:
        RegistryError: ``version`` is not ``X.Y.Z`` or ``port_version`` is not >= 0.
    """
    if not (isinstance(version, str) and VERSION.fullmatch(version)) or \
            type(port_version) is not int or port_version < 0:
        raise RegistryError("version: expected X.Y.Z and a port-version >= 0 -> actual "
                            "<rejected>")
    x, y, z = (int(p) for p in version.split("."))
    return x, y, z, port_version


def parse_json(raw: bytes, what: str) -> Dict[str, Any]:
    """Parse a registry JSON file (a JSON object of at most :data:`MAX_FILE_BYTES`).

    Raises:
        RegistryError: Too large, not UTF-8 JSON, or not an object.
    """
    if len(raw) > MAX_FILE_BYTES:
        raise RegistryError(f"{what}: expected at most {MAX_FILE_BYTES} bytes -> actual more")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RegistryError(f"{what}: expected JSON -> actual unparseable") from None
    if not isinstance(value, dict):
        raise RegistryError(f"{what}: expected a JSON object -> actual another value")
    return value


def port_version_of(manifest_raw: bytes) -> Tuple[str, int]:
    """``(version, port-version)`` of ``ports/smbios-c/vcpkg.json``.

    Raises:
        RegistryError: Not the ``smbios-c`` port, or a malformed version.
    """
    data = parse_json(manifest_raw, PORT_MANIFEST)
    version, port_version = data.get("version"), data.get("port-version", 0)
    if data.get("name") != PORT:
        raise RegistryError(f"{PORT_MANIFEST}: expected name {PORT} -> actual another name")
    version_key(version, port_version)
    return version, port_version


def registered_rows(raw: bytes, what: str = VERSIONS) -> List[Dict[str, Any]]:
    """The rows of ``versions/s-/smbios-c.json``, each ``{git-tree, version, port-version}``.

    Raises:
        RegistryError: Not the vcpkg version-file shape.
    """
    data = parse_json(raw, what)
    rows = data.get("versions")
    if set(data) != {"versions"} or not isinstance(rows, list):
        raise RegistryError(f"{what}: expected one 'versions' list -> actual another shape")
    for row in rows:
        if not (isinstance(row, dict) and set(row) <= {"git-tree", "version", "port-version"}
                and isinstance(row.get("git-tree"), str) and COMMIT.fullmatch(row["git-tree"])):
            raise RegistryError(f"{what}: expected rows of git-tree, version, port-version -> "
                                "actual another row")
        version_key(row.get("version"), row.get("port-version", 0))
    return rows


# ---------------------------------------------------------------------------
# The one allowed change
# ---------------------------------------------------------------------------

def check_registry_change(*, base: Mapping[str, bytes], new: Mapping[str, bytes],
                          version: str, commit: str, git_tree: str) -> Tuple[int, int]:
    """Refuse anything but registering ``version`` at ``REF`` ``commit`` with ``git_tree``.

    Args:
        base: The base commit's bytes of :data:`CHANGED_FILES` plus
            :data:`PORT_MANIFEST`.
        new: The proposed bytes of exactly :data:`CHANGED_FILES`.
        version: ``manifest.version`` (the base port must already carry it:
            ``prepare_release.py`` set it in the release commit).
        commit: ``manifest.commit``, the new ``REF`` (constraint 5).
        git_tree: The ``git-tree`` the caller computed for the new port tree.

    Returns:
        ``(port_version, rows)``: the new entry's ``port-version`` and the
        number of registered rows after the change.

    Raises:
        RegistryError: The first discrepancy, one value-free line.
    """
    if set(new) != set(CHANGED_FILES):
        raise RegistryError(f"registry files: expected exactly {', '.join(CHANGED_FILES)} -> "
                            f"actual {len(set(new) - set(CHANGED_FILES))} extra, "
                            f"{len(set(CHANGED_FILES) - set(new))} missing")
    missing = [p for p in (*CHANGED_FILES, PORT_MANIFEST) if p not in base]
    if missing:
        raise RegistryError(f"{missing[0]} at the base: expected present -> actual missing")
    if not COMMIT.fullmatch(git_tree or ""):
        raise RegistryError("git-tree: expected 40 hex -> actual <rejected>")
    port_version_at_base, port_version = port_version_of(base[PORT_MANIFEST])
    if port_version_at_base != version:
        raise RegistryError(f"{PORT_MANIFEST} at the base: expected version {version} "
                            f"(manifest.version) -> actual {port_version_at_base}")

    if new[PORTFILE] != set_portfile_ref(base[PORTFILE], commit):
        raise RegistryError(f"{PORTFILE}: expected the base portfile with only REF set to "
                            f"{commit[:12]} -> actual other changes")

    old_rows = registered_rows(base[VERSIONS], f"{VERSIONS} at the base")
    new_rows = registered_rows(new[VERSIONS])
    key = version_key(version, port_version)
    old_keys = [version_key(r["version"], r.get("port-version", 0)) for r in old_rows]
    if key in old_keys:
        raise RegistryError(f"{PORT} {version} port-version {port_version}: expected not yet "
                            "registered -> actual already registered (append-only, constraint 9)")
    if old_keys and key <= max(old_keys):
        raise RegistryError(f"{PORT} {version} port-version {port_version}: expected newer than "
                            "every registered version -> actual older")
    want_row = {"git-tree": git_tree, "version": version, "port-version": port_version}
    if not new_rows or new_rows[0] != want_row:
        raise RegistryError(f"{VERSIONS}: expected the new entry {version} port-version "
                            f"{port_version} git-tree {git_tree[:12]} first -> actual another "
                            "entry")
    if new_rows[1:] != old_rows:
        raise RegistryError(f"{VERSIONS}: expected every registered entry unchanged -> actual "
                            "an entry changed, moved or removed (append-only, constraint 9)")

    old_base = parse_json(base[BASELINE], f"{BASELINE} at the base")
    new_base = parse_json(new[BASELINE], BASELINE)
    want_base = json.loads(json.dumps(old_base))
    default = want_base.get("default")
    if set(want_base) != {"default"} or not isinstance(default, dict):
        raise RegistryError(f"{BASELINE} at the base: expected one 'default' object -> actual "
                            "another shape")
    default[PORT] = {"baseline": version, "port-version": port_version}
    if new_base != want_base:
        raise RegistryError(f"{BASELINE}: expected only {PORT} moved to {version} "
                            f"port-version {port_version} -> actual other changes")
    return port_version, len(new_rows)


# ---------------------------------------------------------------------------
# Names (RP-10: fixed templates, validated parts only)
# ---------------------------------------------------------------------------

def branch_name(version: str) -> str:
    """``registry/v<version>`` (RP-9).

    Raises:
        RegistryError: ``version`` is not ``X.Y.Z``.
    """
    if not (isinstance(version, str) and VERSION.fullmatch(version)):
        raise RegistryError("registry branch: expected v<X.Y.Z> -> actual <rejected>")
    return BRANCH_PREFIX + version


def commit_message(version: str, commit: str, run_id: str) -> str:
    """The fixed registry commit message (RP-10): ``registry: smbios-c <v> (REF <c12>, run <id>)``.

    Raises:
        RegistryError: Any part is outside its allowlist.
    """
    branch_name(version)
    if not COMMIT.fullmatch(commit or "") or not RUN_ID.fullmatch(run_id or ""):
        raise RegistryError("registry commit message: expected a commit id and a run id -> "
                            "actual <rejected>")
    return f"registry: {PORT} {version} (REF {commit[:12]}, run {run_id})"


def file_digests(files: Mapping[str, bytes]) -> Dict[str, str]:
    """``{path: sha256}`` of the handed-over files (the prepare job's out-of-band output)."""
    return {path: hashlib.sha256(files[path]).hexdigest() for path in CHANGED_FILES
            if path in files}


def parse_prepared(text: str) -> Optional[Dict[str, Any]]:
    """Parse ``CI_REGISTRY_PREPARED``: ``{"base", "git_tree", "files": {path: sha256}}``.

    Returns:
        ``None`` for an empty value.

    Raises:
        RegistryError: Not a single-line JSON object of exactly that shape.
    """
    if not text:
        return None
    if len(text) > 4096 or "\n" in text or "\r" in text:
        raise RegistryError("CI_REGISTRY_PREPARED: expected single-line JSON -> actual "
                            "<rejected>")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise RegistryError("CI_REGISTRY_PREPARED: expected JSON -> actual unparseable") \
            from None
    files = data.get("files") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and set(data) == {"base", "git_tree", "files"}
            and isinstance(data["base"], str) and COMMIT.fullmatch(data["base"])
            and isinstance(data["git_tree"], str) and COMMIT.fullmatch(data["git_tree"])
            and isinstance(files, dict) and set(files) == set(CHANGED_FILES)
            and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                    for v in files.values())):
        raise RegistryError("CI_REGISTRY_PREPARED: expected base, git_tree and the three file "
                            "digests -> actual <rejected>")
    return data


# ---------------------------------------------------------------------------
# What vcpkg x-add-version writes (dry runs and tests; never published)
# ---------------------------------------------------------------------------

def simulate_add_version(base: Mapping[str, bytes], *, version: str, port_version: int,
                         git_tree: str) -> Dict[str, bytes]:
    """The two ``versions/`` files ``vcpkg x-add-version`` writes for a new entry.

    Used by ``--dry-run`` and the tests to show the exact change the judge
    (:func:`check_registry_change`) accepts. A real run always uses vcpkg's own
    output and checks it; this function is never a source of published bytes.

    Raises:
        RegistryError: The base files are malformed.
    """
    rows = registered_rows(base[VERSIONS], f"{VERSIONS} at the base")
    baseline = parse_json(base[BASELINE], f"{BASELINE} at the base")
    entry = {"git-tree": git_tree, "version": version, "port-version": port_version}
    default = baseline.setdefault("default", {})
    if not isinstance(default, dict):
        raise RegistryError(f"{BASELINE} at the base: expected one 'default' object -> actual "
                            "another shape")
    default[PORT] = {"baseline": version, "port-version": port_version}
    return {VERSIONS: (json.dumps({"versions": [entry] + rows}, indent=2) + "\n").encode(),
            BASELINE: (json.dumps(baseline, indent=2) + "\n").encode()}
