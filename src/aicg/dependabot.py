"""Autonomous Dependabot PR handler.

Dependabot opens dependency-bump PRs on a schedule. The runner's
default behavior was to ignore them because the steward filters PR
lists by author=@me. This module is the parallel sweep:

1. List open PRs authored by ``app/dependabot``.
2. For each, classify mergeable state.
3. If mergeable + CI clean → enable auto-merge.
4. If conflicting / dirty → post ``@dependabot rebase`` comment (once
   per fresh CI cycle so we don't spam) and enable auto-merge so it
   lands as soon as Dependabot regenerates the branch.
5. After ``MAX_REBASE_REQUESTS`` rebase loops without success, mark
   the PR as ``escalated`` so daily-issues can open a tracking issue
   and a human can decide.

Tracks per-PR state in ``.aicg/org/dependabot-state.json`` so retry
counts persist across daily ticks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

DEPENDABOT_REPORT = "dependabot-report.json"
DEPENDABOT_STATE = "dependabot-state.json"
DEPENDABOT_LOGIN = "app/dependabot"
MAX_REBASE_REQUESTS = 3
REBASE_COMMENT_BODY = (
    "@dependabot rebase\n\n"
    "_Posted by aicg dependabot sweep to bring this PR up to date "
    "with main._"
)


def dependabot_run(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Sweep every manifest repo for Dependabot PRs and act on them."""
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    prior_state = _load_prior_state(state_root)
    new_state: dict[str, dict[str, Any]] = {}

    repo_reports: list[dict[str, Any]] = []
    for repo in manifest.repo_names:
        repo_path = manifest.repo_path(workspace, repo)
        if not repo_path.exists():
            continue
        prs = list_dependabot_prs(repo_path)
        outcomes: list[dict[str, Any]] = []
        for pr in prs:
            key = f"{repo}#{pr['number']}"
            prior = prior_state.get(key, {})
            outcome = _process_dependabot_pr(
                repo_path=repo_path,
                repo=repo,
                pr=pr,
                prior=prior,
                apply=apply,
            )
            new_state[key] = {
                "rebase_request_count": outcome.get("rebase_request_count", 0),
                "last_action": outcome.get("action"),
                "last_seen": utc_now(),
                "status": outcome.get("status"),
            }
            outcomes.append(outcome)
        repo_reports.append(
            {
                "repo": repo,
                "pr_count": len(prs),
                "auto_merged": sum(
                    1
                    for o in outcomes
                    if o.get("status") in {"merged", "auto_merge_enabled"}
                ),
                "rebase_requested": sum(
                    1 for o in outcomes if o.get("action") == "rebase_requested"
                ),
                "escalated": sum(1 for o in outcomes if o.get("status") == "escalated"),
                "prs": outcomes,
            }
        )

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "dependabot_sweep",
        "status": "applied" if apply else "dry_run",
        "max_rebase_requests": MAX_REBASE_REQUESTS,
        "repos": repo_reports,
    }
    write_json(state_root / DEPENDABOT_REPORT, report)
    write_json(state_root / DEPENDABOT_STATE, {"prs": new_state})
    return report


def list_dependabot_prs(repo_path: Path) -> list[dict[str, Any]]:
    """Return open Dependabot PRs in this repo via gh CLI."""
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--author",
            DEPENDABOT_LOGIN,
            "--json",
            "number,title,mergeable,mergeStateStatus,statusCheckRollup,headRefName,url",
            "--limit",
            "100",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    try:
        return json.loads(completed.stdout) or []
    except json.JSONDecodeError:
        return []


def _process_dependabot_pr(
    repo_path: Path,
    repo: str,
    pr: dict[str, Any],
    prior: dict[str, Any],
    apply: bool,
) -> dict[str, Any]:
    pr_number = int(pr["number"])
    title = pr.get("title", "")
    mergeable = (pr.get("mergeable") or "").upper()
    state_status = (pr.get("mergeStateStatus") or "").upper()
    rebase_count = int(prior.get("rebase_request_count", 0))

    outcome: dict[str, Any] = {
        "repo": repo,
        "pr_number": pr_number,
        "title": title,
        "mergeable": mergeable,
        "merge_state_status": state_status,
        "rebase_request_count": rebase_count,
    }

    # Anything CONFLICTING / DIRTY needs a rebase request first.
    if mergeable == "CONFLICTING" or state_status == "DIRTY":
        if rebase_count >= MAX_REBASE_REQUESTS:
            outcome["status"] = "escalated"
            outcome["action"] = "no_action"
            outcome["reason"] = (
                f"Dependabot PR has hit {rebase_count} rebase requests with no "
                "successful merge; needs human review."
            )
            return outcome
        if apply:
            comment_result = _post_rebase_comment(repo_path, pr_number)
            outcome["comment_result"] = comment_result
            # Also enable auto-merge so it lands as soon as the new
            # branch lands and CI re-runs clean.
            merge_result = _enable_auto_merge(repo_path, pr_number)
            outcome["merge_result"] = merge_result
        outcome["status"] = "rebase_pending"
        outcome["action"] = "rebase_requested"
        outcome["rebase_request_count"] = rebase_count + 1
        return outcome

    # Already merged (race) — surface and move on.
    if state_status in {"CLEAN", "HAS_HOOKS"} and mergeable == "MERGEABLE":
        if apply:
            merge_result = _enable_auto_merge(repo_path, pr_number)
            outcome["merge_result"] = merge_result
            outcome["status"] = merge_result.get("status", "auto_merge_enabled")
        else:
            outcome["status"] = "would_merge"
        outcome["action"] = "auto_merge"
        return outcome

    # UNSTABLE = some non-required check failing but required checks
    # are green. gh pr merge --auto handles this — it'll merge when
    # required checks land.
    if state_status == "UNSTABLE":
        if apply:
            merge_result = _enable_auto_merge(repo_path, pr_number)
            outcome["merge_result"] = merge_result
            outcome["status"] = merge_result.get("status", "auto_merge_enabled")
        else:
            outcome["status"] = "would_auto_merge_when_required_pass"
        outcome["action"] = "auto_merge"
        return outcome

    # BEHIND, BLOCKED, etc. — try enabling auto-merge anyway; gh will
    # complain if it really can't.
    if apply:
        merge_result = _enable_auto_merge(repo_path, pr_number)
        outcome["merge_result"] = merge_result
        outcome["status"] = merge_result.get("status", "queued")
    else:
        outcome["status"] = "would_queue"
    outcome["action"] = "auto_merge"
    return outcome


def _post_rebase_comment(repo_path: Path, pr_number: int) -> dict[str, Any]:
    completed = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body", REBASE_COMMENT_BODY],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-400:],
        "stderr_tail": completed.stderr[-400:],
    }


def _enable_auto_merge(repo_path: Path, pr_number: int) -> dict[str, Any]:
    """Wrap steward.enable_auto_merge so callers see the classified status."""
    from .steward import enable_auto_merge

    return enable_auto_merge(repo_path, pr_number, method="squash")


def _load_prior_state(state_root: Path) -> dict[str, dict[str, Any]]:
    path = state_root / DEPENDABOT_STATE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("prs", {}) if isinstance(payload, dict) else {}
