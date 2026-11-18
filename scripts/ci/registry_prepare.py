#!/usr/bin/env python3
"""``registry-prepare``: the unprivileged half of a registry update (0012 Story 04.0 Task 03).

Run as ``python3 scripts/ci/stage.py publish --destination vcpkg-registry --only
prepare`` in the ``registry-prepare`` job of ``stage-publish.yml``: ``contents:
read``, no environment, **no credential** (threat model
``docs/security/2026-11-18-release-and-publish-automation.md`` RP-2: toolchain
code never runs beside a write token; ``stage.py`` refuses this phase if
``CI_PUBLISH_TOKEN`` was set at all). It is the only place the registry update
runs ``git``, ``vcpkg`` and a compiler:

1. **Base.** A credential-less ``git fetch --no-tags origin
   +refs/heads/master:refs/ci/master``; the fetched commit is the base, and
   ``CI_COMMIT`` (the release commit) must be its ancestor. A shared clone of
   the checkout is made at the base, so ``master``'s files are edited and
   this job's own scripts are never replaced.
2. **The base must be pending registration.** Its port carries
   ``manifest.version`` (``prepare_release.py`` set it in the release commit),
   that version is not registered yet, and the registry lint passes.
3. **REF** in ``ports/smbios-c/portfile.cmake`` := ``manifest.commit``, never a tag
   resolved by name (constraint 5). The change is committed locally (never
   pushed: nothing in this job can push), because ``vcpkg x-add-version`` reads
   git, not the working tree.
4. ``vcpkg x-add-version smbios-c --x-builtin-ports-root=./ports
   --x-builtin-registry-versions-dir=./versions --verbose``: **without** the
   overwrite switch, so an already registered version fails here
   (constraint 9). Committed locally too.
5. **Self-check** with the same judge ``registry-pr`` applies
   (:func:`registry_update.check_registry_change`): only the three files
   changed, the ``git-tree`` equals the one computed in Python from the base,
   and the full registry lint (append-only history, ``REF`` on ``master``, ``REF``
   equal to the ``v<version>`` tag's commit) passes on the result.
6. **Consumer test** (the spec's validation before any write, ADR 0005 D6):
   ``vcpkg_registry_smoke.run`` installs ``smbios-c`` through this registry
   at the prepared commit, builds and runs the clean-room consumer, checks
   the exported symbols and the installed-file allowlist.
7. **Hand-over.** The three files are copied to
   ``$CI_ARTIFACT_DIR/registry/<path>`` (the workflow uploads them by literal
   path) and :func:`run` returns ``{"base", "git_tree", "files": {path:
   sha256}}``, which ``stage.py`` sets as the job output ``registry_prepared``
   (out-of-band digests for ``registry-pr``).

A library: it prints nothing (P11). Child processes go through
``consumer_smoke._run`` (argv, no shell, no stdin, CR-5 stripped environment,
bounded time). The tool runners are injectable (:class:`Tools`) so the tests
can drive steps 1-7 against a throwaway repository without vcpkg.
Python 3.9+ standard library only.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_vcpkg_registry  # noqa: E402
import consumer_smoke  # noqa: E402
import publish_inputs as pin  # noqa: E402
import registry_update as ru  # noqa: E402

#: The fetched base ref (credential-less fetch, threat model RP-5 wording).
BASE_REF = "refs/ci/master"
#: The local clone of the base under ``$CI_TEMP_DIR``.
CLONE_DIR = "registry-base"
#: Identity of the two local commits (never pushed; registry-pr writes its own).
_IDENTITY = ("-c", "user.name=libbios registry-prepare", "-c",
             "user.email=registry-prepare@invalid", "-c", "commit.gpgsign=false")
#: The exact x-add-version arguments (no overwrite switch, ever: constraint 9).
ADD_VERSION_ARGS: Tuple[str, ...] = ("x-add-version", ru.PORT, "--x-builtin-ports-root=./ports",
                                     "--x-builtin-registry-versions-dir=./versions", "--verbose")
consumer_smoke.PHASE_TIMEOUT_S.update({"git": 120, "x-add-version": 300})


class PrepareError(pin.PublishInputError):
    """A refused or failed prepare step; the first line is one value-free RC1 line.

    A :class:`publish_inputs.PublishInputError`, so ``stage.py publish`` reports
    it as a gate failure (exit 1) like every other refusal.
    """


def _run(phase: str, argv: Sequence[str], cwd: Optional[Path] = None) -> str:
    try:
        return consumer_smoke._run(phase, argv, cwd=cwd)
    except consumer_smoke.SmokeError as exc:
        raise PrepareError(str(exc)) from None


def git(repo: Path, *args: str) -> str:
    """Run ``git -C <repo> <args>``; return stdout (raises :class:`PrepareError`)."""
    return _run("git", ["git", "-C", str(repo), *args])


def add_version(clone: Path, vcpkg_root: Path) -> None:
    """``vcpkg x-add-version`` (:data:`ADD_VERSION_ARGS`) in ``clone``."""
    tool = vcpkg_root / ("vcpkg.exe" if os.name == "nt" else "vcpkg")
    if not tool.is_file():
        raise PrepareError("vcpkg: expected the bootstrapped tool -> actual missing")
    _run("x-add-version", [str(tool), *ADD_VERSION_ARGS], cwd=clone)


def smoke(work_dir: Path, vcpkg_root: Path, clone: Path) -> List[str]:
    """The Story 02.0 registry consumer test against ``clone`` at ``HEAD``."""
    import vcpkg_registry_smoke
    try:
        return vcpkg_registry_smoke.run(work_dir, vcpkg_root=vcpkg_root, repo_root=clone)
    except consumer_smoke.SmokeError as exc:
        raise PrepareError("registry consumer test: " + str(exc)) from None


@dataclass(frozen=True)
class Tools:
    """The tool runners (tests substitute :attr:`add_version` and :attr:`smoke`)."""

    git: Callable[..., str] = git
    add_version: Callable[[Path, Path], None] = add_version
    smoke: Callable[[Path, Path, Path], List[str]] = smoke


def _sha(text: str, what: str) -> str:
    value = text.strip()
    if not ru.COMMIT.fullmatch(value):
        raise PrepareError(f"{what}: expected a commit id -> actual <rejected>")
    return value


def _lint(clone: Path, what: str, *, pending: bool) -> None:
    report = check_vcpkg_registry.check_report(clone, master_ref=BASE_REF)
    if report.problems:
        raise PrepareError(f"registry lint {what}: expected consistent -> actual "
                           f"{report.problems[0]}"[:300])
    is_pending = any("pending registration" in n for n in report.notes)
    if is_pending != pending:
        raise PrepareError(f"registry lint {what}: expected "
                           f"{'pending registration' if pending else 'registered'} -> actual "
                           f"{'pending registration' if is_pending else 'registered'}")


def _port_entries(tools: Tools, clone: Path, commit: str) -> List[Tuple[str, str, str]]:
    out = tools.git(clone, "ls-tree", commit, ru.PORT_DIR + "/")
    entries = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or not path.startswith(ru.PORT_DIR + "/"):
            raise PrepareError(f"{ru.PORT_DIR}: expected ls-tree entries -> actual malformed")
        entries.append((parts[0], path.rsplit("/", 1)[1], parts[2]))
    return entries


def _read(clone: Path, rel: str) -> bytes:
    return pin._read_regular(clone.joinpath(*rel.split("/")), ru.MAX_FILE_BYTES, rel)


def run(*, workspace: Path, temp_dir: Path, artifact_dir: Path, inputs: pin.ReleaseInputs,
        vcpkg_root: Optional[Path], tools: Tools = Tools(), fetch: bool = True
        ) -> Tuple[List[str], Dict[str, object]]:
    """Steps 1-7 of the module docstring; return ``(info lines, prepared output)``.

    Args:
        workspace: The job's checkout (the release commit, full history).
        temp_dir: ``$CI_TEMP_DIR`` (the clone and the consumer build go here).
        artifact_dir: ``$CI_ARTIFACT_DIR`` (the hand-over goes to ``registry/``).
        inputs: The anchored, validated release inputs (constraint 4, 5).
        vcpkg_root: The bootstrapped vcpkg (``None`` only in tests whose
            ``tools`` need none).
        tools: Tool runners.
        fetch: Fetch ``master`` (tests pre-create :data:`BASE_REF` instead).

    Raises:
        PrepareError: The first refusal or failure.
        publish_inputs.PublishInputError: A hand-over file cannot be read.
    """
    if inputs.prerelease:
        raise PrepareError(f"registry: expected a final release tag -> actual pre-release "
                           f"{inputs.tag} (constraint 8)")
    commit, version = inputs.manifest.commit, inputs.base
    lines: List[str] = []

    if fetch:
        tools.git(workspace, "fetch", "--no-tags", "--quiet", "origin",
                  f"+refs/heads/master:{BASE_REF}")
    base = _sha(tools.git(workspace, "rev-parse", "--verify", f"{BASE_REF}^{{commit}}"),
                "fetched master")
    try:
        tools.git(workspace, "merge-base", "--is-ancestor", commit, base)
    except PrepareError:
        raise PrepareError(f"{commit[:12]} in master: expected an ancestor of the fetched "
                           f"master {base[:12]} -> actual not") from None

    clone = temp_dir / CLONE_DIR
    if clone.is_symlink():
        raise PrepareError("registry clone path: expected a directory -> actual symlink")
    if clone.exists():
        shutil.rmtree(clone)
    tools.git(workspace, "clone", "--quiet", "--shared", "--no-checkout", str(workspace),
              str(clone))
    tools.git(clone, "fetch", "--quiet", str(workspace), f"+{BASE_REF}:{BASE_REF}",
              "+refs/tags/*:refs/tags/*")
    tools.git(clone, "checkout", "--quiet", "--detach", base)

    base_files = {rel: _read(clone, rel) for rel in (*ru.CHANGED_FILES, ru.PORT_MANIFEST)}
    try:
        port_at_base, port_version = ru.port_version_of(base_files[ru.PORT_MANIFEST])
    except ru.RegistryError as exc:
        raise PrepareError(str(exc)) from None
    if port_at_base != version:
        raise PrepareError(f"{ru.PORT_MANIFEST} on master: expected {version} "
                           f"(manifest.version; prepare_release.py in the release commit) -> "
                           f"actual {port_at_base}")
    _lint(clone, "of the base", pending=True)

    try:
        new_portfile = ru.set_portfile_ref(base_files[ru.PORTFILE], commit)
    except ru.RegistryError as exc:
        raise PrepareError(str(exc)) from None
    clone.joinpath(*ru.PORTFILE.split("/")).write_bytes(new_portfile)
    tools.git(clone, *_IDENTITY, "commit", "--quiet", "-m",
              f"registry-prepare: {ru.PORT} REF (local)", "--", ru.PORTFILE)
    tools.add_version(clone, vcpkg_root)  # type: ignore[arg-type]
    status = tools.git(clone, "status", "--porcelain", "--untracked-files=all")
    changed = sorted(line[3:] for line in status.splitlines() if line.strip())
    if changed != sorted([ru.BASELINE, ru.VERSIONS]):
        raise PrepareError(f"vcpkg x-add-version: expected {ru.BASELINE} and {ru.VERSIONS} "
                           f"changed -> actual {len(changed)} paths")
    tools.git(clone, *_IDENTITY, "commit", "--quiet", "-m",
              f"registry-prepare: {ru.PORT} {version} versions (local)", "--", ru.BASELINE,
              ru.VERSIONS)

    head = _sha(tools.git(clone, "rev-parse", "HEAD"), "prepared commit")
    diff = sorted(tools.git(clone, "diff", "--name-only", base, head).split())
    if diff != sorted(ru.CHANGED_FILES):
        raise PrepareError(f"prepared change: expected exactly {', '.join(ru.CHANGED_FILES)} -> "
                           f"actual {len(diff)} paths")
    git_tree = _sha(tools.git(clone, "rev-parse", f"HEAD:{ru.PORT_DIR}"), "prepared git-tree")
    new_files = {rel: _read(clone, rel) for rel in ru.CHANGED_FILES}
    try:
        computed = ru.port_tree_sha(_port_entries(tools, clone, base), new_portfile)
        if computed != git_tree:
            raise ru.RegistryError(f"git-tree: expected {computed[:12]} (computed) -> actual "
                                   f"{git_tree[:12]} (git)")
        _pv, rows = ru.check_registry_change(base=base_files, new=new_files, version=version,
                                             commit=commit, git_tree=git_tree)
    except ru.RegistryError as exc:
        raise PrepareError(str(exc)) from None
    _lint(clone, "of the prepared change", pending=False)
    lines.append(f"info: registry-prepare: {ru.PORT} {version} port-version {port_version}, "
                 f"git-tree {git_tree[:12]}, REF {commit[:12]}, {rows} entries, base "
                 f"{base[:12]}; lint and judge pass")

    smoke_lines = tools.smoke(temp_dir / "registry-consumer", vcpkg_root, clone)  # type: ignore[arg-type]
    lines.append("info: registry consumer test: " + ", ".join(
        ln for ln in smoke_lines if "=" in ln)[:200])

    out_dir = artifact_dir / "registry"
    if out_dir.is_symlink():
        raise PrepareError("registry hand-off path: expected a directory -> actual symlink")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for rel in ru.CHANGED_FILES:
        dest = out_dir.joinpath(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(new_files[rel])
    prepared: Dict[str, object] = {"base": base, "git_tree": git_tree,
                                   "files": ru.file_digests(new_files)}
    return lines, prepared
