"""Tests for the post-publish check and the nightly release re-check (0012 Story 04.0 Task 04).

Classification: unit + mock. GitHub's public surface is a set of stub HTTP
servers on loopback (:class:`PublicGitHub`): ``api`` (the anonymous REST view:
drafts are invisible, as for a real consumer), ``web`` (the
``github.com/<repo>/releases/download/<tag>/<name>`` origin, which redirects),
``cdn`` (the allowlisted redirect host that serves the bytes) and ``evil`` (a
host outside the allowlist). Every request records the ``Authorization``
header it carried; none may carry one. No real network, no real release.

Threat model ``docs/security/2026-11-18-release-and-publish-automation.md``,
constraint 11 and RP-7 "Verification", one test each:

- the manifest asset replaced together with an archive is detected
  (:meth:`CheckReleaseTests.test_ManifestReplacedTogetherWithAnArchiveIsDetected`);
- a redirect to a non-allowlisted host is refused, and that host is never contacted;
- an oversize body is cut off at the manifest size and fails;
- a draft release is reported, not checked;
- the exact asset name set, ``prerelease`` and the tag's commit;
- no credential: no request carries ``Authorization``, the module imports no
  ``subprocess``/step runner, and ``stage.py`` refuses the phase when
  ``CI_PUBLISH_TOKEN`` is set at all;
- the nightly re-check: the last 5 releases, anchored by ``release_ledger.json``.

On failure first check: ``fake.requests`` (what the stub saw last) and the
RC1 line; then ``post_publish_check.py``.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import unquote, urlsplit

from ci_test_support import CI_DIR

import post_publish_check as ppc  # noqa: E402
import publish_github_release as pgr  # noqa: E402
import publish_inputs as pin  # noqa: E402
import stage  # noqa: E402
from test_publish_github_release import (COMMIT, LEGS, OTHER, REPO, TAG, inputs_for,  # noqa: E402
                                         make_dist)


class PublicGitHub:
    """Anonymous GitHub stub: ``api``, ``web`` (download origin), ``cdn`` and ``evil``."""

    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Optional[str]]] = []
        self.releases: Dict[str, Dict[str, Any]] = {}
        self.blobs: Dict[Tuple[str, str], bytes] = {}
        self.tags: Dict[str, str] = {}
        self.redirect_to = "cdn"
        self.extra_bytes = 0
        self.list_drafts = False
        self.lock = threading.Lock()
        self.servers: Dict[str, ThreadingHTTPServer] = {}
        for name in ("api", "web", "cdn", "evil"):
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

    def hosts(self) -> ppc.PublicHosts:
        return ppc.PublicHosts(api_base=self.base("api"), download_origin=self.base("web"),
                               redirect_hosts=frozenset({urlsplit(self.base("cdn")).netloc}),
                               scheme="http")

    def client(self) -> ppc.AnonymousClient:
        return ppc.AnonymousClient(REPO, self.hosts(), timeout_s=10)

    def publish(self, tag: str, files: Dict[str, bytes], *, commit: str = COMMIT,
                draft: bool = False, prerelease: bool = False) -> None:
        """A release as the API would show it, plus its downloadable bytes."""
        self.tags[tag] = commit
        self.releases[tag] = {"tag_name": tag, "draft": draft, "prerelease": prerelease,
                              "assets": [{"name": n, "size": len(d), "state": "uploaded"}
                                         for n, d in files.items()]}
        for name, data in files.items():
            self.blobs[(tag, name)] = data

    def view(self, tag: str) -> Dict[str, Any]:
        rel = dict(self.releases[tag])
        rel["assets"] = [dict(a, browser_download_url=f"{self.base('web')}/{REPO}/releases/"
                              f"download/{tag}/{a['name']}") for a in rel["assets"]]
        return rel

    def _handler(self, server_name: str):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, obj: Any = None, raw: Optional[bytes] = None,
                      location: Optional[str] = None) -> None:
                body = raw if raw is not None else json.dumps(obj if obj is not None else {}
                                                              ).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)  # the client cuts an oversize body off

            def do_GET(self) -> None:  # noqa: N802
                path = unquote(urlsplit(self.path).path)
                with fake.lock:
                    fake.requests.append((server_name, path, self.headers.get("Authorization")))
                if server_name == "evil":
                    return self._send(200, raw=b"evil")
                if server_name == "web":
                    prefix = f"/{REPO}/releases/download/"
                    if not path.startswith(prefix):
                        return self._send(404)
                    target = fake.base(fake.redirect_to)
                    return self._send(302, location=f"{target}/blob/{path[len(prefix):]}")
                if server_name == "cdn":
                    tag, _, name = path[len("/blob/"):].partition("/")
                    data = fake.blobs.get((tag, name))
                    if data is None:
                        return self._send(404)
                    return self._send(200, raw=data + b"X" * fake.extra_bytes)
                prefix = f"/repos/{REPO}/"
                rest = path[len(prefix):] if path.startswith(prefix) else ""
                if rest.startswith("releases/tags/"):
                    tag = rest[len("releases/tags/"):]
                    rel = fake.releases.get(tag)
                    if rel is None or rel["draft"]:  # anonymous: a draft is invisible
                        return self._send(404)
                    return self._send(200, fake.view(tag))
                if rest == "releases":
                    items = [fake.view(t) for t in reversed(list(fake.releases))
                             if fake.list_drafts or not fake.releases[t]["draft"]]
                    return self._send(200, items[:5])
                if rest.startswith("git/ref/tags/"):
                    tag = rest[len("git/ref/tags/"):]
                    sha = fake.tags.get(tag)
                    return self._send(200, {"ref": f"refs/tags/{tag}", "object": {
                        "type": "commit", "sha": sha}}) if sha else self._send(404)
                return self._send(404)

        return Handler


def dist_files(root: Path) -> Dict[str, bytes]:
    """Every file of a ``make_dist`` dist/, by name (what a correct release carries)."""
    return {p.name: p.read_bytes() for p in sorted((root / "dist").iterdir())}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-post-publish-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.anchor = make_dist(self.tmp)
        self.fake = PublicGitHub()
        self.addCleanup(self.fake.close)
        self.logged: List[str] = []

    def inputs(self, **kw: Any) -> pin.ReleaseInputs:
        return inputs_for(self.tmp, self.anchor, **kw)

    def check(self) -> str:
        return ppc.post_publish(self.inputs(), REPO, self.logged.append, self.fake.client())

    def refused(self, needle: str) -> str:
        with self.assertRaises(ppc.CheckError) as cm:
            self.check()
        text = str(cm.exception)
        self.assertIn(needle, text)
        self.assertTrue(text.startswith(f"release {TAG}:"), text)  # names the release
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNoCredentialSent()
        return text

    def assertNoCredentialSent(self) -> None:  # noqa: N802
        self.assertTrue(self.fake.requests)
        self.assertEqual([r for r in self.fake.requests if r[2] is not None], [])


class CheckReleaseTests(_Case):
    """Type: unit + mock (stub servers). Constraint 11 / RP-7, one scenario each."""

    def test_PublishedReleaseMatchesAndNoRequestCarriesACredential(self) -> None:
        self.fake.publish(TAG, dist_files(self.tmp))
        summary = self.check()
        self.assertIn(f"release {TAG}: 7 public assets match the out-of-band manifest "
                      f"{self.anchor[:16]}", summary)
        self.assertNoCredentialSent()
        self.assertEqual(len([r for r in self.fake.requests if r[0] == "cdn"]), 7)
        row = json.loads(self.logged[-1].split("anchor): ", 1)[1])
        self.assertEqual(row, {"tag": TAG, "commit": COMMIT, "manifest_sha256": self.anchor,
                               "manifest_bytes": (self.tmp / "dist/manifest.json").stat().st_size})

    def test_ManifestReplacedTogetherWithAnArchiveIsDetected(self) -> None:
        """RP-7: the release's own manifest.json is never the reference."""
        files = dist_files(self.tmp)
        name = pin_archive = next(n for n in files if n.endswith(".tar.gz"))
        evil = bytes(b ^ 0xFF for b in files[name])  # same size, other bytes
        manifest = json.loads(files["manifest.json"])
        for entry in manifest["legs"]:
            if entry["file"] == pin_archive:
                entry["sha256"] = hashlib.sha256(evil).hexdigest()
        evil_manifest = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        self.assertEqual(len(evil_manifest), len(files["manifest.json"]))
        files.update({name: evil, "manifest.json": evil_manifest,
                      name + ".sha256": pin.sidecar_text(hashlib.sha256(evil).hexdigest(),
                                                         name)})
        self.fake.publish(TAG, files)
        self.refused(f"asset {name} sha256 expected")

    def test_RedirectToANonAllowlistedHostIsRefused(self) -> None:
        self.fake.publish(TAG, dist_files(self.tmp))
        self.fake.redirect_to = "evil"
        self.refused("redirect refused")
        self.assertEqual([r for r in self.fake.requests if r[0] == "evil"], [])

    def test_OversizeBodyIsCutOffAtTheManifestSize(self) -> None:
        self.fake.publish(TAG, dist_files(self.tmp))
        self.fake.extra_bytes = 1 << 20
        self.refused("actual more (body cut off at the manifest size)")

    def test_DraftIsReportedNotChecked(self) -> None:
        """Anonymous view: a draft (never promoted) is not visible; reported by name."""
        self.fake.publish(TAG, dist_files(self.tmp), draft=True)
        self.refused("not visible anonymously (a draft")
        self.assertEqual([r for r in self.fake.requests if r[0] in ("web", "cdn")], [])

    def test_ExactAssetNameSet(self) -> None:
        for change, needle in (("extra", "0 missing, 1 extra"), ("missing", "1 missing, 0 extra"),
                               ("renamed", "1 missing, 1 extra")):
            with self.subTest(change=change):
                files = dist_files(self.tmp)
                if change == "extra":
                    files["notes.txt"] = b"x"
                elif change == "missing":
                    files.pop("manifest.json")
                else:
                    data = files.pop("manifest.json")
                    files["manifest (1).json"] = data
                self.fake.publish(TAG, files)
                self.refused(needle)

    def test_SizePrereleaseAndTagCommit(self) -> None:
        files = dist_files(self.tmp)
        self.fake.publish(TAG, files, prerelease=True)
        self.refused("prerelease expected false (the tag) -> actual true")
        self.fake.publish(TAG, files, commit=OTHER)
        self.refused(f"tag expected {COMMIT[:12]} (manifest.commit) -> actual {OTHER[:12]}")
        self.fake.publish(TAG, files)
        self.fake.releases[TAG]["assets"][0]["size"] += 1
        self.refused("bytes expected")

    def test_ShortBodyFails(self) -> None:
        files = dist_files(self.tmp)
        self.fake.publish(TAG, files)
        name = next(n for n in files if n.endswith(".zip"))
        self.fake.blobs[(TAG, name)] = files[name][:-1]
        self.refused(f"download {name} bytes: expected")


class NoCredentialTests(_Case):
    """Type: unit (AST) + integration. Constraint 11: the check holds no credential at all."""

    def test_ModuleImportsNoChildProcessOrStepRunner(self) -> None:
        tree = ast.parse((CI_DIR / "post_publish_check.py").read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                used.add(node.module.split(".")[0])
        self.assertEqual(used & {"subprocess", "ci_step", "publish_github_release", "pty",
                                 "multiprocessing"}, set())
        self.assertNotIn("Authorization", (CI_DIR / "post_publish_check.py").read_text(
            encoding="utf-8").split('"""', 2)[2].replace('"Authorization") is not None', ""))

    def test_RedirectAllowlistEqualsThePublishDestinations(self) -> None:
        self.assertEqual(ppc.REDIRECT_HOSTS, pgr.DOWNLOAD_HOSTS)
        self.assertEqual(ppc.PRODUCTION.scheme, "https")
        self.assertEqual(ppc.PRODUCTION.download_origin, "https://github.com")

    def _stage(self, *args: str, **env: str) -> Tuple[int, str]:
        from test_publish_github_release import registry, stage_env
        out = io.StringIO()
        client = functools.partial(ppc.AnonymousClient, hosts=self.fake.hosts(), timeout_s=10)
        values = stage_env(self, **env)
        values.pop("CI_PUBLISH_TOKEN", None)
        values.update(env)
        with mock.patch.dict(os.environ, values, clear=True), \
                mock.patch.object(ppc, "AnonymousClient", client), \
                mock.patch.object(stage, "DESTINATIONS_FILE", registry(self, True)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = stage.main(["publish", "--destination", "github-release", *args])
        return rc, out.getvalue()

    def test_StagePhaseRefusesWhenACredentialIsSet(self) -> None:
        self.fake.publish(TAG, dist_files(self.tmp))
        rc, text = self._stage("--only", "post-publish", CI_PUBLISH_TOKEN="ghs_" + "x" * 36)
        self.assertEqual(rc, 1, text)
        self.assertIn("expected no credential in this job", text)
        self.assertEqual(self.fake.requests, [])

    def test_StagePhaseRunsAnonymouslyAndPasses(self) -> None:
        self.fake.publish(TAG, dist_files(self.tmp))
        rc, text = self._stage("--only", "post-publish")
        self.assertEqual(rc, 0, text)
        self.assertIn("publish github-release post-publish", text)
        self.assertIn("record in scripts/ci/release_ledger.json", text)
        self.assertNoCredentialSent()

    def test_StagePhaseNeedsTheVerifyResult(self) -> None:
        rc, text = self._stage("--only", "post-publish", CI_VERIFIED_MANIFEST_SHA256="")
        self.assertEqual(rc, 1, text)
        self.assertIn("no verify result", text)
        self.assertEqual(self.fake.requests, [])

    def test_DryRunListsTheAnonymousChecks(self) -> None:
        rc, text = self._stage("--only", "post-publish", "--dry-run")
        self.assertEqual(rc, 0, text)
        self.assertIn("post-publish (no credential, contents: read)", text)
        self.assertIn("redirects only to *.githubusercontent.com over HTTPS", text)
        self.assertEqual(self.fake.requests, [])


class RecheckTests(_Case):
    """Type: unit + mock. The nightly re-check of the last 5 releases (constraint 11)."""

    def ledger(self, rows: List[Dict[str, Any]]) -> Dict[str, ppc.LedgerEntry]:
        path = self.tmp / "ledger.json"
        path.write_text(json.dumps({"schema": 1, "releases": rows}), encoding="utf-8")
        return ppc.load_ledger(path)

    def release(self, version: str, commit: str = COMMIT) -> Dict[str, Any]:
        """Publish a correct release of ``version``; return its ledger row."""
        root = self.tmp / version
        sha = make_dist(root, version=version, commit=commit)
        self.fake.publish(f"v{version}", dist_files(root), commit=commit)
        return {"tag": f"v{version}", "commit": commit, "manifest_sha256": sha,
                "manifest_bytes": (root / "dist/manifest.json").stat().st_size}

    def recheck(self, ledger: Dict[str, ppc.LedgerEntry]) -> str:
        return ppc.recheck(self.fake.client(), ledger, LEGS, self.logged.append)

    def test_NoReleaseYetPasses(self) -> None:
        self.assertIn("no published release to re-check", self.recheck(self.ledger([])))

    def test_LastFiveAnchoredReleasesPass(self) -> None:
        rows = [self.release(f"1.0.{n}") for n in range(1, 8)]
        summary = self.recheck(self.ledger(rows))
        self.assertIn("re-checked 5 published release(s)", summary)
        self.assertIn("v1.0.7", summary)
        self.assertNotIn("v1.0.2", summary)  # only the newest 5
        self.assertNoCredentialSent()

    def test_ReleaseWithoutALedgerRowFailsByName(self) -> None:
        rows = [self.release("1.0.1")]
        self.release("1.0.2")
        with self.assertRaises(ppc.CheckError) as cm:
            self.recheck(self.ledger(rows))
        self.assertIn("release v1.0.2: expected a row in release_ledger.json", str(cm.exception))

    def test_LaterTamperingIsDetectedAndEveryReleaseIsChecked(self) -> None:
        rows = [self.release(f"1.0.{n}") for n in (1, 2, 3)]
        for tag in ("v1.0.1", "v1.0.3"):  # assets AND manifest replaced (RP-7)
            files = {k: v for (t, k), v in self.fake.blobs.items() if t == tag}
            files["manifest.json"] = files["manifest.json"].replace(b"Release", b"Debug__")
            self.fake.publish(tag, files)
        with self.assertRaises(ppc.CheckError) as cm:
            self.recheck(self.ledger(rows))
        text = str(cm.exception)
        self.assertIn("release v1.0.3: asset manifest.json sha256 expected", text)
        self.assertIn("(release_ledger.json)", text)
        self.assertIn("(+1 more release(s) failed)", text)

    def test_DraftIsReportedNotChecked(self) -> None:
        rows = [self.release("1.0.1")]
        self.fake.publish("v1.0.2", {"x": b"y"}, draft=True)
        self.fake.list_drafts = True  # defensive: anonymous listings never show drafts
        self.assertIn("re-checked 1", self.recheck(self.ledger(rows)))
        self.assertIn("info: release v1.0.2: a draft, reported, not checked", self.logged)

    def test_LedgerValidation(self) -> None:
        good = {"tag": "v1.0.1", "commit": COMMIT, "manifest_sha256": "a" * 64,
                "manifest_bytes": 10}
        for rows, needle in (([dict(good, tag="1.0.1")], "malformed row"),
                             ([dict(good, extra=1)], "malformed row"),
                             ([dict(good, manifest_bytes=0)], "malformed row"),
                             ([good, good], "v1.0.1 twice")):
            with self.subTest(needle=needle, rows=len(rows)):
                with self.assertRaises(ppc.CheckError) as cm:
                    self.ledger(rows)
                self.assertIn(needle, str(cm.exception))

    def test_RealLedgerIsValidAndEmpty(self) -> None:
        self.assertEqual(ppc.load_ledger(), {})


if __name__ == "__main__":
    unittest.main()
