"""The ``s3-staging`` publish destination (milestone 0012 Story 04.0 Task 05; ADR 0005 D9 worked example).

Keeps a disposable copy of every verified ``master`` build for testers, outside
GitHub Releases, at::

    s3://<bucket>/<prefix>/<version>/<run-id>-<attempt>/<file>

Tier ``staging``, permission class ``oidc``, environment ``staging``. It runs
only on a push to ``refs/heads/master``, after ``verify`` passed in the same
run (``stage.py`` enforces the event, the verify gate and the manifest anchor
before this module is called), never on a pull request, fork, tag, nightly or
dispatch. It accepts ``binary_cache: restored`` (a staging copy is disposable,
CR-2) and is **not a release**: every object carries
``x-amz-meta-not-a-release: true`` (threat model 2026-11-18 RP-14). The
registry entry ships ``enabled: false``; enabling it is a maintainer action
(bucket, OIDC trust, the ``staging`` environment and the cloud-side policy of
setting S-9, which no code here can enforce).

Rules (ADR 0005 D9 as amended by threat model 2026-11-13 CR-3/CR-5, and
2026-11-18 constraints 1, 2, 3, 7, RP-14):

- **Canonical-path confinement, not a string prefix.** Every key is built only
  from validated parts (:func:`object_key`): each ``/``-separated prefix
  component matches ``[A-Za-z0-9._-]+`` and is neither ``.`` nor ``..``, the
  first is exactly ``staging`` (so ``staging-release``, ``stagingX``,
  ``staging/../release``, ``/staging`` and ``a/../../b`` are all refused);
  the version is ``X.Y.Z[-pre]``; the run id and attempt are decimal; the
  file is a D4 basename of this version or ``manifest.json``. The assembled
  key must equal its own ``posixpath.normpath`` and start with ``staging/``.
  Rejection messages never echo the rejected value.
- **Local files** are only the manifest's (``ReleaseInputs.assets``, each
  already checked against the anchored manifest), re-resolved here to a
  regular, non-symlink file directly under ``$CI_ARTIFACT_DIR/dist/``.
- **Create-only, never a delete (constraint 7).** Every upload is
  ``aws s3api put-object --if-none-match *``: an existing key, including one
  from an earlier attempt of the same run, makes S3 answer
  ``PreconditionFailed``, which is a hard failure. Nothing is retried, nothing
  is overwritten, no further object is written after a failure, and this
  module never issues a delete. ``--checksum-sha256`` carries the manifest
  digest, so S3 itself refuses bytes that differ from the manifest.
- **The aws CLI runs through the step runner** (:func:`ci_step.run_step`) as
  an argv list, never through a shell. Its output stays in the step log: the
  RC1 line carries a fixed, value-free first diagnostic, never an aws error
  text (which can name the assumed role). Its version must equal
  ``pins.json`` ``tools.aws_cli`` (ADR 0005 D3, D9).
- **Credentials (CR-5).** The role is assumed through OIDC, no stored keys.
  This module requests the OIDC token itself (``Env.oidc``: the platform's
  token service URL and request token, supplied by the reporter backend), with
  ``Authorization`` added by ``add_unredirected_header``, no redirect followed,
  and only to an HTTPS host of :data:`OIDC_HOST_SUFFIXES`. It writes the token
  to a 0600 file in a private directory under ``CI_TEMP_DIR`` and passes the
  child only ``AWS_WEB_IDENTITY_TOKEN_FILE``, ``AWS_ROLE_ARN`` and the fixed
  ``AWS_*`` settings of :data:`CHILD_ENV_NAMES`, by name; every other
  ``CI_PUBLISH_*``/``AWS_*`` name is scrubbed from the child environment. The
  token file is removed when the phase ends.
- **Logs** show counts and the base location only, never the role ARN, a
  bucket policy, a presigned URL or a credential.
- **The cloud-side policy is the real boundary** (ADR 0005 "Repository
  settings required", setting S-9): a write outside ``staging/*``, a delete,
  or a role assumption from another repository or environment must fail at
  AWS even if this file were edited. These checks are defence in depth.

A library: it prints nothing (P11). Python 3.9+ standard library only.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Mapping, Sequence, Tuple

import ci_step
import package_archive as pa
import publish_inputs as pin
from destinations import Env, Result, Secret

#: Registry key (ADR 0005 D9).
NAME = "s3-staging"
#: Neutral names this destination reads. ``CI_PUBLISH_S3_REGION`` is an
#: addition to the three names of ADR 0005 D9: ``aws s3api`` needs a region
#: and must not guess one (recorded in the Task 05 as-built notes).
REQUIRED_ENV: Tuple[str, ...] = ("CI_PUBLISH_S3_BUCKET", "CI_PUBLISH_S3_PREFIX",
                                 "CI_PUBLISH_S3_ROLE", "CI_PUBLISH_S3_REGION",
                                 "CI_RUN_ID", "CI_RUN_ATTEMPT", "CI_COMMIT")

#: The only first prefix component (ADR 0005 D9).
STAGING_ROOT = "staging"
_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
_DIGITS = re.compile(r"^\d{1,20}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
_ROLE = re.compile(r"^arn:aws[a-z-]{0,20}:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
_JWT = re.compile(r"^[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{1,4096}$")
_AWS_VERSION = re.compile(r"^aws-cli/(\d{1,4}\.\d{1,4}\.\d{1,4})\s")
_PIN_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")

#: How the aws CLI is started (argv prefix; tests substitute a stub interpreter + script).
AWS: Tuple[str, ...] = ("aws",)
#: Audience of the OIDC token (the AWS STS web-identity audience).
OIDC_AUDIENCE = "sts.amazonaws.com"
#: HTTPS hosts the OIDC token request may go to (the platform's token service).
OIDC_HOST_SUFFIXES: Tuple[str, ...] = (".actions.githubusercontent.com",)
#: The only scheme of the OIDC request (tests substitute a loopback ``http`` stub).
OIDC_SCHEME = "https"
OIDC_TIMEOUT_S = 30.0
OIDC_MAX_BYTES = 64 * 1024
#: ``AWS_*`` names the aws child receives, by name (everything else ``AWS_*`` is scrubbed).
CHILD_ENV_NAMES: Tuple[str, ...] = (
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME", "AWS_REGION",
    "AWS_EC2_METADATA_DISABLED", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_PAGER")
#: Per-object time limit of one aws call.
AWS_TIMEOUT_S = 300.0
#: The aws error code of a conditional write that found an existing key.
_PRECONDITION = re.compile(r"PreconditionFailed|\(412\)")
_PINS_FILE = Path(__file__).resolve().parents[1] / "pins.json"


class PublishError(Exception):
    """A refused or failed staging step; the message is one value-free RC1 line."""


@dataclass(frozen=True)
class Settings:
    """The validated configuration of one run (never rendered: ``repr`` hides the role)."""

    bucket: str
    prefix: str
    role: Secret
    region: str
    run_id: str
    attempt: str

    def __repr__(self) -> str:
        return f"Settings(bucket=<set>, prefix={self.prefix}, role=<redacted>)"


# ---------------------------------------------------------------------------
# Canonical-path confinement (ADR 0005 D9, CR-3)
# ---------------------------------------------------------------------------

def staging_prefix(raw: str) -> str:
    """Return ``raw`` if it is ``staging`` or ``staging/<safe components>``.

    No ``strip()``: a stray ``/`` (leading, trailing or doubled) is an empty
    component and is refused.

    Raises:
        PublishError: The prefix is outside ``staging[/...]`` (never echoed).
    """
    parts = raw.split("/") if isinstance(raw, str) else [""]
    if parts[0] != STAGING_ROOT:
        raise PublishError("CI_PUBLISH_S3_PREFIX: expected staging[/<component>...] -> actual "
                           "<rejected>")
    for part in parts:
        if part in (".", "..") or not _COMPONENT.fullmatch(part):
            raise PublishError("CI_PUBLISH_S3_PREFIX: expected safe components [A-Za-z0-9._-], "
                               "no . or .. -> actual <rejected>")
    return "/".join(parts)


def _file_name(version: str, name: str) -> str:
    """Return ``name`` if it is a D4 basename of ``version`` (or ``manifest.json``)."""
    legs = "|".join(f"{system}-{arch}" for system in ("linux", "windows", "macos")
                    for arch in ("x64", "arm64"))
    pattern = (rf"^{re.escape(pa.ARCHIVE_PREFIX)}-{re.escape(version)}-(?:{legs})"
               r"\.(?:tar\.gz|zip)(?:\.sha256)?$")
    if name != pa.MANIFEST_NAME and not re.fullmatch(pattern, name or ""):
        raise PublishError("file: expected a D4 basename libbios-<version>-<leg>.<ext>[.sha256] "
                           "or manifest.json -> actual <rejected>")
    return name


def object_key(prefix: str, version: str, run_id: str, attempt: str, name: str) -> str:
    """Build ``<prefix>/<version>/<run-id>-<attempt>/<file>`` from validated parts only.

    Raises:
        PublishError: Any part is outside its allowlist, or the assembled key
            is not canonical (``posixpath.normpath``) under ``staging/``.
    """
    prefix = staging_prefix(prefix)
    if not _VERSION.fullmatch(version or ""):
        raise PublishError("version: expected X.Y.Z[-pre] -> actual <rejected>")
    if not (_DIGITS.fullmatch(run_id or "") and _DIGITS.fullmatch(attempt or "")):
        raise PublishError("run id: expected decimal CI_RUN_ID and CI_RUN_ATTEMPT -> actual "
                           "<rejected>")
    name = _file_name(version, name)
    key = f"{prefix}/{version}/{run_id}-{attempt}/{name}"
    if posixpath.normpath(key) != key or not key.startswith(STAGING_ROOT + "/"):
        raise PublishError("key: expected a canonical staging/ key -> actual <rejected>")
    return key


def source_file(artifact_dir: Path, name: str) -> Path:
    """Return ``$CI_ARTIFACT_DIR/dist/<name>`` if it is a regular, non-symlink file directly there.

    Raises:
        PublishError: ``dist/`` is missing, or the file is missing, a symlink,
            not a regular file, or resolves outside ``dist/``.
    """
    try:
        dist = Path(artifact_dir, "dist").resolve(strict=True)
    except OSError:
        raise PublishError("dist: expected a directory -> actual missing") from None
    path = dist / name
    if not path.is_symlink() and not path.exists():
        raise PublishError("file: expected a regular file in dist/ -> actual missing")
    try:
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != dist:
            raise PublishError("file: expected a regular file directly in dist/ -> actual a "
                               "symlink, another kind or elsewhere")
    except OSError:
        raise PublishError("file: expected a regular file in dist/ -> actual missing") from None
    return path


def settings(env: Env) -> Settings:
    """Validate the configuration a real run needs (bucket, prefix, role, region, run id).

    Raises:
        PublishError: The first value that is unset or outside its allowlist.
    """
    def value(name: str) -> str:
        return env.secret(name).reveal()

    bucket, region = value("CI_PUBLISH_S3_BUCKET"), value("CI_PUBLISH_S3_REGION")
    if not _BUCKET.fullmatch(bucket) or ".." in bucket:
        raise PublishError("CI_PUBLISH_S3_BUCKET: expected an S3 bucket name -> actual "
                           "<rejected>")
    prefix = staging_prefix(value("CI_PUBLISH_S3_PREFIX"))
    if not _ROLE.fullmatch(value("CI_PUBLISH_S3_ROLE")):
        raise PublishError("CI_PUBLISH_S3_ROLE: expected an IAM role ARN -> actual <rejected>")
    if not _REGION.fullmatch(region):
        raise PublishError("CI_PUBLISH_S3_REGION: expected an AWS region -> actual <rejected>")
    run_id, attempt = env.get("CI_RUN_ID"), env.get("CI_RUN_ATTEMPT")
    if not (_DIGITS.fullmatch(run_id) and _DIGITS.fullmatch(attempt)):
        raise PublishError("run id: expected decimal CI_RUN_ID and CI_RUN_ATTEMPT -> actual "
                           "<rejected>")
    return Settings(bucket, prefix, env.secret("CI_PUBLISH_S3_ROLE"), region, run_id, attempt)


# ---------------------------------------------------------------------------
# The D9 interface
# ---------------------------------------------------------------------------

def _run_parts(env: Env, dry_run: bool) -> Tuple[str, str]:
    run_id, attempt = env.get("CI_RUN_ID"), env.get("CI_RUN_ATTEMPT")
    if dry_run:
        run_id = run_id if _DIGITS.fullmatch(run_id) else "0"
        attempt = attempt if _DIGITS.fullmatch(attempt) else "1"
    return run_id, attempt


def _keys(manifest: pin.ReleaseInputs, prefix: str, run_id: str, attempt: str
          ) -> List[Tuple[pin.Asset, str]]:
    return [(a, object_key(prefix, manifest.manifest.version, run_id, attempt, a.name))
            for a in manifest.assets]


def validate(manifest: pin.ReleaseInputs, env: Env) -> None:
    """D9 ``validate``: every key and every local file, before any write; no network.

    In a dry run (``env.dry_run``) the configured prefix is not read,
    so the keys are checked under the minimal valid prefix ``staging``; a real
    run validates the configured prefix, bucket, role and region as well.

    Raises:
        PublishError: The first part, key or file that is refused.
    """
    dry_run = env.dry_run
    run_id, attempt = _run_parts(env, dry_run)
    prefix = STAGING_ROOT if dry_run else settings(env).prefix
    if not manifest.assets:
        raise PublishError("manifest: expected at least one file -> actual none")
    for asset, _key in _keys(manifest, prefix, run_id, attempt):
        if asset.path.name != asset.name:
            raise PublishError("file: expected the manifest's own name -> actual another name")


def publish(manifest: pin.ReleaseInputs, artifact_dir: Path, env: Env, dry_run: bool) -> Result:
    """D9 ``publish``: create one object per manifest file, then re-read each (create-only).

    A dry run makes no request, starts no child process and reads no
    credential: it lists every file with the key it would get under the
    configured ``<prefix>`` (unread) and bucket.

    Raises:
        PublishError: The first refusal or failed write; nothing is written
            after it, and nothing is retried or overwritten.
    """
    validate(manifest, env)
    version = manifest.manifest.version
    if dry_run:
        run_id, attempt = _run_parts(env, True)
        rows = _keys(manifest, STAGING_ROOT, run_id, attempt)
        base = f"s3://<bucket>/<prefix>/{version}/{run_id}-{attempt}/"
        lines = [f"info: dry run: would request an OIDC token ({OIDC_AUDIENCE}) for the "
                 "CI_PUBLISH_S3_ROLE role via a 0600 token file; bucket, prefix, role, region: "
                 "staging secrets, unread in a dry run"]
        for asset, key in rows:
            source_file(Path(artifact_dir), asset.name)
            rel = key[len(STAGING_ROOT) + 1:]
            lines.append(f"info: dry run: would put-object dist/{asset.name} ({asset.size} "
                         f"bytes, sha256 {asset.sha256[:16]}) -> s3://<bucket>/<prefix>/{rel} "
                         "(--if-none-match *, metadata not-a-release=true)")
        lines.append(f"info: dry run: would then get-object each of the {len(rows)} keys and "
                     "compare size and sha256 with the manifest; never a delete")
        return Result(f"{len(rows)} objects under {base}", (base,), tuple(lines), 0)

    conf = settings(env)
    rows = _keys(manifest, conf.prefix, conf.run_id, conf.attempt)
    sources = [(asset, key, source_file(Path(artifact_dir), asset.name)) for asset, key in rows]
    base = f"s3://{conf.bucket}/{conf.prefix}/{version}/{conf.run_id}-{conf.attempt}/"
    with _aws_session(env, conf) as session:
        check_aws_version(env, session)
        for index, (asset, key, path) in enumerate(sources, 1):
            _put(env, session, conf, asset, key, path, index, len(sources))
        checked = _reread(env, session, conf, rows)
    return Result(f"{len(sources)} objects created under {base} (create-only), {checked} "
                  "re-read and matching the manifest", (base,), (), 2 * len(sources) + 1)


def check_published(manifest: pin.ReleaseInputs, env: Env) -> str:
    """D9 ``check_published``: download every object and compare size and sha256.

    The staging bucket is private, so this needs the same OIDC role as the
    upload; :func:`publish` calls it in the same job right after the writes.

    Raises:
        PublishError: An object is missing, of another size or digest.
    """
    validate(manifest, env)
    conf = settings(env)
    rows = _keys(manifest, conf.prefix, conf.run_id, conf.attempt)
    with _aws_session(env, conf) as session:
        checked = _reread(env, session, conf, rows)
    return f"{checked} objects re-read and matching the manifest"


# ---------------------------------------------------------------------------
# OIDC token and the aws child
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The OIDC request follows no redirect at all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise PublishError("OIDC token request: expected no redirect -> actual redirect refused")


def _oidc_host_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    return parts.scheme == OIDC_SCHEME and any(host.endswith(s) or netloc == s
                                               for s in OIDC_HOST_SUFFIXES)


def request_oidc_token(env: Env) -> Secret:
    """Exchange the platform's request token for an OIDC ID token (audience :data:`OIDC_AUDIENCE`).

    Raises:
        PublishError: No token service, a refused host, a non-200 answer, a
            network error or a malformed token (value-free messages only).
    """
    if env.oidc is None:
        raise PublishError("OIDC token service: expected id-token: write in this job -> actual "
                           "unavailable")
    url, bearer = env.oidc
    if not _oidc_host_ok(url) or not bearer:
        raise PublishError("OIDC token service: expected an allowlisted HTTPS host -> actual "
                           "refused")
    query = urllib.parse.urlencode({"audience": OIDC_AUDIENCE})
    full = url + ("&" if urllib.parse.urlsplit(url).query else "?") + query
    request = urllib.request.Request(full, method="GET")
    request.add_unredirected_header("Authorization", "Bearer " + bearer.reveal())
    request.add_header("Accept", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=OIDC_TIMEOUT_S) as response:
            status = response.status
            body = response.read(OIDC_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PublishError(f"OIDC token request: expected 200 -> actual {exc.code}") from None
    except PublishError:
        raise
    except (urllib.error.URLError, OSError, ValueError):
        raise PublishError("OIDC token request: expected 200 -> actual network error") from None
    if status != 200 or len(body) > OIDC_MAX_BYTES:
        raise PublishError(f"OIDC token request: expected 200 and at most {OIDC_MAX_BYTES} "
                           f"bytes -> actual {status}")
    try:
        token = json.loads(body.decode("utf-8")).get("value")
    except (UnicodeDecodeError, ValueError, AttributeError, RecursionError):
        token = None
    if not (isinstance(token, str) and _JWT.fullmatch(token)):
        raise PublishError("OIDC token: expected a JWT -> actual <rejected>")
    return Secret(token)


@dataclass(frozen=True)
class _Session:
    """The private token directory and the exact child environment of one aws session."""

    directory: Path
    child_env: Mapping[str, str]


@contextlib.contextmanager
def _aws_session(env: Env, conf: Settings) -> Iterator[_Session]:
    """Write the OIDC token to a 0600 file in a private directory; remove it on exit."""
    token = request_oidc_token(env)
    directory = Path(tempfile.mkdtemp(prefix="s3-oidc-", dir=str(env.temp_dir)))
    try:
        os.chmod(directory, 0o700)
        token_file = directory / "token"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(token_file), flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token.reveal())
        child_env = {
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(token_file),
            "AWS_ROLE_ARN": conf.role.reveal(),
            "AWS_ROLE_SESSION_NAME": f"libbios-staging-{conf.run_id}-{conf.attempt}"[:64],
            "AWS_REGION": conf.region,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": str(directory / "no-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(directory / "no-credentials"),
            "AWS_PAGER": "",
        }
        yield _Session(directory, child_env)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextlib.contextmanager
def _child_env(extra: Mapping[str, str]) -> Iterator[None]:
    """Set ``extra`` in ``os.environ`` for one step-runner call, then restore every name."""
    saved = {k: os.environ.get(k) for k in extra}
    os.environ.update(extra)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _aws(env: Env, session: _Session, label: str, argv: Sequence[str], first: str,
         summary: str = "ok") -> Tuple[int, Path]:
    """Run one aws call through the step runner; return ``(exit, log path)``.

    The RC1 line of a failure carries ``first`` (fixed, value-free), never
    the aws output: no excerpt, no tail, no diagnostic pattern (the log stays
    under ``CI_LOG_DIR`` on the runner and is never uploaded by a publish job).
    """
    def finish(rc: int) -> ci_step.StepFinish:
        if rc != 0:
            return ci_step.StepFinish(first=first)
        if argv == ["--version"]:  # the version only, never the rest of the line
            m = _AWS_VERSION.match(_read_log(ci_step.log_path_for(label, env.log_dir), 4096)
                                   .strip() + " ")
            return ci_step.StepFinish(summary=f"aws-cli {m.group(1) if m else 'unknown'}")
        return ci_step.StepFinish()

    with _child_env(session.child_env):
        result = ci_step.run_step(label, [*AWS, *argv], patterns=(), best_effort_reason=None,
                                  summary=summary, max_diag=0, tail=0, log_dir=env.log_dir,
                                  reporter=env.reporter, results_file=env.results,  # type: ignore[arg-type]
                                  timeout_s=AWS_TIMEOUT_S, pass_env=CHILD_ENV_NAMES,
                                  finish=finish)
    return result.exit, ci_step.log_path_for(label, env.log_dir)


def _read_log(path: Path, limit: int = 1 << 20) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def pinned_aws_version(path: Path = _PINS_FILE) -> str:
    """Return ``pins.json`` ``tools.aws_cli``.

    Raises:
        PublishError: The pins file or the value is missing or malformed.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["tools"]["aws_cli"]
    except (OSError, ValueError, KeyError, TypeError):
        value = None
    if not (isinstance(value, str) and _PIN_VERSION.fullmatch(value)):
        raise PublishError("pins.json tools.aws_cli: expected X.Y.Z -> actual missing")
    return value


def check_aws_version(env: Env, session: _Session) -> str:
    """Require the aws CLI version to equal the pin (ADR 0005 D3/D9; shown in the ``ok:`` line).

    Raises:
        PublishError: aws does not start, or its version differs from the pin.
    """
    want = pinned_aws_version()
    rc, log = _aws(env, session, "s3-staging aws version", ["--version"],
                   "aws --version: expected exit 0 -> actual failed")
    m = _AWS_VERSION.match(_read_log(log, 4096).strip() + " ")
    have = m.group(1) if m else None
    if rc != 0 or have != want:
        raise PublishError(f"aws CLI: expected {want} (pins.json) -> actual "
                           f"{have or 'unknown'}")
    return have


def _put(env: Env, session: _Session, conf: Settings, asset: pin.Asset, key: str, path: Path,
         index: int, total: int) -> None:
    """One conditional write; ``PreconditionFailed`` (an existing key) is a hard failure."""
    checksum = base64.b64encode(bytes.fromhex(asset.sha256)).decode("ascii")
    label = f"s3-staging put {index}/{total}"
    rc, log = _aws(env, session, label,
                   ["s3api", "put-object", "--bucket", conf.bucket, "--key", key,
                    "--body", str(path), "--if-none-match", "*",
                    "--checksum-sha256", checksum,
                    "--metadata", f"sha256={asset.sha256},not-a-release=true",
                    "--no-cli-pager", "--output", "json"],
                   f"put-object {asset.name}: expected created -> actual failed",
                   f"created {asset.name} (create-only)")
    if rc == 0:
        return
    if _PRECONDITION.search(_read_log(log)):
        raise PublishError(f"put-object {asset.name}: expected a new key -> actual key exists "
                           "(create-only: never overwritten, not retried)")
    raise PublishError(f"put-object {asset.name}: expected created -> actual exit {rc}")


def _reread(env: Env, session: _Session, conf: Settings,
            rows: Sequence[Tuple[pin.Asset, str]]) -> int:
    """Download every key into the private directory and compare size and sha256."""
    for index, (asset, key) in enumerate(rows, 1):
        target = session.directory / f"reread-{index}"
        rc, _log = _aws(env, session, f"s3-staging get {index}/{len(rows)}",
                        ["s3api", "get-object", "--bucket", conf.bucket, "--key", key,
                         "--no-cli-pager", "--output", "json", str(target)],
                        f"get-object {asset.name}: expected the object -> actual failed",
                        f"downloaded {asset.name}")
        if rc != 0:
            raise PublishError(f"get-object {asset.name}: expected the object -> actual exit "
                               f"{rc}")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(target, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > asset.size:
                        break
                    digest.update(chunk)
        except OSError:
            raise PublishError(f"get-object {asset.name}: expected a downloaded file -> actual "
                               "missing") from None
        finally:
            with contextlib.suppress(OSError):
                target.unlink()
        if size != asset.size:
            raise PublishError(f"{asset.name} bytes: expected {asset.size} (manifest) -> actual "
                               f"{'more' if size > asset.size else size}")
        if digest.hexdigest() != asset.sha256:
            raise PublishError(f"{asset.name} sha256: expected {asset.sha256[:16]} (manifest) -> "
                               f"actual {digest.hexdigest()[:16]}")
    return len(rows)
