"""GitHub PR operations backed by the local git and gh CLIs."""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from .guardrails import evaluate_guardrails
from .state import state_path


class GitOpsError(RuntimeError):
    """Raised when a git or gh operation fails."""


def ref_segment(name: str) -> str:
    """A repo name made safe as one segment of a git branch name.

    Git rejects segments that start with a dot, so ``.github`` (every org's
    profile repo) silently failed ``checkout -B`` and work landed on main.
    """
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    if text.startswith("."):
        text = "dot" + text.lstrip(".")
    return text.replace(".", "-").strip("-") or "repo"


def branch_name(repo_name: str, work_id: str, today: date | None = None) -> str:
    return f"aicg/{(today or date.today()).isoformat()}/{ref_segment(repo_name)}/{work_id}"


def ensure_branch(repo_path: Path, branch: str) -> bool:
    """Create/reset ``branch`` and check it out; True only if HEAD is now on it."""
    subprocess.run(["git", "-C", str(repo_path), "checkout", "-B", branch],
                   capture_output=True, text=True, check=False)
    head = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True, check=False)
    return head.returncode == 0 and head.stdout.strip() == branch


def stage_all_except_runner_state(repo_path: Path) -> int:
    """``git add --all``, minus the runner's own ``.aicg/`` state. Returns git's exit code.

    Stage everything, then unstage ``.aicg``. An exclude pathspec
    (``-- . ':(exclude).aicg'``) looks equivalent, but when ``.aicg/`` is
    gitignored git stages the files and still exits 1 ("paths are ignored").
    """
    for args in (["add", "--all"], ["reset", "-q", "--", ".aicg"]):
        rc = subprocess.run(
            ["git", "-C", str(repo_path), *args], capture_output=True, text=True, check=False,
        ).returncode
        if rc != 0:
            return rc
    return 0


def discard_work_and_checkout_main(repo_path: Path) -> None:
    """Throw away an abandoned work item's edits and return to ``main``.

    A plain ``checkout main`` carries staged and unstaged edits along, leaving
    ``main`` dirty, and sync then skips the clone (``dirty_skip``) until
    someone cleans it by hand. The runner's ``.aicg/`` state is kept.
    """
    for args in (
        ["reset", "-q", "--hard"],
        ["clean", "-fdq", "-e", ".aicg"],
        ["checkout", "-q", "main"],
    ):
        subprocess.run(
            ["git", "-C", str(repo_path), *args], capture_output=True, text=True, check=False,
        )


def select_work_item(
    work_items: list[dict[str, Any]], work_id: str | None = None
) -> dict[str, Any]:
    """Pick a work item by id, defaulting to the highest-priority one."""
    if not work_items:
        raise GitOpsError("No work items are available for PR creation.")
    if work_id is None:
        return work_items[0]
    match = next((item for item in work_items if item.get("id") == work_id), None)
    if match is None:
        available = ", ".join(item.get("id", "?") for item in work_items)
        raise GitOpsError(
            f"work_id '{work_id}' not found in work plan; available: {available}"
        )
    return match


def _scope_label(work_item: dict[str, Any]) -> tuple[str, str]:
    """Pick a human-readable scope label for the PR body."""
    if work_item.get("module"):
        return ("Module", str(work_item["module"]))
    if work_item.get("project"):
        return ("Project", str(work_item["project"]))
    if work_item.get("path"):
        return ("Path", str(work_item["path"]))
    return ("Work", str(work_item.get("id", "?")))


def prepare_pr(
    repo_path: Path,
    work_plan: dict[str, Any],
    audit_report: dict[str, Any],
    validation_report: dict[str, Any],
    auto_merge: bool = False,
    work_id: str | None = None,
) -> dict[str, Any]:
    # Union work_items + backlog_items so backlog work (e.g.
    # exercise_depth_followup) can also have PRs opened for it.
    work_items = (
        (work_plan.get("work_items") or [])
        + (work_plan.get("backlog_items") or [])
    )
    if not work_items:
        raise GitOpsError("No work items are available for PR creation.")

    item = select_work_item(work_items, work_id=work_id)
    branch = branch_name(work_plan["repo"]["name"], item["id"])
    checkout_branch(repo_path, branch)

    changed = git_changed_files(repo_path)
    if not changed:
        raise GitOpsError("No curriculum changes found to commit or PR.")

    decision = evaluate_guardrails(
        repo_path,
        branch=branch,
        changed_files=changed,
        ci_status="unknown",
        auto_merge=auto_merge,
    )
    if not decision.allowed:
        raise GitOpsError("Guardrails blocked PR creation: " + "; ".join(decision.blockers))

    scope = item.get("module") or item.get("project") or item.get("path") or item.get("id")
    title = f"Fill {work_plan['repo']['name']} {scope} solution gap"
    body_path = state_path(repo_path, "pr-body.md")
    body_path.write_text(
        build_pr_body(item, audit_report, validation_report, branch),
        encoding="utf-8",
    )
    commit_all(repo_path, title)
    pr_url = create_pr(repo_path, title, body_path)
    return {
        "branch": branch,
        "title": title,
        "body_path": str(body_path),
        "pr_url": pr_url,
        "changed_files": changed,
        "guardrails": {
            "allowed": decision.allowed,
            "warnings": list(decision.warnings),
        },
    }


def build_pr_body(
    work_item: dict[str, Any],
    audit_report: dict[str, Any],
    validation_report: dict[str, Any],
    branch: str,
) -> str:
    rollback = f"git checkout main && git branch -D {branch}"
    scope_kind, scope_value = _scope_label(work_item)
    return (
        f"## AICG Work Item\n\n"
        f"- Work ID: `{work_item['id']}`\n"
        f"- {scope_kind}: `{scope_value}`\n"
        f"- Type: `{work_item['type']}`\n\n"
        "## Audit Summary\n\n"
        f"- Status: `{audit_report['summary']['status']}`\n"
        f"- Errors: `{audit_report['summary']['error_count']}`\n"
        f"- Warnings: `{audit_report['summary']['warning_count']}`\n\n"
        "## Validation Summary\n\n"
        f"- Status: `{validation_report['status']}`\n"
        f"- Checks: `{len(validation_report.get('checks', []))}`\n\n"
        "## Rollback\n\n"
        f"```bash\n{rollback}\n```\n"
    )


def checkout_branch(repo_path: Path, branch: str) -> None:
    run(repo_path, ["git", "checkout", "-B", branch])


def commit_all(repo_path: Path, message: str) -> None:
    """Stage everything except gitignored paths and commit.

    ``git add --all`` with no pathspec silently respects gitignore, so
    ``.aicg/`` is skipped without triggering the ``addIgnoredFile``
    advice. Adding an explicit pathspec that hits ``.aicg`` (even via
    ``:(exclude)``) trips the warning and returns non-zero, even when
    staging actually succeeded — confusing and brittle.
    """
    run(repo_path, ["git", "add", "--all"])
    run(repo_path, ["git", "commit", "-m", message])


def create_pr(repo_path: Path, title: str, body_path: Path) -> str:
    # gh pr create requires the branch to exist on origin first.
    branch = run(repo_path, ["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    run(repo_path, ["git", "push", "-u", "origin", branch])
    completed = run(
        repo_path,
        ["gh", "pr", "create", "--base", "main", "--title", title, "--body-file", str(body_path)],
    )
    return completed.stdout.strip()


def git_changed_files(repo_path: Path) -> list[str]:
    """Changed paths, exactly as on disk.

    Uses ``--porcelain -z``: NUL-separated and never C-quoted, so paths with
    spaces or non-ASCII characters come back verbatim (``--short`` quotes
    them, which made the guardrail marker scan skip those files).
    """
    completed = run(repo_path, ["git", "status", "--porcelain", "-z"], check=False)
    entries = [e for e in completed.stdout.split("\0") if e]
    paths: list[str] = []
    skip_next = False
    for entry in entries:
        if skip_next:  # rename/copy: the source path follows the destination
            skip_next = False
            continue
        status, path = entry[:2], entry[3:]
        skip_next = "R" in status or "C" in status
        if not is_aicg_state_path(path):
            paths.append(path)
    return paths


def default_branch(repo_path: Path) -> str:
    """The remote's default branch name, falling back to ``main``."""
    completed = run(
        repo_path, ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False
    )
    ref = completed.stdout.strip()
    return ref.split("/", 1)[1] if completed.returncode == 0 and "/" in ref else "main"


def park_changes(repo_path: Path, label: str, today: date | None = None) -> str | None:
    """Move uncommitted work onto a local ``aicg/parked/...`` branch.

    Used when generated content can't become a PR (verification failed,
    quarantined, guardrails, gh error). Without this the work stayed in the
    working tree, so every later sync skipped the repo as dirty and the
    content sat unseen for weeks. Returns the branch name, or None if the
    tree was already clean. Runner state under ``.aicg/`` is never touched.
    """
    if not git_changed_files(repo_path):
        return None
    stamp = (today or date.today()).isoformat()
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in label)[:80]
    branch = f"aicg/parked/{stamp}/{safe}"
    run(repo_path, ["git", "checkout", "-q", "-B", branch])
    commit_all(repo_path, f"aicg: park unpublished work for {label}")
    run(repo_path, ["git", "checkout", "-q", default_branch(repo_path)])
    return branch


def is_aicg_state_path(path: str) -> bool:
    return path == ".aicg" or path.startswith(".aicg/")


def run(repo_path: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitOpsError(
            f"Command failed ({' '.join(args)}): {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed
