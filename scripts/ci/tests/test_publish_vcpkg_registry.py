"""Tests for the ``vcpkg-registry`` publish destination's ``branch`` phase (0012 Story 04.0 Task 03).

Classification: unit + mock. The GitHub REST API is Task 02's loopback stub
(``test_publish_github_release.FakeGitHub``) extended with an in-memory **Git
Data API** (blobs, trees, commits, refs), so no real network, no real
credential, and no branch is ever created. The stub echoes the
``Authorization`` header into every error body and header, so a path that
renders either would leak the canary into what the tests inspect.

Scenarios map to the threat model ``docs/security/2026-11-18-release-and-publish-automation.md``:

- constraint 1 / RP-2: :class:`NoToolchainTests` (AST: no ``subprocess`` or step
  runner in the credentialed modules; a full branch run with ``Popen`` patched to
  fail; the ``prepare`` phase refuses when a credential is set);
- constraint 3 / RP-8: :class:`CanaryTests`;
- constraint 5: :class:`PreflightTests` (tag moved before the ref write, base
  not descending from the release commit, base not on ``master``);
- constraint 7, 10 / RP-9: :class:`BranchTests` (one new branch, never a PR,
  existing branch is a hard failure with no write);
- constraint 8: pre-release tag refused, no request;
- RP-2 (the unprivileged job is not trusted): :class:`HandoffTests` (a
  tampered portfile with a matching digest, a rewritten entry, a wrong
  ``git-tree``, a digest mismatch);
- RP-9 S-6 drift: :class:`SettingsTests`;
- D9 ``--dry-run``: :class:`DryRunTests`.

On failure first check: the last request the stub recorded (``fake.requests``),
then ``publish_vcpkg_registry.branch`` or ``registry_update.check_registry_change``.

Run: ``python3 scripts/ci/run_tests.py``.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import functools
import hashlib
import io
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from ci_test_support import CI_DIR

import job_summary  # noqa: E402
import publish_github_release as pgr  # noqa: E402
import publish_vcpkg_registry as pvr  # noqa: E402
import registry_update as ru  # noqa: E402
import stage  # noqa: E402
from reporters import get_reporter  # noqa: E402
from test_publish_github_release import (CANARY, COMMIT, LEAK_FORMS, OTHER, REPO,  # noqa: E402
                                         RUN_ID, TAG, VERSION, FakeGitHub, _FakeCase,
                                         stage_env)

REPO_ROOT = CI_DIR.parents[1]
BRANCH = f"registry/v{VERSION}"


def base_files() -> Dict[str, bytes]:
    """The real registry files at a base where prepare_release.py set ``VERSION``."""
    files = {rel: (REPO_ROOT / rel).read_bytes()
             for rel in (*ru.CHANGED_FILES, ru.PORT_MANIFEST)}
    port = json.loads(files[ru.PORT_MANIFEST])
    port["version"] = VERSION
    files[ru.PORT_MANIFEST] = (json.dumps(port, indent=2) + "\n").encode()
    return files


class FakeGitData(FakeGitHub):
    """Task 02's stub plus an in-memory Git Data API holding one base commit."""

    def __init__(self) -> None:
        super().__init__()
        self.blobs: Dict[str, bytes] = {}
        self.trees: Dict[str, List[Dict[str, str]]] = {}
        self.commits: Dict[str, Dict[str, Any]] = {}
        self.heads: Dict[str, str] = {}
        self.compares: Dict[str, str] = {}      # "a...b" -> status (default ahead)
        self.workflow_perms: Optional[Dict[str, Any]] = None  # None -> 403
        self.on_commit = lambda: None
        files = base_files()
        self.base_files = files
        port = self.mktree({"portfile.cmake": self.blob(files[ru.PORTFILE]),
                            "vcpkg.json": self.blob(files[ru.PORT_MANIFEST])})
        ports = self.mktree({"smbios-c": port}, kind="tree")
        s_dash = self.mktree({"smbios-c.json": self.blob(files[ru.VERSIONS])})
        versions = self.mktree({"baseline.json": self.blob(files[ru.BASELINE]),
                                "s-": s_dash}, kinds={"s-": "tree"})
        root = self.mktree({"ports": ports, "versions": versions, "README.md":
                            self.blob(b"readme\n")}, kinds={"ports": "tree", "versions": "tree"})
        self.base_commit = hashlib.sha1(b"base" + root.encode()).hexdigest()
        self.commits[self.base_commit] = {"tree": root, "parents": [COMMIT]}
        self.base_root = root
        self.port_entries = [(ru.FILE_MODE, "portfile.cmake", ru.git_blob_sha(files[ru.PORTFILE])),
                             (ru.FILE_MODE, "vcpkg.json", ru.git_blob_sha(files[ru.PORT_MANIFEST]))]

    # -- object store ---------------------------------------------------------
    def blob(self, data: bytes) -> str:
        sha = ru.git_blob_sha(data)
        self.blobs[sha] = data
        return sha

    def mktree(self, entries: Dict[str, str], kind: str = "blob",
               kinds: Optional[Dict[str, str]] = None) -> str:
        items = []
        for name, sha in entries.items():
            k = (kinds or {}).get(name, kind)
            items.append({"path": name, "mode": "040000" if k == "tree" else "100644",
                          "type": k, "sha": sha})
        sha = ru.git_tree_sha([("40000" if e["type"] == "tree" else e["mode"], e["path"],
                                e["sha"]) for e in items])
        self.trees[sha] = items
        return sha

    def put_path(self, tree: str, parts: List[str], blob: str) -> str:
        entries = {e["path"]: e["sha"] for e in self.trees[tree]}
        kinds = {e["path"]: e["type"] for e in self.trees[tree]}
        if len(parts) == 1:
            entries[parts[0]] = blob
            kinds[parts[0]] = "blob"
        else:
            entries[parts[0]] = self.put_path(entries[parts[0]], parts[1:], blob)
        return self.mktree(entries, kinds=kinds)

    def port_tree_of(self, root: str) -> str:
        ports = next(e["sha"] for e in self.trees[root] if e["path"] == "ports")
        return next(e["sha"] for e in self.trees[ports] if e["path"] == "smbios-c")

    # -- routes ---------------------------------------------------------------
    def route(self, h: Any, method: str, path: str, query: Dict[str, List[str]],
              body: bytes) -> None:
        prefix = f"/repos/{REPO}/"
        rest = path[len(prefix):] if path.startswith(prefix) else ""
        m = re.fullmatch(r"compare/([0-9a-f]{40})\.\.\.([0-9a-z]+)", rest)
        if method == "GET" and m:
            if m.group(2) == "master" and m.group(1) == COMMIT:
                return h._send(200, self.compare)
            status = self.compares.get(f"{m.group(1)}...{m.group(2)}", "ahead")
            return h._send(200, {"status": status, "merge_base_commit": {"sha": m.group(1)}})
        if method == "GET" and rest == "actions/permissions/workflow":
            return h._send(403) if self.workflow_perms is None else \
                h._send(200, self.workflow_perms)
        m = re.fullmatch(r"git/(commits|trees|blobs)/([0-9a-f]{40})", rest)
        if method == "GET" and m:
            kind, sha = m.groups()
            if kind == "commits" and sha in self.commits:
                c = self.commits[sha]
                return h._send(200, {"sha": sha, "tree": {"sha": c["tree"]},
                                     "parents": [{"sha": p} for p in c["parents"]]})
            if kind == "trees" and sha in self.trees:
                return h._send(200, {"sha": sha, "tree": self.trees[sha], "truncated": False})
            if kind == "blobs" and sha in self.blobs:
                return h._send(200, {"sha": sha, "encoding": "base64",
                                     "content": base64.b64encode(self.blobs[sha]).decode()})
            return h._send(404)
        if method == "GET" and rest.startswith("git/ref/heads/"):
            name = rest[len("git/ref/heads/"):]
            return h._send(200, {"ref": "refs/heads/" + name,
                                 "object": {"sha": self.heads[name]}}) if name in self.heads \
                else h._send(404)
        if method == "POST" and rest == "git/blobs":
            data = json.loads(body)
            sha = self.blob(base64.b64decode(data["content"]))
            return h._send(201, {"sha": sha})
        if method == "POST" and rest == "git/trees":
            data = json.loads(body)
            tree = data["base_tree"]
            for item in data["tree"]:
                tree = self.put_path(tree, item["path"].split("/"), item["sha"])
            return h._send(201, {"sha": tree, "tree": self.trees[tree]})
        if method == "POST" and rest == "git/commits":
            data = json.loads(body)
            sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = {"tree": data["tree"], "parents": data["parents"],
                                 "message": data["message"]}
            self.on_commit()
            return h._send(201, {"sha": sha, "tree": {"sha": data["tree"]},
                                 "parents": [{"sha": p} for p in data["parents"]]})
        if method == "POST" and rest == "git/refs":
            data = json.loads(body)
            name = data["ref"][len("refs/heads/"):]
            if name in self.heads:
                return h._send(422)
            self.heads[name] = data["sha"]
            return h._send(201, {"ref": data["ref"], "object": {"sha": data["sha"]}})
        return super().route(h, method, path, query, body)


class _RegistryCase(_FakeCase):
    """A dist/, a registry hand-off and the Git Data stub."""

    def setUp(self) -> None:
        super().setUp()
        self.fake.close()
        self.fake = FakeGitData()
        self.addCleanup(self.fake.close)
        portfile = ru.set_portfile_ref(self.fake.base_files[ru.PORTFILE], COMMIT)
        self.git_tree = ru.port_tree_sha(self.fake.port_entries, portfile)
        self.new = dict(ru.simulate_add_version(self.fake.base_files, version=VERSION,
                                                port_version=0, git_tree=self.git_tree))
        self.new[ru.PORTFILE] = portfile
        self.prepared = self.write_handoff(self.new)

    def write_handoff(self, files: Dict[str, bytes], *, git_tree: Optional[str] = None,
                      digests: Optional[Dict[str, str]] = None) -> str:
        for rel, data in files.items():
            path = self.tmp / "registry" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        prepared = {"base": self.fake.base_commit, "git_tree": git_tree or self.git_tree,
                    "files": digests or ru.file_digests(files)}
        self.prepared = json.dumps(prepared)
        return self.prepared


def registry_file(case: _FakeCase, enabled: bool) -> Path:
    path = case.tmp / "publish_destinations.json"
    path.write_text(json.dumps({"schema": 1, "destinations": [
        {"name": "github-release", "enabled": enabled, "tier": "release",
         "permission_class": "repo-write", "environment": "release",
         "credentials": ["CI_PUBLISH_TOKEN"]},
        {"name": "vcpkg-registry", "enabled": enabled, "tier": "release",
         "permission_class": "repo-write", "environment": "release",
         "credentials": ["CI_PUBLISH_TOKEN"]}]}), encoding="utf-8")
    return path


class StageRun:
    def __init__(self, rc: int, stdout: str, stderr: str, results: str, summary: str) -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.results, self.summary = results, summary

    @property
    def text(self) -> str:
        return self.stdout + self.stderr

    def everything(self) -> str:
        return "\n".join((self.stdout, self.stderr, self.results, self.summary))


def registry_stage(case: _RegistryCase, args: List[str], *, enabled: bool = True,
                   destination: str = "vcpkg-registry", **env: str) -> StageRun:
    """Run ``stage.main(["publish", "--destination", <destination>, *args])`` in-process."""
    out, err = io.StringIO(), io.StringIO()
    bound = functools.partial(pgr.GitHubApi, policy=case.fake.policy(), timeout_s=10)
    full = stage_env(case, CI_REGISTRY_PREPARED=case.prepared, CI_WORKSPACE=str(REPO_ROOT))
    full.update(env)
    with mock.patch.dict(os.environ, full, clear=True), \
            mock.patch.object(pvr, "GitHubApi", bound), \
            mock.patch.object(pgr, "GitHubApi", bound), \
            mock.patch.object(stage, "DESTINATIONS_FILE", registry_file(case, enabled)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = stage.main(["publish", "--destination", destination, *args])
        case.assertNotIn("CI_PUBLISH_TOKEN", os.environ, "the credential must leave the env")
        job_summary.run(get_reporter("github"), job="build", leg="linux",
                        results=case.tmp / "results.jsonl", temp_dir=case.tmp,
                        artifact_dir=case.tmp)

    def read(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    return StageRun(rc, out.getvalue(), err.getvalue(), read(case.tmp / "results.jsonl"),
                    read(case.tmp / "summary.md") + read(case.tmp / "summary-build-linux.json"))


def branch_run(case: _RegistryCase, **env: str) -> StageRun:
    return registry_stage(case, ["--only", "branch"], **env)


# ---------------------------------------------------------------------------

class NoToolchainTests(_RegistryCase):
    """Type: unit (AST) + integration. Constraint 1, 2 / RP-2."""

    FORBIDDEN = {"subprocess", "ci_step", "os.system", "os.popen", "os.exec", "pty",
                 "multiprocessing", "shlex", "exec", "eval", "__import__", "registry_prepare",
                 "consumer_smoke", "vcpkg_registry_smoke", "bootstrap_vcpkg"}

    def test_CredentialedModulesImportNoChildProcessOrToolchain(self) -> None:
        for name in ("publish_vcpkg_registry.py", "registry_update.py"):
            with self.subTest(module=name):
                tree = ast.parse((CI_DIR / name).read_text(encoding="utf-8"))
                used = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        used |= {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        used.add(node.module.split(".")[0])
                    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                        name_ = f"{node.value.id}.{node.attr}"
                        used.add(name_[:7] if name_.startswith("os.exec") else name_)
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        used.add(node.func.id)
                self.assertEqual(used & self.FORBIDDEN, set())

    def test_FullBranchRunStartsNoChildProcess(self) -> None:
        def no_child(*_a: Any, **_k: Any) -> None:
            raise AssertionError("a child process was started on the credentialed path")
        with mock.patch.object(subprocess, "Popen", side_effect=no_child), \
                mock.patch.object(os, "system", side_effect=no_child):
            run = branch_run(self)
        self.assertEqual(run.rc, 0, run.text)

    def test_PreparePhaseRefusesWhenACredentialIsSet(self) -> None:
        """RP-2: the toolchain phase never runs beside a token, whatever the workflow says."""
        def no_tool(*_a: Any, **_k: Any) -> None:
            raise AssertionError("the prepare phase ran a tool with a credential present")
        with mock.patch.object(subprocess, "Popen", side_effect=no_tool):
            run = registry_stage(self, ["--only", "prepare"])
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("expected no credential in this job (RP-2", run.stdout)
        self.assertEqual(self.fake.requests, [])
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, run.everything())


class BranchTests(_RegistryCase):
    """Type: mock. Constraints 7, 10 / RP-9: one new branch, never a PR, never master."""

    def test_BranchHoldsExactlyTheCheckedChange(self) -> None:
        run = branch_run(self)
        self.assertEqual(run.rc, 0, run.text)
        commit = self.fake.heads[BRANCH]
        c = self.fake.commits[commit]
        self.assertEqual(c["parents"], [self.fake.base_commit])
        self.assertEqual(c["message"], f"registry: smbios-c {VERSION} (REF {COMMIT[:12]}, run "
                                       f"{RUN_ID})")
        self.assertEqual(self.fake.port_tree_of(c["tree"]), self.git_tree)
        self.assertEqual(list(self.fake.heads), [BRANCH])
        writes = [(r.method, r.path.split(f"/repos/{REPO}/")[1]) for r in self.fake.writes()]
        self.assertEqual(writes, [("POST", "git/blobs")] * 3 + [("POST", "git/trees"),
                                                                 ("POST", "git/commits"),
                                                                 ("POST", "git/refs")])
        self.assertFalse(any("pulls" in r.path or "merges" in r.path or "heads/master" in r.path
                             for r in self.fake.requests))
        self.assertIn(f"info: registry branch {BRANCH}: smbios-c {VERSION} port-version 0, "
                      f"git-tree {self.git_tree[:12]}, REF {COMMIT[:12]}, 2 entries, base "
                      f"{self.fake.base_commit[:12]}; no PR opened (RP-9)", run.stdout)
        self.assertRegex(run.stdout, rf"ok: publish vcpkg-registry branch: branch {BRANCH} at "
                                     rf"{commit[:12]}; open the PR: https://github\.com/{REPO}/"
                                     rf"compare/master\.\.\.{re.escape(BRANCH)} \(")

    def test_ExistingBranchIsAHardFailureWithNoWrite(self) -> None:
        self.fake.heads[BRANCH] = "f" * 40
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn(f"branch {BRANCH}: expected none -> actual exists (create-only", run.stdout)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.heads[BRANCH], "f" * 40)

    def test_RefCreateRaceIsAFailureNotAnUpdate(self) -> None:
        """Another run created the branch between the check and the write: 422, never PATCH."""
        def race() -> None:
            self.fake.heads[BRANCH] = "f" * 40
        self.fake.on_commit = race
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("POST git refs: expected 201 -> actual 422", run.stdout)
        self.assertFalse(any(r.method == "PATCH" for r in self.fake.requests))

    def test_PreReleaseTagIsRefusedBeforeAnyRequest(self) -> None:
        tag = f"v{VERSION}-rc.1"
        self.fake.tags[tag] = {"type": "commit", "sha": COMMIT}
        run = branch_run(self, CI_REF=f"refs/tags/{tag}", CI_REF_NAME=tag)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn(f"actual pre-release {tag} (constraint 8", run.stdout)
        self.assertEqual(self.fake.requests, [])


class PreflightTests(_RegistryCase):
    """Type: mock. Constraint 5: one release, one commit, re-checked before the ref write."""

    def test_TagMovedBeforeTheRefWriteIsRefused(self) -> None:
        def move() -> None:
            self.fake.tags[TAG] = {"type": "commit", "sha": OTHER}
        self.fake.on_commit = move
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("(the tag moved)", run.stdout)
        self.assertNotIn(BRANCH, self.fake.heads)

    def test_BaseMustDescendFromTheReleaseAndBeOnMaster(self) -> None:
        for key, needle in ((f"{COMMIT}...{self.fake.base_commit}", "release commit in the registry "
                                                             "base"),
                            (f"{self.fake.base_commit}...master", "registry base on master")):
            with self.subTest(key=key):
                self.fake.compares = {key: "diverged"}
                run = branch_run(self)
                self.assertEqual(run.rc, 1, run.text)
                self.assertIn(f"{needle}: expected", run.stdout)
                self.assertIn("-> actual diverged", run.stdout)
                self.assertEqual(self.fake.writes(), [])

    def test_ManifestCommitMustBeTheRef(self) -> None:
        """REF is manifest.commit: a hand-over whose REF is another commit is refused."""
        other = dict(self.new)
        other[ru.PORTFILE] = ru.set_portfile_ref(self.fake.base_files[ru.PORTFILE], OTHER)
        tree = ru.port_tree_sha(self.fake.port_entries, other[ru.PORTFILE])
        other.update(ru.simulate_add_version(self.fake.base_files, version=VERSION,
                                             port_version=0, git_tree=tree))
        self.write_handoff(other, git_tree=tree)
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn(f"expected the base portfile with only REF set to {COMMIT[:12]}",
                      run.stdout)
        self.assertEqual(self.fake.writes(), [])


class HandoffTests(_RegistryCase):
    """Type: mock + invalid input. RP-2: the unprivileged job's output is checked, not trusted."""

    def refused(self, needle: str) -> None:
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn(needle, run.stdout)
        self.assertEqual(self.fake.writes(), [])

    def test_TamperedPortfileWithAMatchingDigestIsRefused(self) -> None:
        evil = dict(self.new)
        evil[ru.PORTFILE] = self.new[ru.PORTFILE] + b"execute_process(COMMAND sh -c evil)\n"
        tree = ru.port_tree_sha(self.fake.port_entries, evil[ru.PORTFILE])
        evil.update(ru.simulate_add_version(self.fake.base_files, version=VERSION,
                                            port_version=0, git_tree=tree))
        self.write_handoff(evil, git_tree=tree)
        self.refused("expected the base portfile with only REF set")

    def test_RewrittenRegisteredEntryIsRefused(self) -> None:
        rows = json.loads(self.new[ru.VERSIONS])["versions"]
        rows[1]["git-tree"] = "e" * 40
        self.write_handoff(dict(self.new, **{ru.VERSIONS: json.dumps({"versions": rows})
                                             .encode()}))
        self.refused("expected every registered entry unchanged")

    def test_WrongGitTreeIsRefused(self) -> None:
        self.write_handoff(self.new, git_tree="e" * 40)
        self.refused(f"git-tree: expected {self.git_tree[:12]} (computed from the base) -> "
                     "actual eeeeeeeeeeee")

    def test_DigestMismatchIsRefusedBeforeAnyRequest(self) -> None:
        digests = ru.file_digests(self.new)
        self.write_handoff(self.new)
        (self.tmp / "registry" / ru.BASELINE).write_bytes(b'{"default": {}}\n')
        self.prepared = json.dumps({"base": self.fake.base_commit, "git_tree": self.git_tree,
                                    "files": digests})
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn(f"{ru.BASELINE} sha256: expected {digests[ru.BASELINE][:16]} "
                      "(registry-prepare output)", run.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_MissingOrMalformedPreparedOutputIsRefused(self) -> None:
        for value, needle in (("", "expected the registry-prepare job's output -> actual unset"),
                              ("{", "CI_REGISTRY_PREPARED: expected JSON")):
            with self.subTest(value=value):
                self.prepared = value
                run = branch_run(self)
                self.assertEqual(run.rc, 1, run.text)
                self.assertIn(needle, run.stdout)
        self.assertEqual(self.fake.requests, [])


class SettingsTests(_RegistryCase):
    """Type: mock. RP-9 / S-6 drift: readable and wrong fails; unreadable is one info line."""

    def test_ActionsMayApprovePullRequestsFails(self) -> None:
        self.fake.workflow_perms = {"can_approve_pull_request_reviews": True,
                                    "default_workflow_permissions": "read"}
        run = branch_run(self)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("Actions may approve pull requests (S-6): expected false -> actual true",
                      run.stdout)
        self.assertEqual(self.fake.writes(), [])

    def test_UnreadableIsAnInfoLineAndGoodPasses(self) -> None:
        run = branch_run(self)
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("manual read-back required (threat model S-6)", run.stdout)
        self.fake.heads.clear()
        self.fake.workflow_perms = {"can_approve_pull_request_reviews": False,
                                    "default_workflow_permissions": "read"}
        run = branch_run(self)
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("S-6 read back as required", run.stdout)


class CanaryTests(_RegistryCase):
    """Type: mock. Constraint 3 / RP-8: no form of the canary anywhere, on every path."""

    def assertNoLeak(self, run: StageRun) -> None:
        blob = run.everything()
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, blob)
        for rec in self.fake.requests:
            self.assertEqual(rec.server, "api")
            self.assertEqual(rec.auth, "Bearer " + CANARY)

    def test_SuccessAndErrorPaths(self) -> None:
        run = branch_run(self)
        self.assertEqual(run.rc, 0, run.text)
        self.assertNoLeak(run)
        for method, suffix, status in (("POST", "/git/blobs", 500), ("POST", "/git/trees", 422),
                                       ("GET", "/git/blobs/" + ru.git_blob_sha(
                                           self.fake.base_files[ru.VERSIONS]), 401)):
            with self.subTest(path=suffix, status=status):
                self.fake.heads.clear()
                self.fake.override = (lambda m, p, mm=method, s=suffix, st=status:
                                      st if m == mm and p.endswith(s) else None)
                run = branch_run(self)
                self.assertEqual(run.rc, 1, run.text)
                self.assertRegex(run.stdout, rf"expected 20[01] -> actual {status}")
                self.assertNotIn("evil", run.everything())
                self.assertNotIn("denied for", run.everything())
                self.assertNoLeak(run)

    def test_CrashPrintsOnlyTheExceptionType(self) -> None:
        def crash(*_a: Any, **_k: Any) -> str:
            raise RuntimeError(f"boom {CANARY} {LEAK_FORMS[1]}")
        with mock.patch.object(pvr, "branch", crash):
            run = branch_run(self)
        self.assertEqual(run.rc, 70, run.text)
        self.assertIn("internal error (RuntimeError)", run.text)
        self.assertNoLeak(run)


class StageGateTests(_RegistryCase):
    """Type: integration (in-process stage). Gates refuse before any request."""

    def test_RealRegistryShipsVcpkgRegistryDisabled(self) -> None:
        """Constraint 12: shipped disabled until S-1..S-8 are read back."""
        entry = stage.load_destination("vcpkg-registry")
        self.assertEqual((entry["enabled"], entry["tier"], entry["environment"],
                          entry["permission_class"], entry["credentials"]),
                         (False, "release", "release", "repo-write", ["CI_PUBLISH_TOKEN"]))
        self.assertEqual(pvr.PHASES, ("prepare", "branch"))
        self.assertEqual(pvr.UNPRIVILEGED_PHASES, ("prepare",))

    def test_DisabledAndVerifyGateRefuse(self) -> None:
        run = registry_stage(self, ["--only", "branch"], enabled=False)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("actual disabled (threat model constraint 12", run.stdout)
        run = branch_run(self, CI_VERIFIED_MANIFEST_SHA256="")
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("no verify result", run.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_PhasesAreChosenPerDestination(self) -> None:
        for destination, phase in (("vcpkg-registry", "draft"), ("github-release", "branch"),
                                   ("github-release", "prepare")):
            with self.subTest(destination=destination, phase=phase):
                run = registry_stage(self, ["--only", phase], destination=destination)
                self.assertEqual(run.rc, 2, run.text)
                self.assertIn("--only must be one of", run.stderr)
        self.assertEqual(self.fake.requests, [])


class DryRunTests(_RegistryCase):
    """Type: unit. D9 ``--dry-run``: every action listed, no network, credential unread."""

    def test_DryRunAgainstTheRealWorkingTree(self) -> None:
        def no_network(*_a: Any, **_k: Any) -> None:
            raise AssertionError("network or credential used in a dry run")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=no_network), \
                mock.patch.object(pgr, "Secret", side_effect=no_network), \
                mock.patch.object(subprocess, "Popen", side_effect=no_network):
            run = registry_stage(self, ["--dry-run"], enabled=False)
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("registry-prepare (contents: read, no credential)", run.stdout)
        self.assertIn("registry-pr (contents: write, REST only)", run.stdout)
        self.assertIn(f"git/refs refs/heads/{BRANCH}\n", run.stdout)
        self.assertIn(f"never opens or approves a PR (RP-9); the maintainer opens it from "
                      f"https://github.com/{REPO}/compare/master...{BRANCH}\n", run.stdout)
        import registry_prepare
        self.assertEqual(pvr.rp_args(), registry_prepare.ADD_VERSION_ARGS)
        self.assertIn("would run vcpkg " + " ".join(pvr.rp_args()) + " (never overwriting)\n",
                      run.stdout)
        import reporters
        self.assertTrue(all(len(line) < reporters.INFO_MAX for line in run.stdout.splitlines()),
                        "a dry-run line reached the reporter cap and was cut")
        self.assertNotIn("--" + "overwrite-version", run.stdout)
        self.assertEqual(self.fake.requests, [])
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, run.everything())

    def test_DryRunOfABumpedTreeShowsTheAcceptedChange(self) -> None:
        tree = self.tmp / "tree"
        for rel, data in self.fake.base_files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(data)
        run = registry_stage(self, ["--dry-run", "--only", "prepare"], enabled=False,
                             CI_WORKSPACE=str(tree), CI_PUBLISH_TOKEN="")
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn(f"would register smbios-c {VERSION} port-version 0, git-tree "
                      f"{self.git_tree[:12]}", run.stdout)
        self.assertIn("the judge accepts the simulated change", run.stdout)

    def test_PreReleaseDryRunListsNothing(self) -> None:
        tag = f"v{VERSION}-rc.1"
        run = registry_stage(self, ["--dry-run"], enabled=False, CI_REF=f"refs/tags/{tag}",
                             CI_REF_NAME=tag)
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("is a pre-release: no registry-prepare or registry-pr", run.stdout)
        self.assertNotIn("would POST", run.stdout)


if __name__ == "__main__":
    unittest.main()
