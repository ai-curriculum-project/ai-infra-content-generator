"""Auto-update GitHub issues from work-queue state.

The org daily loop already deals with PRs via :mod:`aicg.steward`. This
module closes the operator-visibility loop on the *queue* itself:

- ``failed_permanently`` items get a GitHub issue opened so a human
  knows the agent gave up.
- ``deferred`` items that have been waiting beyond ``stuck_after_hours``
  get a comment on their existing issue (or a fresh one) so the queue
  doesn't quietly stall.
- ``verified`` items close any matching open issue once their PR has
  merged.

All issue mutations are gated behind ``--apply``. Dry-run mode reports
intentions without calling ``gh``.

Issues are labelled with ``aicg`` and ``aicg:<status>`` so they can be
queried back by state. The body carries the work-id, audit summary,
plan link, and the most recent run-state stdout/stderr tail.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

ISSUES_REPORT = "issues-report.json"

_ISSUE_LABELS = ("aicg",)
_TITLE_PREFIX = "[aicg]"

_AICG_WORK_ID_PATTERN = re.compile(
    r"`work_id`\s*:\s*`([A-Za-z0-9_\-:./]+)`"
)


class IssuesError(RuntimeError):
    """Raised when issue auto-update cannot proceed."""


@dataclass(frozen=True)
class IssuesConfig:
    enabled: bool
    stuck_after_hours: float
    apply: bool


def issues_run(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    apply: bool = False,
    stuck_after_hours: float | None = None,
) -> dict[str, Any]:
    """Reconcile GitHub issues with the org work queue."""
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)

    queue_path = state_root / "work-queue.json"
    if not queue_path.exists():
        raise IssuesError(
            "No org work queue at "
            f"{queue_path}. Run `aicg org audit` first."
        )
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    config = IssuesConfig(
        enabled=True,
        stuck_after_hours=(
            float(stuck_after_hours)
            if stuck_after_hours is not None
            else _default_stuck_after_hours(manifest)
        ),
        apply=apply,
    )

    by_repo: dict[str, list[dict[str, Any]]] = {}
    for item in queue.get("work_items", []):
        by_repo.setdefault(item.get("repo", ""), []).append(item)

    repo_reports: list[dict[str, Any]] = []
    for repo, items in by_repo.items():
        if not repo:
            continue
        repo_path = manifest.repo_path(workspace, repo)
        repo_reports.append(
            _reconcile_repo(repo, repo_path, items, config)
        )

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "issues",
        "status": "applied" if apply else "dry_run",
        "stuck_after_hours": config.stuck_after_hours,
        "repos": repo_reports,
    }
    write_json(state_root / ISSUES_REPORT, report)
    return report


def _default_stuck_after_hours(manifest: OrgManifest) -> float:
    automation = manifest.automation or {}
    cfg = automation.get("issues", {}) if isinstance(automation, dict) else {}
    try:
        return float(cfg.get("stuck_after_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def _reconcile_repo(
    repo: str,
    repo_path: Path,
    items: list[dict[str, Any]],
    config: IssuesConfig,
) -> dict[str, Any]:
    if not repo_path.exists():
        return {
            "repo": repo,
            "present": False,
            "decisions": [],
        }

    open_issues = list_aicg_open_issues(repo_path)
    issues_by_work_id = _index_issues_by_work_id(open_issues)
    decisions: list[dict[str, Any]] = []

    for item in items:
        decision = _decide(item, issues_by_work_id, config)
        if decision["action"] in {"open", "comment", "close"} and config.apply:
            decision["result"] = _execute(repo_path, decision, item)
        decisions.append(decision)

    return {
        "repo": repo,
        "present": True,
        "open_issue_count": len(open_issues),
        "decision_count": len(decisions),
        "opened": sum(
            1 for d in decisions
            if d["action"] == "open" and d.get("result", {}).get("returncode", 0) == 0
        ),
        "commented": sum(
            1 for d in decisions
            if d["action"] == "comment" and d.get("result", {}).get("returncode", 0) == 0
        ),
        "closed": sum(
            1 for d in decisions
            if d["action"] == "close" and d.get("result", {}).get("returncode", 0) == 0
        ),
        "decisions": decisions,
    }


def _decide(
    item: dict[str, Any],
    issues_by_work_id: dict[str, dict[str, Any]],
    config: IssuesConfig,
) -> dict[str, Any]:
    work_id = item.get("work_id") or item.get("id") or ""
    status = item.get("status", "")
    existing = issues_by_work_id.get(work_id)

    if status == "failed_permanently":
        if existing:
            return {
                "work_id": work_id,
                "action": "comment",
                "reason": "failed_permanently — refresh issue",
                "issue_number": existing.get("number"),
            }
        return {
            "work_id": work_id,
            "action": "open",
            "reason": "Work item failed permanently after max retries.",
            "issue_title": _title_for(item, "failed_permanently"),
            "issue_body": _body_for(item, "failed_permanently"),
            "labels": list(_ISSUE_LABELS) + ["aicg:failed-permanently"],
        }

    if status == "verification_failed":
        if existing:
            return {
                "work_id": work_id,
                "action": "comment",
                "reason": "verification_failed — refresh issue",
                "issue_number": existing.get("number"),
            }
        return {
            "work_id": work_id,
            "action": "open",
            "reason": "Verification failed.",
            "issue_title": _title_for(item, "verification_failed"),
            "issue_body": _body_for(item, "verification_failed"),
            "labels": list(_ISSUE_LABELS) + ["aicg:verification-failed"],
        }

    if status == "deferred":
        updated_at = item.get("updated_at") or item.get("retry_after")
        if _stuck_for_long(updated_at, hours=config.stuck_after_hours):
            if existing:
                return {
                    "work_id": work_id,
                    "action": "comment",
                    "reason": (
                        f"Deferred for over {config.stuck_after_hours}h "
                        f"({item.get('defer_reason', 'unknown')})."
                    ),
                    "issue_number": existing.get("number"),
                }
            return {
                "work_id": work_id,
                "action": "open",
                "reason": (
                    f"Deferred for over {config.stuck_after_hours}h."
                ),
                "issue_title": _title_for(item, "deferred"),
                "issue_body": _body_for(item, "deferred"),
                "labels": list(_ISSUE_LABELS) + ["aicg:stuck-deferred"],
            }

    if status == "escalated":
        if existing:
            return {
                "work_id": work_id,
                "action": "comment",
                "reason": "escalated — refresh issue",
                "issue_number": existing.get("number"),
            }
        return {
            "work_id": work_id,
            "action": "open",
            "reason": "PR-response loop exhausted retries; needs human.",
            "issue_title": _title_for(item, "escalated"),
            "issue_body": _body_for(item, "escalated"),
            "labels": list(_ISSUE_LABELS) + ["aicg:escalated-pr-response"],
        }

    if status == "verified" and existing:
        return {
            "work_id": work_id,
            "action": "close",
            "reason": "Work item verified; closing tracking issue.",
            "issue_number": existing.get("number"),
        }

    return {
        "work_id": work_id,
        "action": "skip",
        "reason": f"No action for status {status!r}.",
    }


def _stuck_for_long(timestamp: str | None, hours: float) -> bool:
    if not timestamp:
        return False
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - parsed > timedelta(hours=hours)


def _title_for(item: dict[str, Any], state: str) -> str:
    scope = item.get("module") or item.get("project") or item.get("type") or "—"
    return f"{_TITLE_PREFIX} {state}: {scope} ({item.get('work_id') or item.get('id')})"


def _body_for(item: dict[str, Any], state: str) -> str:
    work_id = item.get("work_id") or item.get("id") or "?"
    return (
        f"Auto-tracked by the AICG runner.\n\n"
        f"- `work_id`: `{work_id}`\n"
        f"- repo: `{item.get('repo', '?')}`\n"
        f"- module: `{item.get('module', '-')}`\n"
        f"- project: `{item.get('project', '-')}`\n"
        f"- type: `{item.get('type', '-')}`\n"
        f"- status: `{state}`\n"
        f"- updated_at: `{item.get('updated_at', '-')}`\n"
        f"- retry_count: `{item.get('retry_count', 0)}`\n"
        f"- defer_reason: `{item.get('defer_reason', '-')}`\n"
        f"- last_failure: `{item.get('last_failure', '-')}`\n\n"
        f"Inspect state files for full details:\n"
        f"- `.aicg/work-plan.json` in the target repo\n"
        f"- `.aicg/org/work-queue.json` in the runner workspace\n"
        f"- `.aicg/run-state.json` for the most recent generator output\n\n"
        f"Re-running `aicg org daily` will pick this work item up again. "
        f"Use `aicg org issues --apply` to refresh / close tracking issues."
    )


# ---------------------------------------------------------------------------
# gh wrappers
# ---------------------------------------------------------------------------


def list_aicg_open_issues(repo_path: Path) -> list[dict[str, Any]]:
    completed = _run_gh(
        repo_path,
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            "aicg",
            "--json",
            "number,title,body,labels,updatedAt",
            "--limit",
            "200",
        ],
    )
    if completed.returncode != 0:
        return []
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []


def _index_issues_by_work_id(
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Find the work_id reference inside each tracked issue body."""
    by_id: dict[str, dict[str, Any]] = {}
    for issue in issues:
        body = issue.get("body") or ""
        match = _AICG_WORK_ID_PATTERN.search(body)
        if not match:
            continue
        work_id = match.group(1).strip()
        if work_id and work_id not in by_id:
            by_id[work_id] = issue
    return by_id


def _execute(
    repo_path: Path, decision: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    action = decision["action"]
    if action == "open":
        return open_issue(
            repo_path,
            title=decision["issue_title"],
            body=decision["issue_body"],
            labels=decision.get("labels", list(_ISSUE_LABELS)),
        )
    if action == "comment":
        body = (
            f"Refreshed by the AICG runner.\n\n"
            f"- status: `{item.get('status')}`\n"
            f"- defer_reason: `{item.get('defer_reason', '-')}`\n"
            f"- retry_count: `{item.get('retry_count', 0)}`\n"
            f"- last_failure: `{item.get('last_failure', '-')}`\n"
        )
        return comment_issue(repo_path, decision["issue_number"], body=body)
    if action == "close":
        return close_issue(
            repo_path,
            decision["issue_number"],
            comment="Auto-closed by the AICG runner — verification passed.",
        )
    return {"returncode": 0, "stdout": "", "stderr": ""}


def open_issue(
    repo_path: Path,
    title: str,
    body: str,
    labels: list[str],
) -> dict[str, Any]:
    command = ["gh", "issue", "create", "--title", title, "--body", body]
    for label in labels:
        command.extend(["--label", label])
    completed = _run_gh(repo_path, command)
    return _result(completed, command)


def comment_issue(
    repo_path: Path, issue_number: int | None, body: str
) -> dict[str, Any]:
    if issue_number is None:
        return {"returncode": 1, "stdout": "", "stderr": "missing issue_number"}
    command = [
        "gh",
        "issue",
        "comment",
        str(issue_number),
        "--body",
        body,
    ]
    completed = _run_gh(repo_path, command)
    return _result(completed, command)


def close_issue(
    repo_path: Path,
    issue_number: int | None,
    comment: str | None = None,
) -> dict[str, Any]:
    if issue_number is None:
        return {"returncode": 1, "stdout": "", "stderr": "missing issue_number"}
    command = ["gh", "issue", "close", str(issue_number)]
    if comment:
        command.extend(["--comment", comment])
    completed = _run_gh(repo_path, command)
    return _result(completed, command)


def _run_gh(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _result(
    completed: subprocess.CompletedProcess[str], command: list[str]
) -> dict[str, Any]:
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }
