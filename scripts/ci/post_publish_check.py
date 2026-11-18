#!/usr/bin/env python3
"""Post-publish check of a GitHub Release, as a consumer sees it (milestone 0012 Story 04.0 Task 04).

Two entry points, one set of checks (:func:`check_release`):

- :func:`post_publish` - the ``post-publish`` job of ``stage-publish.yml``
  (``stage.py publish --destination github-release --only post-publish``), right
  after ``release-promote``. The expected bytes come from the same run's
  ``dist/manifest.json``, already anchored by ``stage.py`` against the package
  stage's ``manifest_sha256`` job output (RP-4).
- :func:`recheck` - the nightly ``verify`` step ``stage.py verify --only
  releases``: the last :data:`RECHECK_COUNT` published releases. The anchor of
  each is its row in :data:`LEDGER_FILE` (``release_ledger.json``), a reviewed,
  in-repository record of every release's manifest digest; the downloaded
  ``manifest.json`` must match that digest before it is parsed, and its digests
  then bind every other asset.

Threat model ``docs/security/2026-11-18-release-and-publish-automation.md``,
standing constraint 11 and finding RP-7, as this module applies them:

- **No credential, anonymous only.** :class:`AnonymousClient` never sets an
  ``Authorization`` header, reads no token from anywhere, ignores environment
  proxies and starts no child process (an AST test pins the imports). The job
  that runs it has ``contents: read`` and no environment (policy P12).
- **Public URLs only.** Release metadata comes from the unauthenticated REST
  API (which, like a consumer, never sees a draft); asset bytes come from
  each asset's ``browser_download_url``, which must be exactly
  ``https://github.com/<repo>/releases/download/<tag>/<name>``.
- **Redirects** are followed only to :data:`REDIRECT_HOSTS`, over HTTPS; an API
  call follows none. Anything else is refused before a connection is made.
- **Capped bodies.** Each download is streamed into SHA-256 and cut off at the
  manifest's ``bytes`` (+0); a longer or shorter body fails.
- **Exact asset name set**: the manifest's archives, their ``.sha256`` sidecars
  and ``manifest.json``, nothing more, nothing renamed.
- **Out-of-band comparison.** Every asset is compared with the digests of the
  *anchored* manifest, never with the release's own ``manifest.json`` asset
  alone (which an attacker would replace together with the archives); that
  asset must then also be byte-identical to the anchored manifest.
- Also checked: the release is published (not a draft), ``prerelease`` matches
  the tag's ``-<pre>`` suffix, and the tag dereferences (through annotated
  tags) to ``manifest.commit``.

A failure names the release and the first discrepancy. It is **not** a
rollback (ADR 0005 D6): nothing here writes anything anywhere.

A library: it prints nothing (P11); progress goes through the ``log``
callback, failures are :class:`CheckError`. Python 3.9+ standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import publish_inputs as pin

#: Reviewed, in-repository record of every published release's manifest digest
#: (the nightly re-check's out-of-band anchor). Appended by a maintainer from
#: the post-publish job's ``ok:`` line, through a normal PR.
LEDGER_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / "release_ledger.json"
LEDGER_SCHEMA = 1
#: Releases the nightly re-check covers (threat model constraint 11).
RECHECK_COUNT = 5
API_BASE = "https://api.github.com"
DOWNLOAD_ORIGIN = "https://github.com"
#: Where ``github.com/<repo>/releases/download/...`` may redirect (the same set
#: as ``publish_github_release.DOWNLOAD_HOSTS``; a test pins the two together).
REDIRECT_HOSTS: FrozenSet[str] = frozenset({"objects.githubusercontent.com",
                                            "release-assets.githubusercontent.com",
                                            "github-releases.githubusercontent.com"})
API_VERSION = "2022-11-28"
USER_AGENT = "libbios-post-publish/1"
TIMEOUT_S = 60.0
JSON_MAX_BYTES = 4 << 20
CHUNK = 1 << 16
MAX_TAG_DEPTH = 4
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

Log = Callable[[str], None]


class CheckError(Exception):
    """The release does not match what was verified; one value-free RC1 line naming it."""


@dataclass(frozen=True)
class PublicHosts:
    """Where the anonymous client may connect. Tests pass a loopback one.

    Attributes:
        api_base: Scheme + host of the REST API (anonymous GETs only).
        download_origin: Scheme + host every ``browser_download_url`` must start with.
        redirect_hosts: ``host[:port]`` values a download may be redirected to.
        scheme: The only scheme any request or redirect may use.
    """

    api_base: str = API_BASE
    download_origin: str = DOWNLOAD_ORIGIN
    redirect_hosts: FrozenSet[str] = REDIRECT_HOSTS
    scheme: str = "https"

    @staticmethod
    def netloc(base: str) -> str:
        """The lower-case ``host[:port]`` of ``base``."""
        return urllib.parse.urlsplit(base).netloc.lower()


PRODUCTION = PublicHosts()


@dataclass(frozen=True)
class Expected:
    """One asset as the anchored manifest says it must be."""

    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class LedgerEntry:
    """One ``release_ledger.json`` row: the out-of-band anchor of a published release."""

    tag: str
    commit: str
    manifest_sha256: str
    manifest_bytes: int


class _RedirectRefused(Exception):
    """A redirect outside the allowlist (or any redirect of an API call)."""


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an allowlisted host of the policy's scheme."""

    def __init__(self, allowed: FrozenSet[str], scheme: str) -> None:
        super().__init__()
        self.allowed = allowed
        self.scheme = scheme

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != self.scheme or parts.netloc.lower() not in self.allowed:
            fp.close()
            raise _RedirectRefused()
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and new.get_header("Authorization") is not None:
            raise _RedirectRefused()
        return new


class AnonymousClient:
    """Unauthenticated GitHub reads and release downloads for one repository.

    Raises:
        CheckError: ``repository`` is malformed.
    """

    def __init__(self, repository: str, hosts: PublicHosts = PRODUCTION,
                 timeout_s: float = TIMEOUT_S) -> None:
        if not REPOSITORY.fullmatch(repository or ""):
            raise CheckError("CI_REPOSITORY: expected owner/name -> actual <rejected>")
        self.repository = repository
        self.hosts = hosts
        self.timeout_s = timeout_s
        #: ``(method, label)`` of every request (counts for tests and summaries).
        self.calls: List[Tuple[str, str]] = []
        self._api = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                _Redirects(frozenset(), hosts.scheme))
        self._dl = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                               _Redirects(hosts.redirect_hosts, hosts.scheme))

    def __repr__(self) -> str:
        return f"AnonymousClient({self.repository})"

    def _open(self, opener, url: str, label: str, accept: str):
        req = urllib.request.Request(url, method="GET", headers={
            "Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION})
        self.calls.append(("GET", label))
        try:
            return opener.open(req, timeout=self.timeout_s)
        except _RedirectRefused:
            raise CheckError(f"GET {label}: expected no redirect outside the host allowlist -> "
                             "actual redirect refused") from None
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            return _Status(status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CheckError(f"GET {label}: expected a response -> actual network error "
                             f"({type(reason).__name__})") from None

    def get_json(self, path: str, label: str,
                 expect: Sequence[int] = (200,)) -> Tuple[int, Any]:
        """Anonymous ``GET /repos/<repo>/<path>``; ``(status, JSON or None if not 200)``.

        Raises:
            CheckError: An unexpected status, a redirect, a network error or a bad body.
        """
        url = f"{self.hosts.api_base}/repos/{self.repository}/{path}"
        resp = self._open(self._api, url, label, "application/vnd.github+json")
        if isinstance(resp, _Status):
            if resp.status not in expect:
                raise CheckError(f"GET {label}: expected {'/'.join(map(str, expect))} -> actual "
                                 f"{resp.status}")
            return resp.status, None
        with resp:
            status = resp.status
            raw = resp.read(JSON_MAX_BYTES + 1)
        if status not in expect:
            raise CheckError(f"GET {label}: expected {'/'.join(map(str, expect))} -> actual "
                             f"{status}")
        if len(raw) > JSON_MAX_BYTES:
            raise CheckError(f"GET {label}: expected at most {JSON_MAX_BYTES} bytes -> actual more")
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CheckError(f"GET {label}: expected JSON -> actual unparseable") from None

    def download_url(self, tag: str, name: str) -> str:
        """The only acceptable public URL of asset ``name`` of release ``tag``."""
        return (f"{self.hosts.download_origin}/{self.repository}/releases/download/"
                f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(name, safe='')}")

    def download(self, url: str, size: int, label: str, keep: bool = False
                 ) -> Tuple[str, Optional[bytes]]:
        """Stream ``url`` into SHA-256, cut off after ``size`` bytes; ``(sha256, bytes if keep)``.

        Raises:
            CheckError: Another status, a refused redirect, or a body that is
                longer (cut off) or shorter than ``size``.
        """
        resp = self._open(self._dl, url, label, "application/octet-stream")
        if isinstance(resp, _Status):
            raise CheckError(f"GET {label}: expected 200 -> actual {resp.status}")
        digest = hashlib.sha256()
        kept = bytearray() if keep else None
        total = 0
        with resp:
            if resp.status != 200:
                raise CheckError(f"GET {label}: expected 200 -> actual {resp.status}")
            while True:
                chunk = resp.read(min(CHUNK, size + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise CheckError(f"{label} bytes: expected {size} (manifest) -> actual more "
                                     "(body cut off at the manifest size)")
                digest.update(chunk)
                if kept is not None:
                    kept.extend(chunk)
        if total != size:
            raise CheckError(f"{label} bytes: expected {size} (manifest) -> actual {total}")
        return digest.hexdigest(), (bytes(kept) if kept is not None else None)


@dataclass(frozen=True)
class _Status:
    """An HTTP error status (the body is never read or rendered)."""

    status: int


# ---------------------------------------------------------------------------
# Expected assets
# ---------------------------------------------------------------------------

def expected_from_inputs(inputs: pin.ReleaseInputs) -> List[Expected]:
    """The assets the same run's anchored ``dist/`` says the release carries."""
    return [Expected(a.name, a.size, a.sha256) for a in inputs.assets]


def expected_from_manifest(view: pin.ManifestView) -> List[Expected]:
    """The assets an anchored manifest implies: each archive, its sidecar, then the manifest."""
    out: List[Expected] = []
    for entry in view.legs:
        name, sha, size = str(entry["file"]), str(entry["sha256"]), int(entry["bytes"])
        out.append(Expected(name, size, sha))
        text = pin.sidecar_text(sha, name)
        out.append(Expected(name + ".sha256", len(text), hashlib.sha256(text).hexdigest()))
    out.append(Expected("manifest.json", view.size, view.sha256))
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def deref_tag(client: AnonymousClient, tag: str) -> str:
    """The commit the public tag names, through annotated tags.

    Raises:
        CheckError: The tag is absent, malformed or nested too deep.
    """
    ref = "tags/" + tag
    _, data = client.get_json("git/ref/" + urllib.parse.quote(ref, safe="/.-"), "git ref")
    obj = data.get("object") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("ref") == "refs/" + ref and isinstance(obj, dict)):
        raise CheckError(f"release {tag}: tag expected refs/{ref} -> actual another ref")
    for _ in range(MAX_TAG_DEPTH):
        kind, sha = obj.get("type"), obj.get("sha")
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            raise CheckError(f"release {tag}: tag object id expected 40-hex -> actual <rejected>")
        if kind == "commit":
            return sha
        if kind != "tag":
            raise CheckError(f"release {tag}: tag expected a commit -> actual another object")
        _, tag_obj = client.get_json("git/tags/" + sha, "git tag")
        obj = tag_obj.get("object") if isinstance(tag_obj, dict) else None
        if not isinstance(obj, dict):
            raise CheckError(f"release {tag}: annotated tag expected an object -> actual "
                             "malformed")
    raise CheckError(f"release {tag}: tag expected a commit within {MAX_TAG_DEPTH} annotated "
                     "tags -> actual deeper")


def _named(tag: str, exc: CheckError) -> CheckError:
    """``exc`` with its message prefixed ``release <tag>:`` (a failure always names the release)."""
    text = str(exc)
    return exc if text.startswith(f"release {tag}:") else CheckError(f"release {tag}: {text}")


def check_release(client: AnonymousClient, *, tag: str, commit: str, prerelease: bool,
                  expected: Sequence[Expected], manifest_raw: bytes,
                  release: Optional[Mapping[str, Any]] = None) -> str:
    """Every RP-7 check of one release; see :func:`_check_release`. Failures name ``tag``."""
    try:
        return _check_release(client, tag=tag, commit=commit, prerelease=prerelease,
                              expected=expected, manifest_raw=manifest_raw, release=release)
    except CheckError as exc:
        raise _named(tag, exc) from None


def _check_release(client: AnonymousClient, *, tag: str, commit: str, prerelease: bool,
                   expected: Sequence[Expected], manifest_raw: bytes,
                   release: Optional[Mapping[str, Any]] = None) -> str:
    """Every RP-7 check of one release against the out-of-band ``expected`` set.

    Args:
        tag: The release tag.
        commit: ``manifest.commit`` (the tag must dereference to it).
        prerelease: Whether the tag carries a ``-<pre>`` suffix.
        expected: The anchored manifest's assets (:func:`expected_from_inputs`
            or :func:`expected_from_manifest`), ``manifest.json`` last.
        manifest_raw: The anchored manifest's bytes; the downloaded asset must equal them.
        release: The release object if already read (the re-check lists them).

    Returns:
        The ``ok:`` summary.

    Raises:
        CheckError: The first discrepancy, prefixed with ``release <tag>:``.
    """
    if release is None:
        status, release = client.get_json("releases/tags/" + urllib.parse.quote(tag, safe=""),
                                          "release by tag", expect=(200, 404))
        if status == 404:
            raise CheckError(f"release {tag}: expected published and public -> actual not "
                             "visible anonymously (a draft, deleted or never published)")
    if not (isinstance(release, dict) and isinstance(release.get("draft"), bool)
            and isinstance(release.get("assets"), list)):
        raise CheckError(f"release {tag}: expected a release object -> actual malformed")
    if release["draft"]:
        raise CheckError(f"release {tag}: expected published -> actual a draft")
    if release.get("tag_name") != tag:
        raise CheckError(f"release {tag}: tag_name expected {tag} -> actual another tag")
    if release.get("prerelease") is not prerelease:
        raise CheckError(f"release {tag}: prerelease expected {str(prerelease).lower()} (the "
                         f"tag) -> actual {str(release.get('prerelease')).lower()}")
    want = {e.name: e for e in expected}
    assets = [a for a in release["assets"] if isinstance(a, dict)]
    names = [a.get("name") for a in assets]
    if len(assets) != len(release["assets"]) or not all(isinstance(n, str) for n in names):
        raise CheckError(f"release {tag}: assets expected objects -> actual malformed")
    extra, missing = set(names) - set(want), set(want) - set(names)
    if extra or missing or len(names) != len(set(names)):
        raise CheckError(f"release {tag}: assets expected exactly the {len(want)} manifest names "
                         f"-> actual {len(missing)} missing, {len(extra)} extra or renamed")
    for item in assets:
        exp = want[str(item["name"])]
        if item.get("state") not in (None, "uploaded"):
            raise CheckError(f"release {tag}: asset {exp.name} state expected uploaded -> actual "
                             "other")
        if item.get("size") != exp.size:
            raise CheckError(f"release {tag}: asset {exp.name} bytes expected {exp.size} "
                             f"(manifest) -> actual {item.get('size')}")
        if item.get("browser_download_url") != client.download_url(tag, exp.name):
            raise CheckError(f"release {tag}: asset {exp.name} URL expected the public "
                             "github.com release download path -> actual another URL")
    sha = deref_tag(client, tag)
    if sha != commit:
        raise CheckError(f"release {tag}: tag expected {commit[:12]} (manifest.commit) -> actual "
                         f"{sha[:12]}")
    for exp in expected:
        url = client.download_url(tag, exp.name)
        is_manifest = exp.name == "manifest.json"
        actual, body = client.download(url, exp.size, f"download {exp.name}",
                                       keep=is_manifest)
        if actual != exp.sha256:
            raise CheckError(f"release {tag}: asset {exp.name} sha256 expected "
                             f"{exp.sha256[:16]} (out-of-band manifest) -> actual {actual[:16]}")
        if is_manifest and body != manifest_raw:
            raise CheckError(f"release {tag}: asset manifest.json expected byte-identical to "
                             "the verified manifest -> actual different")
    kind = "pre-release" if prerelease else "release"
    return (f"{kind} {tag}: {len(expected)} public assets match the out-of-band manifest "
            f"{hashlib.sha256(manifest_raw).hexdigest()[:16]} (names, sizes, sha256), tag -> "
            f"{commit[:12]}, anonymous")


def post_publish(inputs: pin.ReleaseInputs, repository: str, log: Log,
                 client: Optional[AnonymousClient] = None) -> str:
    """The ``post-publish`` job: the release just promoted, against this run's anchored manifest.

    Returns:
        The ``ok:`` summary, which ends with the ledger row to record.

    Raises:
        CheckError: The first discrepancy (names the release); not a rollback.
    """
    client = client or AnonymousClient(repository)
    manifest = next((a for a in inputs.assets if a.name == "manifest.json"), None)
    if manifest is None:
        raise CheckError(f"release {inputs.tag}: expected manifest.json among the verified "
                         "files -> actual none")
    raw = pin._read_regular(manifest.path, pin.MANIFEST_MAX_BYTES, "manifest.json")
    if hashlib.sha256(raw).hexdigest() != inputs.manifest.sha256:
        raise CheckError(f"release {inputs.tag}: local manifest.json expected the anchored "
                         "digest -> actual changed on disk")
    summary = check_release(client, tag=inputs.tag, commit=inputs.commit,
                            prerelease=inputs.prerelease, expected=expected_from_inputs(inputs),
                            manifest_raw=raw)
    log(f"info: record in scripts/ci/release_ledger.json (nightly re-check anchor): "
        f"{ledger_row(inputs.tag, inputs.commit, inputs.manifest.sha256, len(raw))}")
    return summary


# ---------------------------------------------------------------------------
# The ledger and the nightly re-check
# ---------------------------------------------------------------------------

def ledger_row(tag: str, commit: str, manifest_sha256: str, manifest_bytes: int) -> str:
    """The exact JSON row to append to ``release_ledger.json`` for one release."""
    return json.dumps({"tag": tag, "commit": commit, "manifest_sha256": manifest_sha256,
                       "manifest_bytes": manifest_bytes}, sort_keys=True)


def load_ledger(path: Path = LEDGER_FILE) -> Dict[str, LedgerEntry]:
    """Parse and validate ``release_ledger.json``.

    Raises:
        CheckError: Unreadable, another schema, or a malformed or duplicated row.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CheckError("release_ledger.json: expected readable JSON -> actual unreadable") \
            from None
    rows = data.get("releases") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("schema") == LEDGER_SCHEMA
            and isinstance(rows, list)):
        raise CheckError(f"release_ledger.json: expected schema {LEDGER_SCHEMA} with a "
                         "'releases' list -> actual another shape")
    out: Dict[str, LedgerEntry] = {}
    for row in rows:
        if not (isinstance(row, dict)
                and set(row) == {"tag", "commit", "manifest_sha256", "manifest_bytes"}
                and isinstance(row["tag"], str) and pin.TAG.fullmatch(row["tag"])
                and isinstance(row["commit"], str) and _SHA.fullmatch(row["commit"])
                and isinstance(row["manifest_sha256"], str)
                and pin.HEX64.fullmatch(row["manifest_sha256"])
                and type(row["manifest_bytes"]) is int
                and 0 < row["manifest_bytes"] <= pin.MANIFEST_MAX_BYTES):
            raise CheckError("release_ledger.json: expected rows {tag, commit, manifest_sha256, "
                             "manifest_bytes} -> actual a malformed row")
        if row["tag"] in out:
            raise CheckError(f"release_ledger.json: expected one row per tag -> actual "
                             f"{row['tag']} twice")
        out[row["tag"]] = LedgerEntry(row["tag"], row["commit"], row["manifest_sha256"],
                                      row["manifest_bytes"])
    return out


def _recheck_one(client: AnonymousClient, release: Mapping[str, Any], tag: str,
                 entry: LedgerEntry, packaging_legs: Sequence[str]) -> str:
    base, pre = pin.parse_tag(tag)
    raw: Optional[bytes]
    actual, raw = client.download(client.download_url(tag, "manifest.json"), entry.manifest_bytes,
                                  "download manifest.json", keep=True)
    if actual != entry.manifest_sha256 or raw is None:
        raise CheckError(f"release {tag}: asset manifest.json sha256 expected "
                         f"{entry.manifest_sha256[:16]} (release_ledger.json) -> actual "
                         f"{actual[:16]}")
    try:
        data = json.loads(raw.decode("utf-8"))
        view = pin.validate_manifest(data, actual, len(raw), packaging_legs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CheckError(f"release {tag}: manifest.json expected JSON -> actual unparseable") \
            from None
    except pin.PublishInputError as exc:
        raise CheckError(f"release {tag}: {exc}") from None
    if view.commit != entry.commit or view.version != base:
        raise CheckError(f"release {tag}: manifest expected commit {entry.commit[:12]} and "
                         f"version {base} -> actual {view.commit[:12]}, {view.version}")
    return check_release(client, tag=tag, commit=entry.commit, prerelease=pre is not None,
                         expected=expected_from_manifest(view), manifest_raw=raw,
                         release=release)


def recheck(client: AnonymousClient, ledger: Mapping[str, LedgerEntry],
            packaging_legs: Sequence[str], log: Log, count: int = RECHECK_COUNT) -> str:
    """Nightly: re-check the newest ``count`` published releases against their ledger rows.

    A release without a ledger row fails (it has no out-of-band anchor). A
    draft (never listed anonymously; defensive) is reported and not checked.
    Every release is checked; the failure names the first one that failed.

    Returns:
        The ``ok:`` summary.

    Raises:
        CheckError: At least one release failed.
    """
    _, items = client.get_json(f"releases?per_page={count}", "list releases")
    if not isinstance(items, list):
        raise CheckError("GET list releases: expected a list -> actual malformed")
    checked: List[str] = []
    failures: List[str] = []
    for release in items[:count]:
        tag = release.get("tag_name") if isinstance(release, dict) else None
        if not (isinstance(tag, str) and pin.TAG.fullmatch(tag)):
            failures.append("release <rejected tag>: expected v<X.Y.Z>[-<pre>] -> actual another "
                            "tag name")
            continue
        if release.get("draft") is True:
            log(f"info: release {tag}: a draft, reported, not checked")
            continue
        entry = ledger.get(tag)
        if entry is None:
            failures.append(f"release {tag}: expected a row in release_ledger.json (its "
                            "out-of-band anchor) -> actual none")
            continue
        try:
            _recheck_one(client, release, tag, entry, packaging_legs)
            checked.append(tag)
        except CheckError as exc:
            failures.append(str(_named(tag, exc)))
    if failures:
        more = f" (+{len(failures) - 1} more release(s) failed)" if len(failures) > 1 else ""
        raise CheckError(failures[0] + more)
    if not checked:
        return "no published release to re-check (0 listed anonymously)"
    return (f"re-checked {len(checked)} published release(s) against release_ledger.json, "
            f"anonymous: {', '.join(checked)}")
