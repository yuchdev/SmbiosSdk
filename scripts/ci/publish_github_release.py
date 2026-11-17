#!/usr/bin/env python3
"""The ``github-release`` publish destination (milestone 0012 Story 04.0 Task 02; ADR 0005 D6, D9).

Creates one **draft** GitHub Release for a ``v*`` tag, uploads exactly the
files of the anchored ``dist/manifest.json`` plus the manifest itself, re-reads
them, and later **promotes** the draft. Run only through
``python3 scripts/ci/stage.py publish --destination github-release --only
draft|promote`` (the ``release-draft``/``release-promote`` jobs of
``stage-publish.yml``); the inputs arrive already validated as
:class:`publish_inputs.ReleaseInputs`.

Binding rules (threat model ``docs/security/2026-11-18-release-and-publish-automation.md``,
standing constraints 2, 3, 5, 7 and findings RP-1, RP-5, RP-6, RP-8, RP-12):

- **REST in-process, no child process (RP-2, RP-8).** This module imports no
  ``subprocess`` and no step runner (an AST test pins it); it calls the GitHub
  REST API directly with stdlib ``urllib`` (no marketplace action, P13).
- **The token** is handed in once as a :class:`Secret` (whose ``repr``/``str``
  are redacted), attached with ``Request.add_unredirected_header`` so it is
  never forwarded on a redirect, and only to the API or upload base of the
  :class:`HostPolicy`. Environment proxies are ignored.
- **Host allowlist (RP-8).** An API call follows no redirect at all. An asset
  download follows a redirect only to an HTTPS host of
  :data:`DOWNLOAD_HOSTS`, without ``Authorization``; anything else is refused
  before a connection is made.
- **Value-free errors (RP-8).** A failure names only the method, a fixed
  endpoint label and the status (``POST releases: expected 201 -> actual
  422``), plus GitHub ``errors[].code`` values that match ``^[a-z_]{1,40}$``.
  Response bodies, headers and exception strings are never rendered.
- **One release, one commit (constraint 5, RP-5).** :func:`commit_preflight`
  dereferences the **remote** tag through annotated tags and requires
  ``CI_COMMIT``, and requires ``CI_COMMIT`` to be an ancestor of ``master`` (the
  compare API: status ``ahead``/``identical`` with ``CI_COMMIT`` as the merge
  base). It runs at the start of each phase and again immediately before each
  release write. The release is created with ``target_commitish = CI_COMMIT``;
  after promotion the tag is dereferenced once more.
- **Create-only (constraint 7, RP-6, RP-12).** A published release for the tag
  is never touched: it fails the draft phase, and the promote phase accepts it
  only as an idempotent re-run of this same run with identical assets. A draft
  from another run is a hard failure. The one exception is a draft whose body
  starts with this run's ``CI_RUN_ID`` marker: a re-run may delete **that
  draft's** assets and upload them again.
- **Exact asset re-read (RP-6).** After upload, and again immediately before
  ``PATCH {draft: false}``, the draft's assets must be exactly the expected
  name set, each with the manifest size, and the sha256 of the downloaded
  bytes (capped at that size) must equal the manifest's. The API ``digest``
  field, where present, is a secondary check only.
- **Settings drift (RP-1).** :func:`settings_preflight` reads the ``v*`` tag
  ruleset, the ``release`` environment and immutable releases where the token
  can; a readable wrong value fails, an unreadable one is an ``info:`` line
  ("manual read-back required"). It detects drift; it is not the boundary.

A library: it prints nothing (P11). Progress goes through the ``log``
callback (``info:`` lines the caller renders), failures are
:class:`PublishError`. Python 3.9+ standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import publish_inputs as pin

#: Registry key (ADR 0005 D9).
NAME = "github-release"
#: Neutral names this destination reads (the adapter maps them; D9).
REQUIRED_ENV: Tuple[str, ...] = ("CI_PUBLISH_TOKEN", "CI_REPOSITORY", "CI_COMMIT", "CI_REF_NAME",
                                 "CI_RUN_ID", "CI_MANIFEST_SHA256",
                                 "CI_VERIFIED_MANIFEST_SHA256", "CI_BUILD_BINARY_CACHE")
#: Phases, one per publish job (ADR 0005 D6 "GitHub Release sequence").
PHASES: Tuple[str, ...] = ("draft", "promote")

#: Constant endpoints (RP-8): the token is sent only to these two bases.
API_BASE = "https://api.github.com"
UPLOAD_BASE = "https://uploads.github.com"
#: Hosts an asset download may be redirected to (no ``Authorization``, HTTPS only).
DOWNLOAD_HOSTS: FrozenSet[str] = frozenset({"objects.githubusercontent.com",
                                            "release-assets.githubusercontent.com",
                                            "github-releases.githubusercontent.com"})
API_VERSION = "2022-11-28"
USER_AGENT = "libbios-publish/1"
TIMEOUT_S = 60.0
JSON_MAX_BYTES = 4 << 20
#: Releases listed while proving no release exists for the tag (100 per page).
MAX_RELEASE_PAGES = 10
#: Nested annotated tags followed while dereferencing.
MAX_TAG_DEPTH = 4

REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")
_HTML_URL = re.compile(r"^https://github\.com/[A-Za-z0-9._/-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_STATUS_WORDS = ("ahead", "identical", "behind", "diverged")


class PublishError(Exception):
    """A refused or failed publish step; the message is one value-free RC1 line."""


class Secret:
    """A credential that never renders: ``repr``/``str`` are ``<redacted>`` (RP-8)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def reveal(self) -> str:
        """Return the value (used only for the ``Authorization`` header)."""
        return self._value


@dataclass(frozen=True)
class HostPolicy:
    """Where requests may go (RP-8). Production is :data:`PRODUCTION`; tests use a loopback one.

    Attributes:
        api_base: Scheme + host of every API call (the token goes here).
        upload_base: Scheme + host of asset uploads (the token goes here).
        download_hosts: ``host[:port]`` values an asset download may be
            redirected to (never with the token).
        scheme: The only scheme any request or redirect may use.
    """

    api_base: str = API_BASE
    upload_base: str = UPLOAD_BASE
    download_hosts: FrozenSet[str] = DOWNLOAD_HOSTS
    scheme: str = "https"

    @staticmethod
    def netloc(base: str) -> str:
        """Return the lower-case ``host[:port]`` of ``base``."""
        return urllib.parse.urlsplit(base).netloc.lower()


PRODUCTION = HostPolicy()


class _RedirectRefused(Exception):
    """A redirect to a scheme or host outside the request's allowlist."""


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an allowlisted host of the policy's scheme (none for API calls).

    ``urllib`` never copies a header added with ``add_unredirected_header`` to
    the redirected request, so ``Authorization`` cannot leave the API host; the
    explicit check below is defence in depth.
    """

    def __init__(self, allowed: FrozenSet[str], scheme: str) -> None:
        super().__init__()
        self.allowed = allowed
        self.scheme = scheme

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != self.scheme or parts.netloc.lower() not in self.allowed:
            raise _RedirectRefused()
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (new.has_header("Authorization")
                                or new.get_header("Authorization") is not None):
            raise _RedirectRefused()
        return new


@dataclass(frozen=True)
class Response:
    """Status and body of one exchange (headers are never kept)."""

    status: int
    body: bytes

    def json(self, label: str) -> Any:
        """Return the body as JSON.

        Raises:
            PublishError: The body is not JSON (never echoed).
        """
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise PublishError(f"{label}: expected a JSON response -> actual unparseable") \
                from None


class GitHubApi:
    """A minimal GitHub REST client for one repository.

    Args:
        token: The credential (``CI_PUBLISH_TOKEN``), already removed from the
            process environment by the caller.
        repository: ``owner/name`` (``CI_REPOSITORY``).
        policy: Hosts and scheme (tests pass a loopback policy).
        timeout_s: Per-request timeout.

    Raises:
        PublishError: ``repository`` is malformed or ``token`` is empty.
    """

    def __init__(self, token: Secret, repository: str, policy: HostPolicy = PRODUCTION,
                 timeout_s: float = TIMEOUT_S) -> None:
        if not REPOSITORY.fullmatch(repository or ""):
            raise PublishError("CI_REPOSITORY: expected owner/name -> actual <rejected>")
        if not isinstance(token, Secret) or not token:
            raise PublishError("CI_PUBLISH_TOKEN: expected the publish credential -> actual "
                               "unset")
        self._token = token
        self.repository = repository
        self.policy = policy
        self.timeout_s = timeout_s
        #: ``(method, label)`` of every request made: counts for tests and summaries only.
        self.calls: List[Tuple[str, str]] = []
        self._openers: Dict[FrozenSet[str], urllib.request.OpenerDirector] = {}

    def __repr__(self) -> str:
        return f"GitHubApi({self.repository})"

    def _opener(self, follow: FrozenSet[str]) -> urllib.request.OpenerDirector:
        """One opener per redirect allowlist, built once (a new HTTPS handler loads the CA store)."""
        opener = self._openers.get(follow)
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 _Redirects(follow, self.policy.scheme))
            self._openers[follow] = opener
        return opener

    def request(self, method: str, url: str, label: str, *, expect: Sequence[int] = (200,),
                payload: Optional[Mapping[str, Any]] = None, data: Optional[bytes] = None,
                accept: str = "application/vnd.github+json",
                follow: FrozenSet[str] = frozenset(),
                max_bytes: int = JSON_MAX_BYTES) -> Response:
        """Perform one request; return the response for an ``expect``-ed status.

        The URL's scheme and host must be the policy's API or upload base
        (the only hosts that ever receive the token).

        Raises:
            PublishError: Another status, a refused redirect, a network error
                or an oversized body, as one value-free line.
        """
        parts = urllib.parse.urlsplit(url)
        bases = {self.policy.netloc(self.policy.api_base),
                 self.policy.netloc(self.policy.upload_base)}
        if parts.scheme != self.policy.scheme or parts.netloc.lower() not in bases:
            raise PublishError(f"{method} {label}: expected an allowlisted host -> actual other")
        body = data
        headers = {"Accept": accept, "User-Agent": USER_AGENT,
                   "X-GitHub-Api-Version": API_VERSION}
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        req.add_unredirected_header("Authorization", "Bearer " + self._token.reveal())
        self.calls.append((method, label))
        raw = b""
        try:
            with self._opener(follow).open(req, timeout=self.timeout_s) as resp:
                status = resp.status
                raw = resp.read(max_bytes + 1)
        except _RedirectRefused:
            raise PublishError(f"{method} {label}: expected no redirect outside the host "
                               "allowlist -> actual redirect refused") from None
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(JSON_MAX_BYTES)
            except Exception:  # noqa: BLE001 - the body is optional diagnostics only
                raw = b""
            finally:
                exc.close()
            if status not in expect:
                raise PublishError(self.status_line(method, label, expect, status, raw)) \
                    from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PublishError(f"{method} {label}: expected a response -> actual network error "
                               f"({type(reason).__name__})") from None
        if status not in expect:
            raise PublishError(self.status_line(method, label, expect, status, b""))
        if len(raw) > max_bytes:
            raise PublishError(f"{method} {label}: expected at most {max_bytes} bytes -> actual "
                               "more")
        return Response(status, raw)

    @staticmethod
    def status_line(method: str, label: str, expect: Sequence[int], status: int,
                    raw: bytes) -> str:
        """``<METHOD> <label>: expected <codes> -> actual <status>[ (<codes>)]`` - no body text.

        Only GitHub ``errors[].code`` values matching ``^[a-z_]{1,40}$`` are
        shown (at most three); everything else in ``raw`` is discarded.
        """
        codes: List[str] = []
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
            errors = data.get("errors") if isinstance(data, dict) else None
            for err in errors if isinstance(errors, list) else []:
                code = err.get("code") if isinstance(err, dict) else None
                if isinstance(code, str) and _ERROR_CODE.fullmatch(code) and code not in codes:
                    codes.append(code)
        except (UnicodeDecodeError, ValueError, RecursionError):
            codes = []
        tail = f" ({','.join(codes[:3])})" if codes else ""
        want = "/".join(str(c) for c in expect)
        return f"{method} {label}: expected {want} -> actual {status}{tail}"

    def api(self, path: str) -> str:
        """Return the API URL of ``repos/<repository>/<path>``."""
        return f"{self.policy.api_base}/repos/{self.repository}/{path}"

    def get_json(self, path: str, label: str, expect: Sequence[int] = (200,)) -> Tuple[int, Any]:
        """GET ``path``; return ``(status, JSON or None for a non-200 expected status)``."""
        resp = self.request("GET", self.api(path), label, expect=expect)
        return resp.status, (resp.json(label) if resp.status == 200 else None)


# ---------------------------------------------------------------------------
# Preflights
# ---------------------------------------------------------------------------

#: The caller's sink for ``info:`` lines (rendered through the reporter).
Log = Callable[[str], None]


def deref_tag(api: GitHubApi, tag: str) -> str:
    """Return the commit the **remote** tag names, through annotated tags (RP-5).

    Raises:
        PublishError: The tag is absent, malformed, or not a commit within
            :data:`MAX_TAG_DEPTH` annotated tags.
    """
    ref = "tags/" + tag
    _, data = api.get_json("git/ref/" + urllib.parse.quote(ref, safe="/.-"), "git ref")
    obj = data.get("object") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("ref") == "refs/" + ref
            and isinstance(obj, dict)):
        raise PublishError(f"remote tag {tag}: expected refs/{ref} -> actual another ref")
    for _ in range(MAX_TAG_DEPTH):
        kind, sha = obj.get("type"), obj.get("sha")
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            raise PublishError(f"remote tag {tag}: expected an object id -> actual <rejected>")
        if kind == "commit":
            return sha
        if kind != "tag":
            raise PublishError(f"remote tag {tag}: expected a commit -> actual another object")
        _, tag_obj = api.get_json("git/tags/" + sha, "git tag")
        obj = tag_obj.get("object") if isinstance(tag_obj, dict) else None
        if not isinstance(obj, dict):
            raise PublishError(f"remote tag {tag}: expected an annotated tag -> actual "
                               "malformed")
    raise PublishError(f"remote tag {tag}: expected a commit within {MAX_TAG_DEPTH} annotated "
                       "tags -> actual deeper")


def commit_preflight(api: GitHubApi, inputs: pin.ReleaseInputs) -> None:
    """Constraint 5, run before every write: local binding, remote tag, ancestry of ``master``.

    Raises:
        PublishError: The first discrepancy (one RC1 line).
    """
    try:
        pin.check_commit_binding(inputs.tag, inputs.manifest, inputs.commit)
    except pin.PublishInputError as exc:
        raise PublishError(str(exc)) from None
    sha = deref_tag(api, inputs.tag)
    if sha != inputs.commit:
        raise PublishError(f"remote tag {inputs.tag}: expected {inputs.commit[:12]} (CI_COMMIT) "
                           f"-> actual {sha[:12]} (the tag moved)")
    _, cmp = api.get_json(f"compare/{inputs.commit}...master", "compare master")
    status = cmp.get("status") if isinstance(cmp, dict) else None
    merge_base = cmp.get("merge_base_commit") if isinstance(cmp, dict) else None
    base = merge_base.get("sha") if isinstance(merge_base, dict) else None
    if status not in ("ahead", "identical") or base != inputs.commit:
        shown = status if status in _STATUS_WORDS else "<rejected>"
        raise PublishError(f"{inputs.commit[:12]} on master: expected an ancestor (ahead or "
                           f"identical) -> actual {shown}")


def _tag_ruleset_ok(api: GitHubApi, item: Any) -> bool:
    if not (isinstance(item, dict) and item.get("target") == "tag"
            and item.get("enforcement") == "active" and type(item.get("id")) is int):
        return False
    _, full = api.get_json(f"rulesets/{item['id']}", "ruleset")
    if not isinstance(full, dict):
        return False
    cond = full.get("conditions")
    ref_name = cond.get("ref_name") if isinstance(cond, dict) else None
    include = ref_name.get("include") if isinstance(ref_name, dict) else None
    rules = full.get("rules")
    kinds = {r.get("type") for r in rules if isinstance(r, dict)} if isinstance(rules, list) \
        else set()
    return isinstance(include, list) and "refs/tags/v*" in include and \
        {"creation", "update", "deletion"} <= kinds


def settings_preflight(api: GitHubApi, log: Log) -> None:
    """RP-1 drift check of S-3, S-4 and S-5 where the token can read them (not the boundary).

    A readable setting with the wrong value fails; an unreadable one (403/404)
    is listed in one ``info:`` line as "manual read-back required".

    Raises:
        PublishError: A readable setting has the wrong value.
    """
    manual: List[str] = []

    status, rulesets = api.get_json("rulesets?includes_parents=true&per_page=100", "rulesets",
                                    expect=(200, 403, 404))
    if status == 200:
        if not any(_tag_ruleset_ok(api, item)
                   for item in (rulesets if isinstance(rulesets, list) else [])):
            raise PublishError("v* tag ruleset (S-3): expected active on refs/tags/v* with "
                               "creation, update and deletion restricted -> actual none")
    else:
        manual.append("v* tag ruleset (S-3)")

    status, env = api.get_json("environments/release", "environment", expect=(200, 403, 404))
    if status == 200 and isinstance(env, dict):
        rules = env.get("protection_rules")
        reviewers = [r for r in (rules if isinstance(rules, list) else [])
                     if isinstance(r, dict) and r.get("type") == "required_reviewers"
                     and r.get("reviewers")]
        if not reviewers:
            raise PublishError("environment release reviewers (S-5): expected required "
                               "reviewers -> actual none")
        policy = env.get("deployment_branch_policy")
        if not (isinstance(policy, dict) and policy.get("custom_branch_policies") is True
                and policy.get("protected_branches") is False):
            raise PublishError("environment release deployment rule (S-5): expected selected "
                               "tags -> actual another policy")
        if env.get("can_admins_bypass") is True:
            raise PublishError("environment release admin bypass (S-5): expected false -> "
                               "actual true")
        status, pols = api.get_json("environments/release/deployment-branch-policies",
                                    "deployment policies", expect=(200, 403, 404))
        if status == 200:
            items = pols.get("branch_policies") if isinstance(pols, dict) else None
            if not (isinstance(items, list) and items and all(
                    isinstance(p, dict) and p.get("type") == "tag" and p.get("name") == "v*"
                    for p in items)):
                raise PublishError("environment release deployment rule (S-5): expected tag "
                                   "v* only -> actual other entries")
        else:
            manual.append("release deployment tags (S-5)")
    else:
        manual.append("release environment (S-5)")

    status, imm = api.get_json("immutable-releases", "immutable releases",
                               expect=(200, 403, 404))
    if status == 200:
        if not (isinstance(imm, dict) and imm.get("enabled") is True):
            raise PublishError("immutable releases (S-4): expected enabled -> actual disabled")
    else:
        manual.append("immutable releases (S-4)")
    if manual:
        log("info: settings preflight: not readable with this token, manual read-back required "
            f"(threat model S-1..S-8): {', '.join(manual)}")
    else:
        log("info: settings preflight: S-3, S-4 and S-5 read back as required")


# ---------------------------------------------------------------------------
# Releases and assets
# ---------------------------------------------------------------------------

def _release(obj: Any, what: str) -> Dict[str, Any]:
    if not (isinstance(obj, dict) and type(obj.get("id")) is int
            and isinstance(obj.get("draft"), bool) and isinstance(obj.get("tag_name"), str)):
        raise PublishError(f"{what}: expected a release object -> actual malformed")
    return obj


def releases_for_tag(api: GitHubApi, tag: str) -> List[Dict[str, Any]]:
    """Every release (draft or published) whose ``tag_name`` is ``tag``.

    Raises:
        PublishError: A listing error, or more than :data:`MAX_RELEASE_PAGES`
            pages (create-only cannot be proven; fail closed).
    """
    found: List[Dict[str, Any]] = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        _, items = api.get_json(f"releases?per_page=100&page={page}", "list releases")
        if not isinstance(items, list):
            raise PublishError("list releases: expected a list -> actual malformed")
        found += [_release(r, "list releases") for r in items
                  if isinstance(r, dict) and r.get("tag_name") == tag]
        if len(items) < 100:
            return found
    raise PublishError(f"list releases: expected at most {MAX_RELEASE_PAGES * 100} releases -> "
                       "actual more (create-only cannot be proven)")


def has_marker(release: Mapping[str, Any], run_id: str) -> bool:
    """Whether the release body starts with this run's marker line (RP-6, RP-12)."""
    body = release.get("body")
    marker = pin.run_marker(run_id)
    return isinstance(body, str) and (body == marker or body.startswith(marker + "\n")
                                      or body.startswith(marker + "\r\n"))


def list_assets(api: GitHubApi, release_id: int) -> List[Dict[str, Any]]:
    """The release's assets (at most 100; a release carries 7).

    Raises:
        PublishError: The listing fails or is malformed.
    """
    _, items = api.get_json(f"releases/{release_id}/assets?per_page=100", "list assets")
    if not (isinstance(items, list) and all(
            isinstance(a, dict) and type(a.get("id")) is int and isinstance(a.get("name"), str)
            and type(a.get("size")) is int for a in items)):
        raise PublishError("list assets: expected a list of assets -> actual malformed")
    return items


def download_sha256(api: GitHubApi, asset_id: int, size: int, name: str) -> str:
    """Download one asset (redirected only to :data:`DOWNLOAD_HOSTS`, never with the token).

    The body is capped at ``size`` bytes; anything longer or shorter fails.
    """
    resp = api.request("GET", api.api(f"releases/assets/{asset_id}"), f"download {name}",
                       accept="application/octet-stream", follow=api.policy.download_hosts,
                       max_bytes=size)
    if len(resp.body) != size:
        raise PublishError(f"download {name} bytes: expected {size} -> actual {len(resp.body)}")
    return hashlib.sha256(resp.body).hexdigest()


def reread_assets(api: GitHubApi, release_id: int, assets: Sequence[pin.Asset]) -> int:
    """RP-6: the release holds exactly ``assets`` (names, sizes, downloaded sha256).

    Returns:
        The number of assets verified.

    Raises:
        PublishError: The first discrepancy; the caller writes nothing after it.
    """
    remote = list_assets(api, release_id)
    want = {a.name: a for a in assets}
    names = [str(a["name"]) for a in remote]
    extra = set(names) - set(want)
    missing = set(want) - set(names)
    if extra or missing or len(names) != len(set(names)):
        raise PublishError(f"release {release_id} assets: expected exactly the {len(want)} "
                           f"manifest names -> actual {len(missing)} missing, {len(extra)} extra "
                           "or renamed")
    for item in remote:
        asset = want[str(item["name"])]
        if item.get("state") not in (None, "uploaded"):
            raise PublishError(f"asset {asset.name} state: expected uploaded -> actual other")
        if item["size"] != asset.size:
            raise PublishError(f"asset {asset.name} bytes: expected {asset.size} (manifest) -> "
                               f"actual {item['size']}")
        claim = item.get("digest")
        m = _DIGEST.fullmatch(claim) if isinstance(claim, str) else None
        if m is not None and m.group(1) != asset.sha256:
            raise PublishError(f"asset {asset.name} digest field: expected {asset.sha256[:16]} "
                               f"-> actual {m.group(1)[:16]}")
        actual = download_sha256(api, item["id"], asset.size, asset.name)
        if actual != asset.sha256:
            raise PublishError(f"asset {asset.name} sha256: expected {asset.sha256[:16]} "
                               f"(manifest) -> actual {actual[:16]}")
    return len(remote)


def _upload(api: GitHubApi, release_id: int, asset: pin.Asset) -> None:
    with open(asset.path, "rb") as fh:
        data = fh.read(pin.ASSET_MAX_BYTES + 1)
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise PublishError(f"{asset.name}: expected the verified bytes -> actual changed on "
                           "disk")
    query = urllib.parse.urlencode({"name": asset.name})
    url = f"{api.policy.upload_base}/repos/{api.repository}/releases/{release_id}/assets?{query}"
    resp = api.request("POST", url, f"upload {asset.name}", expect=(201,), data=data)
    obj = resp.json(f"upload {asset.name}")
    if not (isinstance(obj, dict) and obj.get("name") == asset.name
            and obj.get("size") == asset.size):
        raise PublishError(f"upload {asset.name}: expected the same name and size -> actual "
                           "renamed or resized by the server")


def _html_url(release: Mapping[str, Any]) -> str:
    url = release.get("html_url")
    return url if isinstance(url, str) and _HTML_URL.fullmatch(url) else "<url withheld>"


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def draft(api: GitHubApi, inputs: pin.ReleaseInputs, log: Log) -> str:
    """``release-draft``: create one draft (or resume this run's own), upload, re-read.

    Returns:
        The ``ok:`` summary (counts and the draft URL only; RP-10).

    Raises:
        PublishError: The first refusal; no write follows it.
    """
    settings_preflight(api, log)
    commit_preflight(api, inputs)
    existing = releases_for_tag(api, inputs.tag)
    if any(not r["draft"] for r in existing):
        raise PublishError(f"release {inputs.tag}: expected none -> actual a published release "
                           "exists (create-only; never overwritten)")
    ours = [r for r in existing if has_marker(r, inputs.run_id)]
    others = len(existing) - len(ours)
    if others:
        raise PublishError(f"release {inputs.tag}: expected no draft from another run -> actual "
                           f"{others} (review and delete it by hand)")
    if len(ours) > 1:
        raise PublishError(f"release {inputs.tag}: expected at most one draft of run "
                           f"{inputs.run_id} -> actual {len(ours)}")
    if ours:
        release = ours[0]
        if release.get("target_commitish") != inputs.commit or \
                release.get("prerelease") is not inputs.prerelease:
            raise PublishError(f"draft {release['id']}: expected target {inputs.commit[:12]} "
                               "and the tag's pre-release kind -> actual another")
        stale = list_assets(api, release["id"])
        for item in stale:  # the one allowed delete: this run's own draft (RP-6, RP-12)
            api.request("DELETE", api.api(f"releases/assets/{item['id']}"), "delete own asset",
                        expect=(204,))
        log(f"info: re-run of run {inputs.run_id}: resuming its draft {release['id']}, "
            f"{len(stale)} earlier assets removed")
    else:
        commit_preflight(api, inputs)  # immediately before the write (RP-5)
        resp = api.request("POST", api.api("releases"), "releases", expect=(201,), payload={
            "tag_name": inputs.tag, "target_commitish": inputs.commit,
            "name": f"libbios {inputs.tag}", "body": pin.release_body(inputs), "draft": True,
            "prerelease": inputs.prerelease, "generate_release_notes": True,
            "make_latest": "false"})
        release = _release(resp.json("POST releases"), "POST releases")
        if not (release["draft"] is True and release["tag_name"] == inputs.tag
                and release.get("target_commitish") == inputs.commit
                and release.get("prerelease") is inputs.prerelease):
            raise PublishError(f"POST releases: expected a draft for {inputs.tag} at "
                               f"{inputs.commit[:12]} -> actual another release")
    for asset in inputs.assets:
        _upload(api, release["id"], asset)
    count = reread_assets(api, release["id"], inputs.assets)
    kind = "pre-release" if inputs.prerelease else "release"
    return (f"draft {kind} {inputs.tag}: {count} assets uploaded and re-read (names, sizes, "
            f"sha256), {_html_url(release)}")


def promote(api: GitHubApi, inputs: pin.ReleaseInputs, log: Log) -> str:
    """``release-promote``: re-read this run's draft, then publish it (RP-6; idempotent, RP-12).

    Returns:
        The ``ok:`` summary.

    Raises:
        PublishError: The first refusal; nothing is written after it.
    """
    settings_preflight(api, log)
    commit_preflight(api, inputs)
    existing = releases_for_tag(api, inputs.tag)
    ours = [r for r in existing if has_marker(r, inputs.run_id)]
    others = len(existing) - len(ours)
    if others:
        raise PublishError(f"release {inputs.tag}: expected only the draft of run "
                           f"{inputs.run_id} -> actual {others} other release(s)")
    if len(ours) != 1:
        raise PublishError(f"release {inputs.tag}: expected the draft of run {inputs.run_id} -> "
                           f"actual {len(ours)} (did release-draft pass?)")
    release = ours[0]
    if release.get("target_commitish") != inputs.commit or \
            release.get("prerelease") is not inputs.prerelease:
        raise PublishError(f"release {release['id']}: expected target {inputs.commit[:12]} and "
                           "the tag's pre-release kind -> actual another")
    count = reread_assets(api, release["id"], inputs.assets)
    if not release["draft"]:  # a re-run after a successful promote (RP-12)
        return (f"{inputs.tag} already published by run {inputs.run_id}: {count} assets "
                f"re-read and identical, {_html_url(release)}")
    commit_preflight(api, inputs)  # immediately before the write (RP-5)
    resp = api.request("PATCH", api.api(f"releases/{release['id']}"), "promote", payload={
        "draft": False, "prerelease": inputs.prerelease,
        "make_latest": "false" if inputs.prerelease else "true"})
    done = _release(resp.json("PATCH promote"), "PATCH promote")
    if not (done["draft"] is False and done["tag_name"] == inputs.tag
            and done.get("prerelease") is inputs.prerelease):
        raise PublishError(f"release {release['id']} ({inputs.tag}): expected published -> "
                           "actual still a draft or another tag")
    sha = deref_tag(api, inputs.tag)
    if sha != inputs.commit:
        raise PublishError(f"release {inputs.tag} published, but its tag: expected "
                           f"{inputs.commit[:12]} -> actual {sha[:12]} (investigate now)")
    if "immutable" in done:
        if done["immutable"] is not True:
            raise PublishError(f"release {inputs.tag} published, immutable (S-4): expected "
                               "true -> actual false (investigate now)")
    else:
        log("info: immutable: not reported by the API; manual read-back required (S-4)")
    kind = "pre-release" if inputs.prerelease else "release"
    return (f"promoted {kind} {inputs.tag}: {count} assets re-read before publish, "
            f"{_html_url(done)}")


def dry_run_plan(inputs: pin.ReleaseInputs, repository: str, phase: Optional[str]) -> List[str]:
    """The exact reads and writes a real run would make; no network, no credential (D9).

    Returns:
        ``info:`` lines (at most 14 for a three-leg manifest).
    """
    c12 = inputs.commit[:12]
    lines = [f"info: dry run: would read remote tag {inputs.tag} (through annotated tags) and "
             f"require {c12}, and require {c12} to be an ancestor of master (compare API)",
             f"info: dry run: would list the releases of {repository} and refuse any release "
             f"for {inputs.tag} but a draft marked run {inputs.run_id}"]
    if phase in (None, "draft"):
        lines.append(f"info: dry run: would POST releases: tag {inputs.tag}, target_commitish "
                     f"{inputs.commit}, draft true, prerelease "
                     f"{str(inputs.prerelease).lower()}, make_latest false")
        lines += [f"info: dry run: would upload {a.name} ({a.size} bytes, sha256 "
                  f"{a.sha256[:16]})" for a in inputs.assets]
        lines.append(f"info: dry run: would re-read the {len(inputs.assets)} assets (names, "
                     "sizes, downloaded sha256)")
    if phase in (None, "promote"):
        latest = "false" if inputs.prerelease else "true"
        lines.append(f"info: dry run: would re-read the {len(inputs.assets)} assets, then PATCH "
                     f"the draft: draft false, make_latest {latest}; then dereference "
                     f"{inputs.tag} again")
    return lines


def check_published(*_args: Any, **_kwargs: Any) -> None:
    """D9 post-publish check: milestone 0012 Story 04.0 Task 04, not implemented here.

    Raises:
        PublishError: Always.
    """
    raise PublishError("check_published: expected 0012 Story 04.0 Task 04 -> actual not "
                       "implemented")
