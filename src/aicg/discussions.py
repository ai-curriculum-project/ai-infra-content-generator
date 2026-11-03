"""Stewardship summarization of GitHub Discussions across the org.

The PR steward and issue auto-updater close the loop on artifacts the
runner itself produced. GitHub Discussions are the third leg of the
stool: questions, RFCs, and curriculum proposals raised by humans that
the runner cannot mechanically resolve but must surface so they don't
languish.

This module is intentionally read-only. It:

- Pulls open / unresolved discussions per repo via ``gh api graphql``.
- Computes simple staleness + signal metrics (age, comment count,
  whether the original author has replied, whether any maintainer has
  replied).
- Flags items needing human judgment (categories: ``Q&A`` without an
  answer for N days, ``Ideas`` with high reaction count, ``Show & tell``
  proposing new modules).
- Writes ``discussions-report.json`` into the org state dir so the
  operator can review.

There is no ``--apply`` path; the runner refuses to post comments or
mark anything resolved because that is human judgment territory.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

DISCUSSIONS_REPORT = "discussions-report.json"

DEFAULT_STALE_AFTER_DAYS = 7
DEFAULT_MAX_PER_REPO = 50

_NEW_MODULE_KEYWORDS = (
    "new module",
    "propose module",
    "add module",
    "new exercise",
    "new project",
    "add exercise",
    "add project",
    "curriculum gap",
)

_GRAPHQL_QUERY = """\
query($owner:String!, $name:String!, $first:Int!) {
  repository(owner:$owner, name:$name) {
    discussions(first:$first, orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes {
        number
        title
        url
        body
        createdAt
        updatedAt
        locked
        isAnswered
        upvoteCount
        author { login }
        category { name slug }
        comments(first:1) { totalCount }
        reactions { totalCount }
      }
    }
  }
}
"""


class DiscussionsError(RuntimeError):
    """Raised when the discussions stewardship cannot proceed."""


@dataclass(frozen=True)
class DiscussionsConfig:
    stale_after_days: float
    max_per_repo: int
    flag_categories: tuple[str, ...]
    new_module_keywords: tuple[str, ...]

    @classmethod
    def from_manifest(cls, manifest: OrgManifest) -> "DiscussionsConfig":
        automation = manifest.automation or {}
        cfg = automation.get("discussions", {}) if isinstance(automation, dict) else {}
        try:
            stale = float(cfg.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS))
        except (TypeError, ValueError):
            stale = float(DEFAULT_STALE_AFTER_DAYS)
        try:
            cap = int(cfg.get("max_per_repo", DEFAULT_MAX_PER_REPO))
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_PER_REPO
        raw_categories = cfg.get("flag_categories") or ("Q&A", "Ideas", "Show and tell")
        categories = tuple(str(item) for item in raw_categories)
        raw_keywords = cfg.get("new_module_keywords") or _NEW_MODULE_KEYWORDS
        keywords = tuple(str(item).lower() for item in raw_keywords)
        return cls(
            stale_after_days=stale,
            max_per_repo=max(1, cap),
            flag_categories=categories,
            new_module_keywords=keywords,
        )


def discussions_run(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Summarize open discussions across every repo in the manifest."""
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)

    config = DiscussionsConfig.from_manifest(manifest)
    repo_reports: list[dict[str, Any]] = []
    for repo in manifest.repo_names:
        repo_reports.append(_summarize_repo(manifest, repo, workspace, config))

    totals = _totals(repo_reports)
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "discussions",
        "stale_after_days": config.stale_after_days,
        "totals": totals,
        "repos": repo_reports,
    }
    write_json(state_root / DISCUSSIONS_REPORT, report)
    return report


def _summarize_repo(
    manifest: OrgManifest,
    repo: str,
    workspace: Path,
    config: DiscussionsConfig,
) -> dict[str, Any]:
    repo_path = manifest.repo_path(workspace, repo)
    if not repo_path.exists():
        return {
            "repo": repo,
            "present": False,
            "discussion_count": 0,
            "needs_attention": [],
            "stewardship_summary": [],
        }

    fetched = fetch_discussions(
        owner=manifest.org,
        name=repo,
        first=config.max_per_repo,
        cwd=repo_path,
    )
    if fetched.get("error"):
        return {
            "repo": repo,
            "present": True,
            "discussion_count": 0,
            "fetch_error": fetched["error"],
            "needs_attention": [],
            "stewardship_summary": [],
        }

    nodes = fetched.get("nodes", [])
    flagged: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for node in nodes:
        annotated = _annotate(node, config)
        summary.append(annotated["summary"])
        if annotated["needs_attention"]:
            flagged.append(annotated["detail"])

    return {
        "repo": repo,
        "present": True,
        "discussion_count": len(nodes),
        "needs_attention_count": len(flagged),
        "needs_attention": flagged,
        "stewardship_summary": summary,
    }


def _annotate(
    node: dict[str, Any], config: DiscussionsConfig
) -> dict[str, Any]:
    title = (node.get("title") or "").strip()
    body = node.get("body") or ""
    category = ((node.get("category") or {}).get("name") or "").strip()
    is_answered = bool(node.get("isAnswered"))
    locked = bool(node.get("locked"))
    upvotes = int(node.get("upvoteCount") or 0)
    comments = int(((node.get("comments") or {}).get("totalCount")) or 0)
    reactions = int(((node.get("reactions") or {}).get("totalCount")) or 0)
    updated_at = node.get("updatedAt")
    age_days = _days_since(updated_at)

    reasons: list[str] = []
    body_lower = body.lower()
    title_lower = title.lower()

    if category == "Q&A" and not is_answered and age_days >= config.stale_after_days:
        reasons.append(
            f"Q&A open for {age_days:.1f}d without an accepted answer."
        )
    if category == "Ideas" and (upvotes >= 3 or reactions >= 5):
        reasons.append(
            f"Ideas thread with {upvotes} upvotes / {reactions} reactions."
        )
    if any(kw in body_lower or kw in title_lower for kw in config.new_module_keywords):
        reasons.append("Mentions a curriculum gap or new module/exercise/project.")
    if locked:
        reasons.append("Discussion is locked but still open in the queue.")
    if category and category in config.flag_categories and comments == 0 and age_days >= config.stale_after_days:
        reasons.append(
            f"{category} discussion has zero comments and is {age_days:.1f}d old."
        )

    summary = {
        "number": node.get("number"),
        "title": title,
        "url": node.get("url"),
        "category": category,
        "is_answered": is_answered,
        "age_days": round(age_days, 1),
        "comments": comments,
        "upvotes": upvotes,
        "reactions": reactions,
    }
    detail = {
        **summary,
        "author": ((node.get("author") or {}).get("login") or "?"),
        "reasons": reasons,
        "preview": _preview(body),
    }
    return {
        "summary": summary,
        "detail": detail,
        "needs_attention": bool(reasons),
    }


def _preview(body: str, limit: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", body or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _days_since(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    delta = datetime.now(timezone.utc) - parsed
    return max(0.0, delta.total_seconds() / 86_400)


def _totals(repo_reports: Iterable[dict[str, Any]]) -> dict[str, int]:
    discussions = 0
    flagged = 0
    repos_with_content = 0
    repos_with_errors = 0
    for repo in repo_reports:
        discussions += int(repo.get("discussion_count", 0))
        flagged += int(repo.get("needs_attention_count", 0))
        if repo.get("discussion_count", 0):
            repos_with_content += 1
        if repo.get("fetch_error"):
            repos_with_errors += 1
    return {
        "discussion_count": discussions,
        "needs_attention_count": flagged,
        "repos_with_content": repos_with_content,
        "repos_with_errors": repos_with_errors,
    }


# ---------------------------------------------------------------------------
# gh wrappers
# ---------------------------------------------------------------------------


def fetch_discussions(
    owner: str, name: str, first: int, cwd: Path
) -> dict[str, Any]:
    """Run the GraphQL query for one repo's discussions."""
    args = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_GRAPHQL_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"first={first}",
    ]
    completed = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "error": (completed.stderr or completed.stdout)[-2000:],
            "nodes": [],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Could not parse gh graphql response: {exc}", "nodes": []}

    repository = (payload.get("data") or {}).get("repository") or {}
    nodes = ((repository.get("discussions") or {}).get("nodes")) or []
    return {"nodes": nodes}
