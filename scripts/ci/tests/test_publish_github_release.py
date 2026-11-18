"""Tests for the publish stage and its ``github-release`` destination (0012 Story 04.0 Task 02).

Classification: unit + mock (the GitHub REST API is a stub HTTP server on
loopback; no real network, no real credential, no release is ever created).
The stub (:class:`FakeGitHub`) keeps releases, assets, tags and the master
comparison in memory, records every request with the ``Authorization`` header
it received, and **echoes that header into every error body and an error
header**, so any path that renders a response body or header would leak the
canary into what the test inspects. The canary is generated per run.

Scenarios map to the threat model ``docs/security/2026-11-18-release-and-publish-automation.md``:

- constraint 2 / RP-2: :class:`NoChildProcessTests` (AST: no ``subprocess``,
  no step runner; and a full publish run with ``subprocess.Popen`` patched to
  fail);
- constraint 3 / RP-8: :class:`CanaryTests` (success, 4xx, 5xx, redirect,
  network error, crash) and :class:`RedirectTests` (no ``Authorization`` on a
  redirect; a non-allowlisted host is never contacted);
- constraint 4 / RP-4: :class:`AnchorTests` (archive and manifest replaced
  together fail on ``manifest_sha256`` before parsing);
- constraint 5 / RP-5: :class:`TagAndCommitTests`, :class:`PreflightTests`;
- constraint 6 / RP-4, CR-2: :class:`BinaryCacheTests`;
- constraint 7 / RP-6, RP-12: :class:`CreateOnlyTests`, :class:`PromoteTests`;
- ADR 0005 D6 verify gate and the release-tier gates: :class:`StageGateTests`;
- D9 ``--dry-run``: :class:`DryRunTests`.

On failure first check: which request the stub recorded last
(``fake.requests``) and the RC1 line the stage printed; then
``publish_github_release.py`` for API behaviour or ``publish_inputs.py`` for
input refusals.

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
import secrets
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from ci_test_support import CI_DIR

import job_summary  # noqa: E402
import package_archive as pa  # noqa: E402
import publish_github_release as pgr  # noqa: E402
import publish_inputs as pin  # noqa: E402
import stage  # noqa: E402
from reporters import get_reporter  # noqa: E402

#: The canary credential: random per run, shaped like an installation token.
CANARY = "ghs_" + "CANARY" + secrets.token_hex(14)
#: Every form a leak could take: the value, its base64 forms, a distinctive slice.
LEAK_FORMS = (CANARY, base64.b64encode(CANARY.encode()).decode(),
              base64.b64encode(("x-access-token:" + CANARY).encode()).decode(),
              CANARY[10:26])
REPO = "octo/libbios"
COMMIT = "a1" * 20
OTHER = "b2" * 20
VERSION = "1.0.1"
TAG = "v1.0.1"
RUN_ID = "4242"
LEGS = ("linux-x64", "macos-arm64", "windows-x64")


# ---------------------------------------------------------------------------
# A dist/ the package stage would have produced
# ---------------------------------------------------------------------------

def make_dist(root: Path, *, version: str = VERSION, commit: str = COMMIT,
              binary_cache: Optional[str] = "none", extra_leg_key: Optional[str] = None) -> str:
    """Write three archives, their sidecars and manifest.json; return the manifest sha256."""
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    legs = []
    for leg in LEGS:
        name = pa.archive_name(version, leg)
        data = hashlib.sha256(leg.encode()).digest() * 40 + leg.encode()
        (dist / name).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        (dist / (name + ".sha256")).write_bytes(pin.sidecar_text(sha, name))
        key = pa.PLATFORM_KEYS[pa.leg_os(leg)]
        entry: Dict[str, Any] = {"leg": leg, "file": name, "sha256": sha, "bytes": len(data),
                                 "config": "Release", "install_sha256": "c" * 64, "files": 78,
                                 "arch": leg.split("-")[1],
                                 key: {"glibc": "2.35", "msvc": "14.44", "macos_min": "12.7"}[key]}
        if binary_cache is not None:
            entry["binary_cache"] = binary_cache
        if extra_leg_key:
            entry[extra_leg_key] = "x"
        legs.append(entry)
    manifest = {"schema": 1, "version": version, "commit": commit, "legs": legs}
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (dist / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def inputs_for(root: Path, anchor: str, tag: str = TAG, commit: str = COMMIT,
               run_id: str = RUN_ID) -> pin.ReleaseInputs:
    return pin.build_inputs(dist=root / "dist", manifest_sha256=anchor, ref_name=tag,
                            commit=commit, run_id=run_id, packaging_legs=LEGS)


# ---------------------------------------------------------------------------
# The stub GitHub
# ---------------------------------------------------------------------------

class Recorded:
    """One request the stub received."""

    def __init__(self, server: str, method: str, path: str, auth: Optional[str]) -> None:
        self.server, self.method, self.path, self.auth = server, method, path, auth

    def __repr__(self) -> str:  # never shows the header value
        return f"{self.server} {self.method} {self.path} auth={'yes' if self.auth else 'no'}"


class FakeGitHub:
    """In-memory GitHub REST stub: ``api`` (API + uploads), ``download`` and ``evil`` servers."""

    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self.releases: Dict[int, Dict[str, Any]] = {}
        self.assets: Dict[int, Dict[str, Any]] = {}
        self.tags: Dict[str, Dict[str, str]] = {TAG: {"type": "commit", "sha": COMMIT}}
        self.annotated: Dict[str, Dict[str, Any]] = {}
        self.compare: Dict[str, Any] = {"status": "ahead", "merge_base_commit": {"sha": COMMIT}}
        self.settings = "forbidden"            # forbidden | good | no-immutable
        self.immutable_field: Optional[bool] = True
        self.override: Callable[[str, str], Optional[int]] = lambda m, p: None
        self.download_to = "download"          # where asset downloads redirect
        self.api_redirect: Optional[str] = None
        self.tamper: Callable[[Dict[str, Any]], None] = lambda asset: None
        self._next = 100
        self.lock = threading.Lock()
        self.servers: Dict[str, ThreadingHTTPServer] = {}
        for name in ("api", "download", "evil"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(name))
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                             daemon=True).start()
            self.servers[name] = server

    def close(self) -> None:
        for server in self.servers.values():
            server.shutdown()
            server.server_close()

    def base(self, name: str) -> str:
        return f"http://127.0.0.1:{self.servers[name].server_address[1]}"

    def netloc(self, name: str) -> str:
        return f"127.0.0.1:{self.servers[name].server_address[1]}"

    def policy(self) -> pgr.HostPolicy:
        return pgr.HostPolicy(api_base=self.base("api"), upload_base=self.base("api"),
                              download_hosts=frozenset({self.netloc("download")}), scheme="http")

    def writes(self) -> List[Recorded]:
        return [r for r in self.requests if r.method in ("POST", "PATCH", "DELETE", "PUT")]

    def new_id(self) -> int:
        self._next += 1
        return self._next

    def add_release(self, *, draft: bool, body: str, tag: str = TAG, commit: str = COMMIT,
                    prerelease: bool = False) -> Dict[str, Any]:
        rid = self.new_id()
        rel = {"id": rid, "tag_name": tag, "target_commitish": commit, "draft": draft,
               "prerelease": prerelease, "body": body, "name": "x",
               "html_url": f"https://github.com/{REPO}/releases/tag/untagged-{rid}"}
        self.releases[rid] = rel
        return rel

    def add_asset(self, rid: int, name: str, data: bytes) -> Dict[str, Any]:
        aid = self.new_id()
        asset = {"id": aid, "release": rid, "name": name, "size": len(data), "data": data,
                 "state": "uploaded"}
        self.assets[aid] = asset
        return asset

    # -- HTTP -----------------------------------------------------------------
    def _handler(self, server_name: str):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Headers and body are separate writes: without this, Nagle plus the
            # client's delayed ACK costs ~200 ms per request.
            disable_nagle_algorithm = True

            def log_message(self, *args: Any) -> None:  # keep test stderr clean
                pass

            def _send(self, status: int, obj: Any = None, raw: Optional[bytes] = None,
                      headers: Optional[Dict[str, str]] = None) -> None:
                auth = self.headers.get("Authorization") or ""
                if status >= 400 and obj is None:
                    obj = {"message": f"denied for {auth}",
                           "errors": [{"code": "already_exists", "message": auth},
                                      {"code": f"evil {auth}"}]}
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                if status >= 400:
                    self.send_header("X-Echo", auth)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def handle_one(self, method: str) -> None:
                url = urlsplit(self.path)
                with fake.lock:
                    fake.requests.append(Recorded(server_name, method, url.path,
                                                  self.headers.get("Authorization")))
                body = self._body()
                if server_name == "evil":
                    return self._send(200, {"evil": True})
                if server_name == "download":
                    m = re.fullmatch(r"/blob/(\d+)", url.path)
                    asset = fake.assets.get(int(m.group(1))) if m else None
                    if asset is None:
                        return self._send(404)
                    return self._send(200, raw=asset["data"])
                status = fake.override(method, url.path)
                if status is not None:
                    return self._send(status)
                if fake.api_redirect and method == "GET":
                    return self._send(302, {}, headers={"Location": fake.api_redirect})
                return fake.route(self, method, url.path, parse_qs(url.query), body)

            def do_GET(self) -> None:  # noqa: N802
                self.handle_one("GET")

            def do_POST(self) -> None:  # noqa: N802
                self.handle_one("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self.handle_one("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self.handle_one("DELETE")

        return Handler

    def route(self, h: Any, method: str, path: str, query: Dict[str, List[str]],
              body: bytes) -> None:
        prefix = f"/repos/{REPO}/"
        if not path.startswith(prefix):
            return h._send(404)
        rest = path[len(prefix):]
        if method == "GET" and rest.startswith("git/ref/tags/"):
            tag = rest[len("git/ref/tags/"):]
            obj = self.tags.get(tag)
            return h._send(200, {"ref": f"refs/tags/{tag}", "object": obj}) if obj \
                else h._send(404)
        if method == "GET" and rest.startswith("git/tags/"):
            obj = self.annotated.get(rest[len("git/tags/"):])
            return h._send(200, {"object": obj}) if obj else h._send(404)
        if method == "GET" and rest.startswith("compare/"):
            return h._send(200, self.compare)
        if method == "GET" and rest.startswith(("rulesets", "environments", "immutable")):
            return self.settings_route(h, rest)
        if method == "GET" and rest == "releases":
            page = int(query.get("page", ["1"])[0])
            items = list(self.releases.values()) if page == 1 else []
            return h._send(200, [dict(r) for r in items])
        if method == "POST" and rest == "releases":
            data = json.loads(body)
            rel = self.add_release(draft=data["draft"], body=data["body"], tag=data["tag_name"],
                                   commit=data["target_commitish"],
                                   prerelease=data["prerelease"])
            return h._send(201, rel)
        m = re.fullmatch(r"releases/(\d+)/assets", rest)
        if m and method == "GET":
            rid = int(m.group(1))
            out = []
            for a in self.assets.values():
                if a["release"] == rid:
                    view = {"id": a["id"], "name": a["name"], "size": a["size"],
                            "state": a["state"]}
                    self.tamper(view)
                    out.append(view)
            return h._send(200, out)
        if m and method == "POST":
            rid, name = int(m.group(1)), query["name"][0]
            if any(a["release"] == rid and a["name"] == name for a in self.assets.values()):
                return h._send(422)
            asset = self.add_asset(rid, name, body)
            return h._send(201, {"id": asset["id"], "name": name, "size": len(body)})
        m = re.fullmatch(r"releases/assets/(\d+)", rest)
        if m and method == "GET":
            target = self.base(self.download_to)
            return h._send(302, {}, headers={"Location": f"{target}/blob/{m.group(1)}"})
        if m and method == "DELETE":
            self.assets.pop(int(m.group(1)), None)
            return h._send(204, raw=b"")
        m = re.fullmatch(r"releases/(\d+)", rest)
        if m and method == "PATCH":
            rel = self.releases[int(m.group(1))]
            rel.update({k: v for k, v in json.loads(body).items()
                        if k in ("draft", "prerelease")})
            out = dict(rel)
            if self.immutable_field is not None:
                out["immutable"] = self.immutable_field
            return h._send(200, out)
        return h._send(404)

    def settings_route(self, h: Any, rest: str) -> None:
        if self.settings == "forbidden":
            return h._send(403)
        if rest.startswith("rulesets/"):
            return h._send(200, {"conditions": {"ref_name": {"include": ["refs/tags/v*"]}},
                                 "rules": [{"type": t} for t in ("creation", "update",
                                                                 "deletion")]})
        if rest.startswith("rulesets"):
            return h._send(200, [{"id": 7, "target": "tag", "enforcement": "active"}])
        if rest == "environments/release":
            return h._send(200, {"protection_rules": [{"type": "required_reviewers",
                                                       "reviewers": [{"type": "User"}]}],
                                 "deployment_branch_policy": {"protected_branches": False,
                                                              "custom_branch_policies": True},
                                 "can_admins_bypass": False})
        if rest.startswith("environments/release/deployment-branch-policies"):
            return h._send(200, {"branch_policies": [{"name": "v*", "type": "tag"}]})
        if rest == "immutable-releases":
            return h._send(200, {"enabled": self.settings != "no-immutable"})
        return h._send(404)


class _FakeCase(unittest.TestCase):
    """A temp dist/ plus a running stub; helpers to run phases against it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-publish-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.anchor = make_dist(self.tmp)
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.logged: List[str] = []

    def api(self, policy: Optional[pgr.HostPolicy] = None) -> pgr.GitHubApi:
        return pgr.GitHubApi(pgr.Secret(CANARY), REPO, policy or self.fake.policy(),
                             timeout_s=10)

    def inputs(self, **kw: Any) -> pin.ReleaseInputs:
        return inputs_for(self.tmp, self.anchor, **kw)

    def draft(self, **kw: Any) -> str:
        return pgr.draft(self.api(), self.inputs(**kw), self.logged.append)

    def promote(self, **kw: Any) -> str:
        return pgr.promote(self.api(), self.inputs(**kw), self.logged.append)

    def refused(self, fn: Callable[[], Any], needle: str) -> str:
        """Run ``fn``: it must raise PublishError naming ``needle`` in RC1 form, value-free."""
        with self.assertRaises(pgr.PublishError) as cm:
            fn()
        text = str(cm.exception)
        self.assertIn(needle, text)
        self.assertRegex(text, r": expected .+ -> actual .+")
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, text)
        return text


# ---------------------------------------------------------------------------
# Stage-level runs (in-process, so the stub's loopback policy can be injected)
# ---------------------------------------------------------------------------

class StageRun:
    """Result of one in-process ``stage.py publish`` run."""

    def __init__(self, rc: int, stdout: str, stderr: str, results: str, summary: str) -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.results, self.summary = results, summary

    @property
    def text(self) -> str:
        return self.stdout + self.stderr

    def everything(self) -> str:
        return "\n".join((self.stdout, self.stderr, self.results, self.summary))


def stage_env(case: _FakeCase, **extra: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    env.update({"CI_PLATFORM": "github", "CI_REPORTER": "plain", "CI_EVENT": "tag",
                "CI_REF": f"refs/tags/{TAG}", "CI_REF_NAME": TAG, "CI_COMMIT": COMMIT,
                "CI_RUN_ID": RUN_ID, "CI_RUN_ATTEMPT": "1", "CI_REPOSITORY": REPO,
                "CI_TEMP_DIR": str(case.tmp), "CI_ARTIFACT_DIR": str(case.tmp),
                "CI_LOG_DIR": str(case.tmp / "logs"),
                "CI_RESULTS_FILE": str(case.tmp / "results.jsonl"),
                "CI_SUMMARY_FILE": str(case.tmp / "summary.md"),
                "CI_MANIFEST_SHA256": case.anchor, "CI_VERIFIED_MANIFEST_SHA256": case.anchor,
                "CI_BUILD_BINARY_CACHE": json.dumps({leg: "none" for leg in LEGS}),
                "CI_PUBLISH_TOKEN": CANARY})
    env.update(extra)
    return env


def registry(case: _FakeCase, enabled: bool = True) -> Path:
    path = case.tmp / "publish_destinations.json"
    path.write_text(json.dumps({"schema": 1, "destinations": [
        {"name": "github-release", "enabled": enabled, "tier": "release",
         "permission_class": "repo-write", "environment": "release",
         "credentials": ["CI_PUBLISH_TOKEN"]}]}), encoding="utf-8")
    return path


def run_stage(case: _FakeCase, args: List[str], *, enabled: bool = True,
              policy: Optional[pgr.HostPolicy] = None, **env: str) -> StageRun:
    """Run ``stage.main(["publish", "--destination", "github-release", *args])`` in-process.

    Afterwards the job summary is rendered from the RC3 records exactly as the
    workflow's summary step would, so its file and artifact can be scanned too.
    """
    out, err = io.StringIO(), io.StringIO()
    bound = functools.partial(pgr.GitHubApi, policy=policy or case.fake.policy(), timeout_s=10)
    with mock.patch.dict(os.environ, stage_env(case, **env), clear=True), \
            mock.patch.object(pgr, "GitHubApi", bound), \
            mock.patch.object(stage, "DESTINATIONS_FILE", registry(case, enabled)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = stage.main(["publish", "--destination", "github-release", *args])
        case.assertNotIn("CI_PUBLISH_TOKEN", os.environ, "the credential must leave the env")
        job_summary.run(get_reporter("github"), job="build", leg="linux",
                        results=case.tmp / "results.jsonl", temp_dir=case.tmp,
                        artifact_dir=case.tmp)

    def read(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    summary = read(case.tmp / "summary.md") + read(case.tmp / "summary-build-linux.json")
    return StageRun(rc, out.getvalue(), err.getvalue(), read(case.tmp / "results.jsonl"),
                    summary)


# ---------------------------------------------------------------------------
# Constraint 2: no child process, in-process REST only
# ---------------------------------------------------------------------------

class NoChildProcessTests(_FakeCase):
    """Type: unit (AST) + integration. Constraint 2, RP-2: the credential never reaches a child."""

    FORBIDDEN = {"subprocess", "ci_step", "os.system", "os.popen", "os.exec", "pty",
                 "multiprocessing", "shlex", "exec", "eval", "__import__"}

    def test_DestinationModulesImportNoSubprocessOrStepRunner(self) -> None:
        for name in ("publish_github_release.py", "publish_inputs.py"):
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

    def test_FullDraftRunStartsNoChildProcess(self) -> None:
        def no_child(*_a: Any, **_k: Any) -> None:
            raise AssertionError("a child process was started on the publish path")
        with mock.patch.object(subprocess, "Popen", side_effect=no_child), \
                mock.patch.object(os, "system", side_effect=no_child):
            run = run_stage(self, ["--only", "draft"])
        self.assertEqual(run.rc, 0, run.text)


# ---------------------------------------------------------------------------
# Constraint 3: the credential never leaks
# ---------------------------------------------------------------------------

class CanaryTests(_FakeCase):
    """Type: mock. Constraint 3 / RP-8: no form of the canary anywhere, on every path."""

    def assertNoLeak(self, run: StageRun) -> None:
        blob = run.everything()
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, blob)
        for rec in self.fake.requests:
            if rec.server != "api":
                self.assertIsNone(rec.auth, f"credential sent to {rec}")
            else:
                self.assertEqual(rec.auth, "Bearer " + CANARY)

    def test_SuccessDraftThenPromote(self) -> None:
        run = run_stage(self, ["--only", "draft"])
        self.assertEqual(run.rc, 0, run.text)
        self.assertRegex(run.stdout, r"ok: publish github-release draft: draft release v1\.0\.1: "
                                     r"7 assets uploaded and re-read")
        self.assertNoLeak(run)
        run = run_stage(self, ["--only", "promote"])
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("promoted release v1.0.1: 7 assets re-read before publish", run.stdout)
        self.assertNoLeak(run)
        self.assertTrue(any(r.server == "download" for r in self.fake.requests))

    def test_4xxAnd5xxRenderOnlyStatusAndAllowlistedCodes(self) -> None:
        for status in (422, 500, 401):
            with self.subTest(status=status):
                self.fake.override = (lambda m, p, s=status:
                                      s if m == "POST" and p.endswith("/releases") else None)
                run = run_stage(self, ["--only", "draft"])
                self.assertEqual(run.rc, 1, run.text)
                self.assertIn(f"POST releases: expected 201 -> actual {status} (already_exists)",
                              run.stdout)
                self.assertNotIn("evil", run.everything())
                self.assertNotIn("denied for", run.everything())
                self.assertNoLeak(run)

    def test_RedirectAndNetworkErrorPaths(self) -> None:
        self.fake.download_to = "evil"
        run = run_stage(self, ["--only", "draft"])
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("redirect refused", run.stdout)
        self.assertEqual([r for r in self.fake.requests if r.server == "evil"], [])
        self.assertNoLeak(run)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        dead = pgr.HostPolicy(api_base=f"http://127.0.0.1:{port}",
                              upload_base=f"http://127.0.0.1:{port}", scheme="http")
        run = run_stage(self, ["--only", "draft"], policy=dead)
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("network error", run.stdout)
        self.assertNoLeak(run)

    def test_CrashPrintsOnlyTheExceptionType(self) -> None:
        def crash(*_a: Any, **_k: Any) -> str:
            raise RuntimeError(f"boom {CANARY} {LEAK_FORMS[1]}")
        with mock.patch.object(pgr, "draft", crash):
            run = run_stage(self, ["--only", "draft"])
        self.assertEqual(run.rc, 70, run.text)
        self.assertIn("internal error (RuntimeError)", run.stdout)
        self.assertNoLeak(run)

    def test_SecretNeverRenders(self) -> None:
        secret = pgr.Secret(CANARY)
        api = self.api()
        for text in (repr(secret), str(secret), f"{secret}", repr(api), repr(vars(api)),
                     str([secret])):
            self.assertNotIn(CANARY, text)


class RedirectTests(_FakeCase):
    """Type: mock. RP-8: urllib must not forward Authorization; hosts are allowlisted."""

    def test_DownloadRedirectCarriesNoAuthorization(self) -> None:
        rel = self.fake.add_release(draft=True, body=pin.run_marker(RUN_ID))
        asset = self.fake.add_asset(rel["id"], "manifest.json", b"x" * 5)
        digest = pgr.download_sha256(self.api(), asset["id"], 5, "manifest.json")
        self.assertEqual(digest, hashlib.sha256(b"x" * 5).hexdigest())
        dl = [r for r in self.fake.requests if r.server == "download"]
        self.assertEqual(len(dl), 1)
        self.assertIsNone(dl[0].auth)

    def test_ApiCallFollowsNoRedirectAtAll(self) -> None:
        for target in ("evil", "download"):
            with self.subTest(target=target):
                self.fake.api_redirect = f"{self.fake.base(target)}/blob/1"
                with self.assertRaisesRegex(pgr.PublishError, "redirect refused"):
                    pgr.deref_tag(self.api(), TAG)
                self.assertEqual([r for r in self.fake.requests if r.server != "api"], [])

    def test_OversizeDownloadIsCutOff(self) -> None:
        rel = self.fake.add_release(draft=True, body=pin.run_marker(RUN_ID))
        asset = self.fake.add_asset(rel["id"], "manifest.json", b"y" * 64)
        with self.assertRaisesRegex(pgr.PublishError, "expected at most 10 bytes"):
            pgr.download_sha256(self.api(), asset["id"], 10, "manifest.json")

    def test_CredentialGoesOnlyToThePolicyBases(self) -> None:
        with self.assertRaisesRegex(pgr.PublishError, "allowlisted host"):
            self.api().request("GET", f"{self.fake.base('evil')}/x", "probe")
        with self.assertRaisesRegex(pgr.PublishError, "allowlisted host"):
            self.api().request("GET", self.fake.base("api").replace("http:", "https:") + "/x",
                               "probe")
        self.assertEqual([r for r in self.fake.requests if r.server == "evil"], [])
        self.assertEqual(pgr.PRODUCTION.api_base, "https://api.github.com")
        self.assertEqual(pgr.PRODUCTION.upload_base, "https://uploads.github.com")
        self.assertEqual(pgr.PRODUCTION.scheme, "https")


# ---------------------------------------------------------------------------
# Constraint 4: the manifest is anchored out of band
# ---------------------------------------------------------------------------

class AnchorTests(unittest.TestCase):
    """Type: unit. Constraint 4 / RP-4: sha256(manifest.json) is checked before parsing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.anchor = make_dist(self.tmp)

    def test_ArchiveAndManifestReplacedTogetherFail(self) -> None:
        """The planned CR-8 proof: a consistent substitute still differs from the job output."""
        dist = self.tmp / "dist"
        evil = pa.archive_name(VERSION, "linux-x64")
        (dist / evil).write_bytes(b"substituted")
        data = json.loads((dist / "manifest.json").read_text())
        data["legs"][0].update(sha256=hashlib.sha256(b"substituted").hexdigest(), bytes=11)
        (dist / "manifest.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(pin.PublishInputError,
                                    rf"manifest.json sha256: expected {self.anchor[:16]} "
                                    r"\(manifest job output\) -> actual "):
            inputs_for(self.tmp, self.anchor)

    def test_AnchorIsCheckedBeforeJsonParsing(self) -> None:
        (self.tmp / "dist" / "manifest.json").write_bytes(b"\xff{not json")
        with mock.patch.object(json, "loads", side_effect=AssertionError("parsed")):
            with self.assertRaisesRegex(pin.PublishInputError, "sha256: expected"):
                pin.load_anchored_manifest(self.tmp / "dist", self.anchor)

    def test_FilesAreCheckedAgainstTheManifest(self) -> None:
        dist = self.tmp / "dist"
        name = pa.archive_name(VERSION, "macos-arm64")
        for mutate, needle in (
                (lambda: (dist / name).write_bytes((dist / name).read_bytes()[:-1] + b"!"),
                 f"{name} sha256: expected"),
                (lambda: (dist / (name + ".sha256")).write_text("0" * 64 + "  x\n"),
                 f"{name}.sha256 bytes: expected"),
                (lambda: (dist / name).unlink(), f"{name}: expected a file in dist/")):
            with self.subTest(needle=needle):
                make_dist(self.tmp)
                mutate()
                with self.assertRaisesRegex(pin.PublishInputError, re.escape(needle)):
                    inputs_for(self.tmp, self.anchor)

    def test_SymlinkedManifestIsRefused(self) -> None:
        dist = self.tmp / "dist"
        real = self.tmp / "elsewhere.json"
        real.write_bytes((dist / "manifest.json").read_bytes())
        (dist / "manifest.json").unlink()
        (dist / "manifest.json").symlink_to(real)
        with self.assertRaisesRegex(pin.PublishInputError, "symlink"):
            inputs_for(self.tmp, self.anchor)

    def test_MalformedAnchorAndHostileManifestFieldsAreRefused(self) -> None:
        """RP-10: a | , a backtick or a link in a field never reaches the release text."""
        with self.assertRaisesRegex(pin.PublishInputError, "CI_MANIFEST_SHA256"):
            inputs_for(self.tmp, "abc;EVIL")
        for field, value in (("config", "Rel|ease"), ("glibc", "2.35`x`"),
                             ("config", "[x](http://e)")):
            with self.subTest(field=field, value=value):
                make_dist(self.tmp)
                dist = self.tmp / "dist"
                data = json.loads((dist / "manifest.json").read_text())
                data["legs"][0][field] = value
                raw = json.dumps(data).encode()
                (dist / "manifest.json").write_bytes(raw)
                with self.assertRaisesRegex(pin.PublishInputError, "<rejected>") as cm:
                    inputs_for(self.tmp, hashlib.sha256(raw).hexdigest())
                self.assertNotIn(value, str(cm.exception))
        anchor = make_dist(self.tmp, extra_leg_key="notes")
        with self.assertRaisesRegex(pin.PublishInputError, "unknown key"):
            inputs_for(self.tmp, anchor)

    def test_ReleaseBodyIsBuiltOnlyFromValidatedFields(self) -> None:
        body = pin.release_body(inputs_for(self.tmp, self.anchor))
        self.assertTrue(body.startswith(f"<!-- ci-run-id: {RUN_ID} -->\n"))
        self.assertEqual(body.count("| `libbios-1.0.1-"), 6)
        self.assertIn("| `manifest.json` |", body)


# ---------------------------------------------------------------------------
# Constraint 5: one release, one commit
# ---------------------------------------------------------------------------

class TagAndCommitTests(unittest.TestCase):
    """Type: unit. Constraint 5 (local half) / RP-5: the tag grammar and the manifest binding."""

    def test_TagGrammar(self) -> None:
        for bad in ("v1.0.0x", "v1.0", "v1.0.0-", "v01.0.0", "1.0.0", "v1.0.0+build",
                    "v1.0.0-rc 1", "v1.0.0\n", "v1.0.0-" + "a" * 41, "v1234567.0.0"):
            with self.subTest(tag=bad), self.assertRaises(pin.PublishInputError) as cm:
                pin.parse_tag(bad)
            self.assertIn("<rejected>", str(cm.exception))
        self.assertEqual(pin.parse_tag("v1.0.1"), ("1.0.1", None))
        self.assertEqual(pin.parse_tag("v1.0.1-rc.1"), ("1.0.1", "rc.1"))
        self.assertEqual(pin.parse_tag("v0.10.0"), ("0.10.0", None))

    def test_BindingToManifestVersionAndCommit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            anchor = make_dist(root)
            self.assertTrue(inputs_for(root, anchor, tag="v1.0.1-rc.1").prerelease)
            self.assertFalse(inputs_for(root, anchor).prerelease)
            with self.assertRaisesRegex(pin.PublishInputError,
                                        r"tag v1\.0\.2 version: expected 1\.0\.1 \(manifest\) "
                                        r"-> actual 1\.0\.2"):
                inputs_for(root, anchor, tag="v1.0.2")
            with self.assertRaisesRegex(pin.PublishInputError,
                                        rf"manifest.commit: expected {OTHER[:12]} \(CI_COMMIT\) "
                                        rf"-> actual {COMMIT[:12]}"):
                inputs_for(root, anchor, commit=OTHER)
            with self.assertRaisesRegex(pin.PublishInputError, "manifest.version: expected X.Y.Z"):
                inputs_for(root, make_dist(root, version="1.0.1-rc.1"), tag="v1.0.1-rc.1")


class PreflightTests(_FakeCase):
    """Type: mock. Constraint 5 (remote half) / RP-5: one discrepancy line each, no write."""

    def test_TagMovedIsRefused(self) -> None:
        self.fake.tags[TAG] = {"type": "commit", "sha": OTHER}
        text = self.refused(self.draft, f"remote tag {TAG}: expected {COMMIT[:12]} (CI_COMMIT) "
                                        f"-> actual {OTHER[:12]} (the tag moved)")
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(text.count("->"), 1)

    def test_AnnotatedTagIsDereferenced(self) -> None:
        self.fake.tags[TAG] = {"type": "tag", "sha": "d" * 40}
        self.fake.annotated["d" * 40] = {"type": "tag", "sha": "e" * 40}
        self.fake.annotated["e" * 40] = {"type": "commit", "sha": COMMIT}
        self.assertEqual(pgr.deref_tag(self.api(), TAG), COMMIT)
        self.fake.annotated["e" * 40] = {"type": "commit", "sha": OTHER}
        self.refused(self.draft, "(the tag moved)")
        self.assertEqual(self.fake.writes(), [])

    def test_MissingTagIsRefused(self) -> None:
        del self.fake.tags[TAG]
        self.refused(self.draft, "GET git ref: expected 200 -> actual 404")
        self.assertEqual(self.fake.writes(), [])

    def test_CommitNotOnMasterIsRefused(self) -> None:
        for status, base in (("diverged", "f" * 40), ("behind", COMMIT), ("ahead", "f" * 40),
                             ("<script>", COMMIT)):
            with self.subTest(status=status):
                self.fake.compare = {"status": status, "merge_base_commit": {"sha": base}}
                text = self.refused(self.draft, f"{COMMIT[:12]} on master: expected an "
                                                "ancestor (ahead or identical) -> actual")
                self.assertNotIn("<script>", text)
        self.assertEqual(self.fake.writes(), [])

    def test_PreflightRunsAgainImmediatelyBeforeTheCreate(self) -> None:
        self.draft()
        refs = [i for i, r in enumerate(self.fake.requests) if "git/ref/" in r.path]
        create = next(i for i, r in enumerate(self.fake.requests)
                      if r.method == "POST" and r.path.endswith("/releases"))
        self.assertEqual(len([i for i in refs if i < create]), 2)
        between = self.fake.requests[max(i for i in refs if i < create) + 1:create]
        self.assertEqual([r.path.rsplit("/", 1)[-1][:7] for r in between], ["a1a1a1a"])

    def test_SettingsDriftFailsWhenReadableAndIsInfoWhenNot(self) -> None:
        self.draft()
        self.assertTrue(any("manual read-back required" in ln for ln in self.logged))
        self.fake.releases.clear()
        self.fake.assets.clear()
        self.fake.settings = "good"
        self.logged.clear()
        self.draft(run_id="4243")
        self.assertIn("info: settings preflight: S-3, S-4 and S-5 read back as required",
                      self.logged)
        self.fake.releases.clear()
        self.fake.assets.clear()
        self.fake.settings = "no-immutable"
        before = len(self.fake.writes())
        self.refused(lambda: self.draft(run_id="4244"),
                     "immutable releases (S-4): expected enabled -> actual disabled")
        self.assertEqual(len(self.fake.writes()), before)


# ---------------------------------------------------------------------------
# Constraint 6: release tier is cold-built only
# ---------------------------------------------------------------------------

class BinaryCacheTests(unittest.TestCase):
    """Type: unit. Constraint 6 / RP-4, CR-2: missing, restored and mismatch are refused."""

    def view(self, binary_cache: Optional[str]) -> pin.ManifestView:
        with tempfile.TemporaryDirectory() as d:
            anchor = make_dist(Path(d), binary_cache=binary_cache)
            raw = (Path(d) / "dist" / "manifest.json").read_bytes()
            return pin.validate_manifest(json.loads(raw), anchor, len(raw), LEGS)

    def test_ReleaseTier(self) -> None:
        none = {leg: "none" for leg in LEGS}
        pin.check_binary_cache(self.view("none"), "release", none)
        for value, build, needle in (
                (None, none, "binary_cache: expected none -> actual missing"),
                ("restored", none, "expected none (release tier is cold-built only, CR-2) -> "
                                   "actual restored"),
                ("none", {**none, "macos-arm64": "restored"},
                 "macos-arm64 binary_cache: expected restored (build job output) -> actual none"),
                ("none", None, "CI_BUILD_BINARY_CACHE: expected the build job's")):
            with self.subTest(value=value, build=build):
                with self.assertRaisesRegex(pin.PublishInputError, re.escape(needle)):
                    pin.check_binary_cache(self.view(value), "release", build)

    def test_StagingTierAcceptsRestoredButNotMissingOrMismatch(self) -> None:
        restored = {leg: "restored" for leg in LEGS}
        pin.check_binary_cache(self.view("restored"), "staging", restored)
        pin.check_binary_cache(self.view("restored"), "staging", None)
        with self.assertRaises(pin.PublishInputError):
            pin.check_binary_cache(self.view(None), "staging", None)
        with self.assertRaises(pin.PublishInputError):
            pin.check_binary_cache(self.view("none"), "staging", restored)

    def test_BuildMapIsValidated(self) -> None:
        self.assertIsNone(pin.parse_build_binary_cache(""))
        for bad in ("{", '{"linux-x64": "cold"}', '{"../x": "none"}', '["none"]',
                    '{"linux-x64":\n"none"}'):
            with self.subTest(bad=bad), self.assertRaises(pin.PublishInputError):
                pin.parse_build_binary_cache(bad)


# ---------------------------------------------------------------------------
# Constraint 7: create-only, RP-6 re-read
# ---------------------------------------------------------------------------

class CreateOnlyTests(_FakeCase):
    """Type: mock. Constraint 7 / RP-6, RP-12: an existing target is a hard failure."""

    def test_DraftCreatesOneDraftWithExactlyTheManifestFiles(self) -> None:
        summary = self.draft()
        self.assertIn("7 assets uploaded and re-read", summary)
        (rel,) = self.fake.releases.values()
        self.assertTrue(rel["draft"])
        self.assertEqual(rel["target_commitish"], COMMIT)
        self.assertFalse(rel["prerelease"])
        self.assertTrue(rel["body"].startswith(pin.run_marker(RUN_ID)))
        names = sorted(a["name"] for a in self.fake.assets.values())
        want = sorted([pa.archive_name(VERSION, leg) + ext for leg in LEGS
                       for ext in ("", ".sha256")] + ["manifest.json"])
        self.assertEqual(names, want)
        creates = [r for r in self.fake.writes() if r.path.endswith("/releases")]
        self.assertEqual(len(creates), 1, "only release-draft creates a release (no race)")

    def test_PreReleaseTagGivesAPreRelease(self) -> None:
        self.fake.tags["v1.0.1-rc.1"] = {"type": "commit", "sha": COMMIT}
        self.draft(tag="v1.0.1-rc.1")
        (rel,) = self.fake.releases.values()
        self.assertTrue(rel["prerelease"])
        self.promote(tag="v1.0.1-rc.1")
        self.assertFalse(rel["draft"])

    def test_ExistingPublishedReleaseIsNeverTouched(self) -> None:
        self.fake.add_release(draft=False, body="older")
        self.refused(self.draft, "a published release exists")
        self.assertEqual(self.fake.writes(), [])

    def test_DraftFromAnotherRunIsRefused(self) -> None:
        self.fake.add_release(draft=True, body=pin.run_marker("999") + "\nnotes")
        self.fake.add_release(draft=True, body=f"text {pin.run_marker(RUN_ID)}")  # not line 1
        self.refused(self.draft, "expected no draft from another run -> actual 2")
        self.assertEqual(self.fake.writes(), [])

    def test_ReRunResumesOnlyItsOwnDraft(self) -> None:
        rel = self.fake.add_release(draft=True, body=pin.run_marker(RUN_ID) + "\n")
        stale = self.fake.add_asset(rel["id"], "manifest.json", b"half-uploaded")
        self.draft()
        self.assertEqual(len(self.fake.releases), 1)
        deletes = [r for r in self.fake.writes() if r.method == "DELETE"]
        self.assertEqual([r.path.rsplit("/", 1)[-1] for r in deletes], [str(stale["id"])])
        self.assertTrue(any("resuming its draft" in ln for ln in self.logged))
        self.assertEqual(len([a for a in self.fake.assets.values()
                              if a["release"] == rel["id"]]), 7)

    def test_UploadOfAnExistingAssetNameFails(self) -> None:
        self.fake.override = lambda m, p: 422 if (m == "POST" and p.endswith("/assets")) else None
        self.refused(self.draft, "POST upload libbios-1.0.1-linux-x64.tar.gz: expected 201 -> "
                                 "actual 422 (already_exists)")
        self.assertEqual(len([r for r in self.fake.writes() if r.path.endswith("/assets")]), 1)


class PromoteTests(_FakeCase):
    """Type: mock. RP-6: the exact asset set, sizes and digests are re-read before promotion."""

    def drafted(self) -> Dict[str, Any]:
        self.draft()
        (rel,) = self.fake.releases.values()
        return rel

    def assertNoPromote(self) -> None:
        self.assertEqual([r for r in self.fake.writes() if r.method == "PATCH"], [])

    def asset(self, suffix: str) -> Dict[str, Any]:
        return next(a for a in self.fake.assets.values() if a["name"].endswith(suffix))

    def test_PromotePublishesAfterAFullReRead(self) -> None:
        rel = self.drafted()
        summary = self.promote()
        self.assertFalse(rel["draft"])
        self.assertIn("promoted release v1.0.1", summary)
        patch_at = next(i for i, r in enumerate(self.fake.requests) if r.method == "PATCH")
        downloads_before = [r for r in self.fake.requests[:patch_at] if r.server == "download"]
        self.assertEqual(len(downloads_before), 14, "draft re-read + promote re-read")

    def test_ExtraRenamedResizedOrTamperedAssetBlocksPromotion(self) -> None:
        def size(v: Dict[str, Any]) -> None:
            if v["name"] == "manifest.json":
                v["size"] += 1

        cases = (
            ("extra", lambda: self.fake.add_asset(self.rel["id"], "evil.sh", b"x"),
             "0 missing, 1 extra or renamed"),
            ("renamed", lambda: self.asset("manifest.json").update(name="manifest.json.1"),
             "1 missing, 1 extra or renamed"),
            ("size", lambda: setattr(self.fake, "tamper", size),
             "asset manifest.json bytes: expected"),
            ("digest", lambda: self.asset("linux-x64.tar.gz").update(
                data=b"Z" * self.asset("linux-x64.tar.gz")["size"]),
             "asset libbios-1.0.1-linux-x64.tar.gz sha256: expected"),
            ("digest field", lambda: setattr(self.fake, "tamper",
                                             lambda v: v.update(digest="sha256:" + "0" * 64)),
             "digest field: expected"),
        )
        for name, mutate, needle in cases:
            with self.subTest(case=name):
                self.fake.releases.clear()
                self.fake.assets.clear()
                self.fake.tamper = lambda v: None
                self.rel = self.drafted()
                mutate()
                self.refused(self.promote, needle)
                self.assertTrue(self.rel["draft"])
                self.assertNoPromote()

    def test_NoDraftOfThisRunOrAnotherReleaseBlocksPromotion(self) -> None:
        self.refused(self.promote, "expected the draft of run 4242 -> actual 0")
        self.fake.add_release(draft=True, body=pin.run_marker("1"))
        self.refused(self.promote, "other release(s)")
        self.assertNoPromote()

    def test_ReRunAfterPublishIsIdempotent(self) -> None:
        self.drafted()
        self.promote()
        writes = len(self.fake.writes())
        summary = self.promote()
        self.assertIn("already published by run 4242", summary)
        self.assertEqual(len(self.fake.writes()), writes, "an idempotent re-run writes nothing")

    def test_MutablePublishedReleaseIsALoudFailure(self) -> None:
        self.drafted()
        self.fake.immutable_field = False
        self.refused(self.promote, "immutable (S-4): expected true -> actual false")
        self.fake.immutable_field = None
        self.fake.releases.clear()
        self.fake.assets.clear()
        self.draft(run_id="77")
        self.logged.clear()
        self.promote(run_id="77")
        self.assertTrue(any("immutable: not reported" in ln for ln in self.logged))

    def test_TagMovedBetweenDraftAndPromote(self) -> None:
        self.drafted()
        self.fake.tags[TAG] = {"type": "commit", "sha": OTHER}
        self.refused(self.promote, "(the tag moved)")
        self.assertNoPromote()


# ---------------------------------------------------------------------------
# Stage gates (fail closed) and the dry run
# ---------------------------------------------------------------------------

class StageGateTests(_FakeCase):
    """Type: integration (in-process stage). Each gate refuses before any request."""

    def test_VerifyGateFailsClosedWithoutAVerifyResult(self) -> None:
        """ADR 0005 D6: without the same run's verify-gate result publish must refuse."""
        for verified, needle in (("", "no verify result (every packaging leg must pass"),
                                 ("e" * 64, "verify passed for another manifest")):
            with self.subTest(verified=verified):
                run = run_stage(self, ["--only", "draft"], CI_VERIFIED_MANIFEST_SHA256=verified)
                self.assertEqual(run.rc, 1, run.text)
                self.assertIn(needle, run.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_DisabledDestinationLocalRunAndWrongEventAreRefused(self) -> None:
        for kw, enabled, needle in (
                ({}, False, "actual disabled (threat model constraint 12"),
                ({"CI_PLATFORM": "local"}, True, "expected a CI run -> actual a local run"),
                ({"CI_EVENT": "push", "CI_REF": "refs/heads/master", "CI_REF_NAME": "master"},
                 True, "expected a release tag event -> actual push"),
                ({"CI_REF": "refs/tags/v9.9.9"}, True, "expected a release tag event")):
            with self.subTest(needle=needle):
                run = run_stage(self, ["--only", "draft"], enabled=enabled, **kw)
                self.assertEqual(run.rc, 1, run.text)
                self.assertIn(needle, run.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_UsageErrorsExit2(self) -> None:
        for args in ([], ["--only", "deploy"], ["--only", "draft", "--leg", "linux"]):
            with self.subTest(args=args):
                run = run_stage(self, args)
                self.assertEqual(run.rc, 2, run.text)
                self.assertIn("stage.py: error:", run.stderr)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = stage.main(["publish", "--destination", "../EVIL", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertNotIn("EVIL", err.getvalue())

    def test_RealRegistryShipsGithubReleaseDisabled(self) -> None:
        """Constraint 12: the shipped entry stays disabled until S-1..S-8 are read back."""
        entry = stage.load_destination("github-release")
        self.assertEqual((entry["enabled"], entry["tier"], entry["environment"],
                          entry["permission_class"], entry["credentials"]),
                         (False, "release", "release", "repo-write", ["CI_PUBLISH_TOKEN"]))
        self.assertEqual(pgr.REQUIRED_ENV[0], "CI_PUBLISH_TOKEN")

    def test_PreflightPhaseWritesNothing(self) -> None:
        run = run_stage(self, ["--only", "preflight"])
        self.assertEqual(run.rc, 0, run.text)
        self.assertIn("tag and master ancestry bind", run.stdout)
        self.assertEqual(self.fake.writes(), [])


class DryRunTests(_FakeCase):
    """Type: unit. D9 ``--dry-run``: exact files, no network, the credential never read."""

    def test_DryRunListsEveryWriteWithoutNetworkOrCredential(self) -> None:
        def no_network(*_a: Any, **_k: Any) -> None:
            raise AssertionError("network or credential used in a dry run")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=no_network), \
                mock.patch.object(pgr, "Secret", side_effect=no_network):
            run = run_stage(self, ["--dry-run"], enabled=False, CI_VERIFIED_MANIFEST_SHA256="")
        self.assertEqual(run.rc, 0, run.text)
        uploads = re.findall(r"would upload (\S+) \((\d+) bytes, sha256 ([0-9a-f]{16})\)",
                             run.stdout)
        self.assertEqual(len(uploads), 7)
        self.assertEqual(uploads[-1][0], "manifest.json")
        self.assertIn(f"target_commitish {COMMIT}, draft true, prerelease false", run.stdout)
        self.assertIn("make_latest true", run.stdout)
        self.assertIn("disabled (real run refuses), verify gate refuses (no verify result), "
                      "0 requests, credential unread", run.stdout)
        self.assertEqual(self.fake.requests, [])
        for leak in LEAK_FORMS:
            self.assertNotIn(leak, run.everything())

    def test_DryRunStillRefusesBadInputs(self) -> None:
        make_dist(self.tmp, binary_cache="restored")
        run = run_stage(self, ["--dry-run"], CI_MANIFEST_SHA256="", CI_BUILD_BINARY_CACHE="")
        self.assertEqual(run.rc, 1, run.text)
        self.assertIn("expected none (release tier is cold-built only, CR-2) -> actual restored",
                      run.stdout)


if __name__ == "__main__":
    unittest.main()
