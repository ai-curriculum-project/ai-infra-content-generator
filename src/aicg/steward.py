"""PR / issue / discussion stewardship for autonomous org operation.

The steward closes the audit -> plan -> generate -> verify -> PR loop by
performing the final merge step under explicit guardrails.

Lifecycle per PR:

    open
      |
      v
    ci_pending   (statusCheckRollup is PENDING / IN_PROGRESS)
      |
      v
    ci_passed    (rollup SUCCESS)            ci_failed (FAILURE)  -> end
      |
      v
    guardrails_ok                            guardrails_blocked   -> end
      |
      v
    merge_requested
      |
      v
    merged

Every transition is recorded in ``steward-report.json`` so the next
invocation can resume mid-loop. The merge call uses
``gh pr merge --auto --squash --delete-branch`` so GitHub does the
final flip once required status checks are green, which means the
runner does not have to hold the lock until merge actually completes.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .audit import PlaceholderCache, scan_placeholders
from .guardrails import (
    RESTRICTED_AUTO_MERGE_PATTERNS,
    GuardrailDecision,
    matches_any,
)
from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

STEWARD_REPORT = "steward-report.json"
PR_RESPONSE_QUEUE = "pr-response-queue.json"
DEFAULT_MAX_RESPONSE_ATTEMPTS = 3


class StewardError(RuntimeError):
    """Raised when the steward cannot proceed."""


def steward_run(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    apply: bool = False,
    ci_timeout_seconds: int = 600,
    ci_poll_seconds: int = 30,
    merge_method: str = "squash",
) -> dict[str, Any]:
    """Steward every open PR in the manifest's repos.

    When ``apply`` is False the steward operates in dry-run mode: it
    reads PR + CI state and prints the decision it would have made, but
    does not call ``gh pr merge``.
    """
    state_dir = state_dir_for_manifest(manifest, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    repo_reports: list[dict[str, Any]] = []
    for repo in manifest.repo_names:
        repo_path = manifest.repo_path(workspace, repo)
        repo_reports.append(
            steward_repo(
                repo=repo,
                repo_path=repo_path,
                apply=apply,
                ci_timeout_seconds=ci_timeout_seconds,
                ci_poll_seconds=ci_poll_seconds,
                merge_method=merge_method,
            )
        )

    report = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "operation": "steward",
        "status": "applied" if apply else "dry_run",
        "ci_timeout_seconds": ci_timeout_seconds,
        "ci_poll_seconds": ci_poll_seconds,
        "merge_method": merge_method,
        "repos": repo_reports,
    }
    write_json(state_dir / STEWARD_REPORT, report)

    # Side-effect: write the PR-response queue so daily-remediate can
    # pick up auto-respond work items. Keeps the steward read-only by
    # default (no commits, no comments).
    response_queue = _build_response_queue(repo_reports, state_dir)
    write_json(state_dir / PR_RESPONSE_QUEUE, response_queue)

    # When the steward actually merged something (apply=True and at least
    # one PR landed), refresh the structural curriculum manifest so
    # downstream consumers (research prompts, /find, the canonical-source
    # staleness audit) see the new state.
    if apply and _any_pr_merged(repo_reports):
        _refresh_curriculum_manifest(workspace, report)

    return report


def _any_pr_merged(repo_reports: list[dict[str, Any]]) -> bool:
    for repo in repo_reports:
        for pr in repo.get("prs", []) or []:
            if pr.get("decision") == "merged":
                return True
    return False


def _refresh_curriculum_manifest(workspace: Path, report: dict[str, Any]) -> None:
    """Call scripts/refresh-curriculum-manifest.sh; failures are non-fatal."""
    import subprocess
    from pathlib import Path as _Path

    here = _Path(__file__).resolve().parent.parent.parent  # repo root
    script = here / "scripts" / "refresh-curriculum-manifest.sh"
    if not script.exists():
        return
    try:
        completed = subprocess.run(
            [str(script), "--workspace", str(workspace)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        report["curriculum_manifest_refresh"] = {
            "ran": True,
            "returncode": completed.returncode,
            "stderr_tail": completed.stderr[-300:],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["curriculum_manifest_refresh"] = {
            "ran": True,
            "returncode": -1,
            "error": str(exc),
        }


def _build_response_queue(
    repo_reports: list[dict[str, Any]], state_dir: Path
) -> dict[str, Any]:
    """Diff the new review_blocked PRs against the prior queue.

    Items already in flight (response_count > 0 but blockers unchanged)
    keep their retry count. New blockers reset the count. Items that
    exceeded ``DEFAULT_MAX_RESPONSE_ATTEMPTS`` get ``escalated`` so
    daily-issues opens a tracking issue instead.
    """
    prior_path = state_dir / PR_RESPONSE_QUEUE
    prior_items: dict[str, dict[str, Any]] = {}
    if prior_path.exists():
        try:
            payload = json.loads(prior_path.read_text(encoding="utf-8"))
            for item in payload.get("items", []):
                prior_items[item["id"]] = item
        except (OSError, json.JSONDecodeError):
            prior_items = {}

    new_items: list[dict[str, Any]] = []
    for repo_report in repo_reports:
        repo = repo_report.get("repo", "")
        for pr in repo_report.get("prs", []):
            if pr.get("state") != "review_blocked":
                continue
            item_id = f"{repo}:respond-pr-{pr['pr_number']}"
            blockers = pr.get("review_blockers", []) or []
            blocker_sig = _blocker_signature(blockers)
            prior = prior_items.get(item_id)
            response_count = int((prior or {}).get("response_count", 0))
            prior_sig = (prior or {}).get("blocker_signature")
            if prior_sig and prior_sig == blocker_sig:
                status = (
                    "escalated"
                    if response_count >= DEFAULT_MAX_RESPONSE_ATTEMPTS
                    else "ready"
                )
            else:
                # New / changed blockers: reset retry count.
                response_count = 0
                status = "ready"
            new_items.append(
                {
                    "id": item_id,
                    "repo": repo,
                    "pr_number": pr["pr_number"],
                    "head_ref": pr.get("head_ref"),
                    "title": pr.get("title"),
                    "type": "respond_pr_review",
                    "severity": "high",
                    "status": status,
                    "response_count": response_count,
                    "max_response_attempts": DEFAULT_MAX_RESPONSE_ATTEMPTS,
                    "blocker_signature": blocker_sig,
                    "blockers": blockers,
                    "updated_at": utc_now(),
                }
            )
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "item_count": len(new_items),
        "items": new_items,
    }


def _blocker_signature(blockers: list[dict[str, Any]]) -> str:
    """Deterministic fingerprint over blocker identity (not content)."""
    parts = []
    for b in blockers:
        if b.get("kind") == "unresolved_thread":
            parts.append(f"t:{b.get('thread_id')}")
        elif b.get("kind") == "changes_requested":
            parts.append(f"cr:{b.get('author')}")
    return "|".join(sorted(parts))


def steward_repo(
    repo: str,
    repo_path: Path,
    apply: bool,
    ci_timeout_seconds: int,
    ci_poll_seconds: int,
    merge_method: str,
) -> dict[str, Any]:
    if not repo_path.exists():
        return {
            "repo": repo,
            "path": str(repo_path),
            "present": False,
            "prs": [],
        }

    prs = list_open_prs(repo_path)
    pr_outcomes: list[dict[str, Any]] = []
    for pr in prs:
        pr_outcomes.append(
            steward_pr(
                repo=repo,
                repo_path=repo_path,
                pr=pr,
                apply=apply,
                ci_timeout_seconds=ci_timeout_seconds,
                ci_poll_seconds=ci_poll_seconds,
                merge_method=merge_method,
            )
        )

    return {
        "repo": repo,
        "path": str(repo_path),
        "present": True,
        "pr_count": len(pr_outcomes),
        "merged_count": sum(1 for item in pr_outcomes if item["state"] == "merged"),
        "blocked_count": sum(
            1 for item in pr_outcomes if item["state"] == "guardrails_blocked"
        ),
        "ci_failed_count": sum(
            1 for item in pr_outcomes if item["state"] == "ci_failed"
        ),
        "review_blocked_count": sum(
            1 for item in pr_outcomes if item["state"] == "review_blocked"
        ),
        "prs": pr_outcomes,
    }


def steward_pr(
    repo: str,
    repo_path: Path,
    pr: dict[str, Any],
    apply: bool,
    ci_timeout_seconds: int,
    ci_poll_seconds: int,
    merge_method: str,
) -> dict[str, Any]:
    number = pr.get("number")
    history: list[dict[str, Any]] = [
        {"timestamp": utc_now(), "state": "open"}
    ]

    ci_result = wait_for_ci(
        repo_path=repo_path,
        pr_number=number,
        timeout_seconds=ci_timeout_seconds,
        poll_seconds=ci_poll_seconds,
    )
    history.append(
        {
            "timestamp": utc_now(),
            "state": "ci_pending",
            "elapsed_seconds": ci_result["elapsed_seconds"],
        }
    )
    if ci_result["status"] not in {"success", "no_ci_present"}:
        terminal = "ci_failed" if ci_result["status"] == "failure" else "ci_timeout"
        history.append({"timestamp": utc_now(), "state": terminal})
        return {
            "repo": repo,
            "pr_number": number,
            "title": pr.get("title"),
            "state": terminal,
            "head_ref": pr.get("headRefName"),
            "ci": ci_result,
            "history": history,
        }
    history.append(
        {
            "timestamp": utc_now(),
            "state": "ci_passed" if ci_result["status"] == "success" else "no_ci_present",
        }
    )

    # Pull the full PR view (with file list + labels + mergeable state)
    # before evaluating guardrails. ``pr_view`` was already called by
    # ``wait_for_ci`` but its return is not propagated, so we ask
    # again. Cheap enough — this is the only place guardrails run.
    full_pr = pr_view(repo_path, number) or pr
    # Merge the list-summary fields into the full view so callers see
    # the union (list-only callers got ``title``, view-only callers
    # got ``files`` etc.).
    merged_pr = {**pr, **full_pr}
    decision = evaluate_pr_guardrails(repo_path=repo_path, pr=merged_pr)
    if not decision.allowed:
        history.append(
            {
                "timestamp": utc_now(),
                "state": "guardrails_blocked",
                "blockers": list(decision.blockers),
            }
        )
        return {
            "repo": repo,
            "pr_number": number,
            "title": pr.get("title"),
            "state": "guardrails_blocked",
            "head_ref": pr.get("headRefName"),
            "blockers": list(decision.blockers),
            "warnings": list(decision.warnings),
            "history": history,
        }
    history.append({"timestamp": utc_now(), "state": "guardrails_ok"})

    # Review-state check: CHANGES_REQUESTED reviews and unresolved
    # review threads block auto-merge. Bot threads count too — once a
    # bot stops complaining, its thread typically goes outdated/resolved.
    owner, repo_name = _owner_repo_from_url(merged_pr.get("url", ""))
    if owner and repo_name:
        review_state = fetch_review_state(repo_path, owner, repo_name, number)
        if review_state.get("error"):
            history.append(
                {
                    "timestamp": utc_now(),
                    "state": "review_state_unknown",
                    "error": review_state["error"][:400],
                }
            )
        else:
            classification = classify_review_state(
                review_state, pr_author=(merged_pr.get("author") or {}).get("login")
            )
            if classification["verdict"] == "blocked":
                history.append(
                    {
                        "timestamp": utc_now(),
                        "state": "review_blocked",
                        "blockers": classification["blockers"],
                    }
                )
                return {
                    "repo": repo,
                    "pr_number": number,
                    "title": pr.get("title"),
                    "head_ref": pr.get("headRefName"),
                    "state": "review_blocked",
                    "review_blockers": classification["blockers"],
                    "history": history,
                }
            history.append({"timestamp": utc_now(), "state": "review_ok"})

    if not apply:
        history.append({"timestamp": utc_now(), "state": "dry_run_skipped_merge"})
        return {
            "repo": repo,
            "pr_number": number,
            "title": pr.get("title"),
            "state": "would_merge",
            "head_ref": pr.get("headRefName"),
            "history": history,
        }

    merge_result = enable_auto_merge(repo_path, number, method=merge_method)
    if merge_result["returncode"] != 0:
        history.append(
            {
                "timestamp": utc_now(),
                "state": "merge_failed",
                "stderr": merge_result["stderr"],
            }
        )
        return {
            "repo": repo,
            "pr_number": number,
            "title": pr.get("title"),
            "state": "merge_failed",
            "head_ref": pr.get("headRefName"),
            "merge_result": merge_result,
            "history": history,
        }
    history.append({"timestamp": utc_now(), "state": "merge_requested"})
    final_state = pr_state(repo_path, number)
    if final_state == "MERGED":
        history.append({"timestamp": utc_now(), "state": "merged"})
        return {
            "repo": repo,
            "pr_number": number,
            "title": pr.get("title"),
            "state": "merged",
            "head_ref": pr.get("headRefName"),
            "history": history,
        }
    return {
        "repo": repo,
        "pr_number": number,
        "title": pr.get("title"),
        "state": "merge_pending",
        "head_ref": pr.get("headRefName"),
        "history": history,
    }


# ---------------------------------------------------------------------------
# gh wrappers (one wrapper per command so they're easy to mock in tests)
# ---------------------------------------------------------------------------


def list_open_prs(repo_path: Path) -> list[dict[str, Any]]:
    """Return open PRs for ``repo_path`` via ``gh pr list``."""
    completed = _run_gh(
        repo_path,
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,headRefOid,author,labels,isDraft",
            "--limit",
            "100",
        ],
    )
    if completed.returncode != 0:
        return []
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []


def pr_view(repo_path: Path, pr_number: int) -> dict[str, Any]:
    completed = _run_gh(
        repo_path,
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,title,headRefName,mergeable,mergeStateStatus,state,labels,statusCheckRollup,changedFiles,files",
        ],
    )
    if completed.returncode != 0:
        return {}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}


def pr_state(repo_path: Path, pr_number: int) -> str:
    """Return ``OPEN``, ``MERGED``, or ``CLOSED`` for a PR."""
    completed = _run_gh(
        repo_path,
        ["gh", "pr", "view", str(pr_number), "--json", "state"],
    )
    if completed.returncode != 0:
        return "UNKNOWN"
    try:
        return json.loads(completed.stdout).get("state", "UNKNOWN")
    except json.JSONDecodeError:
        return "UNKNOWN"


def enable_auto_merge(
    repo_path: Path, pr_number: int, method: str = "squash"
) -> dict[str, Any]:
    """Call ``gh pr merge --auto`` and classify the outcome.

    gh's stdout is unreliable for distinguishing 'merged immediately'
    from 'queued for auto-merge', so after the call we re-query the
    PR's actual state to ground the status. Without this, callers see
    ``status=None`` even when the PR has merged — which made the inline
    -merge loop spin 89 times on PR #5.
    """
    method = method.lower()
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(
        method, "--squash"
    )
    command = ["gh", "pr", "merge", str(pr_number), "--auto", flag, "--delete-branch"]
    completed = _run_gh(repo_path, command)

    status = _classify_merge_outcome(repo_path, pr_number, completed)
    return {
        "status": status,
        "command": " ".join(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
    }


def _classify_merge_outcome(
    repo_path: Path,
    pr_number: int,
    completed: subprocess.CompletedProcess[str],
) -> str:
    """Map gh's command result + a follow-up state read into a single status."""
    out = (completed.stdout or "") + (completed.stderr or "")
    lowered = out.lower()
    if completed.returncode != 0:
        if "no commits" in lowered or "already merged" in lowered:
            return "already_merged"
        if "auto-merge is not allowed" in lowered:
            return "auto_merge_not_allowed"
        return "merge_call_failed"
    # gh returned 0 — re-read the PR's actual state so we can tell
    # 'merged immediately' from 'auto-merge enabled and waiting'.
    view = _run_gh(
        repo_path,
        ["gh", "pr", "view", str(pr_number), "--json", "state,mergedAt,autoMergeRequest"],
    )
    if view.returncode != 0:
        return "merged"  # best-effort; gh said it merged
    try:
        payload = json.loads(view.stdout)
    except json.JSONDecodeError:
        return "merged"
    if payload.get("state") == "MERGED" or payload.get("mergedAt"):
        return "merged"
    if payload.get("autoMergeRequest"):
        return "auto_merge_enabled"
    # State unchanged after the call — treat as auto_merge_enabled since
    # gh reported success.
    return "auto_merge_enabled"


def _run_gh(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# CI rollup
# ---------------------------------------------------------------------------


_CI_TERMINAL_SUCCESS = {"SUCCESS"}
_CI_TERMINAL_FAILURE = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


CI_ABSENT_GRACE_SECONDS = 30


def wait_for_ci(
    repo_path: Path,
    pr_number: int,
    timeout_seconds: int,
    poll_seconds: int,
    ci_absent_grace_seconds: int = CI_ABSENT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Poll ``gh pr view`` until the status-check rollup is terminal.

    Repos with no CI workflow at all return an empty rollup forever.
    Rather than hang until ``timeout_seconds`` elapses, return
    ``no_ci_present`` after ``ci_absent_grace_seconds`` of consistently
    empty rollups. GitHub typically registers status checks within
    5-15 seconds of a push, so 30s grace is safe.
    """
    start = time.monotonic()
    deadline = start + timeout_seconds
    last_rollup: list[dict[str, Any]] = []
    while True:
        view = pr_view(repo_path, pr_number)
        last_rollup = view.get("statusCheckRollup", []) or []
        status = classify_rollup(last_rollup)
        elapsed = int(time.monotonic() - start)
        if status == "success":
            return {
                "status": "success",
                "rollup": last_rollup,
                "elapsed_seconds": elapsed,
            }
        if status == "failure":
            return {
                "status": "failure",
                "rollup": last_rollup,
                "elapsed_seconds": elapsed,
            }
        # CI-absent short-circuit: empty rollup + past grace period →
        # there's nothing to wait for. Treat as effectively passing so
        # the steward proceeds to guardrails + review check.
        if not last_rollup and elapsed >= ci_absent_grace_seconds:
            return {
                "status": "no_ci_present",
                "rollup": last_rollup,
                "elapsed_seconds": elapsed,
            }
        if time.monotonic() >= deadline:
            return {
                "status": "timeout",
                "rollup": last_rollup,
                "elapsed_seconds": timeout_seconds,
            }
        time.sleep(poll_seconds)


_PENDING_STATES = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "EXPECTED"}

_REVIEW_THREADS_QUERY = """\
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviews(first:50, states:[APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING]) {
        nodes {
          state
          author { login }
          body
          submittedAt
        }
      }
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first:1) {
            nodes {
              body
              path
              line
              author { login __typename }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_failed_checks(
    repo_path: Path, owner: str, repo_name: str, pr_number: int
) -> list[dict[str, Any]]:
    """Return one entry per FAILED check on the PR's head commit.

    Each entry: ``{name, conclusion, details_url, annotations}`` where
    ``annotations`` is a list of ``{path, start_line, message, level}``
    (empty when the checker emitted no annotations, e.g. older bash
    scripts that just exit non-zero).
    """
    # The head SHA — required for /commits/SHA/check-runs.
    head_sha = _fetch_pr_head_sha(repo_path, owner, repo_name, pr_number)
    if not head_sha:
        return []
    checks_args = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/commits/{head_sha}/check-runs",
        "--paginate",
    ]
    completed = subprocess.run(
        checks_args, cwd=repo_path, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    failed: list[dict[str, Any]] = []
    for run in payload.get("check_runs", []) or []:
        if str(run.get("conclusion", "")).upper() not in {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
        }:
            continue
        run_id = run.get("id")
        annotations = _fetch_check_annotations(
            repo_path, owner, repo_name, run_id
        ) if run_id else []
        failed.append(
            {
                "name": run.get("name") or "?",
                "conclusion": run.get("conclusion"),
                "details_url": run.get("details_url") or run.get("html_url"),
                "annotations": annotations,
            }
        )
    return failed


def _fetch_pr_head_sha(
    repo_path: Path, owner: str, repo_name: str, pr_number: int
) -> str | None:
    args = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/pulls/{pr_number}",
        "--jq",
        ".head.sha",
    ]
    completed = subprocess.run(
        args, cwd=repo_path, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip().strip('"')
    return sha or None


def _fetch_check_annotations(
    repo_path: Path, owner: str, repo_name: str, check_run_id: int
) -> list[dict[str, Any]]:
    args = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/check-runs/{check_run_id}/annotations",
        "--paginate",
    ]
    completed = subprocess.run(
        args, cwd=repo_path, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return []
    try:
        items = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    return [
        {
            "path": ann.get("path"),
            "start_line": ann.get("start_line"),
            "end_line": ann.get("end_line"),
            "level": ann.get("annotation_level"),
            "message": (ann.get("message") or "")[:600],
            "title": ann.get("title"),
        }
        for ann in items
        if isinstance(ann, dict)
    ]


def _owner_repo_from_url(url: str) -> tuple[str | None, str | None]:
    """Pull (owner, repo) out of a GitHub PR URL.

    Tolerates the absence of the URL — falls back to (None, None) which
    skips the review-state check rather than crashing the steward.
    """
    if not url or "github.com" not in url:
        return None, None
    parts = url.rstrip("/").split("/")
    # Expected shape: https://github.com/<owner>/<repo>/pull/<n>
    if len(parts) < 5:
        return None, None
    try:
        owner = parts[-4]
        repo = parts[-3]
        return owner, repo
    except IndexError:
        return None, None


def fetch_review_state(
    repo_path: Path, owner: str, repo_name: str, pr_number: int
) -> dict[str, Any]:
    """Fetch PR reviews + review threads via gh graphql.

    Returns ``{"reviews": [...], "threads": [...], "error": str|None}``.
    Errors are returned (not raised) so the steward can fall back to
    'block until a human looks' instead of crashing the whole pass.
    """
    args = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={repo_name}",
        "-F",
        f"number={pr_number}",
    ]
    completed = subprocess.run(
        args, cwd=repo_path, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return {
            "reviews": [],
            "threads": [],
            "error": (completed.stderr or completed.stdout)[-1500:],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"reviews": [], "threads": [], "error": f"parse: {exc}"}
    pr = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    reviews = ((pr.get("reviews") or {}).get("nodes")) or []
    threads = ((pr.get("reviewThreads") or {}).get("nodes")) or []
    return {"reviews": reviews, "threads": threads, "error": None}


def classify_review_state(
    review_state: dict[str, Any], pr_author: str | None = None
) -> dict[str, Any]:
    """Classify reviews + threads into a single mergeable verdict.

    Returns ``{"verdict": "mergeable"|"blocked", "blockers": [...]}``.
    Latest review per author wins. CHANGES_REQUESTED is a hard block.
    Unresolved (non-outdated) review threads are blockers too.
    Outdated threads are ignored on the assumption that the author
    addressed them with later commits.
    """
    blockers: list[dict[str, Any]] = []

    # Latest review per author.
    latest_by_author: dict[str, dict[str, Any]] = {}
    for review in review_state.get("reviews", []):
        author = (review.get("author") or {}).get("login") or "?"
        # Skip the PR author's own pending self-review.
        if pr_author and author == pr_author and review.get("state") == "PENDING":
            continue
        prev = latest_by_author.get(author)
        if prev is None or (review.get("submittedAt") or "") > (prev.get("submittedAt") or ""):
            latest_by_author[author] = review

    for author, review in latest_by_author.items():
        if (review.get("state") or "").upper() == "CHANGES_REQUESTED":
            blockers.append(
                {
                    "kind": "changes_requested",
                    "author": author,
                    "body": (review.get("body") or "")[:500],
                }
            )

    for thread in review_state.get("threads", []):
        if thread.get("isResolved"):
            continue
        if thread.get("isOutdated"):
            continue
        comments = (thread.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        author_info = first.get("author") or {}
        login = author_info.get("login") or "?"
        is_bot = author_info.get("__typename") == "Bot" or login.endswith("[bot]")
        blockers.append(
            {
                "kind": "unresolved_thread",
                "thread_id": thread.get("id"),
                "path": first.get("path"),
                "line": first.get("line"),
                "author": login,
                "is_bot": is_bot,
                "body": (first.get("body") or "")[:500],
            }
        )

    return {
        "verdict": "blocked" if blockers else "mergeable",
        "blockers": blockers,
    }


def classify_rollup(rollup: list[dict[str, Any]]) -> str:
    """Reduce GitHub's per-check rollup to a single status.

    The runner treats ``SUCCESS`` and ``SKIPPED`` as passes. Any
    ``IN_PROGRESS`` / ``QUEUED`` / ``PENDING`` keeps the rollup
    pending. Any ``FAILURE``/``ERROR``/etc. classifies the whole rollup
    as failure (one bad apple).
    """
    if not rollup:
        return "pending"
    has_pending = False
    for entry in rollup:
        # GitHub mixes two shapes: check runs (conclusion + status) and
        # commit statuses (state).
        status = (entry.get("status") or "").upper()
        conclusion = (entry.get("conclusion") or "").upper()
        state = (entry.get("state") or "").upper()
        # Pending signals can show up on either field.
        if status in _PENDING_STATES or state in _PENDING_STATES:
            has_pending = True
            continue
        terminal = conclusion or state
        if not terminal:
            has_pending = True
            continue
        if terminal in _CI_TERMINAL_FAILURE:
            return "failure"
        if terminal in {"NEUTRAL", "STALE", "SKIPPED"}:
            continue
        if terminal not in _CI_TERMINAL_SUCCESS:
            # Unknown terminal — treat as failure rather than silently
            # passing.
            return "failure"
    return "pending" if has_pending else "success"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def evaluate_pr_guardrails(
    repo_path: Path,
    pr: dict[str, Any],
) -> GuardrailDecision:
    """Re-check guardrails against the merged state of a PR.

    This duplicates ``guardrails.evaluate_guardrails`` but operates on
    the *PR view* (changed file list comes from ``gh``) so the steward
    does not need a local checkout of the head ref.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    if pr.get("isDraft"):
        blockers.append("PR is in draft state.")

    label_names = [
        (item.get("name") or "").lower()
        for item in (pr.get("labels") or [])
    ]
    if "do-not-merge" in label_names or "wip" in label_names:
        blockers.append("PR carries a do-not-merge / wip label.")

    head_ref = pr.get("headRefName") or ""
    if head_ref in {"main", "master"}:
        blockers.append("PR head ref is main/master.")

    changed = [
        item.get("path")
        for item in (pr.get("files") or [])
        if item.get("path")
    ]
    restricted = [
        path for path in changed if matches_any(path, RESTRICTED_AUTO_MERGE_PATTERNS)
    ]
    if restricted:
        blockers.append(
            "Restricted files in PR: " + ", ".join(sorted(restricted))
        )

    # Marker freedom: scan only the changed files in the local working
    # tree. ``gh pr checkout`` would be more accurate but for the
    # default aicg branch shape the head ref is local already.
    marker_findings = scan_placeholders(repo_path, cache=PlaceholderCache(repo_path))
    blocker_markers = [
        item
        for item in marker_findings
        if item.get("type") in {"needs_research", "manual_review"}
        and (not changed or item.get("path") in changed)
    ]
    if blocker_markers:
        sample = ", ".join(item.get("path", "?") for item in blocker_markers[:3])
        blockers.append(f"Marker remains in changed file(s): {sample}")

    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        blockers.append("PR has merge conflicts.")
    if mergeable not in {"MERGEABLE", "UNKNOWN", "CONFLICTING", ""}:
        warnings.append(f"Unexpected mergeable state: {mergeable}")

    return GuardrailDecision(
        allowed=not blockers,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )
