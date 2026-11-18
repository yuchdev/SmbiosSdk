"""Tests for the ``s3-staging`` publish destination (milestone 0012 Story 04.0 Task 05; ADR 0005 D9).

Classification: unit + mock. No network and no real AWS: the ``aws`` CLI is a
stub Python script (``s3_staging.AWS`` is patched to ``[python, stub]``) that
emulates a bucket in a temporary directory, records every argv and the child
environment it received, and can be told to fail. The OIDC token service is a
loopback HTTP stub. Nothing here creates an S3 object or bucket.

Scenarios (``docs/dev/adding-a-publish-destination.md``, "Example: S3
staging area"; threat model 2026-11-13 CR-3/CR-5, 2026-11-18 constraints 1-3,
7, RP-14):

- canonical-path confinement: ``staging-release``, ``stagingX``,
  ``staging/../x``, ``/staging``, ``a/../../b`` and friends are refused, and
  no refusal echoes the value;
- a manifest name with ``/`` or ``..``, a non-numeric run id or attempt, and a
  symlink in ``dist/`` are refused;
- create-only: a stub ``aws`` answering ``PreconditionFailed`` for an existing
  key is a hard failure, and no second write (nor any retry or delete) follows;
- the OIDC token goes to a 0600 file, only ``AWS_*`` names of
  ``CHILD_ENV_NAMES`` reach the child, never a ``CI_PUBLISH_*`` name, and the
  role ARN, the request token and the JWT appear in no output (canary);
- the staging tier: push to master only, verify gate required,
  ``binary_cache: restored`` accepted, release-class jobs refused.

On failure first check: ``scripts/ci/destinations/s3_staging.py`` for the
rule named in the failing assertion; ``stage.run_publish`` for the gates.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from ci_test_support import CI_DIR  # noqa: F401  (puts scripts/ci on sys.path)

import destinations  # noqa: E402
import package_archive as pa  # noqa: E402
import publish_inputs as pin  # noqa: E402
import stage  # noqa: E402
from destinations import s3_staging as s3  # noqa: E402
from test_publish_github_release import COMMIT, LEGS, RUN_ID, make_dist  # noqa: E402

VERSION = "1.0.1"
BUCKET = "libbios-staging-test"
PREFIX = "staging/libbios"
#: Canaries: random per run, each shaped like the real thing.
ROLE = f"arn:aws:iam::{secrets.randbelow(10 ** 12):012d}:role/CANARY{secrets.token_hex(6)}"
OIDC_REQUEST = "CANARYREQ" + secrets.token_hex(16)
JWT = "eyJCANARY" + secrets.token_hex(8) + ".eyJ" + secrets.token_hex(12) + "." + \
    secrets.token_hex(16)
LEAKS = (ROLE, ROLE.split("/")[-1], OIDC_REQUEST, JWT, JWT.split(".")[2],
         base64.b64encode(JWT.encode()).decode())

#: The stub aws CLI. Bucket = a directory; argv and child environment recorded.
STUB = r'''
import json, os, shutil, sys
args = sys.argv[1:]
rec = {"argv": args, "env": {k: v for k, v in os.environ.items()
                             if k.startswith(("AWS_", "CI_"))}}
token = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
if token:
    try:
        st = os.stat(token)
        rec["token_mode"] = oct(st.st_mode & 0o777)
        rec["token_dir_mode"] = oct(os.stat(os.path.dirname(token)).st_mode & 0o777)
        with open(token) as fh:
            rec["token_ok"] = fh.read() == os.environ["STUB_JWT"]
    except OSError:
        rec["token_mode"] = "missing"
with open(os.environ["STUB_RECORD"], "a") as fh:
    fh.write(json.dumps(rec) + "\n")
store = os.environ["STUB_STORE"]
mode = os.environ.get("STUB_MODE", "")
if args == ["--version"]:
    print("aws-cli/" + os.environ.get("STUB_VERSION", "0") + " Python/3.12.0 Linux/6 exe/x86_64")
    sys.exit(0)
def opt(name):
    return args[args.index(name) + 1]
if args[:2] == ["s3api", "put-object"]:
    puts = sum(1 for _ in open(os.environ["STUB_RECORD"]) if '"put-object"' in _)
    if mode == "fail-put-3" and puts == 3:
        print("An error occurred (AccessDenied): User: " + os.environ["AWS_ROLE_ARN"] + " denied")
        sys.exit(254)
    path = os.path.join(store, opt("--bucket"), opt("--key"))
    create_only = "--if-none-match" in args and opt("--if-none-match") == "*"
    # Real S3: without the condition an existing key is silently overwritten.
    if create_only and (os.path.exists(path) or (mode == "exists-3" and puts == 3)):
        print("An error occurred (PreconditionFailed) when calling the PutObject operation: "
              "At least one of the pre-conditions you specified did not hold. "
              + os.environ["AWS_ROLE_ARN"])
        sys.exit(254)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(opt("--body"), path)
    print(json.dumps({"ETag": "x"}))
    sys.exit(0)
if args[:2] == ["s3api", "get-object"]:
    path = os.path.join(store, opt("--bucket"), opt("--key"))
    if not os.path.exists(path):
        print("An error occurred (NoSuchKey)")
        sys.exit(254)
    shutil.copyfile(path, args[-1])
    if mode == "tamper-get":
        with open(args[-1], "ab") as fh:
            fh.write(b"x")
    print(json.dumps({"Metadata": {}}))
    sys.exit(0)
print("unexpected aws call")
sys.exit(2)
'''


class _OidcHandler(BaseHTTPRequestHandler):
    server: "OidcStub"

    def log_message(self, *_a: Any) -> None:  # silence the stub
        pass

    def do_GET(self) -> None:  # noqa: N802
        stub = self.server
        stub.requests.append((self.path, self.headers.get("Authorization")))
        if stub.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://evil.example/steal")
            self.end_headers()
            return
        if stub.mode.isdigit():
            self.send_response(int(stub.mode))
            self.end_headers()
            self.wfile.write(json.dumps({"message": "denied " + ROLE}).encode())
            return
        body = json.dumps({"value": JWT if stub.mode != "bad-jwt" else "not a jwt"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OidcStub(ThreadingHTTPServer):
    """Loopback OIDC token service; records ``(path, Authorization)`` of each request."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _OidcHandler)
        self.requests: List[Any] = []
        self.mode = ""
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def netloc(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _S3Case(unittest.TestCase):
    """A temp dist/, a stub aws, a stub OIDC service."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ci-s3-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.anchor = make_dist(self.tmp, binary_cache="restored")
        self.stub = self.tmp / "aws_stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.record = self.tmp / "aws.jsonl"
        self.store = self.tmp / "bucket-store"
        self.store.mkdir()
        self.oidc = OidcStub()
        self.addCleanup(self.oidc.close)
        for patch in (mock.patch.object(s3, "AWS", (sys.executable, str(self.stub))),
                      mock.patch.object(s3, "OIDC_SCHEME", "http"),
                      mock.patch.object(s3, "OIDC_HOST_SUFFIXES", (self.oidc.netloc,)),
                      mock.patch.dict(os.environ, {"STUB_RECORD": str(self.record),
                                                   "STUB_STORE": str(self.store),
                                                   "STUB_JWT": JWT,
                                                   "STUB_VERSION": s3.pinned_aws_version()})):
            patch.start()
            self.addCleanup(patch.stop)

    def inputs(self) -> pin.ReleaseInputs:
        raw, data = pin.load_anchored_manifest(self.tmp / "dist", self.anchor)
        view = pin.validate_manifest(data, self.anchor, len(raw), LEGS)
        return pin.ReleaseInputs("", view.version, False, COMMIT, RUN_ID, view,
                                 pin.local_assets(self.tmp / "dist", view))

    def env(self, *, dry_run: bool = False, prefix: str = PREFIX, oidc: bool = True,
            **values: str) -> destinations.Env:
        creds = {} if dry_run else {
            "CI_PUBLISH_S3_BUCKET": destinations.Secret(BUCKET),
            "CI_PUBLISH_S3_PREFIX": destinations.Secret(prefix),
            "CI_PUBLISH_S3_ROLE": destinations.Secret(ROLE),
            "CI_PUBLISH_S3_REGION": destinations.Secret("eu-central-1")}
        base = {"CI_RUN_ID": RUN_ID, "CI_RUN_ATTEMPT": "2", "CI_COMMIT": COMMIT}
        base.update(values)
        from reporters import get_reporter
        return destinations.Env(
            values=base, credentials=creds, dry_run=dry_run, log=lambda _l: None,
            temp_dir=self.tmp, log_dir=self.tmp / "logs", results=self.tmp / "results.jsonl",
            reporter=get_reporter("plain"),
            oidc=(f"http://{self.oidc.netloc}/token?api-version=2.0",
                  destinations.Secret(OIDC_REQUEST)) if oidc and not dry_run else None)

    def calls(self) -> List[Dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def publish(self, **kw: Any) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                result = s3.publish(self.inputs(), self.tmp, self.env(**kw), False)
            except s3.PublishError as exc:
                return "REFUSED: " + str(exc) + "\n" + out.getvalue()
        return "OK: " + result.summary + "\n" + out.getvalue()

    def assertNoLeak(self, text: str) -> None:
        for leak in LEAKS:
            self.assertNotIn(leak, text)


# ---------------------------------------------------------------------------
# Canonical-path confinement
# ---------------------------------------------------------------------------

class ConfinementTests(unittest.TestCase):
    """Type: unit + invalid input. ADR 0005 D9 / CR-3: canonical path, not a string prefix."""

    def test_GoodPrefixesAndKey(self) -> None:
        for prefix in ("staging", "staging/libbios", "staging/a.b_c-d/e"):
            with self.subTest(prefix=prefix):
                self.assertEqual(s3.staging_prefix(prefix), prefix)
        self.assertEqual(s3.object_key("staging/libbios", "1.0.1", "4242", "3",
                                       "libbios-1.0.1-linux-x64.tar.gz.sha256"),
                         "staging/libbios/1.0.1/4242-3/libbios-1.0.1-linux-x64.tar.gz.sha256")
        self.assertEqual(s3.object_key("staging", "1.0.1", "1", "1", "manifest.json"),
                         "staging/1.0.1/1-1/manifest.json")

    def test_RejectedPrefixesNeverEchoed(self) -> None:
        for prefix in ("staging-release", "stagingX", "staging/../x", "staging/../release",
                       "/staging", "a/../../b", "staging/", "staging//x", "staging/.",
                       "staging/..", "", "Staging", "staging/a b", "staging\\x", "release",
                       "staging/x/../../release", " staging"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(s3.PublishError) as cm:
                    s3.staging_prefix(prefix)
                text = str(cm.exception)
                self.assertRegex(text, r"^CI_PUBLISH_S3_PREFIX: expected .+ -> actual <rejected>$")
                if prefix.strip():
                    self.assertNotIn(prefix.strip(), text.replace("staging[/", ""))

    def test_RejectedNamesVersionsAndRunIds(self) -> None:
        good = ("staging", "1.0.1", "4242", "1", "libbios-1.0.1-linux-x64.tar.gz")
        for index, bad in ((4, "dist/libbios-1.0.1-linux-x64.tar.gz"), (4, "../manifest.json"),
                           (4, "libbios-1.0.1-linux-x64.tar.gz/../../x"), (4, ".."),
                           (4, "libbios-2.0.0-linux-x64.tar.gz"), (4, "evil.sh"),
                           (4, "manifest.json/.."), (4, "libbios-1.0.1-linux-x64.tar.gz\n"),
                           (1, "1.0"), (1, "../1.0.1"), (1, "1.0.1/.."),
                           (2, "42a"), (2, ""), (2, "-1"), (3, "x"), (3, "1/2")):
            with self.subTest(part=index, value=bad):
                parts = list(good)
                parts[index] = bad
                with self.assertRaises(s3.PublishError) as cm:
                    s3.object_key(*parts)
                self.assertIn("-> actual <rejected>", str(cm.exception))
                if len(bad) > 2:
                    self.assertNotIn(bad, str(cm.exception))


class SourceFileTests(_S3Case):
    """Type: unit + invalid input. Local files: regular, non-symlink, directly in dist/."""

    def test_RegularFileAccepted(self) -> None:
        name = pa.archive_name(VERSION, "linux-x64")
        self.assertEqual(s3.source_file(self.tmp, name), (self.tmp / "dist" / name).resolve())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_SymlinkInDistIsRefused(self) -> None:
        dist = self.tmp / "dist"
        name = pa.archive_name(VERSION, "linux-x64")
        (self.tmp / "outside").write_bytes((dist / name).read_bytes())
        (dist / name).unlink()
        (dist / name).symlink_to(self.tmp / "outside")
        with self.assertRaisesRegex(s3.PublishError, "symlink"):
            s3.source_file(self.tmp, name)
        # The whole publish refuses too, before any aws call.
        (dist / name).unlink()
        (dist / name).symlink_to(dist / (name + ".sha256"))
        with self.assertRaises((s3.PublishError, pin.PublishInputError)):
            s3.publish(self.inputs(), self.tmp, self.env(), False)
        self.assertEqual(self.calls(), [])

    def test_MissingAndDirectoryAreRefused(self) -> None:
        with self.assertRaisesRegex(s3.PublishError, "missing"):
            s3.source_file(self.tmp, "libbios-1.0.1-macos-x64.tar.gz")
        (self.tmp / "dist" / "manifest.json").unlink()
        (self.tmp / "dist" / "manifest.json").mkdir()
        with self.assertRaisesRegex(s3.PublishError, "regular file"):
            s3.source_file(self.tmp, "manifest.json")


class SettingsTests(_S3Case):
    """Type: unit + invalid input. Configuration is validated before any request."""

    def test_BadValuesAreRefusedUnechoedBeforeAnyCall(self) -> None:
        for name, value in (("CI_PUBLISH_S3_BUCKET", "Bad_Bucket"),
                            ("CI_PUBLISH_S3_BUCKET", "a..b"),
                            ("CI_PUBLISH_S3_ROLE", "arn:aws:iam::12:user/x"),
                            ("CI_PUBLISH_S3_REGION", "mars"),
                            ("CI_PUBLISH_S3_PREFIX", "staging-release")):
            with self.subTest(name=name):
                env = self.env()
                creds = dict(env.credentials)
                creds[name] = destinations.Secret(value)
                env = destinations.Env(values=env.values, credentials=creds, temp_dir=self.tmp,
                                       log_dir=env.log_dir, results=env.results,
                                       reporter=env.reporter, oidc=env.oidc)
                with self.assertRaises(s3.PublishError) as cm:
                    s3.publish(self.inputs(), self.tmp, env, False)
                self.assertIn(name, str(cm.exception))
                self.assertNotIn(value, str(cm.exception))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.oidc.requests, [])

    def test_NonNumericRunIdOrAttemptIsRefused(self) -> None:
        for values in ({"CI_RUN_ID": "12ab"}, {"CI_RUN_ATTEMPT": "x"}, {"CI_RUN_ID": ""}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(s3.PublishError, "run id: expected decimal"):
                    s3.publish(self.inputs(), self.tmp, self.env(**values), False)
        self.assertEqual(self.calls(), [])

    def test_SettingsReprHidesTheRole(self) -> None:
        conf = s3.settings(self.env())
        self.assertNoLeak(repr(conf) + str(conf) + repr(self.env()))


# ---------------------------------------------------------------------------
# Dry run, real run, create-only
# ---------------------------------------------------------------------------

class DryRunTests(_S3Case):
    """Type: unit. D9 ``--dry-run``: exact files and keys; no network, no child, no credential."""

    def test_DryRunListsEveryKeyWithoutAnyCall(self) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("network or child process in a dry run")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=boom), \
                mock.patch.object(subprocess, "Popen", side_effect=boom):
            result = s3.publish(self.inputs(), self.tmp, self.env(dry_run=True), True)
        keys = [line.split(" -> ")[1].split(" ")[0] for line in result.lines if "put-object" in line]
        self.assertEqual(len(keys), 7)
        self.assertEqual(keys[0], f"s3://<bucket>/<prefix>/{VERSION}/{RUN_ID}-2/"
                                  f"{pa.archive_name(VERSION, 'linux-x64')}")
        self.assertEqual(keys[-1], f"s3://<bucket>/<prefix>/{VERSION}/{RUN_ID}-2/manifest.json")
        self.assertTrue(all("--if-none-match *" in line for line in result.lines
                            if "put-object" in line))
        self.assertEqual(result.requests, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.oidc.requests, [])


class PublishTests(_S3Case):
    """Type: mock. Create-only writes through the step runner, then a re-read."""

    def test_SuccessCreatesEveryObjectOnceAndRereadsIt(self) -> None:
        text = self.publish()
        self.assertTrue(text.startswith("OK: 7 objects created under s3://" + BUCKET +
                                        f"/{PREFIX}/{VERSION}/{RUN_ID}-2/ (create-only), 7 "
                                        "re-read"), text)
        calls = self.calls()
        self.assertEqual(calls[0]["argv"], ["--version"])
        puts = [c for c in calls if c["argv"][:2] == ["s3api", "put-object"]]
        gets = [c for c in calls if c["argv"][:2] == ["s3api", "get-object"]]
        self.assertEqual((len(puts), len(gets), len(calls)), (7, 7, 15))
        for call in puts:
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--if-none-match") + 1], "*")
            self.assertTrue(argv[argv.index("--key") + 1].startswith(
                f"{PREFIX}/{VERSION}/{RUN_ID}-2/"))
            self.assertIn("not-a-release=true", argv[argv.index("--metadata") + 1])
            self.assertIn("--checksum-sha256", argv)
        self.assertFalse(any("delete" in " ".join(c["argv"]) for c in calls))
        stored = sorted(p.name for p in (self.store / BUCKET).rglob("*") if p.is_file())
        self.assertEqual(len(stored), 7)
        self.assertNoLeak(text + (self.tmp / "results.jsonl").read_text())

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_ChildGetsTheTokenFileOnlyAndNoCredentialName(self) -> None:
        with mock.patch.dict(os.environ, {"CI_PUBLISH_S3_ROLE": "leak-me",
                                          "AWS_SECRET_ACCESS_KEY": "leak-me-too",
                                          "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "leak-me-three"}):
            self.assertTrue(self.publish().startswith("OK: "))
        for call in self.calls():
            env = call["env"]
            aws_names = {k for k in env if k.startswith("AWS_")}
            self.assertLessEqual(aws_names, set(s3.CHILD_ENV_NAMES))
            self.assertLessEqual({"AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_REGION",
                                  "AWS_ROLE_SESSION_NAME"}, aws_names)
            self.assertFalse([k for k in env if k.startswith("CI_PUBLISH_")])
            self.assertNotIn("leak-me-too", json.dumps(env))
            self.assertNotIn("leak-me-three", json.dumps(env))
            self.assertEqual(env["AWS_ROLE_ARN"], ROLE)
            self.assertEqual(env["AWS_EC2_METADATA_DISABLED"], "true")
            self.assertEqual((call["token_mode"], call["token_dir_mode"], call["token_ok"]),
                             ("0o600", "0o700", True))
        token_file = Path(self.calls()[0]["env"]["AWS_WEB_IDENTITY_TOKEN_FILE"])
        self.assertFalse(token_file.exists(), "the token file is removed when the phase ends")
        self.assertFalse(token_file.parent.exists())
        for name in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"):
            self.assertNotIn(name, os.environ, "the parent environment is restored")

    def test_ExistingKeyIsAHardFailureWithNoSecondWrite(self) -> None:
        """Regression (CR-3): PreconditionFailed is never retried or overwritten."""
        with mock.patch.dict(os.environ, {"STUB_MODE": "exists-3"}):
            text = self.publish()
        self.assertTrue(text.startswith("REFUSED: put-object "), text)
        self.assertIn("expected a new key -> actual key exists (create-only", text)
        calls = self.calls()
        puts = [c for c in calls if c["argv"][:2] == ["s3api", "put-object"]]
        self.assertEqual(len(puts), 3, "no write after the refused one, no retry")
        self.assertFalse([c for c in calls if c["argv"][:2] != ["s3api", "put-object"]
                          and c["argv"] != ["--version"]])
        self.assertNoLeak(text)

    def test_SecondAttemptOfTheSameRunWritesAFreshLocation(self) -> None:
        self.assertTrue(self.publish().startswith("OK: "))
        again = self.publish()  # same attempt: every key exists
        self.assertTrue(again.startswith("REFUSED: "), again)
        self.assertIn("key exists", again)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = s3.publish(self.inputs(), self.tmp, self.env(CI_RUN_ATTEMPT="3"), False)
        self.assertIn(f"/{RUN_ID}-3/", result.summary)

    def test_OtherAwsFailureNamesNoRole(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_MODE": "fail-put-3"}):
            text = self.publish()
        self.assertIn("expected created -> actual exit 254", text)
        self.assertNoLeak(text + (self.tmp / "results.jsonl").read_text())

    def test_AwsVersionMustEqualThePin(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_VERSION": "1.0.0"}):
            text = self.publish()
        self.assertIn(f"aws CLI: expected {s3.pinned_aws_version()} (pins.json) -> actual 1.0.0",
                      text)
        self.assertEqual([c["argv"] for c in self.calls()], [["--version"]])

    def test_RereadDigestMismatchFails(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_MODE": "tamper-get"}):
            text = self.publish()
        self.assertRegex(text, r"REFUSED: \S+ bytes: expected \d+ \(manifest\) -> actual more")


class OidcTests(_S3Case):
    """Type: mock. The OIDC exchange: one host, no redirect, value-free errors (CR-5, RP-8)."""

    def test_RequestCarriesAudienceAndBearerOnlyToTheService(self) -> None:
        self.assertTrue(self.publish().startswith("OK: "))
        ((path, auth),) = self.oidc.requests
        self.assertIn("audience=sts.amazonaws.com", path)
        self.assertIn("api-version=2.0", path)
        self.assertEqual(auth, "Bearer " + OIDC_REQUEST)

    def test_ErrorPathsAreValueFree(self) -> None:
        for mode, needle in (("403", "expected 200 -> actual 403"),
                             ("500", "expected 200 -> actual 500"),
                             ("redirect", "redirect refused"),
                             ("bad-jwt", "expected a JWT -> actual <rejected>")):
            with self.subTest(mode=mode):
                self.oidc.mode = mode
                text = self.publish()
                self.assertTrue(text.startswith("REFUSED: "), text)
                self.assertIn(needle, text)
                self.assertNoLeak(text)
        self.assertEqual(self.calls(), [], "no aws call without a token")

    def test_NetworkErrorAndRefusedHost(self) -> None:
        self.oidc.close()
        text = self.publish()
        self.assertIn("OIDC token request: expected 200 -> actual network error", text)
        with mock.patch.object(s3, "OIDC_SCHEME", "https"):
            text = self.publish()
        self.assertIn("allowlisted HTTPS host -> actual refused", text)
        self.assertIn("unavailable", self.publish(oidc=False))
        self.assertEqual(self.calls(), [])

    def test_ProductionAllowlistIsHttpsGithubTokenService(self) -> None:
        with mock.patch.object(s3, "OIDC_SCHEME", "https"), \
                mock.patch.object(s3, "OIDC_HOST_SUFFIXES", (".actions.githubusercontent.com",)):
            self.assertTrue(s3._oidc_host_ok(
                "https://pipelinesghubeus1.actions.githubusercontent.com/x/idtoken?api-version=2"))
            for url in ("http://pipelines.actions.githubusercontent.com/x",
                        "https://actions.githubusercontent.com.evil.example/x",
                        "https://evil.example/actions.githubusercontent.com"):
                self.assertFalse(s3._oidc_host_ok(url), url)


# ---------------------------------------------------------------------------
# Stage level: the staging tier
# ---------------------------------------------------------------------------

def _registry(path: Path, enabled: bool = True) -> Path:
    path.write_text(json.dumps({"schema": 1, "destinations": [
        {"name": "s3-staging", "enabled": enabled, "tier": "staging",
         "permission_class": "oidc", "environment": "staging",
         "credentials": ["CI_PUBLISH_S3_BUCKET", "CI_PUBLISH_S3_PREFIX", "CI_PUBLISH_S3_ROLE",
                         "CI_PUBLISH_S3_REGION"], "binary_cache_policy": "any"}]}),
        encoding="utf-8")
    return path


class StagingTierTests(_S3Case):
    """Type: integration (in-process stage.py). Push to master only, after verify, in its class job."""

    def stage(self, *args: str, enabled: bool = True, **env: str) -> Dict[str, Any]:
        values = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
        values.update({
            "CI_PLATFORM": "github", "CI_REPORTER": "plain", "CI_EVENT": "push",
            "CI_REF": "refs/heads/master", "CI_REF_NAME": "master", "CI_COMMIT": COMMIT,
            "CI_RUN_ID": RUN_ID, "CI_RUN_ATTEMPT": "1", "CI_JOB": "publish-staging-oidc",
            "CI_TEMP_DIR": str(self.tmp), "CI_ARTIFACT_DIR": str(self.tmp),
            "CI_LOG_DIR": str(self.tmp / "logs"), "CI_RESULTS_FILE": str(self.tmp / "r.jsonl"),
            "CI_MANIFEST_SHA256": self.anchor, "CI_VERIFIED_MANIFEST_SHA256": self.anchor,
            "CI_BUILD_BINARY_CACHE": json.dumps({leg: "restored" for leg in LEGS}),
            "CI_DESTINATION": "s3-staging",
            "CI_PUBLISH_S3_BUCKET": BUCKET, "CI_PUBLISH_S3_PREFIX": PREFIX,
            "CI_PUBLISH_S3_ROLE": ROLE, "CI_PUBLISH_S3_REGION": "eu-central-1",
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"http://{self.oidc.netloc}/token?api-version=2.0",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": OIDC_REQUEST})
        values.update(env)
        out = io.StringIO()
        with mock.patch.dict(os.environ, values, clear=True), \
                mock.patch.object(stage, "DESTINATIONS_FILE",
                                  _registry(self.tmp / "dests.json", enabled)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = stage.main(["publish", *args])
            left = sorted(k for k in os.environ
                          if k.startswith(("CI_PUBLISH_", "ACTIONS_ID_TOKEN")))
        results = self.tmp / "r.jsonl"
        text = out.getvalue() + (results.read_text() if results.exists() else "")
        self.assertNoLeak(text)
        self.assertEqual(left, [], "credentials and the OIDC request leave os.environ")
        return {"rc": rc, "text": text}

    def test_MasterPushInItsClassJobPublishesRestoredLegs(self) -> None:
        run = self.stage()
        self.assertEqual(run["rc"], 0, run["text"])
        self.assertIn("ok: publish s3-staging: 7 objects created under s3://", run["text"])

    def test_OtherEventsAreRefusedBeforeAnyCall(self) -> None:
        for env in ({"CI_EVENT": "tag", "CI_REF": "refs/tags/v1.0.1", "CI_REF_NAME": "v1.0.1"},
                    {"CI_EVENT": "pr", "CI_REF": "refs/pull/3/merge", "CI_REF_NAME": "3/merge"},
                    {"CI_EVENT": "schedule"},
                    {"CI_EVENT": "push", "CI_REF": "refs/heads/topic", "CI_REF_NAME": "topic"}):
            with self.subTest(env=env):
                run = self.stage(**env)
                self.assertEqual(run["rc"], 1, run["text"])
                self.assertIn("expected a push to master", run["text"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.oidc.requests, [])

    def test_DisabledVerifyGateAndWrongJobAreRefused(self) -> None:
        for kw, needle in (({"enabled": False}, "expected enabled"),
                           ({"CI_VERIFIED_MANIFEST_SHA256": ""}, "verify gate: expected"),
                           ({"CI_JOB": "publish-release-repo-write"},
                            "expected a staging-tier oidc job"),
                           ({"CI_JOB": "release-draft"}, "expected a staging-tier oidc job"),
                           ({"CI_JOB": "evil\njob"}, "actual <rejected>"),
                           ({"CI_BUILD_BINARY_CACHE": ""}, "CI_BUILD_BINARY_CACHE: expected"),
                           ({"CI_COMMIT": "c" * 40}, "manifest.commit: expected")):
            with self.subTest(kw=kw):
                run = self.stage(**kw)
                self.assertEqual(run["rc"], 1, run["text"])
                self.assertIn(needle, run["text"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.oidc.requests, [])

    def test_CrashAndFailurePathsPrintNoCanary(self) -> None:
        """Constraint 3 / RP-8 for this destination: crash, 4xx/5xx and aws failure paths."""
        def crash(*_a: Any, **_k: Any) -> None:
            raise RuntimeError(f"boom {ROLE} {JWT} {OIDC_REQUEST}")
        with mock.patch.object(s3, "publish", crash):
            run = self.stage()
        self.assertEqual(run["rc"], 70, run["text"])
        self.assertIn("internal error (RuntimeError)", run["text"])
        for mode in ("403", "500", "redirect"):
            with self.subTest(oidc=mode):
                self.oidc.mode = mode
                run = self.stage()
                self.assertEqual(run["rc"], 1, run["text"])
        self.oidc.mode = ""
        with mock.patch.dict(os.environ, {"STUB_MODE": "fail-put-3"}):
            run = self.stage()
        self.assertEqual(run["rc"], 1, run["text"])
        self.assertIn("expected created -> actual exit 254", run["text"])
        log_text = "".join(p.read_text(errors="replace") for p in (self.tmp / "logs").rglob("*.log"))
        self.assertIn(ROLE, log_text, "the aws output (naming the role) stays in the runner log")

    def test_OnlyIsRefusedForASinglePhaseDestination(self) -> None:
        run = self.stage("--only", "draft")
        self.assertEqual(run["rc"], 2, run["text"])
        self.assertIn("single phase", run["text"])

    def test_DryRunReadsNoCredentialAndCallsNothing(self) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("network, child or credential in a dry run")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=boom), \
                mock.patch.object(subprocess, "Popen", side_effect=boom), \
                mock.patch.object(destinations, "Secret", side_effect=boom):
            run = self.stage("--dry-run", enabled=False)
        self.assertEqual(run["rc"], 0, run["text"])
        self.assertEqual(len([ln for ln in run["text"].splitlines()
                              if "would put-object" in ln]), 7)
        self.assertIn("disabled (real run refuses)", run["text"])
        self.assertIn("credential unread", run["text"])
        self.assertNotIn(BUCKET, run["text"])
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
