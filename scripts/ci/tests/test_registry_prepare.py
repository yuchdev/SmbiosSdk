"""Tests for ``scripts/ci/registry_prepare.py``, the unprivileged ``registry-prepare`` phase (0012 Story 04.0 Task 03).

Classification: integration. Each test builds a throwaway **upstream**
repository in the shape this repository has after a release commit (a
registered ``1.0.0`` at its source commit, then ``prepare_release.py 1.0.1``:
port at ``1.0.1``, baseline still ``1.0.0``, i.e. pending registration), clones
it as the job's **workspace** at the release commit, and runs
:func:`registry_prepare.run` with the real ``git``. ``vcpkg x-add-version`` is
replaced by a Python stand-in that writes what vcpkg writes
(:func:`registry_update.simulate_add_version`; the real tool is exercised only
on CI), and the consumer test by a stub. No network: ``origin`` is a local path.

Scenarios (threat model ``docs/security/2026-11-18-release-and-publish-automation.md``):
the normal hand-over; constraint 5 (``REF`` = ``manifest.commit``); constraint 8
(pre-release refused before any git); constraint 9 (already registered,
x-add-version overwriting an entry, extra files); the release commit not on
master; a failing consumer test leaves no hand-over; nothing is ever pushed.

On failure first check: the stand-in ``fake_add_version`` against what
``vcpkg x-add-version`` really writes, then ``registry_prepare.run``'s step order.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ci_test_support import CI_DIR

import check_vcpkg_registry as reg  # noqa: E402
import publish_inputs as pin  # noqa: E402
import registry_prepare as rp  # noqa: E402
import registry_update as ru  # noqa: E402
from test_publish_github_release import inputs_for, make_dist  # noqa: E402

REPO_ROOT = CI_DIR.parents[1]


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                           "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                          check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fake_add_version(clone: Path, _vcpkg_root: Optional[Path]) -> None:
    """What ``vcpkg x-add-version smbios-c`` writes (reads the committed port from git)."""
    tree = rp.git(clone, "rev-parse", f"HEAD:{ru.PORT_DIR}").strip()
    base = {rel: (clone / rel).read_bytes() for rel in (ru.BASELINE, ru.VERSIONS)}
    _v, pv = ru.port_version_of((clone / ru.PORT_MANIFEST).read_bytes())
    version = json.loads((clone / ru.PORT_MANIFEST).read_text())["version"]
    for rel, data in ru.simulate_add_version(base, version=version, port_version=pv,
                                             git_tree=tree).items():
        (clone / rel).write_bytes(data)


def fake_smoke(_work: Path, _root: Optional[Path], clone: Path) -> List[str]:
    return ["structures=32", "symbols=13", "installed=10", f"head={rp.git(clone, 'rev-parse', 'HEAD')[:7]}"]


class Upstream:
    """``master`` after a release commit, plus the workspace checkout of that commit."""

    def __init__(self, tmp: Path, *, bump: bool = True, side: bool = False,
                 register: bool = False) -> None:
        self.upstream = tmp / "upstream"
        self.upstream.mkdir()
        sh(self.upstream, "init", "-q")
        sh(self.upstream, "symbolic-ref", "HEAD", "refs/heads/master")
        files = {rel: (REPO_ROOT / rel).read_bytes()
                 for rel in (*ru.CHANGED_FILES, ru.PORT_MANIFEST)}
        write(self.upstream, "src.txt", b"one\n")
        self.source = self.commit("source 1.0.0")
        # 1.0.0 registered at self.source, as the release procedure does it.
        port = files[ru.PORTFILE]
        write(self.upstream, ru.PORTFILE, ru.set_portfile_ref(port, self.source))
        write(self.upstream, ru.PORT_MANIFEST, files[ru.PORT_MANIFEST])
        write(self.upstream, ru.BASELINE, files[ru.BASELINE])
        write(self.upstream, ru.VERSIONS, b'{"versions": []}\n')
        self.commit("port 1.0.0")
        tree = sh(self.upstream, "rev-parse", f"HEAD:{ru.PORT_DIR}")
        self.tree_100 = tree
        write(self.upstream, ru.VERSIONS, json.dumps({"versions": [
            {"git-tree": tree, "version": "1.0.0", "port-version": 0}]}, indent=2).encode())
        self.commit("versions 1.0.0")
        if side:
            sh(self.upstream, "checkout", "-q", "-b", "side")
        port_json = json.loads(files[ru.PORT_MANIFEST])
        port_json["version"] = "1.0.1" if bump else "1.0.0"
        write(self.upstream, ru.PORT_MANIFEST, (json.dumps(port_json, indent=2) + "\n").encode())
        write(self.upstream, "src.txt", b"two\n")
        self.release = self.commit("release 1.0.1 (prepare_release.py)")
        if side:
            sh(self.upstream, "checkout", "-q", "master")
        if register:  # someone already registered 1.0.1 on master
            write(self.upstream, ru.PORTFILE, ru.set_portfile_ref(
                (self.upstream / ru.PORTFILE).read_bytes(), self.release))
            self.commit("port 1.0.1")
            fake_add_version(self.upstream, None)
            self.commit("versions 1.0.1")
        sh(self.upstream, "tag", "v1.0.1", self.release)
        self.workspace = tmp / "workspace"
        sh(tmp, "clone", "-q", str(self.upstream), str(self.workspace))
        sh(self.workspace, "checkout", "-q", "--detach", self.release)
        self.refs_before = sh(self.upstream, "for-each-ref", "--format=%(refname) %(objectname)")

    def commit(self, message: str) -> str:
        sh(self.upstream, "add", "-A")
        sh(self.upstream, "commit", "-q", "-m", message)
        return sh(self.upstream, "rev-parse", "HEAD")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-registry-prepare-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def setup_repo(self, **kw) -> Upstream:
        self.up = Upstream(self.tmp, **kw)
        self.anchor = make_dist(self.tmp, version="1.0.1", commit=self.up.release)
        return self.up

    def prepare(self, *, tag: str = "v1.0.1", add_version: Callable = fake_add_version,
                smoke: Callable = fake_smoke):
        inputs = inputs_for(self.tmp, self.anchor, tag=tag, commit=self.up.release)
        return rp.run(workspace=self.up.workspace, temp_dir=self.tmp / "temp",
                      artifact_dir=self.tmp / "artifacts", inputs=inputs, vcpkg_root=None,
                      tools=rp.Tools(add_version=add_version, smoke=smoke))

    def refused(self, needle: str, **kw) -> str:
        with self.assertRaises(rp.PrepareError) as cm:
            self.prepare(**kw)
        text = str(cm.exception).splitlines()[0]
        self.assertIn(needle, text)
        self.assertFalse((self.tmp / "artifacts" / "registry").exists(), "no hand-over on failure")
        self.assertEqual(sh(self.up.upstream, "for-each-ref",
                            "--format=%(refname) %(objectname)"), self.up.refs_before)
        return text


class PrepareTests(_Case):
    """Type: integration (real git). Normal case and the hand-over contract."""

    def test_HandOverIsTheOneCheckedChange(self) -> None:
        up = self.setup_repo()
        (self.tmp / "temp").mkdir()
        lines, prepared = self.prepare()
        base = sh(up.upstream, "rev-parse", "master")
        self.assertEqual(prepared["base"], base)
        clone = self.tmp / "temp" / rp.CLONE_DIR
        self.assertEqual(prepared["git_tree"], sh(clone, "rev-parse", f"HEAD:{ru.PORT_DIR}"))
        out = self.tmp / "artifacts" / "registry"
        files = {rel: (out / rel).read_bytes() for rel in ru.CHANGED_FILES}
        self.assertEqual(prepared["files"], ru.file_digests(files))
        self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                                if p.is_file()), sorted(ru.CHANGED_FILES))
        self.assertEqual(ru.portfile_ref(files[ru.PORTFILE]), up.release)  # constraint 5
        rows = json.loads(files[ru.VERSIONS])["versions"]
        self.assertEqual([r["version"] for r in rows], ["1.0.1", "1.0.0"])
        self.assertEqual(rows[1]["git-tree"], up.tree_100)  # append-only
        self.assertEqual(json.loads(files[ru.BASELINE])["default"]["smbios-c"],
                         {"baseline": "1.0.1", "port-version": 0})
        self.assertTrue(lines[0].startswith("info: registry-prepare: smbios-c 1.0.1 "
                                            "port-version 0, git-tree "))
        self.assertIn("structures=32, symbols=13, installed=10", lines[1])
        # Nothing was pushed anywhere, and the result passes the full lint in the clone.
        self.assertEqual(sh(up.upstream, "for-each-ref", "--format=%(refname) %(objectname)"),
                         up.refs_before)
        self.assertEqual(reg.check(clone, master_ref=rp.BASE_REF), [])

    def test_ArgvNeverOverwrites(self) -> None:
        self.assertEqual(rp.ADD_VERSION_ARGS, ("x-add-version", "smbios-c",
                                               "--x-builtin-ports-root=./ports",
                                               "--x-builtin-registry-versions-dir=./versions",
                                               "--verbose"))
        tree = ast.parse((CI_DIR / "registry_prepare.py").read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings}
        self.assertFalse(any("--" + "overwrite" in lit for lit in literals))
        self.assertFalse(any("CI_PUBLISH" in lit or "push" == lit for lit in literals))


class RefusalTests(_Case):
    """Type: integration + invalid input. Each refusal leaves no hand-over and pushes nothing."""

    def test_PreReleaseTagIsRefusedBeforeAnyGit(self) -> None:
        self.setup_repo()
        calls: List[tuple] = []

        def no_git(*args: str) -> str:
            calls.append(args)
            raise AssertionError("git ran for a pre-release")
        inputs = inputs_for(self.tmp, self.anchor, tag="v1.0.1-rc.1", commit=self.up.release)
        with self.assertRaises(rp.PrepareError) as cm:
            rp.run(workspace=self.up.workspace, temp_dir=self.tmp, artifact_dir=self.tmp,
                   inputs=inputs, vcpkg_root=None, tools=rp.Tools(git=no_git))
        self.assertIn("actual pre-release v1.0.1-rc.1 (constraint 8)", str(cm.exception))
        self.assertEqual(calls, [])

    def test_PortNotBumpedInTheReleaseCommit(self) -> None:
        self.setup_repo(bump=False)
        self.refused("ports/smbios-c/vcpkg.json on master: expected 1.0.1 (manifest.version")

    def test_ReleaseCommitNotOnMaster(self) -> None:
        self.setup_repo(side=True)
        self.refused("in master: expected an ancestor of the fetched master")

    def test_AlreadyRegisteredVersion(self) -> None:
        self.setup_repo(register=True)
        self.refused("expected pending registration -> actual registered")

    def test_AddVersionTouchingAnotherFile(self) -> None:
        self.setup_repo()

        def extra(clone: Path, root: Optional[Path]) -> None:
            fake_add_version(clone, root)
            write(clone, "ports/smbios-c/usage", b"x\n")
        self.refused("vcpkg x-add-version: expected versions/baseline.json and "
                     "versions/s-/smbios-c.json changed -> actual 3 paths", add_version=extra)

    def test_AddVersionOverwritingAnEntry(self) -> None:
        self.setup_repo()

        def overwrite(clone: Path, root: Optional[Path]) -> None:
            fake_add_version(clone, root)
            data = json.loads((clone / ru.VERSIONS).read_text())
            data["versions"][1]["git-tree"] = "e" * 40
            (clone / ru.VERSIONS).write_text(json.dumps(data, indent=2))
        self.refused("expected every registered entry unchanged", add_version=overwrite)

    def test_FailingConsumerTestLeavesNoHandOver(self) -> None:
        self.setup_repo()

        def broken(*_a) -> List[str]:
            raise rp.PrepareError("registry consumer test: installed files: expected the 10 "
                                  "allowlisted files -> actual 1 extra, 0 missing (first: "
                                  "+lib/libsmbios.dylib)")
        self.refused("registry consumer test: installed files", smoke=broken)


class ErrorTypeTests(unittest.TestCase):
    """Type: unit. A prepare refusal is a gate failure (exit 1) in stage.py, not a crash."""

    def test_PrepareErrorIsAPublishInputError(self) -> None:
        self.assertTrue(issubclass(rp.PrepareError, pin.PublishInputError))


if __name__ == "__main__":
    unittest.main()
