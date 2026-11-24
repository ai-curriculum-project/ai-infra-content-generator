"""Automatic remediation for stuck aicg PRs.

The steward used to *record* why a PR could not merge (blocked, CI failed,
conflicting) and stop there, so PRs piled up for weeks. This module turns
those terminal states into actions:

* **close_duplicate** — the daily job re-picked a work item whose PR was
  still open (the weekly audit resets gaps to ``ready`` from main). Keep the
  newest PR per work item, close the rest.
* **rebase** — merge conflicts: merge ``origin/main`` into the branch using
  the runner's existing auto-resolver.
* **resolve_markers** — the PR's own content added ``needs-research``
  markers: run the research agent on just those files on the PR branch
  (cite or soften), commit, push; CI re-runs and the steward re-evaluates.
  Bounded per pass and per PR; once a PR's attempts are spent the steward
  lets it merge and the markers join main's backlog for ``resolve-markers``.
* **close_stale** — any other stuck state after ``STALE_PR_DAYS``: close it.
  The gap is still on main, so the next audit re-queues it and it is
  regenerated against current main instead of rotting.

It also hosts the marker comparison the merge guardrail uses: a PR is only
blocked for ``needs-research`` / ``manual-review`` markers it *introduces*,
not for markers already on main (those belong to ``resolve-markers``).

Planning functions are pure; ``apply_action`` and the git helpers do the IO.
Only ``aicg/<date>/...`` work branches are ever touched; human-reviewed
PRs (``review_blocked``) are never closed.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .audit import PLACEHOLDER_PATTERNS

STALE_PR_DAYS = 7
MAX_MARKER_ATTEMPTS = 3  # agent resolution attempts per PR before merging with markers
MAX_AGENT_RUNS_PER_PASS = 2  # resolve_markers runs per steward pass (each is an agent session)
MARKER_BLOCKER_PREFIX = "Marker introduced in changed file(s): "
BLOCKING_MARKERS = ("needs_research", "manual_review")
CONFLICT_BLOCKER = "PR has merge conflicts"
# Terminal steward states that are the runner's to fix. ``review_blocked``
# is deliberately absent: a human asked for changes.
STUCK_STATES = frozenset({"guardrails_blocked", "ci_failed", "ci_timeout", "merge_failed"})

_WORK_BRANCH = re.compile(r"^aicg/(\d{4}-\d{2}-\d{2})/(.+)$")


@dataclass
class RemediationContext:
    """Per-steward-run state shared across repos: who may run the agent, and
    how many attempts each PR has had (persisted by the steward)."""

    manifest: Any = None
    attempts: dict[str, int] = field(default_factory=dict)  # "<repo>#<pr>" -> attempts
    agent_budget: int = MAX_AGENT_RUNS_PER_PASS


@dataclass(frozen=True)
class RemediationAction:
    kind: str  # close_duplicate | rebase | resolve_markers | close_stale
    pr_number: int
    head_ref: str
    reason: str
    superseded_by: int | None = None
    paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Work keys: identify "the same work item" across differently-dated branches
# ---------------------------------------------------------------------------


def work_key(head_ref: str) -> str | None:
    """``aicg/<date>/<repo>/<item>`` -> ``<repo>/<item>``; None for other refs."""
    m = _WORK_BRANCH.match(head_ref or "")
    return m.group(2) if m else None


def branch_date(head_ref: str) -> date | None:
    m = _WORK_BRANCH.match(head_ref or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def item_work_key(repo: str, work_id: str) -> str:
    """The work key of the branch the runner would open for this item.

    Mirrors ``org_runner``: ``aicg/{today}/{ref_segment(repo)}/{_slug_for_branch(work_id)}``.
    """
    from .gitops import ref_segment
    from .org_runner import _slug_for_branch

    return f"{ref_segment(repo)}/{_slug_for_branch(work_id)}"


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------


def plan_duplicate_closures(open_prs: list[dict[str, Any]]) -> list[RemediationAction]:
    """Close all but the newest open PR for each work item.

    Newest = highest PR number: it was generated against the most recent main.
    """
    by_key: dict[str, list[dict[str, Any]]] = {}
    for pr in open_prs:
        key = work_key(pr.get("headRefName", ""))
        if key and isinstance(pr.get("number"), int):
            by_key.setdefault(key, []).append(pr)
    actions: list[RemediationAction] = []
    for prs in by_key.values():
        if len(prs) < 2:
            continue
        keep, *older = sorted(prs, key=lambda p: p["number"], reverse=True)
        actions.extend(
            RemediationAction(
                kind="close_duplicate",
                pr_number=p["number"],
                head_ref=p["headRefName"],
                reason=f"duplicate of #{keep['number']} for the same work item",
                superseded_by=keep["number"],
            )
            for p in older
        )
    return actions


def _has_conflict(outcome: dict[str, Any]) -> bool:
    return any(str(b).startswith(CONFLICT_BLOCKER) for b in outcome.get("blockers") or [])


def marker_blocked_paths(outcome: dict[str, Any]) -> list[str]:
    """Files to resolve when introduced markers are the PR's ONLY blocker."""
    blockers = [str(b) for b in outcome.get("blockers") or []]
    if len(blockers) != 1 or not blockers[0].startswith(MARKER_BLOCKER_PREFIX):
        return []
    return [p.strip() for p in blockers[0][len(MARKER_BLOCKER_PREFIX):].split(", ") if p.strip()]


def plan_post_steward(
    outcomes: list[dict[str, Any]],
    *,
    today: date,
    stale_days: int = STALE_PR_DAYS,
    marker_attempts: dict[int, int] | None = None,
    max_marker_attempts: int = MAX_MARKER_ATTEMPTS,
) -> list[RemediationAction]:
    """Decide what to do with PRs the steward could not merge this pass."""
    attempts = marker_attempts or {}
    actions: list[RemediationAction] = []
    for o in outcomes:
        ref = o.get("head_ref") or ""
        opened = branch_date(ref)
        number = o.get("pr_number")
        if opened is None or not isinstance(number, int) or o.get("state") not in STUCK_STATES:
            continue
        if _has_conflict(o):
            # Cheap and deterministic — always try before giving up on a PR.
            actions.append(RemediationAction("rebase", number, ref, "merge conflicts with main"))
            continue
        marker_paths = marker_blocked_paths(o)
        if marker_paths:
            # Regenerating would recreate the markers; resolve them in place.
            # Once attempts are spent the steward merges it (not closed).
            used = attempts.get(number, 0)
            if used < max_marker_attempts:
                actions.append(
                    RemediationAction(
                        "resolve_markers", number, ref,
                        f"PR introduced markers (attempt {used + 1}/{max_marker_attempts})",
                        paths=tuple(marker_paths),
                    )
                )
            continue
        age = (today - opened).days
        if age > stale_days:
            actions.append(
                RemediationAction(
                    "close_stale", number, ref, f"stuck {age}d in {o.get('state')}; will be regenerated"
                )
            )
    return actions


# ---------------------------------------------------------------------------
# Applying (IO)
# ---------------------------------------------------------------------------


def _run_gh(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo_path, capture_output=True, text=True, check=False)


def _close_comment(action: RemediationAction) -> str:
    if action.kind == "close_duplicate":
        return (
            f"Closing: superseded by #{action.superseded_by}, a newer PR for the same "
            "work item (aicg steward remediation)."
        )
    return (
        f"Closing: {action.reason}. The gap is still on main, so the next audit "
        "re-queues it and it is regenerated against current main (aicg steward remediation)."
    )


def apply_action(
    repo_path: Path,
    action: RemediationAction,
    *,
    manifest: Any = None,
    repo_name: str | None = None,
) -> dict[str, Any]:
    """Execute one action. Returns the action dict plus ``result`` / ``detail``."""
    record = action.as_dict()
    if action.kind == "resolve_markers":
        return {**record, **_resolve_on_branch(repo_path, action, manifest, repo_name or repo_path.name)}
    if action.kind == "rebase":
        from .org_runner import _auto_rebase_branch_on_main

        outcome = _auto_rebase_branch_on_main(
            repo_path=repo_path, branch=action.head_ref, pr_number=action.pr_number
        )
        return {**record, "result": outcome.get("status", "error"), "detail": outcome.get("conflicts")}
    if action.kind in {"close_duplicate", "close_stale"}:
        done = _run_gh(
            repo_path,
            ["gh", "pr", "close", str(action.pr_number), "--comment", _close_comment(action), "--delete-branch"],
        )
        if done.returncode != 0:
            return {**record, "result": "error", "detail": (done.stderr or done.stdout)[-400:]}
        return {**record, "result": "closed"}
    return {**record, "result": "error", "detail": f"unknown action {action.kind}"}


def _resolve_on_branch(
    repo_path: Path, action: RemediationAction, manifest: Any, repo_name: str
) -> dict[str, Any]:
    """Run the marker resolver on the PR branch; commit + push accepted edits.

    Always returns the runner clone to main. Refuses to touch a checkout with
    uncommitted (non-runner-state) changes.
    """
    from . import marker_resolution
    from .agent_cli import AgentLimitReached
    from .assessments import _user_changes, changed_paths

    if _user_changes(repo_path, changed_paths(repo_path)):
        return {"result": "skipped_dirty", "detail": "runner checkout has uncommitted changes"}
    branch = action.head_ref
    if _git(repo_path, "fetch", "-q", "origin", branch).returncode != 0 or _git(
        repo_path, "checkout", "-q", "-B", branch, "FETCH_HEAD"
    ).returncode != 0:
        _git(repo_path, "checkout", "-q", "-f", "main")
        return {"result": "error", "detail": f"could not check out {branch}"}
    try:
        verdict = marker_resolution.resolve_file_markers(
            manifest, repo_path, repo_name, list(action.paths)
        )
        if not verdict.accepted:
            return {"result": "rejected", "detail": verdict.reason}
        _git(repo_path, "add", "--", *verdict.changed)
        commit = _git(
            repo_path, "commit", "-q", "-m",
            f"aicg: resolve {verdict.resolved} needs-research marker(s) on PR #{action.pr_number} (steward)",
        )
        if commit.returncode != 0:
            return {"result": "error", "detail": (commit.stderr or commit.stdout)[-400:]}
        push = _git(repo_path, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        if push.returncode != 0:
            return {"result": "error", "detail": (push.stderr or push.stdout)[-400:]}
        return {"result": "pushed", "detail": f"resolved {verdict.resolved}", "resolved": verdict.resolved}
    except AgentLimitReached:
        return {"result": "agent_limit", "detail": "agent subscription limit reached"}
    finally:
        _git(repo_path, "checkout", "-q", "-f", "main")


def open_pr_work_keys(repo_path: Path) -> set[str] | None:
    """Work keys of the repo's open aicg PRs; None if gh is unavailable."""
    import json

    # Cross-repo items name pseudo repos (e.g. ``_pairing``) with no checkout.
    if not repo_path.is_dir():
        return None
    try:
        done = _run_gh(
            repo_path, ["gh", "pr", "list", "--state", "open", "--json", "headRefName", "--limit", "200"]
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    try:
        prs = json.loads(done.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return {k for p in prs if (k := work_key(p.get("headRefName", "")))}


# ---------------------------------------------------------------------------
# Marker guardrail support
# ---------------------------------------------------------------------------


def count_blocking_markers(text: str) -> int:
    return sum(len(PLACEHOLDER_PATTERNS[m].findall(text)) for m in BLOCKING_MARKERS)


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args], capture_output=True, text=True, check=False
    )


def _file_at(repo_path: Path, rev: str, path: str) -> str:
    shown = _git(repo_path, "show", f"{rev}:{path}")
    return shown.stdout if shown.returncode == 0 else ""  # absent at rev = no markers


def introduced_marker_paths(repo_path: Path, head_ref: str, paths: list[str]) -> list[str] | None:
    """Changed files where the PR head has MORE blocking markers than its base.

    Compares the PR head against its merge-base with ``origin/main``, so a
    marker that was already on main (and merely carried along) is not the
    PR's fault. Returns None when the comparison can't be made (no git, head
    unreachable) so callers can fail closed.
    """
    head_rev = f"refs/aicg-guardrail/{head_ref}"
    fetched = _git(repo_path, "fetch", "-q", "origin", "main", f"+{head_ref}:{head_rev}")
    if fetched.returncode != 0:
        return None
    base = _git(repo_path, "merge-base", "origin/main", head_rev)
    if base.returncode != 0 or not base.stdout.strip():
        base = _git(repo_path, "merge-base", "FETCH_HEAD", head_rev)
    if base.returncode != 0 or not base.stdout.strip():
        return None
    base_rev = base.stdout.strip()
    return [
        p
        for p in paths
        if count_blocking_markers(_file_at(repo_path, head_rev, p))
        > count_blocking_markers(_file_at(repo_path, base_rev, p))
    ]
