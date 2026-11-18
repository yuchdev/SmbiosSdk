#!/usr/bin/env python3
"""The ``vcpkg-registry`` publish destination (milestone 0012 Story 04.0 Tasks 03, 05; ADR 0005 D6, D9).

Task 05 moved this module from ``scripts/ci/publish_vcpkg_registry.py`` behind
the D9 interface of :mod:`destinations` (:func:`validate`, :func:`publish`,
:func:`check_published`); ``stage.py`` calls nothing else here. The destination
keeps the name ``vcpkg-registry`` (the jobs are ``registry-prepare`` and
``registry-pr``): it writes a branch, never a PR (RP-9), so the task spec's
``registry-pr``/``registry_pr`` destination name was not adopted. Permission
class ``repo-write``, tier ``release``.

Registers a final release in this repository's own vcpkg registry
(``docs/dev/vcpkg-registry.md``) as a **branch** ``registry/v<version>``. A human
opens and merges the PR. Threat model
``docs/security/2026-11-18-release-and-publish-automation.md`` rewrites the
task spec's single ``registry-pr`` job into two (RP-2) and removes the PR opening
(RP-9, constraint 10):

``registry-prepare`` (phase ``prepare``; ``registry_prepare.py``)
    Unprivileged: ``contents: read``, no environment, no credential, not in
    ``publish_jobs.json``. It runs the toolchain: it fetches ``master``, sets
    the port ``REF`` to ``manifest.commit``, runs ``vcpkg x-add-version`` and
    the registry consumer test, and hands three files over by artifact ID with
    their sha256, the base commit and the ``git-tree`` as job outputs.
``registry-pr`` (phase ``branch``; :func:`branch`, this module)
    ``contents: write`` only, REST only. Nothing here runs ``git``, ``vcpkg``, a
    compiler or a child process (an AST test pins that). It:

    1. runs the settings drift preflight (:func:`publish_github_release.settings_preflight`
       plus S-6, :func:`workflow_settings_preflight`) and the commit preflight
       (constraint 5: tag, ``manifest.commit``, remote tag dereference,
       ancestry of ``master``);
    2. refuses a pre-release tag (constraint 8);
    3. requires the prepared base to descend from ``CI_COMMIT`` and to be on
       ``master`` (compare API);
    4. reads the base commit's registry files **through the Git Data API**
       and recomputes the port ``git-tree`` itself
       (:func:`registry_update.port_tree_sha`);
    5. accepts the handed-over files only if they are exactly the one change
       :func:`registry_update.check_registry_change` allows, so the unprivileged
       job cannot smuggle anything else onto the branch;
    6. requires ``refs/heads/registry/v<version>`` to be absent (create-only,
       constraint 7), then creates three blobs, one tree on ``base_tree`` (the
       base), one commit whose parent is the base, checks the new tree's
       ``ports/smbios-c`` id against the computed ``git-tree``, repeats the
       commit preflight, and creates the ref. An existing ref is a hard
       failure, never an update.

    It never opens, approves or merges a PR and never writes ``master``.

Credential handling is Task 02's (RP-8): :class:`publish_github_release.GitHubApi`
attaches the token with ``add_unredirected_header``, only to the API host,
follows no redirect, and renders only ``<METHOD> <label>: expected <codes> ->
actual <status>``. The commit message is the fixed RP-10 template.

A library: it prints nothing (P11). Python 3.9+ standard library only.
"""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import publish_inputs as pin
import registry_update as ru
from destinations import Env, Result
from destinations import github_release as pgr

#: Registry key (ADR 0005 D9).
NAME = "vcpkg-registry"
#: Neutral names the ``branch`` phase reads (the adapter maps them; D9).
REQUIRED_ENV: Tuple[str, ...] = ("CI_PUBLISH_TOKEN", "CI_REPOSITORY", "CI_COMMIT", "CI_REF_NAME",
                                 "CI_RUN_ID", "CI_MANIFEST_SHA256",
                                 "CI_VERIFIED_MANIFEST_SHA256", "CI_BUILD_BINARY_CACHE",
                                 "CI_REGISTRY_PREPARED")
#: Phases, one per job: ``prepare`` is the unprivileged job, ``branch`` the credentialed one.
PHASES: Tuple[str, ...] = ("prepare", "branch")
#: Phases that run the toolchain and so must never see a credential (RP-2, constraint 1).
UNPRIVILEGED_PHASES: Tuple[str, ...] = ("prepare",)
#: Where ``registry-prepare`` puts the three files, under ``$CI_ARTIFACT_DIR``.
HANDOFF_DIR = "registry"
PublishError = pgr.PublishError
Secret = pgr.Secret
GitHubApi = pgr.GitHubApi


@dataclass(frozen=True)
class Handoff:
    """The ``registry-prepare`` hand-over, its digests already checked.

    Attributes:
        base: The ``master`` commit the change is based on.
        git_tree: The ``ports/smbios-c`` tree ``registry-prepare`` recorded.
        files: Bytes of exactly :data:`registry_update.CHANGED_FILES`.
    """

    base: str
    git_tree: str
    files: Mapping[str, bytes]


def load_handoff(artifact_dir: Path, prepared_text: str) -> Handoff:
    """Read ``$CI_ARTIFACT_DIR/registry/<path>`` and check each file against ``CI_REGISTRY_PREPARED``.

    The digests travel as ``registry-prepare`` job outputs, outside the
    artifact (the same out-of-band anchoring as constraint 4).

    Raises:
        publish_inputs.PublishInputError: Missing or malformed outputs, a
            missing, linked, oversized or altered file.
    """
    try:
        prepared = ru.parse_prepared(prepared_text)
    except ru.RegistryError as exc:
        raise pin.PublishInputError(str(exc)) from None
    if prepared is None:
        raise pin.PublishInputError("CI_REGISTRY_PREPARED: expected the registry-prepare job's "
                                    "output -> actual unset")
    root = artifact_dir / HANDOFF_DIR
    files: Dict[str, bytes] = {}
    for rel in ru.CHANGED_FILES:
        path = root.joinpath(*rel.split("/"))
        try:
            inside = path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        except OSError:
            raise pin.PublishInputError(f"{rel}: expected in the registry hand-off -> actual "
                                        "missing") from None
        if not inside:
            raise pin.PublishInputError(f"{rel}: expected inside the hand-off -> actual "
                                        "elsewhere")
        data = pin._read_regular(path, ru.MAX_FILE_BYTES, rel)
        files[rel] = data
    digests = ru.file_digests(files)
    for rel in ru.CHANGED_FILES:
        if digests[rel] != prepared["files"][rel]:
            raise pin.PublishInputError(f"{rel} sha256: expected {prepared['files'][rel][:16]} "
                                        f"(registry-prepare output) -> actual {digests[rel][:16]}")
    return Handoff(prepared["base"], prepared["git_tree"], files)


# ---------------------------------------------------------------------------
# Preflights
# ---------------------------------------------------------------------------

def workflow_settings_preflight(api: pgr.GitHubApi, log: pgr.Log) -> None:
    """S-6 drift check (RP-9): Actions may not approve PRs; default token read-only.

    Raises:
        PublishError: The setting is readable and wrong.
    """
    status, data = api.get_json("actions/permissions/workflow", "workflow permissions",
                                expect=(200, 403, 404))
    if status != 200 or not isinstance(data, dict):
        log("info: settings preflight: not readable with this token, manual read-back "
            "required (threat model S-6): workflow permissions")
        return
    if data.get("can_approve_pull_request_reviews") is not False:
        raise PublishError("Actions may approve pull requests (S-6): expected false -> actual "
                           "true or unset")
    if data.get("default_workflow_permissions") != "read":
        raise PublishError("default workflow permissions (S-6): expected read -> actual write")
    log("info: settings preflight: S-6 read back as required")


def _ancestor(api: pgr.GitHubApi, older: str, newer: str, what: str) -> None:
    """``older`` is an ancestor of (or equal to) ``newer`` (compare API, merge base = ``older``)."""
    _, cmp = api.get_json(f"compare/{older}...{newer}", "compare " + what)
    status = cmp.get("status") if isinstance(cmp, dict) else None
    merge_base = cmp.get("merge_base_commit") if isinstance(cmp, dict) else None
    sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
    if status not in ("ahead", "identical") or sha != older:
        shown = status if status in ("ahead", "identical", "behind", "diverged") else "<rejected>"
        raise PublishError(f"{what}: expected {older[:12]} an ancestor (ahead or identical) -> "
                           f"actual {shown}")


# ---------------------------------------------------------------------------
# Reading the base through the Git Data API
# ---------------------------------------------------------------------------

def _tree(api: pgr.GitHubApi, sha: str, what: str) -> List[Dict[str, Any]]:
    _, data = api.get_json(f"git/trees/{sha}", "git tree")
    entries = data.get("tree") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("sha") == sha and data.get("truncated") is not True
            and isinstance(entries, list) and all(
                isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("sha"), str) and ru.COMMIT.fullmatch(e["sha"])
                and e.get("type") in ("blob", "tree", "commit") and isinstance(e.get("mode"), str)
                for e in entries)):
        raise PublishError(f"git tree {what}: expected a complete tree -> actual malformed")
    return entries


def _entry(entries: Sequence[Mapping[str, Any]], name: str, kind: str, what: str
           ) -> Mapping[str, Any]:
    hits = [e for e in entries if e["path"] == name]
    if len(hits) != 1 or hits[0]["type"] != kind:
        raise PublishError(f"{what}/{name}: expected one {kind} -> actual {len(hits)} or another "
                           "kind")
    return hits[0]


def _blob(api: pgr.GitHubApi, sha: str, what: str) -> bytes:
    _, data = api.get_json(f"git/blobs/{sha}", "git blob")
    content = data.get("content") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("encoding") == "base64"
            and isinstance(content, str) and len(content) <= 2 * ru.MAX_FILE_BYTES):
        raise PublishError(f"{what}: expected a base64 blob -> actual malformed")
    try:
        raw = base64.b64decode(content.replace("\n", ""), validate=True)
    except ValueError:
        raise PublishError(f"{what}: expected a base64 blob -> actual undecodable") from None
    if ru.git_blob_sha(raw) != sha:
        raise PublishError(f"{what}: expected blob {sha[:12]} -> actual other content")
    return raw


def read_base(api: pgr.GitHubApi, base: str) -> Tuple[str, Dict[str, bytes],
                                                       List[Tuple[str, str, str]]]:
    """Return ``(root tree, files, port entries)`` of commit ``base``, read through the API.

    ``files`` holds :data:`registry_update.CHANGED_FILES` and the port manifest;
    every blob is checked against its object id.

    Raises:
        PublishError: The commit, a tree or a blob is missing or malformed.
    """
    _, commit = api.get_json(f"git/commits/{base}", "git commit")
    tree = commit.get("tree") if isinstance(commit, dict) else None
    root = tree.get("sha") if isinstance(tree, dict) else None
    if not (isinstance(commit, dict) and commit.get("sha") == base and isinstance(root, str)
            and ru.COMMIT.fullmatch(root)):
        raise PublishError(f"registry base {base[:12]}: expected a commit -> actual malformed")
    top = _tree(api, root, "root")
    ports = _tree(api, _entry(top, "ports", "tree", "")["sha"], "ports")
    port = _tree(api, _entry(ports, ru.PORT, "tree", "ports")["sha"], ru.PORT_DIR)
    versions = _tree(api, _entry(top, "versions", "tree", "")["sha"], "versions")
    s_dash = _tree(api, _entry(versions, "s-", "tree", "versions")["sha"], "versions/s-")
    entries = [(str(e["mode"]), str(e["path"]), str(e["sha"])) for e in port]
    files = {
        ru.PORTFILE: _blob(api, _entry(port, "portfile.cmake", "blob", ru.PORT_DIR)["sha"],
                           ru.PORTFILE),
        ru.PORT_MANIFEST: _blob(api, _entry(port, "vcpkg.json", "blob", ru.PORT_DIR)["sha"],
                                ru.PORT_MANIFEST),
        ru.BASELINE: _blob(api, _entry(versions, "baseline.json", "blob", "versions")["sha"],
                           ru.BASELINE),
        ru.VERSIONS: _blob(api, _entry(s_dash, f"{ru.PORT}.json", "blob", "versions/s-")["sha"],
                           ru.VERSIONS),
    }
    return root, files, entries


def _new_port_tree(api: pgr.GitHubApi, root: str) -> str:
    top = _tree(api, root, "new root")
    ports = _tree(api, _entry(top, "ports", "tree", "")["sha"], "new ports")
    return str(_entry(ports, ru.PORT, "tree", "ports")["sha"])


# ---------------------------------------------------------------------------
# The branch phase
# ---------------------------------------------------------------------------

def branch(api: pgr.GitHubApi, inputs: pin.ReleaseInputs, log: pgr.Log, *,
           handoff: Handoff) -> str:
    """``registry-pr``: create ``registry/v<version>`` holding the one checked registry change.

    Returns:
        The ``ok:`` summary: the branch, its commit, the entry, and the compare
        URL the maintainer opens the PR from (RP-9).

    Raises:
        PublishError: The first refusal; nothing is written after it.
    """
    if inputs.prerelease:
        raise PublishError(f"registry: expected a final release tag -> actual pre-release "
                           f"{inputs.tag} (constraint 8: pre-releases never touch the registry)")
    pgr.settings_preflight(api, log)
    workflow_settings_preflight(api, log)
    pgr.commit_preflight(api, inputs)
    commit, version = inputs.manifest.commit, inputs.base
    _ancestor(api, commit, handoff.base, "release commit in the registry base")
    _ancestor(api, handoff.base, "master", "registry base on master")

    root, base_files, port_entries = read_base(api, handoff.base)
    try:
        git_tree = ru.port_tree_sha(port_entries, handoff.files[ru.PORTFILE])
        if git_tree != handoff.git_tree:
            raise ru.RegistryError(f"git-tree: expected {git_tree[:12]} (computed from the base) "
                                   f"-> actual {handoff.git_tree[:12]} (registry-prepare)")
        port_version, rows = ru.check_registry_change(base=base_files, new=handoff.files,
                                                      version=version, commit=commit,
                                                      git_tree=git_tree)
        name = ru.branch_name(version)
        message = ru.commit_message(version, commit, inputs.run_id)
    except ru.RegistryError as exc:
        raise PublishError(str(exc)) from None
    ref = "refs/heads/" + name
    status, _ = api.get_json("git/ref/heads/" + urllib.parse.quote(name, safe="/.-"),
                             "git ref branch", expect=(200, 404))
    if status == 200:
        raise PublishError(f"branch {name}: expected none -> actual exists (create-only; never "
                           "updated: review it, then delete it by hand to retry)")

    modes = {n: m for m, n, _s in port_entries}
    tree_items = []
    for rel in ru.CHANGED_FILES:
        data = handoff.files[rel]
        resp = api.request("POST", api.api("git/blobs"), "git blob", expect=(201,),
                           payload={"content": base64.b64encode(data).decode("ascii"),
                                    "encoding": "base64"})
        obj = resp.json("POST git blob")
        if not (isinstance(obj, dict) and obj.get("sha") == ru.git_blob_sha(data)):
            raise PublishError(f"POST git blob {rel}: expected {ru.git_blob_sha(data)[:12]} -> "
                               "actual another object")
        mode = modes.get("portfile.cmake", ru.FILE_MODE) if rel == ru.PORTFILE else ru.FILE_MODE
        tree_items.append({"path": rel, "mode": mode, "type": "blob", "sha": obj["sha"]})
    resp = api.request("POST", api.api("git/trees"), "git tree", expect=(201,),
                       payload={"base_tree": root, "tree": tree_items})
    obj = resp.json("POST git tree")
    new_root = obj.get("sha") if isinstance(obj, dict) else None
    if not (isinstance(new_root, str) and ru.COMMIT.fullmatch(new_root)):
        raise PublishError("POST git tree: expected a tree id -> actual malformed")
    port_tree = _new_port_tree(api, new_root)
    if port_tree != git_tree:
        raise PublishError(f"new {ru.PORT_DIR} tree: expected {git_tree[:12]} (the registered "
                           f"git-tree) -> actual {port_tree[:12]}")
    resp = api.request("POST", api.api("git/commits"), "git commit", expect=(201,),
                       payload={"message": message, "tree": new_root, "parents": [handoff.base]})
    obj = resp.json("POST git commit")
    new_commit = obj.get("sha") if isinstance(obj, dict) else None
    parents = obj.get("parents") if isinstance(obj, dict) else None
    tree = obj.get("tree") if isinstance(obj, dict) else None
    if not (isinstance(new_commit, str) and ru.COMMIT.fullmatch(new_commit)
            and isinstance(tree, dict) and tree.get("sha") == new_root
            and isinstance(parents, list) and len(parents) == 1
            and isinstance(parents[0], dict) and parents[0].get("sha") == handoff.base):
        raise PublishError("POST git commit: expected the new tree on the base -> actual "
                           "another commit")
    pgr.commit_preflight(api, inputs)  # immediately before the write (RP-5)
    resp = api.request("POST", api.api("git/refs"), "git refs", expect=(201,),
                       payload={"ref": ref, "sha": new_commit})
    obj = resp.json("POST git refs")
    target = obj.get("object") if isinstance(obj, dict) else None
    if not (isinstance(obj, dict) and obj.get("ref") == ref and isinstance(target, dict)
            and target.get("sha") == new_commit):
        raise PublishError(f"POST git refs: expected {ref} at {new_commit[:12]} -> actual "
                           "another ref (investigate now)")
    log(f"info: registry branch {name}: {ru.PORT} {version} port-version {port_version}, "
        f"git-tree {git_tree[:12]}, REF {commit[:12]}, {rows} entries, base "
        f"{handoff.base[:12]}; no PR opened (RP-9)")
    # The ok: line is cut at 160 characters (RC1), so it carries only what the
    # maintainer acts on: the branch and the compare URL to open the PR from.
    return (f"branch {name} at {new_commit[:12]}; open the PR: "
            f"https://github.com/{api.repository}/compare/master...{name}")


# ---------------------------------------------------------------------------
# Dry run (no network, no credential, no child process)
# ---------------------------------------------------------------------------

def local_preview(workspace: Path) -> Tuple[Dict[str, bytes], List[Tuple[str, str, str]]]:
    """The working tree's registry files and port entries, as if it were the base.

    Raises:
        PublishError: A registry file is missing, linked or oversized.
    """
    files: Dict[str, bytes] = {}
    for rel in (*ru.CHANGED_FILES, ru.PORT_MANIFEST):
        try:
            files[rel] = pin._read_regular(workspace.joinpath(*rel.split("/")),
                                           ru.MAX_FILE_BYTES, rel)
        except pin.PublishInputError as exc:
            raise PublishError(str(exc)) from None
    port_dir = workspace.joinpath(*ru.PORT_DIR.split("/"))
    entries = []
    for path in sorted(port_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise PublishError(f"{ru.PORT_DIR}: expected regular files only -> actual another "
                               "kind")
        entries.append((ru.FILE_MODE, path.name,
                        ru.git_blob_sha(pin._read_regular(path, ru.MAX_FILE_BYTES, path.name))))
    return files, entries


def rp_args() -> Tuple[str, ...]:
    """The ``vcpkg`` arguments ``registry-prepare`` runs (shown by the dry run).

    Kept here as data, not imported from ``registry_prepare`` (which starts
    child processes), so this credentialed module stays free of it; a test
    pins the two equal.
    """
    return ("x-add-version", ru.PORT, "--x-builtin-ports-root=./ports",
            "--x-builtin-registry-versions-dir=./versions", "--verbose")


def dry_run_plan(inputs: pin.ReleaseInputs, repository: str, phase: Optional[str], *,
                 workspace: Path) -> List[str]:
    """The reads, tool runs and writes a real run would make; the change simulated locally.

    The working tree stands in for the ``master`` base that ``registry-prepare``
    fetches. The new ``versions/`` files are :func:`registry_update.simulate_add_version`'s,
    and the judge (:func:`registry_update.check_registry_change`) is run on them,
    so a dry run shows whether a real run would be accepted.

    Returns:
        ``info:`` lines.

    Raises:
        PublishError: The working tree's registry files are unreadable.
    """
    c12 = inputs.commit[:12]
    if inputs.prerelease:
        return [f"info: dry run: {inputs.tag} is a pre-release: no registry-prepare or "
                "registry-pr (constraint 8); a real run refuses"]
    try:
        name = ru.branch_name(inputs.base)
        message = ru.commit_message(inputs.base, inputs.commit, inputs.run_id)
    except ru.RegistryError as exc:
        raise PublishError(str(exc)) from None
    base, entries = local_preview(workspace)
    lines: List[str] = []
    try:
        port_at_base, port_version = ru.port_version_of(base[ru.PORT_MANIFEST])
    except ru.RegistryError as exc:
        raise PublishError(str(exc)) from None
    new_portfile = None
    git_tree = None
    if port_at_base != inputs.base:
        lines.append(f"info: dry run: the base port carries {port_at_base}, not {inputs.base}: "
                     f"run scripts/release/prepare_release.py {inputs.base} in the release "
                     "commit; a real run refuses")
    else:
        try:
            new_portfile = ru.set_portfile_ref(base[ru.PORTFILE], inputs.commit)
            git_tree = ru.port_tree_sha(entries, new_portfile)
            new = dict(ru.simulate_add_version(base, version=inputs.base,
                                               port_version=port_version, git_tree=git_tree))
            new[ru.PORTFILE] = new_portfile
            _pv, rows = ru.check_registry_change(base=base, new=new, version=inputs.base,
                                                 commit=inputs.commit, git_tree=git_tree)
        except ru.RegistryError as exc:
            raise PublishError(str(exc)) from None
    if phase in (None, "prepare"):
        lines += [
            "info: dry run: registry-prepare (contents: read, no credential): would fetch master "
            "(credential-less) as the base and require "
            f"{c12} to be an ancestor of it",
            f"info: dry run: would set REF {inputs.commit} (manifest.commit) in "
            f"{ru.PORTFILE} and commit locally (never pushed)",
            f"info: dry run: would run vcpkg {' '.join(rp_args())} (never overwriting)",
        ]
        if git_tree is not None:
            lines.append(f"info: dry run: would register {ru.PORT} {inputs.base} port-version "
                         f"{port_version}, git-tree {git_tree[:12]} (computed from the working "
                         f"tree), {rows} entries; the judge accepts the simulated change")
        lines.append("info: dry run: would run the registry lint and the vcpkg registry "
                     "consumer test (installed-file allowlist), then hand over "
                     f"{', '.join(ru.CHANGED_FILES)} by artifact ID with their sha256")
    if phase in (None, "branch"):
        lines += [
            f"info: dry run: registry-pr (contents: write, REST only): would run the settings "
            f"and commit preflights, read the base through the Git Data API, recompute the "
            f"git-tree and require branch {name} absent (create-only)",
            f"info: dry run: would POST 3 blobs, a tree on the base tree, the commit "
            f"\"{message}\" and git/refs refs/heads/{name}",
            f"info: dry run: never opens or approves a PR (RP-9); the maintainer opens it from "
            f"https://github.com/{repository}/compare/master...{name}",
        ]
    return lines


# ---------------------------------------------------------------------------
# The ADR 0005 D9 interface (Task 05): the only members stage.py calls
# ---------------------------------------------------------------------------

def validate(manifest: pin.ReleaseInputs, env: Env) -> None:
    """D9 ``validate``: a release tag and a well-formed repository; no network.

    A pre-release tag passes here (the dry run explains that a real run
    refuses it); :func:`branch` refuses it before any request (constraint 8).

    Raises:
        PublishError: No release tag, or ``CI_REPOSITORY`` set but malformed.
    """
    if not pin.TAG.fullmatch(manifest.tag or ""):
        raise PublishError(f"{NAME}: expected a release tag v<X.Y.Z> -> actual none (release "
                           "tier only)")
    repository = env.get("CI_REPOSITORY")
    if repository and not pgr.REPOSITORY.fullmatch(repository):
        raise PublishError("CI_REPOSITORY: expected owner/name -> actual <rejected>")


def publish(manifest: pin.ReleaseInputs, artifact_dir: Path, env: Env, dry_run: bool) -> Result:
    """D9 ``publish``: phase ``preflight`` or ``branch`` (``registry-pr``); REST only.

    The ``prepare`` phase is unprivileged and never reaches this function:
    ``stage.py`` runs ``registry_prepare`` in a job without a credential
    (RP-2), and this module imports nothing that starts a child process. The
    hand-over is loaded and checked against ``CI_REGISTRY_PREPARED`` before the
    API client (and so the credential) is touched.

    Raises:
        PublishError: A refused or failed request, or an unknown phase.
        publish_inputs.PublishInputError: A missing or altered hand-over.
    """
    validate(manifest, env)
    repository = env.get("CI_REPOSITORY") or "<owner>/<repo>"
    if dry_run:
        lines = dry_run_plan(manifest, repository, env.phase, workspace=env.workspace)
        return Result(f"registry change for {manifest.tag}",
                      (f"https://github.com/{repository}/tree/registry/v{manifest.base}",),
                      tuple(lines), 0)
    if env.phase not in ("preflight", "branch"):
        raise PublishError(f"{NAME}: expected phase preflight or branch -> actual another "
                           "phase")
    handoff = (load_handoff(Path(artifact_dir), env.get("CI_REGISTRY_PREPARED"))
               if env.phase == "branch" else None)
    api = GitHubApi(env.secret("CI_PUBLISH_TOKEN"), env.get("CI_REPOSITORY"))
    if handoff is None:
        pgr.settings_preflight(api, env.log)
        workflow_settings_preflight(api, env.log)
        pgr.commit_preflight(api, manifest)
        summary = (f"preflight {manifest.tag}: tag and master ancestry bind "
                   f"{manifest.commit[:12]}, {len(manifest.assets)} assets match the manifest")
    else:
        summary = branch(api, manifest, env.log, handoff=handoff)
    return Result(summary, (f"https://github.com/{api.repository}/tree/registry/"
                            f"v{manifest.base}",), (), len(api.calls))


def check_published(manifest: pin.ReleaseInputs, env: Env) -> str:
    """D9 ``check_published``: not run at publish time (fail closed if called).

    Before a human merges the PR there is no published registry state to
    re-read; the branch itself is checked inside :func:`branch` (tree id
    against the computed ``git-tree``). After the merge, the nightly
    ``stage.py verify --only registry`` (Task 04) is this destination's
    post-publish check: the vcpkg consumer test against the published registry.

    Raises:
        PublishError: Always.
    """
    del manifest, env
    raise PublishError(f"{NAME} check_published: expected the nightly verify --only registry "
                       "after the human merge -> actual called at publish time")
