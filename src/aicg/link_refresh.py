"""Deterministic ``refresh_links`` handler.

For each URL the audit flagged broken in a single doc, try (in order):

1. **Re-check** — HEAD then (when HEAD is ambiguous) GET the URL.
   Audit results are often stale (transient DNS / 5xx / rate-limit / CDN
   anti-bot challenge). If the link is now alive — or auth-walled rather
   than missing — no replacement is made.
2. **Follow redirects** — if the URL now returns 3xx, follow it (up to
   ``MAX_REDIRECTS`` hops) and use the final 200 URL as the replacement.
3. **Wayback Machine** — query ``archive.org/wayback/available`` for the
   most recent good snapshot and use that, BUT only when:
      a) the original URL is unambiguously broken (404/410/DNS-fail), and
      b) the URL is referenced in a human-read context (not a Helm repo,
         OCI registry, ``pip install -i``, ``git clone``, ``Chart.yaml
         repository:``, etc. — Wayback HTML wrappers silently break
         downstream tooling there).

If none of those produce a verified replacement, leave the URL alone and
let the next audit re-emit the work item.

When at least one replacement is found, write the doc, commit on a
branch, open a PR — **never auto-merge**. Content changes from this
handler go through human review (the curriculum has public readers
now).

Pure stdlib + ``urllib``; no Claude in the loop. Two fetcher functions
are injectable so the handler is testable without real HTTP.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .manifest import CanonicalSourceRegistry
from .state import utc_now

USER_AGENT = "AICG-link-refresh/1.0 (+https://github.com/ai-infra-curriculum)"
HTTP_TIMEOUT = 10.0
MAX_REDIRECTS = 5
WAYBACK_API = "https://archive.org/wayback/available"

# Domains we will never substitute INTO a curriculum doc as a
# replacement for an external citation. VeriSwarm.ai is the
# maintainer-level attribution but practitioner sources, not
# authoritative ones — see source policy.
DISALLOWED_REPLACEMENT_DOMAINS = frozenset(
    {
        "veriswarm.ai",
        "www.veriswarm.ai",
        "localhost",
        "127.0.0.1",
    }
)

# HTTP statuses that prove the URL is gone. Anything outside this set
# is treated as ambiguous (auth-walled / rate-limited / bot-blocked /
# transient) and is NOT a justification to substitute Wayback.
_UNAMBIGUOUSLY_BROKEN_STATUSES = frozenset(
    {
        404,  # not found
        410,  # gone
        451,  # legal removal
    }
)

# Lines (or surrounding code-block context) matching any of these
# patterns indicate a URL is consumed by tooling, not read by a human.
# Wayback snapshots silently break Helm/OCI/pip/git semantics — refuse
# to substitute in those positions and surface as unresolved instead.
_MACHINE_CONTEXT_RE = re.compile(
    r"(?:"
    r"helm\s+repo\s+add"
    r"|^\s*repository\s*:"
    r"|^\s*image\s*:"
    r"|^\s*registry\s*:"
    r"|^\s*chart\s*:"
    r"|^\s*url\s*:"  # YAML keyless URLs, generic url: fields
    r"|pip\s+install\s+(?:-i|--index-url|--extra-index-url)"
    r"|git\s+clone\s"
    r"|docker\s+pull\s"
    r"|kubectl\s+apply\s+-f\s+http"
    r"|oci://"
    r"|--repository-url"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class LinkResolution:
    original: str
    replacement: str | None
    source: str  # "alive" | "redirect" | "canonical" | "wayback" | "unresolved" | "machine_endpoint" | "ambiguous"
    note: str = ""

    @property
    def needs_edit(self) -> bool:
        return (
            self.replacement is not None
            and self.replacement != self.original
            and self.source != "alive"
        )


HttpFetcher = Callable[[str, str], "FetchResult"]  # (url, method) -> FetchResult
WaybackFetcher = Callable[[str], str | None]  # url -> archived_url | None


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    final_url: str
    error: str = ""


def default_http_fetcher(url: str, method: str = "HEAD") -> FetchResult:
    """One HTTP request, no redirect-following (caller follows manually)."""
    # Guard scheme up front. urllib raises ValueError("unknown url type: ...")
    # on scheme-less URLs (e.g. a relative redirect Location header that
    # leaked into the queue), and ValueError is not in the catch list below.
    # Unhandled, it would crash the entire remediate tick.
    if not url.startswith(("http://", "https://")):
        return FetchResult(
            status=None, final_url=url, error=f"unsupported url scheme: {url!r}"
        )
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT}
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None  # disable automatic follow

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as resp:
            return FetchResult(status=resp.status, final_url=resp.geturl())
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        # Resolve relative Location headers against the request URL — some
        # servers return paths only (`/en/blog/...`) which downstream code
        # cannot HEAD without a scheme.
        if location:
            location = urllib.parse.urljoin(url, location)
        return FetchResult(status=exc.code, final_url=location or url)
    except urllib.error.URLError as exc:
        return FetchResult(status=None, final_url=url, error=str(exc.reason))
    except TimeoutError:
        return FetchResult(status=None, final_url=url, error="timeout")
    except (OSError, ValueError) as exc:
        return FetchResult(status=None, final_url=url, error=str(exc))


def default_wayback_fetcher(url: str) -> str | None:
    """Ask archive.org for the most recent good snapshot of ``url``."""
    api_url = f"{WAYBACK_API}?url={urllib.request.quote(url, safe=':/?&=')}"
    request = urllib.request.Request(
        api_url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    snapshot = (
        data.get("archived_snapshots", {}).get("closest") or {}
    )
    if not snapshot.get("available"):
        return None
    if str(snapshot.get("status", "")) != "200":
        return None
    return snapshot.get("url") or None


def resolve_link(
    url: str,
    *,
    http_fetcher: HttpFetcher = default_http_fetcher,
    wayback_fetcher: WaybackFetcher = default_wayback_fetcher,
    machine_consumed: bool = False,
    canonical_sources: CanonicalSourceRegistry | None = None,
) -> LinkResolution:
    """Try canonical successor → re-check → redirect chase → wayback.

    When ``canonical_sources`` is provided, it is consulted FIRST. If the
    URL has a known successor (vendor rebrand, project successor, OCI
    migration, etc.), use that directly — short-circuiting the HTTP
    probe entirely. This prevents bad-Wayback substitutions for URLs
    we have ground-truth canonical replacements for.

    Wayback substitution is suppressed when ``machine_consumed=True`` —
    the caller has determined the URL is consumed by tooling (Helm repo
    add, ``pip install -i``, ``git clone``, etc.) and a Wayback HTML
    wrapper would silently break the surrounding example.

    A URL that responds with an *ambiguous* status (401/403/429/5xx,
    network error) is never replaced — only 404/410/451 count as
    unambiguously broken. This avoids replacing live IETF/Cloudflare
    pages just because the bot probe was rate-limited.
    """
    if canonical_sources is not None:
        successor = canonical_sources.lookup_successor(url)
        if successor is not None and _canonical_replacement_allowed(successor):
            return LinkResolution(
                original=url,
                replacement=successor,
                source="canonical",
                note="registered successor in canonical_sources.json",
            )
        # Also short-circuit machine-endpoint protection: a registered
        # machine_consumed=true source (e.g., Fulcio) is always
        # protected from Wayback, regardless of surrounding context.
        if canonical_sources.is_known_machine_endpoint(url):
            machine_consumed = True
    # Try HEAD, then GET when HEAD is non-200. Many CDNs / endpoints
    # return 4xx or 5xx for HEAD even though GET works (Bitnami chart
    # repos, Cloudflare-fronted RFC pages).
    primary = http_fetcher(url, "HEAD")
    if _is_success(primary.status):
        return LinkResolution(original=url, replacement=url, source="alive")

    if _is_redirect(primary.status) and primary.final_url:
        final = _follow_redirects(primary.final_url, http_fetcher, seen={url})
        if final is not None:
            return LinkResolution(original=url, replacement=final, source="redirect")

    # Re-probe with GET if HEAD didn't conclude.
    confirm = http_fetcher(url, "GET") if primary.status != 200 else primary
    if _is_success(confirm.status):
        return LinkResolution(original=url, replacement=url, source="alive")
    if _is_redirect(confirm.status) and confirm.final_url:
        final = _follow_redirects(confirm.final_url, http_fetcher, seen={url})
        if final is not None:
            return LinkResolution(original=url, replacement=final, source="redirect")

    # At this point the URL is non-200, non-redirect. Three subcases:
    # 1. The server returned a clear non-broken status (401/403/429/5xx) →
    #    "ambiguous": URL is alive but we can't see past the auth wall /
    #    rate limit / anti-bot challenge. Leave the original in place.
    # 2. The server returned a clear broken status (404/410/451) → fall
    #    through to the Wayback / machine-endpoint branch below.
    # 3. No status at all (timeout, DNS, scheme-less URL) → "unresolved":
    #    we never got a clean probe, so we can't decide.
    final_status = confirm.status if confirm.status is not None else primary.status
    final_error = confirm.error or primary.error

    if final_status is None:
        return LinkResolution(
            original=url,
            replacement=None,
            source="unresolved",
            note=final_error or "no response",
        )

    if not _is_unambiguously_broken(final_status):
        return LinkResolution(
            original=url,
            replacement=None,
            source="ambiguous",
            note=(
                f"status {final_status} — auth-walled / rate-limited / transient; "
                "leaving original in place"
            ),
        )

    # Genuinely broken (404 / 410 / 451). Wayback is the last resort —
    # but only for human-read contexts.
    if machine_consumed:
        return LinkResolution(
            original=url,
            replacement=None,
            source="machine_endpoint",
            note=(
                "broken but URL is consumed by tooling "
                "(helm/oci/pip/git/yaml field) — needs canonical successor, "
                "Wayback wrapper would corrupt downstream commands"
            ),
        )

    archived = wayback_fetcher(url)
    if archived and _replacement_allowed(archived):
        return LinkResolution(
            original=url, replacement=archived, source="wayback"
        )

    return LinkResolution(
        original=url,
        replacement=None,
        source="unresolved",
        note=final_error or (f"status {final_status}" if final_status else "no response"),
    )


def apply_replacements(body: str, resolutions: list[LinkResolution]) -> str:
    """Substitute each unique resolved URL in the doc body.

    Two safety properties:

    - **Dedupe by original** — if the same broken URL appears in N
      resolution slots (e.g., audit re-emitted it per occurrence), only
      the first resolution wins. Without this guard, multiple
      ``body.replace()`` passes can compose: the second pass finds the
      original URL embedded as a substring inside the first
      replacement's Wayback wrapper and rewrites it, producing
      ``web.archive.org/X/http://web.archive.org/Y/...``.
    - **Longest first** so a shorter URL that's a prefix of another
      doesn't eat the longer URL.
    """
    seen_originals: set[str] = set()
    unique: list[LinkResolution] = []
    for r in resolutions:
        if r.original in seen_originals:
            continue
        seen_originals.add(r.original)
        unique.append(r)

    pairs = sorted(
        (r for r in unique if r.needs_edit and r.replacement),
        key=lambda r: len(r.original),
        reverse=True,
    )
    new_body = body
    for r in pairs:
        new_body = new_body.replace(r.original, r.replacement)
    return new_body


def handle_refresh_links_item(
    *,
    workspace: Path,
    item: dict[str, Any],
    http_fetcher: HttpFetcher = default_http_fetcher,
    wayback_fetcher: WaybackFetcher = default_wayback_fetcher,
    canonical_sources: CanonicalSourceRegistry | None = None,
    open_pr: bool = True,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve and apply replacements for one ``refresh_links`` item.

    ``repo_path`` is the repo's local checkout; callers with a manifest pass
    ``manifest.repo_path(...)`` (``.github`` doesn't live at ``workspace/.github``).

    Returns a result dict describing what happened. Always opens a PR
    when at least one URL was replaced; never modifies main directly.
    """
    repo = item.get("repo", "")
    path_rel = item.get("path", "")
    repo_path = repo_path or workspace / repo
    target = repo_path / path_rel
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "refresh_links",
        "repo": repo,
        "path": path_rel,
        "status": "started",
    }

    if not target.exists():
        result["status"] = "skipped"
        result["reason"] = "file_not_found"
        return result

    # Dedupe URLs from the audit before resolving — the audit can emit
    # the same URL once per occurrence, but the resolution is the same
    # and re-resolving wastes HTTP calls.
    urls: list[str] = []
    seen: set[str] = set()
    for detail in item.get("details") or []:
        url = detail.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        result["status"] = "skipped"
        result["reason"] = "no_urls"
        return result

    body = target.read_text(encoding="utf-8")
    machine_consumed = _machine_consumed_urls(body, urls)

    resolutions = [
        resolve_link(
            url,
            http_fetcher=http_fetcher,
            wayback_fetcher=wayback_fetcher,
            machine_consumed=(url in machine_consumed),
            canonical_sources=canonical_sources,
        )
        for url in urls
    ]
    result["resolutions"] = [
        {"url": r.original, "replacement": r.replacement, "source": r.source, "note": r.note}
        for r in resolutions
    ]

    edits = [r for r in resolutions if r.needs_edit]
    alive = [r for r in resolutions if r.source == "alive"]
    unresolved = [r for r in resolutions if r.source in ("unresolved", "ambiguous", "machine_endpoint")]

    result["summary"] = {
        "edited": len(edits),
        "now_alive": len(alive),
        "unresolved": len(unresolved),
    }

    if not edits:
        # Either everything is now alive (audit was stale) or we can't
        # fix anything. Either way: no PR, leave the item for the next
        # audit cycle to re-evaluate.
        if alive and not unresolved:
            result["status"] = "no_action_now_alive"
        else:
            result["status"] = "no_replacements_found"
        return result

    new_body = apply_replacements(body, resolutions)
    if new_body == body:
        # Defensive — every needs_edit resolution should have produced
        # a substitution. If body is unchanged, surface that instead of
        # opening an empty PR.
        result["status"] = "no_diff_after_apply"
        return result

    target.write_text(new_body, encoding="utf-8")
    result["status"] = "edited"

    if open_pr:
        pr_outcome = _open_link_refresh_pr(repo_path, repo, path_rel, edits, unresolved)
        result["pr"] = pr_outcome
        result["status"] = pr_outcome.get("status", "edited")

    return result


def _is_success(status: int | None) -> bool:
    return bool(status and 200 <= status < 300)


def _is_redirect(status: int | None) -> bool:
    return bool(status and 300 <= status < 400)


def _is_unambiguously_broken(status: int | None) -> bool:
    """True only for statuses that definitively prove the URL is gone.

    401/403/429/5xx are excluded — auth walls, anti-bot challenges, and
    transient failures should not justify a Wayback substitution.
    """
    return status in _UNAMBIGUOUSLY_BROKEN_STATUSES


def _follow_redirects(
    start: str, http_fetcher: HttpFetcher, *, seen: set[str]
) -> str | None:
    """Chase up to MAX_REDIRECTS hops; return the first 200 URL or None."""
    current = start
    for _ in range(MAX_REDIRECTS):
        if current in seen:
            return None
        seen.add(current)
        # Try HEAD first; some servers return 4xx for HEAD on a 200
        # destination, so fall back to GET when HEAD is ambiguous.
        hop = http_fetcher(current, "HEAD")
        if _is_success(hop.status):
            return current if _replacement_allowed(current) else None
        if _is_redirect(hop.status) and hop.final_url:
            current = hop.final_url
            continue
        confirm = http_fetcher(current, "GET")
        if _is_success(confirm.status):
            return current if _replacement_allowed(current) else None
        if _is_redirect(confirm.status) and confirm.final_url:
            current = confirm.final_url
            continue
        return None
    return None


def _machine_consumed_urls(body: str, urls: list[str]) -> set[str]:
    """Return URLs whose surrounding context indicates tooling consumption.

    A URL is "machine-consumed" if:
    - It appears inside a fenced code block (```), OR
    - Its line matches a tooling pattern (helm repo add, ``repository:``,
      ``image:``, ``git clone``, ``oci://``, ``pip install -i``, …).

    Replacing such a URL with a Wayback snapshot silently breaks the
    surrounding command — the snapshot is HTML, not a Helm index.yaml
    or OCI manifest.
    """
    machine: set[str] = set()
    in_code = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        for url in urls:
            if url not in line:
                continue
            if in_code or _MACHINE_CONTEXT_RE.search(line):
                machine.add(url)
    return machine


def _replacement_allowed(url: str) -> bool:
    """Source-policy filter for HTTP-fetched replacements (redirect chase / Wayback).

    Restricts to ``http(s)://`` because the resolver actively fetched these.
    """
    if not url.startswith(("http://", "https://")):
        return False
    return _domain_allowed(url)


def _canonical_replacement_allowed(url: str) -> bool:
    """Source-policy filter for canonical-source successors.

    Canonical entries are human-curated and frequently point at
    machine-consumed schemes (``oci://``, ``git://``, ``file://``)
    rather than ``http(s)://``. Only the domain blocklist applies.
    """
    return _domain_allowed(url)


def _domain_allowed(url: str) -> bool:
    if "://" in url:
        rest = url.split("://", 1)[1]
    else:
        rest = url
    host = rest.split("/", 1)[0].lower().split(":", 1)[0]
    return host not in DISALLOWED_REPLACEMENT_DOMAINS


def _open_link_refresh_pr(
    repo_path: Path,
    repo: str,
    path_rel: str,
    edits: list[LinkResolution],
    unresolved: list[LinkResolution],
) -> dict[str, Any]:
    today = utc_now()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", path_rel.lower()).strip("-")[:80]
    branch = f"aicg/{today}/refresh-links/{slug or 'doc'}"
    title = f"docs: refresh broken links in {path_rel}"

    lines = [
        "## Refresh broken links",
        "",
        f"`{path_rel}` — replaced {len(edits)} broken link(s) "
        "using deterministic resolution (re-check / redirect chase / "
        "Wayback Machine). No Claude in the loop.",
        "",
        "**Replacements:**",
        "",
        "| Source | Old | New |",
        "| --- | --- | --- |",
    ]
    for r in edits:
        lines.append(f"| {r.source} | {r.original} | {r.replacement} |")
    if unresolved:
        lines += ["", "**Still unresolved (left in place):**", ""]
        for r in unresolved:
            lines.append(f"- {r.original} — {r.note or 'no resolution found'}")
    lines += [
        "",
        "Please eyeball the replacements — Wayback snapshots in "
        "particular may have stale rendering. Merge when satisfied.",
    ]
    body = "\n".join(lines)

    steps = [
        ["git", "-C", str(repo_path), "fetch", "origin", "main"],
        ["git", "-C", str(repo_path), "checkout", "main"],
        ["git", "-C", str(repo_path), "pull", "--ff-only"],
        ["git", "-C", str(repo_path), "checkout", "-B", branch],
        ["git", "-C", str(repo_path), "add", path_rel],
        ["git", "-C", str(repo_path), "commit", "-m", title],
        ["git", "-C", str(repo_path), "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--base", "main", "--title", title, "--body", body],
    ]
    trail: list[dict[str, Any]] = []
    pr_url: str | None = None
    for cmd in steps:
        completed = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, check=False
        )
        trail.append(
            {
                "step": cmd[1] if cmd[0] == "git" else cmd[0],
                "argv": cmd[-1] if cmd[0] == "gh" else " ".join(cmd[-3:]),
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-300:],
                "stderr_tail": completed.stderr[-300:],
            }
        )
        if cmd[:3] == ["gh", "pr", "create"] and completed.returncode == 0:
            pr_url = completed.stdout.strip()
        if completed.returncode != 0 and cmd[1] != "commit":
            subprocess.run(
                ["git", "-C", str(repo_path), "checkout", "main"],
                capture_output=True, text=True, check=False,
            )
            return {"status": "pr_failed", "branch": branch, "steps": trail}
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", "main"],
        capture_output=True, text=True, check=False,
    )
    return {"status": "pr_opened", "branch": branch, "pr_url": pr_url, "steps": trail}
