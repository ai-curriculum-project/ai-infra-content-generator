"""Manifest-driven org automation operations."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent_cli import AgentLimitReached, retry_after_has_passed
from .audit import audit_repo
from .generator import GenerationNotConfigured, generate_from_plan
from .manifest import (
    ManifestError,
    load_curriculum_manifest,
    summarize_manifest_for_prompt,
)
from .org_config import OrgManifest, RoleConfig, state_dir_for_manifest
from .planner import plan_from_audit
from .state import read_state, utc_now, write_json

# Path to the structural curriculum manifest, relative to the
# content-generator repo root. Used for grounding research prompts in
# what's already covered so the research agent has continuity to defend
# against rather than rediscovering coverage every cycle.
_CURRICULUM_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent.parent / "manifest" / "curriculum.manifest.json"
)

ORG_QUEUE = "work-queue.json"
ORG_RESEARCH_PLAN = "job-research-plan.json"
ORG_STEWARD_REPORT = "steward-report.json"

# Work-item types daily_remediate knows how to drive end-to-end. When a
# new handler ships, add the type here so the selector starts picking it
# up. Items whose type is NOT in this set stay in the queue (preserved
# for when a handler exists) but are skipped during selection — this
# avoids the runner spinning on a high-priority type with no handler and
# starving the items it CAN do.
HANDLED_WORK_TYPES = frozenset(
    {
        "respond_pr_review",
        # cross-repo (audit-derived, dispatched via _handle_cross_repo_item)
        "pairing_mismatch",
        "curriculum_nav_drift",
        "learning_gap",
        "org_profile_stale",
        # per-repo plan items (the structural generator drives these)
        "exercise_depth_followup",
        "project_solution_gap",
        "fill_solution_gap",
        "module_solution_gap",
        "exercise_solution_gap",
        # deterministic link-refresh (see link_refresh.handle_refresh_links_item)
        "refresh_links",
    }
)


class OrgRunnerError(RuntimeError):
    """Raised when an org-level operation cannot proceed."""


def sync_repositories(
    manifest: OrgManifest,
    workspace: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    actions: list[dict[str, Any]] = []
    if not dry_run:
        workspace.mkdir(parents=True, exist_ok=True)

    for repo in manifest.repo_names:
        repo_path = manifest.repo_path(workspace, repo)
        remote = manifest.remote_for(repo)
        if repo_path.exists():
            dirty = is_git_dirty(repo_path)
            command = ["git", "-C", str(repo_path), "pull", "--ff-only"]
            status = "dirty_skip" if dirty else "planned" if dry_run else "pulled"
            action = repo_action(repo, repo_path, remote, command, status)
            if dirty:
                action["warning"] = "Repository has local changes; skipping pull."
            elif not dry_run:
                action.update(run_command(command))
        else:
            command = ["git", "clone", remote, str(repo_path)]
            action = repo_action(repo, repo_path, remote, command, "planned" if dry_run else "cloned")
            if not dry_run:
                action.update(run_command(command))
        actions.append(action)

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "sync",
        "dry_run": dry_run,
        "workspace": str(workspace),
        "actions": actions,
    }


def plan_monthly_release(
    manifest: OrgManifest,
    workspace: Path,
    today: date | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    tag = release_tag(manifest, today or date.today())
    actions: list[dict[str, Any]] = []
    for repo in manifest.release_repo_names:
        repo_path = manifest.repo_path(workspace, repo)
        if not repo_path.exists():
            actions.append(
                {
                    "repo": repo,
                    "path": str(repo_path),
                    "status": "missing",
                    "message": "Repository is not present in workspace.",
                }
            )
            continue
        if is_git_dirty(repo_path):
            actions.append(
                {
                    "repo": repo,
                    "path": str(repo_path),
                    "status": "blocked",
                    "message": "Repository has local changes.",
                }
            )
            continue
        if git_tag_exists(repo_path, tag):
            actions.append({"repo": repo, "path": str(repo_path), "status": "exists", "tag": tag})
            continue
        tag_command = ["git", "-C", str(repo_path), "tag", "-a", tag, "-m", f"{tag} curriculum release"]
        push_command = ["git", "-C", str(repo_path), "push", "origin", tag]
        action = {
            "repo": repo,
            "path": str(repo_path),
            "status": "planned" if not apply else "tagged",
            "tag": tag,
            "commands": [shell_join(tag_command), shell_join(push_command)],
        }
        if apply:
            tag_result = run_command(tag_command)
            push_result = run_command(push_command)
            action["results"] = [tag_result, push_result]
            if tag_result["returncode"] != 0 or push_result["returncode"] != 0:
                action["status"] = "failed"
        actions.append(action)

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "monthly_release",
        "apply": apply,
        "tag": tag,
        "actions": actions,
        "note": "Tag pushes trigger per-repo release packaging workflows when installed.",
    }


def release_tag(manifest: OrgManifest, today: date) -> str:
    return today.strftime(manifest.release.get("tag_format", "v%Y.%m"))


def generate_research_packets(
    manifest: OrgManifest,
    workspace: Path,
    month: str | None = None,
    state_dir: Path | None = None,
    role_id: str | None = None,
) -> dict[str, Any]:
    month = month or date.today().strftime("%Y-%m")
    state_dir = state_dir_for_manifest(manifest, state_dir)
    prompt_dir = state_dir / "research" / month
    prompt_dir.mkdir(parents=True, exist_ok=True)

    roles = sorted(manifest.roles, key=lambda item: item.level)
    if role_id is not None:
        roles = [r for r in roles if r.id == role_id]
        if not roles:
            valid = ", ".join(sorted(r.id for r in manifest.roles))
            raise ValueError(
                f"Unknown role {role_id!r}. Known roles: {valid}"
            )

    packets: list[dict[str, Any]] = []
    for role in roles:
        learning_path = workspace / role.learning_repo
        prompt_path = prompt_dir / f"{role.id}.md"
        prompt_path.write_text(build_research_prompt(manifest, role, month), encoding="utf-8")
        packets.append(
            {
                "role": role.id,
                "title": role.title,
                "learning_repo": role.learning_repo,
                "learning_path": str(learning_path),
                "prompt_path": str(prompt_path),
                "job_requirements_markdown": str(
                    learning_path
                    / manifest.job_requirements.get("markdown_file", "JOB_REQUIREMENTS.md")
                ),
                "job_requirements_json": str(
                    learning_path
                    / manifest.job_requirements.get("structured_file", ".aicg/job-requirements.json")
                ),
                "status": "prompt_ready",
            }
        )

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "monthly_research",
        "month": month,
        "minimum_postings_per_role": manifest.research.get("minimum_postings_per_role", 25),
        "source_window_days": manifest.research.get("source_window_days", 45),
        "ownership_strategy": manifest.job_requirements.get(
            "ownership_strategy", "lowest_level_role"
        ),
        "packets": packets,
    }
    write_json(state_dir / ORG_RESEARCH_PLAN, report)
    return report


def run_org_audit(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    state_dir = state_dir_for_manifest(manifest, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    queue_items: list[dict[str, Any]] = []
    repo_reports: list[dict[str, Any]] = []

    for repo in manifest.solution_repo_names:
        repo_path = manifest.repo_path(workspace, repo)
        if not repo_path.exists():
            repo_reports.append({"repo": repo, "status": "missing", "path": str(repo_path)})
            continue
        audit = audit_repo(workspace, repo)
        plan = plan_from_audit(audit, repo_path=repo_path)
        repo_reports.append(
            {
                "repo": repo,
                "status": audit["summary"]["status"],
                "errors": audit["summary"]["error_count"],
                "warnings": audit["summary"]["warning_count"],
                "work_items": plan["work_item_count"],
            }
        )
        for item in plan.get("work_items", []):
            queue_items.append(
                {
                    "id": f"{repo}:{item['id']}",
                    "repo": repo,
                    "work_id": item["id"],
                    "module": item.get("module"),
                    "type": item["type"],
                    "title": item["title"],
                    "status": "ready",
                    "priority": queue_priority(manifest, repo, item),
                    "created_at": utc_now(),
                }
            )

        # Backlog items (exercise_depth_followup etc.) become medium-
        # severity work so they sit below structural gaps but get
        # worked eventually rather than living only in per-repo state.
        for item in plan.get("backlog_items", []):
            biased_item = {**item, "severity": "medium"}
            queue_items.append(
                {
                    "id": f"{repo}:{item['id']}",
                    "repo": repo,
                    "work_id": item["id"],
                    "module": item.get("module"),
                    "type": item["type"],
                    "severity": "medium",
                    "title": item["title"],
                    "status": "ready",
                    "priority": queue_priority(manifest, repo, biased_item),
                    "created_at": utc_now(),
                }
            )

        # Curriculum-nav audit per repo: orphan-on-disk + broken-references.
        try:
            from .nav_audit import audit_curriculum_nav

            nav_report = audit_curriculum_nav(repo_path)
            for item in nav_report.get("work_items", []):
                biased = {**item, "severity": item.get("severity", "medium")}
                queue_items.append(
                    {
                        "id": f"{repo}:{item['id']}",
                        "repo": repo,
                        "work_id": item["id"],
                        "type": item["type"],
                        "severity": biased["severity"],
                        "title": item["title"],
                        "path": item.get("path"),
                        "status": "ready",
                        "priority": queue_priority(manifest, repo, biased),
                        "created_at": utc_now(),
                    }
                )
        except Exception:  # noqa: BLE001
            # Nav-audit is best-effort; never let it block the main loop.
            pass

        # Splice in PR-response items emitted by the previous steward
        # run. These are high-severity so they jump ahead of structural
        # gaps — keeping an in-flight PR's loop closed beats opening
        # new ones.
        for refresh_item in _collect_pr_response_items(state_dir, repo):
            queue_items.append(
                {
                    "id": refresh_item["id"],
                    "repo": repo,
                    "work_id": refresh_item["id"].split(":", 1)[-1],
                    "type": refresh_item["type"],
                    "severity": refresh_item.get("severity", "high"),
                    "title": refresh_item.get(
                        "title",
                        f"Respond to PR #{refresh_item.get('pr_number')}",
                    ),
                    "pr_number": refresh_item.get("pr_number"),
                    "head_ref": refresh_item.get("head_ref"),
                    "blockers": refresh_item.get("blockers", []),
                    "response_count": refresh_item.get("response_count", 0),
                    "max_response_attempts": refresh_item.get(
                        "max_response_attempts", 3
                    ),
                    "status": refresh_item.get("status", "ready"),
                    "priority": queue_priority(
                        manifest,
                        repo,
                        {**refresh_item, "severity": "high"},
                    ),
                    "created_at": utc_now(),
                }
            )

        # Splice in freshness work items if the most recent audit-links /
        # audit-versions / review runs left reports behind. These are
        # additive — structural gaps remain in the queue too.
        for refresh_item in _collect_freshness_items(repo_path):
            promoted = {
                "id": f"{repo}:{refresh_item['id']}",
                "repo": repo,
                "work_id": refresh_item["id"],
                "type": refresh_item["type"],
                "severity": refresh_item.get("severity", "low"),
                "title": refresh_item.get("title", refresh_item["id"]),
                "path": refresh_item.get("path"),
                "status": "ready",
                "priority": queue_priority(manifest, repo, refresh_item),
                "created_at": utc_now(),
            }
            # Preserve the audit's payload so the handler has something
            # to work with. refresh_links needs `details` (the broken
            # URLs), refresh_versions needs `matches` + `target` info.
            for k in ("details", "broken_count", "matches", "target",
                      "current_target"):
                if k in refresh_item:
                    promoted[k] = refresh_item[k]
            queue_items.append(promoted)

    # Learning-repo structural audits. Each learning_gap item gets
    # medium-severity priority (below solution structural, above
    # default refresh).
    try:
        from .learning_audit import audit_learning_repo

        for learning_repo in manifest.learning_repo_names:
            learning_path = workspace / learning_repo
            if not learning_path.exists():
                continue
            la_report = audit_learning_repo(learning_path)
            for item in la_report.get("work_items", []):
                biased = {**item, "severity": item.get("severity", "medium")}
                queue_items.append(
                    {
                        "id": f"{learning_repo}:{item['id']}",
                        "repo": learning_repo,
                        "work_id": item["id"],
                        "type": item["type"],
                        "severity": biased["severity"],
                        "title": item["title"],
                        "path": item.get("path"),
                        "status": "ready",
                        "priority": queue_priority(manifest, learning_repo, biased),
                        "created_at": utc_now(),
                    }
                )
    except Exception:  # noqa: BLE001
        pass

    # Cross-repo pairing audit (one report per org).
    try:
        from .pairing_audit import audit_pairing

        pa_report = audit_pairing(manifest, workspace, state_dir=state_dir)
        for item in pa_report.get("work_items", []):
            biased = {**item, "severity": item.get("severity", "medium")}
            queue_items.append(
                {
                    "id": item["id"],
                    "repo": "_pairing",
                    "work_id": item["id"],
                    "type": item["type"],
                    "severity": biased["severity"],
                    "title": item["title"],
                    "status": "ready",
                    "priority": queue_priority(manifest, "_pairing", biased),
                    "created_at": utc_now(),
                }
            )
    except Exception:  # noqa: BLE001
        pass

    # Org-profile audit on the .github repo: report only. Profile pages are
    # generated by `aicg org publish-profiles` (the weekly publish job), so an
    # agent editing them would just be overwritten on the next render.
    try:
        from .nav_audit import audit_org_profile

        audit_org_profile(manifest, workspace, state_dir=state_dir)
    except Exception:  # noqa: BLE001
        pass

    queue_items.sort(key=lambda item: (item["priority"], item["repo"], item["work_id"]))
    queue = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "weekly_audit",
        "workspace": str(workspace.resolve()),
        "work_item_count": len(queue_items),
        "repo_reports": repo_reports,
        "work_items": queue_items,
    }
    write_json(state_dir / ORG_QUEUE, queue)
    return queue


DRAIN_WALL_CLOCK_SECONDS = 7200  # 2h cap per tick
VERIFY_SELF_HEAL_MAX_ATTEMPTS = 3
INLINE_MERGE_POLL_SECONDS = 15
# 30 min: 3 CI self-heal cycles × (3 min CI wait + ~5 min agent) + buffer.
# Below this the timeout fires before the loop can use its full retry budget.
INLINE_MERGE_TIMEOUT_SECONDS = 1800
CI_SELF_HEAL_MAX_ATTEMPTS = 3
CI_WAIT_PER_ATTEMPT_SECONDS = 300  # 5 min per CI cycle
REVIEW_SELF_HEAL_MAX_ATTEMPTS = 3


def run_daily_remediation(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    drain_until_empty: bool = False,
    wall_clock_cap_seconds: int = DRAIN_WALL_CLOCK_SECONDS,
) -> dict[str, Any]:
    """Drive ONE work item end-to-end from ready to merged.

    Default is single-item-per-tick: pick the highest-priority ready
    item, run the full pipeline (generate → verify-with-self-heal →
    propagate → commit/PR → inline-merge), exit. Pass
    ``drain_until_empty=True`` to keep processing items until the queue
    is empty, subscription limit hits, or the wall-clock cap fires.

    Single-item mode is the intended default: one item lands fully
    (committed, PR opened, merged on main) per tick. The next hourly
    tick picks up the next item. No partial state left between ticks.
    """
    state_dir = state_dir_for_manifest(manifest, state_dir)
    queue_path = state_dir / ORG_QUEUE
    if not queue_path.exists():
        run_org_audit(manifest, workspace, state_dir=state_dir)
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    refresh_deferred_items(queue)

    deadline = time.monotonic() + max(60, wall_clock_cap_seconds)
    drained: list[dict[str, Any]] = []
    aggregate_status = "no_items"
    exit_reason = "queue_empty"

    skipped_unhandled = 0
    while True:
        ready_all = [
            item for item in queue.get("work_items", []) if item.get("status") == "ready"
        ]
        ready = [
            item for item in ready_all
            if item.get("type", "") in HANDLED_WORK_TYPES
        ]
        skipped_unhandled = len(ready_all) - len(ready)
        if not ready:
            if not drained:
                # Preserve old behavior on the very first iteration:
                # nothing pickable, fall through to supplemental work.
                # Surface the skipped-unhandled count so operators see
                # why the queue looks idle.
                supp = generate_supplemental_packet(manifest, workspace, state_dir)
                if skipped_unhandled:
                    supp["skipped_unhandled_types"] = skipped_unhandled
                return supp
            break
        if time.monotonic() >= deadline:
            exit_reason = "wall_clock_cap"
            break

        item = ready[0]
        item_result = _process_one_item(
            manifest=manifest,
            workspace=workspace,
            state_dir=state_dir,
            queue=queue,
            queue_path=queue_path,
            item=item,
        )
        drained.append(item_result)
        aggregate_status = item_result.get("status", aggregate_status)

        # Hard exits: subscription limit + escalation force us to stop
        # so other timers / a human can take a look.
        if item_result.get("status") == "deferred" and item_result.get(
            "defer_reason"
        ) == "agent_subscription_limit":
            exit_reason = "subscription_limit"
            break
        if not drain_until_empty:
            exit_reason = "single_item_mode"
            break

    summary = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "operation": "daily_remediate",
        "status": aggregate_status,
        "exit_reason": exit_reason,
        "items_processed": len(drained),
        "items": drained,
        "skipped_unhandled_types": skipped_unhandled,
    }
    write_json(state_dir / "daily-run-state.json", summary)
    return summary


def _process_one_item(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path,
    queue: dict[str, Any],
    queue_path: Path,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Drive a single work item from ready → merged (or to a failure)."""
    repo_path = manifest.repo_path(workspace, item["repo"])
    result = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "process_work_item",
        "selected": item,
        "status": "started",
    }
    if item.get("type") == "respond_pr_review":
        return _handle_pr_response_item(
            manifest=manifest,
            workspace=workspace,
            repo_path=repo_path,
            item=item,
            state_dir=state_dir,
            result=result,
        )

    if item.get("type") == "refresh_links":
        from .link_refresh import handle_refresh_links_item

        # Catch unexpected handler crashes here so the queue item is
        # always dispositioned. An un-dispositioned item gets re-picked
        # next tick, producing an infinite failure loop on the same URL
        # (see the 2026-06-01 16:00 onward incident — scheme-less URL
        # crashed the urllib fetcher 15 hours in a row).
        try:
            refresh_result = handle_refresh_links_item(
                workspace=workspace,
                item=item,
                repo_path=manifest.repo_path(workspace, item.get("repo", "")),
            )
        except Exception as exc:  # noqa: BLE001 - intentional broad catch at boundary
            refresh_result = {
                "status": "handler_crashed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        result.update(refresh_result)
        # Mark the queue item so the selector won't re-pick it.
        if refresh_result.get("status") in {
            "pr_opened", "edited", "no_action_now_alive",
        }:
            item["status"] = "pr_open" if refresh_result.get("pr") else "completed"
            item["pr_url"] = refresh_result.get("pr", {}).get("pr_url")
            item["pr_branch"] = refresh_result.get("pr", {}).get("branch")
        elif refresh_result.get("status") in {
            "no_replacements_found", "no_diff_after_apply",
        }:
            # Defer with a long retry — next audit cycle may surface
            # Wayback snapshots that aren't indexed yet, or the upstream
            # site may come back. Don't burn cycles re-checking sooner.
            item["status"] = "deferred"
            item["defer_reason"] = "no_link_resolution"
            item["retry_after"] = _retry_after_in_minutes(60 * 24 * 7)
        else:
            item["status"] = "failed"
            item["last_failure"] = refresh_result.get("reason", "unknown")
        item["updated_at"] = utc_now()
        write_json(queue_path, queue)
        return result

    if item.get("type") in {
        "pairing_mismatch",
        "curriculum_nav_drift",
        "learning_gap",
        "org_profile_stale",
    }:
        return _handle_cross_repo_item(
            manifest=manifest,
            workspace=workspace,
            item=item,
            state_dir=state_dir,
            queue=queue,
            queue_path=queue_path,
            result=result,
        )


    try:
        plan = read_state(repo_path, "work-plan.json")
    except FileNotFoundError:
        # Org queue references this repo but the per-repo plan got
        # cleared (gitignored .aicg/ wiped, fresh clone, etc.). Re-
        # audit on the fly so the tick can proceed instead of crashing.
        from .planner import plan_from_audit

        audit = audit_repo(workspace, item["repo"])
        plan = plan_from_audit(audit, repo_path=repo_path)
    try:
        run_state = generate_from_plan(
            repo_path,
            plan,
            module=item.get("module"),
            work_id=item["work_id"],
            command_override=content_generation_command(manifest),
        )
        item["status"] = "generated"
        item["updated_at"] = utc_now()
        item["retry_count"] = 0
        result["status"] = "generated"
        result["run_state"] = run_state

        # Verify with self-heal: if the agent's output misses a required
        # heading, has a broken source citation, etc., feed the findings
        # back to the agent so it can fix them. Cap retries; escalate
        # past the cap.
        verify_outcome = _verify_with_self_heal(
            manifest=manifest,
            workspace=workspace,
            repo_path=repo_path,
            item=item,
            plan=plan,
        )
        result["verify"] = verify_outcome["verify"]
        result["self_heal_attempts"] = verify_outcome["attempts"]

        if verify_outcome["status"] == "verified" and not _quality_gate_passed(
            manifest, repo_path, item, result
        ):
            # Eval-gate quarantine (P1, config-gated): structurally valid but
            # below the calibrated quality bar — do not propagate/PR/merge.
            item["status"] = "quarantined"
            item["verified_at"] = utc_now()
        elif verify_outcome["status"] == "verified":
            item["status"] = "verified"
            from .propagate import propagate_repo

            propagate_report = propagate_repo(
                workspace, item["repo"], work_id=item["work_id"]
            )
            result["propagate"] = {
                "status": propagate_report["status"],
                "updated_count": len(propagate_report.get("updated", [])),
            }
            pr_outcome = _open_work_item_pr(repo_path, plan, item)
            result["pr"] = pr_outcome
            if pr_outcome.get("status") == "opened":
                item["pr_url"] = pr_outcome.get("pr_url")
                item["pr_branch"] = pr_outcome.get("branch")
                # Inline drive PR to merge: poll CI + reviews, auto-
                # respond to anything blocking, merge when clean.
                merge_outcome = _drive_pr_to_merge(
                    manifest=manifest,
                    repo_path=repo_path,
                    pr_url=pr_outcome["pr_url"],
                    branch=pr_outcome["branch"],
                )
                result["inline_merge"] = merge_outcome
                if merge_outcome.get("status") == "merged":
                    item["status"] = "merged"
        else:
            item["status"] = "verification_failed"
            item["last_failure"] = verify_outcome.get("last_finding_summary", "")
        item["verified_at"] = utc_now()
        _park_unpublished_work(repo_path, item, result)
    except GenerationNotConfigured as exc:
        item["status"] = "prompt_ready"
        item["updated_at"] = utc_now()
        result["status"] = "prompt_ready"
        result["prompt_path"] = str(exc.prompt_path)
        result["output_dir"] = str(exc.output_dir)
    except ValueError as exc:
        # Cross-repo audit items (pairing, nav, learning, profile)
        # don't appear in any per-repo work-plan; the structural
        # generator can't find them and raises ValueError. Defer with
        # a long retry_after so the tick keeps moving instead of
        # crashing repeatedly. A dedicated handler can pick them up
        # later.
        if "No matching work item" not in str(exc):
            raise
        item["status"] = "deferred"
        item["updated_at"] = utc_now()
        item["defer_reason"] = "unhandled_work_type"
        item["last_failure"] = (
            f"Work-item type `{item.get('type', '?')}` has no per-repo "
            "plan entry; no handler wired yet."
        )
        item["retry_after"] = _retry_after_in_minutes(60 * 24)
        result["status"] = "deferred"
        result["defer_reason"] = "unhandled_work_type"
        result["retry_after"] = item["retry_after"]
    except AgentLimitReached as exc:
        item["status"] = "deferred"
        item["updated_at"] = utc_now()
        item["defer_reason"] = "agent_subscription_limit"
        item["limit_scope"] = exc.result.limit_scope
        item["retry_after"] = exc.result.retry_after
        result["status"] = "deferred"
        result["defer_reason"] = "agent_subscription_limit"
        result["limit_scope"] = exc.result.limit_scope
        result["retry_after"] = exc.result.retry_after
    except RuntimeError as exc:
        # generate_from_plan raises RuntimeError when the agent
        # returned a non-zero exit code and no known limit pattern
        # matched. Treat that as a transient opaque failure and
        # schedule a retry; surface ``failed_permanently`` after
        # ``max_retries`` attempts.
        retry_config = _opaque_retry_config(manifest)
        retry_count = int(item.get("retry_count", 0)) + 1
        if retry_count >= retry_config["max_retries"]:
            item["status"] = "failed_permanently"
            item["updated_at"] = utc_now()
            item["retry_count"] = retry_count
            item["last_failure"] = str(exc)
            result["status"] = "failed_permanently"
            result["retry_count"] = retry_count
            result["error"] = str(exc)
        else:
            item["status"] = "deferred"
            item["updated_at"] = utc_now()
            item["retry_count"] = retry_count
            item["defer_reason"] = "opaque_generator_failure"
            item["retry_after"] = _retry_after_in_minutes(
                retry_config["retry_delay_minutes"]
            )
            item["last_failure"] = str(exc)
            result["status"] = "deferred"
            result["defer_reason"] = "opaque_generator_failure"
            result["retry_count"] = retry_count
            result["retry_after"] = item["retry_after"]
            result["error"] = str(exc)
    write_json(queue_path, queue)
    # Each item also persists its own per-item state file for debugging.
    write_json(state_dir / "daily-item-last.json", result)
    return result


def _verify_with_self_heal(
    manifest: OrgManifest,
    workspace: Path,
    repo_path: Path,
    item: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Verify the work item; on failure, re-invoke the agent with the
    findings as context and retry. Caps at VERIFY_SELF_HEAL_MAX_ATTEMPTS.
    """
    from .judge import JudgeConfig
    from .verify import verify_repo

    judge_config = JudgeConfig.from_manifest(manifest)
    attempts: list[dict[str, Any]] = []
    last_report: dict[str, Any] = {}
    last_findings: list[dict[str, Any]] = []

    for attempt_num in range(1, VERIFY_SELF_HEAL_MAX_ATTEMPTS + 1):
        last_report = verify_repo(
            workspace,
            item["repo"],
            work_id=item["work_id"],
            judge_config=judge_config if judge_config.enabled else None,
        )
        attempts.append(
            {
                "attempt": attempt_num,
                "status": last_report["status"],
                "work_item_count": last_report["work_item_count"],
            }
        )
        if last_report["status"] in {"verified", "no_items"}:
            return {
                "status": "verified" if last_report["status"] == "verified" else "no_items",
                "verify": {
                    "status": last_report["status"],
                    "work_item_count": last_report["work_item_count"],
                },
                "attempts": attempts,
            }
        # Collect findings from this attempt.
        last_findings = _collect_verify_findings(last_report)
        if attempt_num >= VERIFY_SELF_HEAL_MAX_ATTEMPTS:
            break
        # Ask the agent to address the findings, then re-verify.
        heal_outcome = _invoke_verify_self_heal_agent(
            manifest=manifest,
            repo_path=repo_path,
            item=item,
            plan=plan,
            findings=last_findings,
            attempt=attempt_num,
        )
        attempts[-1]["self_heal"] = heal_outcome
        if heal_outcome.get("status") not in {"ok", "no_changes"}:
            # Agent failed or hit a limit; stop trying.
            break

    summary = "; ".join(
        f"{f['type']}: {f['message'][:160]}" for f in last_findings[:3]
    ) or "verification_failed (no findings reported)"
    return {
        "status": "verification_failed",
        "verify": {
            "status": last_report.get("status", "verification_failed"),
            "work_item_count": last_report.get("work_item_count", 0),
        },
        "attempts": attempts,
        "last_finding_summary": summary,
    }


def _quality_gate_passed(
    manifest: OrgManifest,
    repo_path: Path,
    item: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    """P1 eval-gate: score a just-authored artifact with the calibrated judge.

    Returns True to proceed to PR/merge, False to quarantine. This gates the
    *existing* autonomous author loop by quality. It is OFF unless the pipeline's
    ``author`` phase is enabled AND the judge is enabled — so default behavior is
    unchanged. Defensive by construction: any missing path or error returns True,
    so a gate bug can never block the production author flow.
    """
    try:
        from .agent_cli import classify_limit_scope
        from .judge import JudgeConfig, judge_artifact_freshness
        from .pipeline_config import PipelineConfig

        pc = PipelineConfig.from_manifest(manifest)
        jc = JudgeConfig.from_manifest(manifest)
        if not pc.author_enabled or not jc.enabled:
            return True  # gate disabled -> preserve current behavior exactly

        rel = item.get("path")
        if not rel:
            return True  # no scoreable artifact path on this item
        artifact = repo_path / rel
        if not artifact.exists():
            return True

        artifact_id = "".join(c if c.isalnum() else "-" for c in rel).strip("-").lower()
        verdict = judge_artifact_freshness(
            repo_path=repo_path, artifact_path=artifact, artifact_id=artifact_id, config=jc
        )
        if verdict is None:
            return True
        # Never quarantine good content because the judge was rate-limited: a
        # limited call returns a bogus 0. Pass on a limit; gate only on a real score.
        limit_text = f"{verdict.raw or ''}\n" + "\n".join(verdict.blockers or [])
        if classify_limit_scope(limit_text) is not None:
            result["quality_gate"] = {"skipped": "rate_limited"}
            return True
        bar = jc.freshness_threshold()
        passed = verdict.score >= bar
        result["quality_gate"] = {"score": verdict.score, "bar": bar, "passed": passed}
        return passed
    except Exception:  # noqa: BLE001 - a gate error must never block the author flow
        return True


def _collect_verify_findings(verify_report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for wi in verify_report.get("work_items", []):
        for finding in wi.get("findings", []):
            findings.append(finding)
        for action in wi.get("actions", []):
            for finding in action.get("findings", []) or []:
                # Tag with target path for clarity.
                f = dict(finding)
                f.setdefault("target", action.get("path"))
                findings.append(f)
    return findings


def _invoke_verify_self_heal_agent(
    manifest: OrgManifest,
    repo_path: Path,
    item: dict[str, Any],
    plan: dict[str, Any],
    findings: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    """Build a self-heal prompt and re-invoke the content agent."""
    if not findings:
        return {"status": "no_changes", "reason": "no findings to address"}

    output_dir = (
        repo_path / ".aicg" / "self-heal" / item["work_id"] / f"attempt-{attempt}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt.md"
    prompt_path.write_text(_build_self_heal_prompt(item, findings), encoding="utf-8")

    command = content_generation_command(manifest).format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(repo_path),
        work_id=f"self-heal:{item['work_id']}:{attempt}",
        runner=str(Path(__file__).resolve().parents[2]),
    )
    from .agent_cli import run_agent_command

    agent_result = run_agent_command(command, cwd=repo_path)
    if agent_result.limit_reached:
        return {
            "status": "subscription_limit",
            "retry_after": agent_result.retry_after,
        }
    # Trust the working tree, not the exit code. If files were edited
    # we let verify re-read them and decide; if not AND exit was non-
    # zero, surface as agent_failed.
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    has_changes = bool(status.stdout.strip())
    if not has_changes and agent_result.returncode != 0:
        return {
            "status": "agent_failed",
            "returncode": agent_result.returncode,
            "stderr_tail": agent_result.stderr[-400:],
        }
    return {
        "status": "ok",
        "agent_returncode": agent_result.returncode,
        "had_changes": has_changes,
    }


def _build_self_heal_prompt(
    item: dict[str, Any], findings: list[dict[str, Any]]
) -> str:
    lines = [
        f"# Self-heal: address verify findings for {item['work_id']}",
        "",
        "## Goal",
        "",
        "The previous attempt at this work item produced content that",
        "failed contract verification. Fix the specific findings listed",
        "below by editing only the affected files. Do NOT regenerate",
        "from scratch and do NOT broaden the scope.",
        "",
        "## Findings",
        "",
    ]
    for i, f in enumerate(findings, 1):
        target = f.get("target") or "?"
        lines.append(f"### {i}. `{f.get('type')}` ({f.get('severity', 'error')})")
        lines.append("")
        lines.append(f"- Target: `{target}`")
        msg = (f.get("message") or "").strip()
        if msg:
            lines.append(f"- Message: {msg}")
        for key, value in f.items():
            if key in {"type", "severity", "message", "target"}:
                continue
            if value in (None, "", [], {}):
                continue
            lines.append(f"- {key}: {value}")
        lines.append("")
    lines.extend(
        [
            "## Output contract",
            "",
            "- Edit ONLY the files listed in the findings.",
            "- Preserve the existing content; add or rename headings",
            "  rather than rewriting whole sections.",
            "- Do NOT touch CURRICULUM.md, VERSIONS.md, or anything",
            "  outside the affected files.",
        ]
    )
    return "\n".join(lines) + "\n"


def _drive_pr_to_merge(
    manifest: OrgManifest,
    repo_path: Path,
    pr_url: str,
    branch: str,
) -> dict[str, Any]:
    """Inline-poll a freshly-opened PR through to merge, with CI self-heal.

    Per CI cycle:
      1. wait_for_ci (CI_WAIT_PER_ATTEMPT_SECONDS budget per attempt)
      2. CI failure → fetch annotations → ask agent to address → push
         to the same branch → loop, up to CI_SELF_HEAL_MAX_ATTEMPTS
      3. CI passes (or no_ci_present) → guardrails → reviews → merge
    """
    from .steward import (
        _owner_repo_from_url,
        classify_review_state,
        enable_auto_merge,
        evaluate_pr_guardrails,
        fetch_failed_checks,
        fetch_review_state,
        pr_view,
        wait_for_ci,
    )

    pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return {"status": "no_pr_number", "pr_url": pr_url}

    owner, repo_name = _owner_repo_from_url(pr_url)
    history: list[dict[str, Any]] = []
    deadline = time.monotonic() + INLINE_MERGE_TIMEOUT_SECONDS
    review_attempts = 0

    # Auto-rebase: if main has moved since the PR was opened, merge it
    # in first so we don't waste a CI cycle on a guaranteed-conflict
    # merge. The conflict handler keeps main's version for any file
    # and takes the union for VERSIONS.md (append-only by contract).
    rebase_outcome = _auto_rebase_branch_on_main(
        repo_path=repo_path, branch=branch, pr_number=pr_number
    )
    history.append({"phase": "auto_rebase", "result": rebase_outcome.get("status")})
    if rebase_outcome.get("status") == "conflicts_unresolved":
        return {
            "status": "rebase_conflicts",
            "pr_url": pr_url,
            "conflicts": rebase_outcome.get("conflicts"),
            "history": history,
        }

    ci_attempts = 0
    while time.monotonic() < deadline:
        ci_attempts += 1
        ci_result = wait_for_ci(
            repo_path=repo_path,
            pr_number=pr_number,
            timeout_seconds=min(
                CI_WAIT_PER_ATTEMPT_SECONDS, int(deadline - time.monotonic())
            ),
            poll_seconds=INLINE_MERGE_POLL_SECONDS,
        )
        history.append({"phase": "ci", "attempt": ci_attempts, "result": ci_result["status"]})

        if ci_result["status"] == "failure":
            if ci_attempts >= CI_SELF_HEAL_MAX_ATTEMPTS:
                return {
                    "status": "ci_failed_escalated",
                    "pr_url": pr_url,
                    "ci_attempts": ci_attempts,
                    "history": history,
                }
            if not owner or not repo_name:
                return {
                    "status": "ci_failed",
                    "pr_url": pr_url,
                    "reason": "could not parse owner/repo from PR URL",
                    "history": history,
                }
            failed_checks = fetch_failed_checks(
                repo_path, owner, repo_name, pr_number
            )
            heal_outcome = _invoke_ci_self_heal_agent(
                manifest=manifest,
                repo_path=repo_path,
                pr_number=pr_number,
                branch=branch,
                failed_checks=failed_checks,
                attempt=ci_attempts,
            )
            history.append(
                {
                    "phase": "ci_self_heal",
                    "attempt": ci_attempts,
                    "result": heal_outcome.get("status"),
                    "failed_checks": [c["name"] for c in failed_checks],
                }
            )
            if heal_outcome.get("status") not in {"pushed", "no_changes"}:
                return {
                    "status": "ci_failed",
                    "pr_url": pr_url,
                    "history": history,
                    "self_heal": heal_outcome,
                }
            # Pushed a fix — loop to re-wait for CI.
            continue
        if ci_result["status"] == "timeout":
            return {
                "status": "ci_timeout",
                "pr_url": pr_url,
                "history": history,
            }
        # success or no_ci_present → continue to guardrails / reviews
        pr = pr_view(repo_path, pr_number) or {}
        guardrails = evaluate_pr_guardrails(repo_path=repo_path, pr=pr)
        if not guardrails.allowed:
            history.append({"phase": "guardrails", "blockers": list(guardrails.blockers)})
            return {
                "status": "guardrails_blocked",
                "pr_url": pr_url,
                "blockers": list(guardrails.blockers),
                "history": history,
            }

        if owner and repo_name:
            review_state = fetch_review_state(repo_path, owner, repo_name, pr_number)
            if not review_state.get("error"):
                cls = classify_review_state(
                    review_state, pr_author=(pr.get("author") or {}).get("login")
                )
                if cls["verdict"] == "blocked":
                    history.append(
                        {
                            "phase": "review",
                            "verdict": "blocked",
                            "attempt": review_attempts + 1,
                        }
                    )
                    if review_attempts >= REVIEW_SELF_HEAL_MAX_ATTEMPTS:
                        return {
                            "status": "review_blocked_escalated",
                            "pr_url": pr_url,
                            "blockers": cls["blockers"],
                            "history": history,
                        }
                    review_attempts += 1
                    heal_outcome = _invoke_review_self_heal_agent(
                        manifest=manifest,
                        repo_path=repo_path,
                        pr_number=pr_number,
                        branch=branch,
                        blockers=cls["blockers"],
                        attempt=review_attempts,
                    )
                    history.append(
                        {
                            "phase": "review_self_heal",
                            "attempt": review_attempts,
                            "result": heal_outcome.get("status"),
                            "blocker_count": len(cls["blockers"]),
                        }
                    )
                    if heal_outcome.get("status") not in {"pushed", "no_changes"}:
                        return {
                            "status": "review_blocked",
                            "pr_url": pr_url,
                            "blockers": cls["blockers"],
                            "self_heal": heal_outcome,
                            "history": history,
                        }
                    # Pushed a fix — loop back to wait for new CI then
                    # re-check reviews. Bot threads typically auto-resolve
                    # once the underlying issue is addressed.
                    continue

        merge_result = enable_auto_merge(repo_path, pr_number, method="squash")
        history.append({"phase": "merge", "result": merge_result.get("status")})
        if merge_result.get("status") == "merged":
            return {"status": "merged", "pr_url": pr_url, "history": history}
        if merge_result.get("status") == "auto_merge_enabled":
            return {
                "status": "auto_merge_enabled",
                "pr_url": pr_url,
                "history": history,
            }
        # Merge call returned something else — wait and retry.
        time.sleep(INLINE_MERGE_POLL_SECONDS)

    return {"status": "merge_timeout", "pr_url": pr_url, "history": history}


def _auto_rebase_branch_on_main(
    repo_path: Path, branch: str, pr_number: int
) -> dict[str, Any]:
    """Merge origin/main into the PR branch (auto-resolving known cases).

    Why merge, not rebase: a merge keeps each PR's history while still
    incorporating main. Rebase would force-push, which is brittle when
    the steward / GitHub auto-merge are watching for the same SHA.

    Auto-resolutions:
      - VERSIONS.md: take the union of rows (append-only contract)
      - Any other conflicting file: keep main's version (already merged
        + validated)

    Returns status:
      - `up_to_date`: no merge needed
      - `merged_clean`: merge completed without conflicts
      - `merged_with_auto_resolution`: conflicts auto-resolved + pushed
      - `conflicts_unresolved`: hit a case the auto-resolver doesn't know
      - `error`: git command failed
    """
    try:
        # Make sure we have the latest main.
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin", "main"],
            capture_output=True, text=True, check=False,
        )
        # Ensure we're on the PR branch.
        subprocess.run(
            ["git", "-C", str(repo_path), "checkout", branch],
            capture_output=True, text=True, check=False,
        )
        # Bail if branch is already up to date with main.
        rev_check = subprocess.run(
            ["git", "-C", str(repo_path), "merge-base", "--is-ancestor",
             "origin/main", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        if rev_check.returncode == 0:
            _checkout_main(repo_path)
            return {"status": "up_to_date"}

        merge = subprocess.run(
            ["git", "-C", str(repo_path), "merge", "origin/main", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        if merge.returncode == 0:
            push = subprocess.run(
                ["git", "-C", str(repo_path), "push"],
                capture_output=True, text=True, check=False,
            )
            _checkout_main(repo_path)
            return {
                "status": "merged_clean",
                "push_rc": push.returncode,
            }

        # Conflict — resolve known patterns.
        conflicts = _list_unmerged_files(repo_path)
        unresolved = _auto_resolve_known_conflicts(repo_path, conflicts)
        if unresolved:
            # Abort the merge so the branch is left clean for the operator.
            subprocess.run(
                ["git", "-C", str(repo_path), "merge", "--abort"],
                capture_output=True, text=True, check=False,
            )
            _checkout_main(repo_path)
            return {
                "status": "conflicts_unresolved",
                "conflicts": unresolved,
            }

        commit = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        if commit.returncode != 0:
            subprocess.run(
                ["git", "-C", str(repo_path), "merge", "--abort"],
                capture_output=True, text=True, check=False,
            )
            _checkout_main(repo_path)
            return {
                "status": "error",
                "step": "commit",
                "stderr_tail": commit.stderr[-400:],
            }
        push = subprocess.run(
            ["git", "-C", str(repo_path), "push"],
            capture_output=True, text=True, check=False,
        )
        _checkout_main(repo_path)
        return {
            "status": "merged_with_auto_resolution",
            "resolved_files": [c for c in conflicts],
            "push_rc": push.returncode,
        }
    except Exception as exc:  # noqa: BLE001
        _checkout_main(repo_path)
        return {"status": "error", "exception": str(exc)}


def _list_unmerged_files(repo_path: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True, check=False,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _auto_resolve_known_conflicts(
    repo_path: Path, conflicts: list[str]
) -> list[str]:
    """Auto-resolve the conflict patterns the runner knows about.

    Returns the list of files that could NOT be resolved automatically.
    """
    unresolved: list[str] = []
    for rel in conflicts:
        path = repo_path / rel
        if rel == "VERSIONS.md":
            if _resolve_versions_md_conflict(path):
                subprocess.run(
                    ["git", "-C", str(repo_path), "add", rel],
                    capture_output=True, text=True, check=False,
                )
                continue
        # Default: keep main's version (PR #15-style fixes have already
        # been validated against CI).
        rc = subprocess.run(
            ["git", "-C", str(repo_path), "checkout", "--theirs", "--", rel],
            capture_output=True, text=True, check=False,
        )
        if rc.returncode == 0:
            subprocess.run(
                ["git", "-C", str(repo_path), "add", rel],
                capture_output=True, text=True, check=False,
            )
        else:
            unresolved.append(rel)
    return unresolved


def _resolve_versions_md_conflict(path: Path) -> bool:
    """Take the union of all rows across both sides of a VERSIONS.md merge.

    VERSIONS.md is append-only by contract — each PR adds a row for its
    work item under the current month heading. Conflicts arise when two
    PRs added different rows in the same month block. The resolution is
    deterministic: keep all rows, dedup by ``work_id``, preserve order
    of first appearance.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    # Split conflict markers. Pattern:
    #   <<<<<<< HEAD
    #   ...ours...
    #   =======
    #   ...theirs...
    #   >>>>>>> origin/main
    if "<<<<<<<" not in text:
        return False

    resolved_lines: list[str] = []
    seen_work_ids: set[str] = set()
    i = 0
    lines = text.splitlines()
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            ours: list[str] = []
            theirs: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("======="):
                ours.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # skip =======
            while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                theirs.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # skip >>>>>>>
            # Union — emit ours first then theirs, dedup by `work_id`
            # (extract from the row's second column wrapped in backticks).
            for source in (ours, theirs):
                for line in source:
                    work_id = _extract_work_id_from_versions_row(line)
                    if work_id and work_id in seen_work_ids:
                        continue
                    if work_id:
                        seen_work_ids.add(work_id)
                    resolved_lines.append(line)
        else:
            resolved_lines.append(lines[i])
            i += 1

    new_text = "\n".join(resolved_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    path.write_text(new_text, encoding="utf-8")
    return True


def _extract_work_id_from_versions_row(line: str) -> str | None:
    """Pull the work-id from a VERSIONS.md table row like:
    | 2026-05-27 | `work-id` | `scope` | Title |
    """
    import re as _re

    match = _re.search(r"\|\s*`([^`]+)`\s*\|", line)
    return match.group(1) if match else None


def _invoke_review_self_heal_agent(
    manifest: OrgManifest,
    repo_path: Path,
    pr_number: int,
    branch: str,
    blockers: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    """Invoke the agent to address review comments / changes-requested.

    Same shape as the CI self-heal handler: checkout PR branch, build a
    prompt enumerating each blocker, run the agent, trust the working
    tree, commit + push.
    """
    subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "origin", branch],
        capture_output=True, text=True, check=False,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", branch],
        capture_output=True, text=True, check=False,
    )

    output_dir = (
        repo_path / ".aicg" / "review-self-heal" / f"pr-{pr_number}" / f"attempt-{attempt}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt.md"
    prompt_path.write_text(
        _build_review_self_heal_prompt(pr_number, blockers), encoding="utf-8"
    )

    command = content_generation_command(manifest).format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(repo_path),
        work_id=f"review-heal:pr-{pr_number}:{attempt}",
        runner=str(Path(__file__).resolve().parents[2]),
    )
    from .agent_cli import run_agent_command

    agent_result = run_agent_command(command, cwd=repo_path)
    if agent_result.limit_reached:
        _checkout_main(repo_path)
        return {
            "status": "subscription_limit",
            "retry_after": agent_result.retry_after,
        }

    # Trust the working tree (same pattern as CI self-heal).
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    has_changes = bool(status.stdout.strip())
    if not has_changes:
        _checkout_main(repo_path)
        if agent_result.returncode != 0:
            return {
                "status": "agent_failed",
                "returncode": agent_result.returncode,
                "stderr_tail": agent_result.stderr[-400:],
            }
        return {"status": "no_changes"}

    for args in (
        ["git", "-C", str(repo_path), "add", "--all"],
        ["git", "-C", str(repo_path), "commit", "-m",
         f"aicg: address PR #{pr_number} review (attempt {attempt})"],
        ["git", "-C", str(repo_path), "push"],
    ):
        c = subprocess.run(args, capture_output=True, text=True, check=False)
        if c.returncode != 0:
            _checkout_main(repo_path)
            return {
                "status": "push_failed",
                "step": args[3],
                "stderr_tail": c.stderr[-400:],
            }
    _checkout_main(repo_path)
    return {"status": "pushed", "agent_returncode": agent_result.returncode}


def _build_review_self_heal_prompt(
    pr_number: int, blockers: list[dict[str, Any]]
) -> str:
    lines = [
        f"# Address review feedback on PR #{pr_number}",
        "",
        "## Goal",
        "",
        "Reviewers (human or bot) left feedback that blocks auto-merge.",
        "Address each blocker below with the smallest possible code",
        "change. Do NOT rewrite scope and do NOT touch unrelated files.",
        "",
        "## Blockers",
        "",
    ]
    for i, b in enumerate(blockers, 1):
        if b.get("kind") == "changes_requested":
            lines.append(f"### {i}. CHANGES_REQUESTED from @{b.get('author')}")
            lines.append("")
            lines.append(f"> {(b.get('body') or '').strip() or '(no body)'}")
            lines.append("")
        elif b.get("kind") == "unresolved_thread":
            actor = "bot" if b.get("is_bot") else "human"
            lines.append(
                f"### {i}. Unresolved review thread "
                f"({actor}: @{b.get('author')}) "
                f"in `{b.get('path')}:{b.get('line')}`"
            )
            lines.append("")
            lines.append(f"> {(b.get('body') or '').strip() or '(no body)'}")
            lines.append("")
    lines.extend(
        [
            "## Output contract",
            "",
            "- Edit only the files referenced by these blockers.",
            "- Preserve the existing structure; don't delete sections.",
            "- Do NOT touch CURRICULUM.md, README.md, or VERSIONS.md.",
            "- Do NOT mark review threads resolved yourself — only the",
            "  reviewer can do that. Your job is to push commits that",
            "  address the underlying issue. Bot threads auto-resolve",
            "  when their metric recovers; human threads stay open until",
            "  the human resolves them.",
        ]
    )
    return "\n".join(lines) + "\n"


def _invoke_ci_self_heal_agent(
    manifest: OrgManifest,
    repo_path: Path,
    pr_number: int,
    branch: str,
    failed_checks: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    """Invoke the agent on the failed CI checks, then commit + push."""
    # Make sure the local working tree is on the PR branch — the runner
    # restored main after PR creation.
    subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "origin", branch],
        capture_output=True, text=True, check=False,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", branch],
        capture_output=True, text=True, check=False,
    )

    output_dir = (
        repo_path / ".aicg" / "ci-self-heal" / f"pr-{pr_number}" / f"attempt-{attempt}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt.md"
    prompt_path.write_text(
        _build_ci_self_heal_prompt(pr_number, failed_checks), encoding="utf-8"
    )

    command = content_generation_command(manifest).format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(repo_path),
        work_id=f"ci-heal:pr-{pr_number}:{attempt}",
        runner=str(Path(__file__).resolve().parents[2]),
    )
    from .agent_cli import run_agent_command

    agent_result = run_agent_command(command, cwd=repo_path)
    if agent_result.limit_reached:
        _checkout_main(repo_path)
        return {
            "status": "subscription_limit",
            "retry_after": agent_result.retry_after,
        }

    # Trust the working tree, not the exit code. Claude often returns
    # non-zero when it tried a denied tool (git add) but the file
    # edits via Edit/Write still landed. If anything is dirty, commit
    # and push; if nothing is dirty AND exit was non-zero, treat as
    # a real failure.
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    has_changes = bool(status.stdout.strip())

    if not has_changes:
        _checkout_main(repo_path)
        if agent_result.returncode != 0:
            return {
                "status": "agent_failed",
                "returncode": agent_result.returncode,
                "stderr_tail": agent_result.stderr[-400:],
            }
        return {"status": "no_changes"}

    for args in (
        ["git", "-C", str(repo_path), "add", "--all"],
        ["git", "-C", str(repo_path), "commit", "-m",
         f"aicg: address CI failures on PR #{pr_number} (attempt {attempt})"],
        ["git", "-C", str(repo_path), "push"],
    ):
        c = subprocess.run(args, capture_output=True, text=True, check=False)
        if c.returncode != 0:
            _checkout_main(repo_path)
            return {
                "status": "push_failed",
                "step": args[3],
                "stderr_tail": c.stderr[-400:],
            }
    _checkout_main(repo_path)
    # Note agent exit so observers can see whether the changes came
    # from a clean run or a partial-success one.
    return {
        "status": "pushed",
        "agent_returncode": agent_result.returncode,
    }


def _build_ci_self_heal_prompt(
    pr_number: int, failed_checks: list[dict[str, Any]]
) -> str:
    lines = [
        f"# Address CI failures on PR #{pr_number}",
        "",
        "## Goal",
        "",
        "The PR you just opened failed CI. Fix the failures listed",
        "below by editing files on the current branch. Do NOT regenerate",
        "the content from scratch — make the minimal edit needed to",
        "satisfy each failing check.",
        "",
        "## Failed checks",
        "",
    ]
    for i, check in enumerate(failed_checks, 1):
        lines.append(f"### {i}. `{check.get('name')}` ({check.get('conclusion')})")
        lines.append("")
        if check.get("details_url"):
            lines.append(f"- Details: <{check['details_url']}>")
        annotations = check.get("annotations") or []
        if annotations:
            lines.append("- Annotations:")
            for ann in annotations[:20]:
                path = ann.get("path") or "?"
                line = ann.get("start_line") or "?"
                msg = (ann.get("message") or "").splitlines()[0][:300]
                level = ann.get("level") or "?"
                lines.append(f"  - `{path}:{line}` ({level}): {msg}")
        else:
            lines.append(
                "- No annotations exposed by this check. Infer the failure"
                " from the check name; if you cannot tell what's wrong from"
                " the name alone, leave a brief comment in the PR and exit"
                " without editing rather than guessing."
            )
        lines.append("")
    lines.extend(
        [
            "## Output contract",
            "",
            "- Edit ONLY files inside this repo on the current branch.",
            "- Preserve the existing structure; do not delete sections.",
            "- Do NOT touch CURRICULUM.md, README.md, or VERSIONS.md.",
            "- One atomic commit covering all fixes is fine.",
        ]
    )
    return "\n".join(lines) + "\n"


def _pr_number_from_url(url: str) -> int | None:
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


def _opaque_retry_config(manifest: OrgManifest) -> dict[str, Any]:
    automation = manifest.automation or {}
    retry = automation.get("opaque_retry", {}) if isinstance(automation, dict) else {}
    max_retries = int(retry.get("max_retries", 3))
    delay_minutes = int(retry.get("retry_delay_minutes", 15))
    return {"max_retries": max_retries, "retry_delay_minutes": delay_minutes}


def _retry_after_in_minutes(minutes: int) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def refresh_deferred_items(queue: dict[str, Any]) -> None:
    for item in queue.get("work_items", []):
        if item.get("status") != "deferred":
            continue
        if retry_after_has_passed(item.get("retry_after")):
            item["status"] = "ready"
            item["updated_at"] = utc_now()
            item.pop("defer_reason", None)


def steward_report(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    apply: bool = False,
    ci_timeout_seconds: int = 600,
    ci_poll_seconds: int = 30,
    merge_method: str = "squash",
) -> dict[str, Any]:
    """Run the PR steward (auto-merger or dry-run)."""
    from .steward import steward_run

    return steward_run(
        manifest,
        workspace,
        state_dir=state_dir,
        apply=apply,
        ci_timeout_seconds=ci_timeout_seconds,
        ci_poll_seconds=ci_poll_seconds,
        merge_method=merge_method,
    )


def generate_supplemental_packet(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path,
) -> dict[str, Any]:
    month = date.today().strftime("%Y-%m")
    prompt_dir = state_dir / "supplemental" / month
    prompt_dir.mkdir(parents=True, exist_ok=True)
    packets = []
    for role in manifest.roles:
        prompt_path = prompt_dir / f"{role.id}.md"
        supplemental_dir = manifest.job_requirements.get("supplemental_dir", "supplemental")
        prompt_path.write_text(
            build_supplemental_prompt(manifest, role, month, supplemental_dir),
            encoding="utf-8",
        )
        packets.append(
            {
                "role": role.id,
                "learning_repo": role.learning_repo,
                "prompt_path": str(prompt_path),
                "target_dir": str(workspace / role.learning_repo / supplemental_dir),
            }
        )
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "supplemental",
        "status": "prompt_ready",
        "packets": packets,
    }
    write_json(state_dir / "supplemental-plan.json", report)
    return report


def _existing_curriculum_section(role: RoleConfig) -> str:
    """Compact summary of the role's existing modules / projects.

    Pulled from the structural curriculum manifest so the research agent
    starts grounded in coverage that already exists. Falls back to a
    short note when the manifest hasn't been built yet — the prompt is
    still useful without it, just less specific.

    For canary roles (whose agent output is consumed as a v2 delta
    against the per-role manifest), emit the full hierarchy. For
    non-canary roles (whose agent output is the legacy add-modules
    format), emit a brief slug-only list — enough signal to avoid
    duplicates without burning tokens on exercise lists the agent
    won't reference.
    """
    from .research import CANARY_ROLES_NEW_FORMAT

    is_canary = role.id in CANARY_ROLES_NEW_FORMAT

    if not _CURRICULUM_MANIFEST_PATH.exists():
        return (
            "## Existing curriculum to build on (do not duplicate)\n\n"
            "_(curriculum.manifest.json not built yet — assume the role already has the modules and projects listed in its `curriculum-plan.json`)_"
        )
    try:
        cur = load_curriculum_manifest(_CURRICULUM_MANIFEST_PATH)
    except ManifestError:
        return (
            "## Existing curriculum to build on (do not duplicate)\n\n"
            "_(curriculum.manifest.json failed to load — fall back to `curriculum-plan.json`)_"
        )
    summary = summarize_manifest_for_prompt(
        cur, only_track_slug=role.id, brief=not is_canary
    )
    intro = (
        "Treat every module / exercise / project below as **already covered**. "
        "Propose deltas only when the job market has materially shifted "
        "(see Continuity bias)."
        if is_canary
        else "These already exist — do NOT propose duplicates."
    )
    return (
        "## Existing curriculum to build on (do not duplicate)\n\n"
        f"{intro}\n\n"
        f"{summary}"
    )


def _continuity_bias_section(manifest: OrgManifest) -> str:
    """Codify the 'continuity over novelty' policy in the prompt itself.

    The author of the curriculum explicitly does NOT want massive drift
    cycle-to-cycle. Encode that here so every research agent sees the
    same instructions, and the validators in research.py can hold the
    line when the prompt drifts.
    """
    return (
        "## Continuity bias (READ THIS)\n\n"
        "The curriculum is mature and learners rely on it staying stable. "
        "**Default to no change.** Propose net-new content only when ALL of:\n\n"
        "- ≥ 3 distinct job postings from the last 90 days cite a requirement that the existing curriculum does NOT cover, AND\n"
        "- The requirement frequency is ≥ 0.30 (i.e., shows up in ≥ 30% of sampled postings), AND\n"
        "- No existing module / exercise / project can be incrementally extended to cover it.\n\n"
        "When in doubt, prefer **adding an exercise to an existing module** over **creating a new module**. "
        "Prefer **citing existing content** over **proposing new content**. "
        "Removals are out of scope for this packet — open a separate proposal if you believe a requirement is no longer relevant.\n\n"
        "Deltas proposing **> 20% additions** or **> 10% removals** in one cycle will be auto-flagged "
        "`requires_explicit_approval: true` and routed to human review. Pad them with weak postings to clear the threshold and you waste a cycle.\n"
    )


def _v2_output_contract_section(role: RoleConfig, month: str) -> str:
    """Output contract for canary roles using the new curriculum-plan v2 manifest.

    Routed via :data:`CANARY_ROLES_NEW_FORMAT` in ``research.py``. The
    agent emits a single delta file at
    ``.aicg/curriculum-plan-delta-v2.json``; ``research_apply`` validates
    it against ``manifest/curriculum_plan.<role>.manifest.json`` in the
    content-generator repo (NOT the legacy ``curriculum-plan.json``).
    """
    return (
        "## Output Contract (curriculum-plan v2)\n\n"
        "Write exactly these files inside the learning repo (paths relative to the repo root):\n\n"
        "1. `JOB_REQUIREMENTS.md` — grouped requirements, posting evidence, curriculum links, external resources for out-of-scope items.\n"
        "2. `.aicg/job-requirements.json` — machine-readable requirements, evidence, owner role, coverage path, status.\n"
        "3. `.aicg/curriculum-plan-delta-v2.json` — *proposed* delta against the existing per-role manifest. Operates on the requirement IDs already listed in the **Existing curriculum** section above. Use this schema:\n\n"
        "```jsonc\n"
        "{\n"
        '  "schema_version": 1,\n'
        f'  "role": "{role.id}",\n'
        f'  "month": "{month}",\n'
        '  "rationale": "One paragraph: what specific market shift in the last 90 days drives every item below.",\n'
        '  "research_window": {\n'
        '    "window_start": "YYYY-MM-DD",\n'
        '    "window_end":   "YYYY-MM-DD",\n'
        '    "postings_sampled": 47,\n'
        '    "last_refreshed": "YYYY-MM-DD",\n'
        '    "sources": [{"name": "linkedin", "count": 22}]\n'
        '  },\n'
        '  "additions": [\n'
        '    {\n'
        '      "rationale": "Why this requirement, why now",\n'
        '      "requirement": {\n'
        '        "id": "REQ-<ROLE-CODE>-<SLUG>",\n'
        '        "label": "Short human label",\n'
        '        "frequency": 0.34,\n'
        '        "provenance": "research",\n'
        '        "requires_confirmation": false,\n'
        '        "evidence": [\n'
        '          {"posting_id": "p1", "phrase": "...", "employer": "...", "url": "...", "date_observed": "YYYY-MM-DD"},\n'
        '          {"posting_id": "p2", "phrase": "..."},\n'
        '          {"posting_id": "p3", "phrase": "..."}\n'
        '        ],\n'
        '        "exercises": [], "projects": [], "solutions": [], "tests": [],\n'
        '        "coverage_status": "missing"\n'
        '      }\n'
        '    }\n'
        '  ],\n'
        '  "updates": [\n'
        '    {\n'
        '      "id": "REQ-<ROLE-CODE>-EXISTING",\n'
        '      "frequency": 0.78,\n'
        '      "evidence_add": [...],\n'
        '      "exercises_add": ["mod-006/exercise-02-helm-chart"],\n'
        '      "notes": "Bumped frequency from baseline due to higher prevalence in this window."\n'
        '    }\n'
        '  ],\n'
        '  "removals": [\n'
        '    {"id": "REQ-<ROLE-CODE>-OBSOLETE", "migration_note": "Why this is no longer relevant — required for high-frequency removals."}\n'
        '  ]\n'
        "}\n"
        "```\n\n"
        "Validator rules (enforced mechanically by `aicg org plan-delta-apply`):\n\n"
        f"- Each addition must carry **≥ 3 evidence items**\n"
        f"- Each addition must have **frequency ≥ 0.30**\n"
        "- Each addition must have **provenance: \"research\"**\n"
        "- Updates / removals must reference IDs that exist in the **Existing curriculum** section\n"
        "- Removals of high-frequency (> 0.50) requirements require a `migration_note`\n"
        "- Deltas with > 20% additions or > 10% removals auto-flag `requires_explicit_approval: true`\n\n"
        "**The expected steady-state output is `additions: []`, `updates: []`, `removals: []`** "
        "with a rationale saying \"market unchanged this cycle.\" "
        "Do NOT pad with weak postings to clear thresholds — empty deltas are correct.\n\n"
        "Do NOT edit `CURRICULUM.md`, `CURRICULUM_INDEX.md`, `README.md`, or `VERSIONS.md` directly — those regenerate from the merged manifest.\n"
    )


def _legacy_output_contract_section() -> str:
    """The legacy output-contract block for non-canary roles."""
    return (
        "## Output Contract\n\n"
        "Write exactly these files inside the learning repo (paths are relative to the repo root):\n\n"
        "1. `JOB_REQUIREMENTS.md` — grouped requirements, posting evidence, curriculum links, and external resources for out-of-scope items.\n"
        "2. `.aicg/job-requirements.json` — machine-readable requirements, evidence, owner role, coverage path, and status.\n"
        "3. `.aicg/curriculum-plan-delta.json` — *proposed* additions, strictly additive (never delete existing items). The runner will apply per-run caps and reject items lacking evidence; nothing is auto-merged. Use this schema:\n\n"
        "```json\n"
        "{\n"
        '  "schema_version": 1,\n'
        '  "role_id": "<role>",\n'
        '  "month": "<YYYY-MM>",\n'
        '  "rationale": "One paragraph: what specific change in the job market over the last 90 days drives every item below. Vague rationales get the proposal rejected by humans on review.",\n'
        '  "modules": [\n'
        '    {\n'
        '      "id": "mod-XXX-<slug>",\n'
        '      "title": "...",\n'
        '      "description": "...",\n'
        '      "exercises": [],\n'
        '      "rationale": "Why THIS module, why now, and which exact requirement(s) from JOB_REQUIREMENTS.md it covers.",\n'
        '      "evidence": [\n'
        '        {"employer": "...", "title": "...", "url": "...", "date_observed": "YYYY-MM-DD", "quote": "..."}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "exercises": [\n'
        '    {\n'
        '      "module_id": "mod-XXX-<existing-module>",\n'
        '      "exercise": {"slug": "exercise-NN-<slug>", "title": "...", "summary": "..."},\n'
        '      "evidence": [{"employer": "...", "title": "...", "url": "...", "date_observed": "YYYY-MM-DD", "quote": "..."}]\n'
        '    }\n'
        '  ],\n'
        '  "projects": [\n'
        '    {"id": "project-NN-<slug>", "title": "...", "description": "...", "rationale": "...", "evidence": [...]}\n'
        '  ]\n'
        "}\n"
        "```\n\n"
        "Every item MUST carry an `evidence` array citing at least 3 distinct job postings from the last 90 days that demonstrate the gap. Items with fewer citations get rejected mechanically; do not pad with low-quality postings. PREFER ZERO ADDITIONS over weakly-justified ones — empty arrays are the expected output when nothing has materially shifted. If a requirement is already covered at a lower level, link to it in `JOB_REQUIREMENTS.md` and do NOT add it.\n\n"
        "Do NOT edit `CURRICULUM.md`, `CURRICULUM_INDEX.md`, `README.md`, or `VERSIONS.md` directly — the runner regenerates those from the merged plan. Mark unresolved claims with `<!-- needs-research: ... -->`.\n"
    )


def build_research_prompt(manifest: OrgManifest, role: RoleConfig, month: str) -> str:
    from .research import CANARY_ROLES_NEW_FORMAT

    hierarchy = "\n".join(
        f"- level {item.level}: {item.title} (`{item.learning_repo}`)" for item in manifest.roles
    )
    if role.id in CANARY_ROLES_NEW_FORMAT:
        output_contract = _v2_output_contract_section(role, month)
    else:
        output_contract = _legacy_output_contract_section()
    if role.aliases:
        alias_line = (
            "- This role's title is emerging/fragmented. ALSO search these equivalent "
            "titles and count their postings toward the evidence threshold: "
            + ", ".join(f"`{a}`" for a in role.aliases)
            + ".\n"
        )
    else:
        alias_line = ""
    return (
        f"# Job Requirements Research Packet - {role.title} - {month}\n\n"
        f"Preferred content agent: {content_generation_label(manifest)}.\n\n"
        f"{_continuity_bias_section(manifest)}\n"
        f"{_existing_curriculum_section(role)}\n\n"
        "## Goal\n\n"
        f"Research current job postings for `{role.title}` and update "
        f"`{role.learning_repo}` requirements without duplicating lower-level coverage.\n\n"
        "## Research Requirements\n\n"
        f"- Analyze at least {manifest.research.get('minimum_postings_per_role', 25)} relevant postings.\n"
        f"{alias_line}"
        f"- Prefer postings from the last {manifest.research.get('source_window_days', 45)} days.\n"
        "- Capture employer, title, URL, date observed, location, and requirements.\n"
        "- Store raw normalized findings in `.aicg/job-requirements.json`.\n"
        "- Update `JOB_REQUIREMENTS.md` with requirement coverage links.\n\n"
        "## Ownership Rule\n\n"
        "If a requirement belongs to multiple roles, assign the primary coverage to the "
        "lowest-level role where it is genuinely required. Higher-level roles should link "
        "to that owner unless they need different depth, architecture context, or leadership context.\n\n"
        "## Role Hierarchy\n\n"
        f"{hierarchy}\n\n"
        f"{output_contract}"
    )


def build_supplemental_prompt(
    manifest: OrgManifest,
    role: RoleConfig,
    month: str,
    supplemental_dir: str,
) -> str:
    return (
        f"# Supplemental Content Packet - {role.title} - {month}\n\n"
        f"Preferred content agent: {content_generation_label(manifest)}.\n\n"
        "No ready gap-remediation work items were available. Review the role's "
        "`JOB_REQUIREMENTS.md` and `.aicg/job-requirements.json`, then create brief "
        f"source-backed supplemental content under `{supplemental_dir}/` for requirements "
        "that are useful but do not belong in the main curriculum path.\n\n"
        "Constraints:\n\n"
        "- Keep each supplemental page short and focused.\n"
        "- Link to main curriculum coverage when it exists.\n"
        "- Prefer official sources and clearly label practitioner references.\n"
        "- Do not duplicate lower-level role content; link to it instead.\n"
    )


def repo_action(
    repo: str,
    repo_path: Path,
    remote: str,
    command: list[str],
    status: str,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "path": str(repo_path),
        "remote": remote,
        "command": shell_join(command),
        "status": status,
    }


def queue_priority(manifest: OrgManifest, repo: str, item: dict[str, Any]) -> int:
    """Compute a deterministic priority for the work queue.

    Lower number = higher priority (sorted ascending). The score is:

        severity_bias + role_level × 1000 + item.priority

    where ``severity_bias`` lets high-severity refresh items jump ahead
    of structural gaps. The default work item gets bias = 0; a
    high-severity refresh (broken security guidance, EOL'd tool) gets
    a large negative bias.
    """
    role = next(
        (role for role in manifest.roles if repo in (role.solution_repo, role.learning_repo)),
        None,
    )
    base = role.level if role else 100
    severity_bias = _severity_bias(item)
    return severity_bias + base * 1000 + int(item.get("priority", 100))


def _handle_cross_repo_item(
    manifest: OrgManifest,
    workspace: Path,
    item: dict[str, Any],
    state_dir: Path,
    queue: dict[str, Any],
    queue_path: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Process a cross-repo audit item end-to-end.

    Routes by ``item['type']`` (and ``item['subtype']`` where relevant):

    - ``curriculum_nav_drift`` → edit the named CURRICULUM doc in the
      target repo.
    - ``learning_gap`` → edit the named file in the learning repo.
    - ``org_profile_stale`` → edit ``profile/README.md`` in ``.github``.
    - ``pairing_mismatch``:
       - ``exercise_missing_in_learning`` → write a learning brief
         in the learning repo.
       - ``exercise_slug_drift`` → align the solution-side slug to the
         learning side (learning is authoritative).
       - ``project_only_in_solutions`` → defer (human review).

    The handler runs the same generate → commit → push → PR → inline
    -merge pipeline as structural items but skips verify (no plan) and
    skips propagate (these don't add new curriculum content so
    VERSIONS.md doesn't need a row).
    """
    work_type = item["type"]
    subtype = item.get("subtype", "")

    target = _resolve_cross_repo_target(manifest, workspace, item)
    if target is None:
        item["status"] = "deferred"
        item["updated_at"] = utc_now()
        item["defer_reason"] = "needs_human_judgment"
        item["last_failure"] = (
            f"Cross-repo item `{work_type}/{subtype}` needs human review; "
            "deferring."
        )
        item["retry_after"] = _retry_after_in_minutes(60 * 24 * 7)  # 1 week
        result["status"] = "deferred"
        result["defer_reason"] = "needs_human_judgment"
        write_json(queue_path, queue)
        return result

    repo_path: Path = target["repo_path"]
    repo_name: str = target["repo"]

    # Branch + prompt.
    today = utc_now()[:10]
    safe_id = _slug_for_branch(item["work_id"])
    from .gitops import ensure_branch, ref_segment, stage_all_except_runner_state

    branch = f"aicg/{today}/{ref_segment(repo_name)}/{safe_id}"
    prompt_dir = repo_path / ".aicg" / "cross-repo" / item["work_id"]
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text(
        _build_cross_repo_prompt(item, target), encoding="utf-8"
    )
    output_dir = prompt_dir / "agent-run"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Checkout a fresh branch from main, and refuse to go on unless we're on it:
    # a failed checkout used to leave us on main, so the commit below landed there.
    for args in (
        ["git", "-C", str(repo_path), "fetch", "origin", "main"],
        ["git", "-C", str(repo_path), "checkout", "main"],
        ["git", "-C", str(repo_path), "pull", "--ff-only"],
    ):
        subprocess.run(args, capture_output=True, text=True, check=False)
    if not ensure_branch(repo_path, branch):
        _checkout_main(repo_path)
        item["status"] = "failed_permanently"
        item["updated_at"] = utc_now()
        item["last_failure"] = f"could not check out work branch {branch!r}; nothing was run"
        result["status"] = "failed_permanently"
        write_json(queue_path, queue)
        return result

    command = content_generation_command(manifest).format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(repo_path),
        work_id=item["work_id"],
        runner=str(Path(__file__).resolve().parents[2]),
    )
    from .agent_cli import run_agent_command

    agent_result = run_agent_command(command, cwd=repo_path)
    if agent_result.limit_reached:
        _set_aside_work(repo_path, item)
        item["status"] = "deferred"
        item["updated_at"] = utc_now()
        item["defer_reason"] = "agent_subscription_limit"
        item["retry_after"] = agent_result.retry_after
        result["status"] = "deferred"
        result["defer_reason"] = "agent_subscription_limit"
        result["retry_after"] = agent_result.retry_after
        write_json(queue_path, queue)
        return result

    # Trust working tree; commit any changes (never the runner's own .aicg/ state).
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain", "--", ".", ":(exclude).aicg"],
        capture_output=True, text=True, check=False,
    )
    has_changes = bool(status.stdout.strip())
    if not has_changes:
        _set_aside_work(repo_path, item)
        item["status"] = "failed_permanently" if agent_result.returncode != 0 else "no_changes"
        item["updated_at"] = utc_now()
        item["last_failure"] = (
            f"Agent produced no on-disk changes (rc={agent_result.returncode}); "
            f"{agent_result.stderr[-200:]}"
        )
        result["status"] = item["status"]
        write_json(queue_path, queue)
        return result

    msg = f"aicg: {work_type} {subtype} for {item.get('path', item['work_id'])}"
    if stage_all_except_runner_state(repo_path) != 0:
        _set_aside_work(repo_path, item)
        item["status"] = "failed_permanently"
        item["last_failure"] = "git add failed"
        result["status"] = "failed_permanently"
        write_json(queue_path, queue)
        return result
    for args in (
        ["git", "-C", str(repo_path), "commit", "-m", msg],
        ["git", "-C", str(repo_path), "push", "-u", "origin", branch],
    ):
        c = subprocess.run(args, capture_output=True, text=True, check=False)
        if c.returncode != 0:
            _set_aside_work(repo_path, item)
            item["status"] = "failed_permanently"
            item["last_failure"] = f"git step `{args[3]}` failed: {c.stderr[-200:]}"
            result["status"] = "failed_permanently"
            write_json(queue_path, queue)
            return result

    # gh pr create
    pr_body_path = prompt_dir / "pr-body.md"
    pr_body_path.write_text(
        _build_cross_repo_pr_body(item, target), encoding="utf-8"
    )
    title = f"aicg: {work_type} — {item.get('path', item['work_id'])[:80]}"
    pr_create = subprocess.run(
        [
            "gh", "pr", "create",
            "--base", "main",
            "--title", title,
            "--body-file", str(pr_body_path),
        ],
        cwd=repo_path,
        capture_output=True, text=True, check=False,
    )
    if pr_create.returncode != 0:
        _set_aside_work(repo_path, item)
        item["status"] = "failed_permanently"
        item["last_failure"] = f"gh pr create failed: {pr_create.stderr[-200:]}"
        result["status"] = "failed_permanently"
        write_json(queue_path, queue)
        return result
    pr_url = pr_create.stdout.strip()
    result["pr"] = {"status": "opened", "pr_url": pr_url, "branch": branch}
    item["pr_url"] = pr_url
    item["pr_branch"] = branch

    # Drive to merge (auto-rebase + CI self-heal + review self-heal +
    # enable_auto_merge are all reused from the structural flow).
    _checkout_main(repo_path)
    merge_outcome = _drive_pr_to_merge(
        manifest=manifest,
        repo_path=repo_path,
        pr_url=pr_url,
        branch=branch,
    )
    result["inline_merge"] = merge_outcome
    if merge_outcome.get("status") == "merged":
        item["status"] = "merged"
        item["updated_at"] = utc_now()
    elif merge_outcome.get("status") == "auto_merge_enabled":
        item["status"] = "auto_merge_enabled"
        item["updated_at"] = utc_now()
    else:
        item["status"] = "pr_open"
        item["updated_at"] = utc_now()

    result["status"] = item["status"]
    write_json(queue_path, queue)
    return result


def _resolve_cross_repo_target(
    manifest: OrgManifest, workspace: Path, item: dict[str, Any]
) -> dict[str, Any] | None:
    """Pick the repo + file the agent should edit for this item.

    Returns ``None`` when the item needs human judgment.
    """
    work_type = item.get("type", "")
    subtype = item.get("subtype", "")

    if work_type == "org_profile_stale":
        return {
            "repo": ".github",
            "repo_path": manifest.repo_path(workspace, ".github"),
            "target_file": item.get("path") or "profile/README.md",
        }

    if work_type in {"learning_gap", "curriculum_nav_drift"}:
        repo = item.get("repo")
        if not repo:
            return None
        return {
            "repo": repo,
            "repo_path": manifest.repo_path(workspace, repo),
            "target_file": (item.get("path") or "").split("#", 1)[0],
        }

    if work_type == "pairing_mismatch":
        if subtype == "exercise_missing_in_learning":
            # Need to write a learning brief on the learning side.
            role_id = item.get("role")
            role = next((r for r in manifest.roles if r.id == role_id), None)
            if role is None:
                return None
            return {
                "repo": role.learning_repo,
                "repo_path": workspace / role.learning_repo,
                "target_file": item.get("solution_path") or "",
            }
        if subtype == "exercise_slug_drift":
            # Rename the solutions-side directory to match learning
            # (learning is authoritative). Agent will move dir + update
            # any references.
            role_id = item.get("role")
            role = next((r for r in manifest.roles if r.id == role_id), None)
            if role is None:
                return None
            return {
                "repo": role.solution_repo,
                "repo_path": workspace / role.solution_repo,
                "target_file": item.get("solution_path") or "",
            }
        # project_only_in_solutions: orphan solution project; human
        # decides whether to backfill the learning brief or delete.
        return None

    return None


def _build_cross_repo_prompt(
    item: dict[str, Any], target: dict[str, Any]
) -> str:
    work_type = item.get("type", "?")
    subtype = item.get("subtype", "?")
    details = item.get("details") or item.get("title") or ""
    repo = target["repo"]
    file_hint = target.get("target_file") or "(see details)"

    common = (
        f"# {work_type}: {subtype}\n\n"
        f"Repo: `{repo}`\n"
        f"Target: `{file_hint}`\n\n"
        f"## Why this work exists\n\n{details}\n\n"
        "## Scope\n\n"
        "- Edit only the files needed to address the issue above.\n"
        "- Do NOT touch CURRICULUM.md sections you weren't asked to,\n"
        "  do NOT touch VERSIONS.md (the runner manages that).\n"
        "- Preserve all existing structure and tone.\n"
    )

    if work_type == "curriculum_nav_drift":
        return common + (
            "\n## Specific instructions\n\n"
            "The named nav doc is out of sync with what's on disk. "
            "If a module/project is missing from the doc, add a row in "
            "the appropriate table preserving the existing column shape. "
            "If a referenced module/project doesn't exist on disk, remove "
            "the row. Do NOT invent content — just align the navigation."
        )

    if work_type == "learning_gap" and subtype == "thin_module":
        return common + (
            "\n## Specific instructions\n\n"
            "This module is a skeleton: the files exist but it was never written. "
            "Author the WHOLE module in the module directory that contains the path "
            "above, at the depth of this repo's complete modules (read two of them "
            "first): a README with objectives and structure, full lecture chapters "
            "(at least 3,000 words in total; complete modules here are usually "
            "10,000+), complete exercise briefs in exercises/ (goal, steps, "
            "deliverables, acceptance criteria; no worked solutions), and quiz.md. "
            "Keep the module id, folder, and exercise slugs unchanged so the "
            "solutions repo still pairs. Use official sources only; mark unresolved "
            "claims with `<!-- needs-research: ... -->`."
        )

    if work_type == "learning_gap":
        return common + (
            "\n## Specific instructions\n\n"
            "Write learning content for the path above. Match the tone and "
            "depth of the other modules' content in this repo. Use official "
            "sources only; mark unresolved claims with "
            "`<!-- needs-research: ... -->`. Do not exceed reasonable scope."
        )

    if work_type == "org_profile_stale":
        return common + (
            "\n## Specific instructions\n\n"
            "Bring the org profile in line with the current set of org "
            "repos. Preserve the existing format; add references for any "
            "missing repos; remove orphan references."
        )

    if work_type == "pairing_mismatch":
        if subtype == "exercise_missing_in_learning":
            return common + (
                "\n## Specific instructions\n\n"
                "A solution exercise exists with no corresponding learning "
                "brief. Write the missing learning brief at the standard "
                "path for that module + exercise number. The brief should "
                "state the goal, the expected output, and acceptance "
                "criteria; do NOT include the worked solution."
            )
        if subtype == "exercise_slug_drift":
            return common + (
                "\n## Specific instructions\n\n"
                "The solutions-side exercise directory uses a different "
                "slug than the learning side. Rename the solutions-side "
                "directory to match the learning slug (learning is "
                "authoritative). Update any internal references / index "
                "files that mention the old slug."
            )

    return common


def _build_cross_repo_pr_body(
    item: dict[str, Any], target: dict[str, Any]
) -> str:
    return (
        f"## AICG cross-repo audit work item\n\n"
        f"- Type: `{item.get('type')}` / `{item.get('subtype', '?')}`\n"
        f"- Work ID: `{item.get('work_id')}`\n"
        f"- Repo: `{target.get('repo')}`\n"
        f"- Target: `{target.get('target_file', '?')}`\n\n"
        f"### Why\n\n{item.get('details') or item.get('title') or ''}\n\n"
        f"### Loop note\n\nGenerated by the autonomous runner. CI must pass.\n"
    )


def _slug_for_branch(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_\-]+", "-", text).strip("-").lower()[:120]


def _handle_pr_response_item(
    manifest: OrgManifest,
    workspace: Path,
    repo_path: Path,
    item: dict[str, Any],
    state_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Process a respond_pr_review work item.

    Checks out the PR branch, builds a response prompt that lists each
    blocker, invokes the response agent, and pushes follow-up commits
    to the PR. The next steward pass re-checks reviews; if blockers
    clear, it merges. If the agent runs out of retries the item is
    flipped to escalated and daily-issues picks it up.
    """
    pr_number = item.get("pr_number")
    head_ref = item.get("head_ref")
    blockers = item.get("blockers", [])
    response_count = int(item.get("response_count", 0))
    max_attempts = int(item.get("max_response_attempts", 3))

    if response_count >= max_attempts:
        item["status"] = "escalated"
        result["status"] = "escalated"
        result["reason"] = (
            f"PR #{pr_number} hit max response attempts ({max_attempts}); "
            "daily-issues will surface to a human."
        )
        _persist_pr_response_state(state_dir, item)
        return result

    if not pr_number or not head_ref:
        item["status"] = "failed_permanently"
        result["status"] = "failed_permanently"
        result["reason"] = "missing pr_number or head_ref"
        _persist_pr_response_state(state_dir, item)
        return result

    # Check out the PR branch so the agent edits the right tree.
    co = subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "origin", head_ref],
        capture_output=True, text=True, check=False,
    )
    if co.returncode != 0:
        result["status"] = "fetch_failed"
        result["stderr_tail"] = co.stderr[-400:]
        return result
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", head_ref],
        capture_output=True, text=True, check=False,
    )

    prompt = _build_pr_response_prompt(pr_number, blockers, item)
    prompt_dir = repo_path / ".aicg" / "pr-responses" / f"pr-{pr_number}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    output_dir = prompt_dir / f"attempt-{response_count + 1}"
    output_dir.mkdir(parents=True, exist_ok=True)

    command = content_generation_command(manifest).format(
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(repo_path),
        work_id=item["id"],
        runner=str(Path(__file__).resolve().parents[2]),
    )
    from .agent_cli import run_agent_command

    agent_result = run_agent_command(command, cwd=repo_path)
    item["response_count"] = response_count + 1

    if agent_result.limit_reached:
        item["status"] = "deferred"
        item["retry_after"] = agent_result.retry_after
        result["status"] = "deferred"
        result["retry_after"] = agent_result.retry_after
        _checkout_main(repo_path)
        _persist_pr_response_state(state_dir, item)
        return result

    # Trust the working tree. Claude often exits non-zero after a
    # blocked tool call but still leaves valid edits via Edit/Write.
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    has_changes = bool(status.stdout.strip())
    if not has_changes and agent_result.returncode != 0:
        result["status"] = "agent_failed"
        result["returncode"] = agent_result.returncode
        result["stderr_tail"] = agent_result.stderr[-400:]
        _checkout_main(repo_path)
        _persist_pr_response_state(state_dir, item)
        return result

    # Commit + push whatever the agent changed.
    push_outcome = _commit_and_push_pr_response(repo_path, pr_number, response_count + 1)
    if agent_result.returncode != 0:
        push_outcome["agent_returncode"] = agent_result.returncode
    result["status"] = push_outcome["status"]
    result["push"] = push_outcome
    item["status"] = (
        "ready"
        if push_outcome["status"] in {"pushed", "no_changes"}
        else "failed_permanently"
    )
    _checkout_main(repo_path)
    _persist_pr_response_state(state_dir, item)
    return result


def _build_pr_response_prompt(
    pr_number: int, blockers: list[dict[str, Any]], item: dict[str, Any]
) -> str:
    lines = [
        f"# Respond to PR #{pr_number} review comments",
        "",
        "## Goal",
        "",
        "Address every blocker listed below by editing the code on the",
        "current branch. You are NOT writing new curriculum content — you",
        "are responding to a reviewer (human or bot). Make the smallest",
        "change that resolves each blocker.",
        "",
        "## Source policy",
        "",
        "Same as content generation: official sources first, no invented",
        "facts, mark unresolved claims with `<!-- needs-research: ... -->`.",
        "",
        "## Blockers",
        "",
    ]
    for i, b in enumerate(blockers, 1):
        if b.get("kind") == "changes_requested":
            lines.append(f"### {i}. CHANGES_REQUESTED from @{b.get('author')}")
            lines.append("")
            lines.append(f"> {(b.get('body') or '').strip() or '(no body)'}")
            lines.append("")
        elif b.get("kind") == "unresolved_thread":
            actor = "bot" if b.get("is_bot") else "human"
            lines.append(
                f"### {i}. Unresolved review thread "
                f"({actor}: @{b.get('author')}) "
                f"in `{b.get('path')}:{b.get('line')}`"
            )
            lines.append("")
            lines.append(f"> {(b.get('body') or '').strip() or '(no body)'}")
            lines.append("")
    lines.extend(
        [
            "## Output contract",
            "",
            "- Edit only files in this repo on the current branch.",
            "- Make atomic commits per blocker where possible.",
            "- Do NOT mark review threads resolved — only the reviewer can do",
            "  that. Your job is to push commits that address the underlying",
            "  issue; bot threads will auto-resolve when their metric recovers,",
            "  human threads stay open until the human resolves them.",
            "- Do NOT touch CURRICULUM.md, README.md, VERSIONS.md, or anything",
            "  outside the scope of the reviewer's comment.",
        ]
    )
    return "\n".join(lines) + "\n"


def _commit_and_push_pr_response(
    repo_path: Path, pr_number: int, attempt: int
) -> dict[str, Any]:
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if not status.stdout.strip():
        return {"status": "no_changes"}
    msg = f"aicg: respond to PR #{pr_number} review (attempt {attempt})"
    for args in (
        ["git", "-C", str(repo_path), "add", "--all"],
        ["git", "-C", str(repo_path), "commit", "-m", msg],
        ["git", "-C", str(repo_path), "push"],
    ):
        c = subprocess.run(args, capture_output=True, text=True, check=False)
        if c.returncode != 0:
            return {
                "status": "push_failed",
                "step": args[3],
                "stderr_tail": c.stderr[-400:],
            }
    return {"status": "pushed", "message": msg}


def _set_aside_work(repo_path: Path, item: dict[str, Any]) -> None:
    """Leave ``main`` clean after a cross-repo item fails partway.

    A plain ``checkout main`` carried the agent's edits onto ``main``, and sync
    then skipped the clone as dirty until someone cleaned it by hand. Park the
    edits on an ``aicg/parked/...`` branch so they can be recovered; if even
    that fails, discard them.
    """
    from .gitops import GitOpsError, discard_work_and_checkout_main, park_changes

    try:
        branch = park_changes(repo_path, item["work_id"])
    except GitOpsError:
        branch = None
    if branch:
        item["parked_branch"] = branch
    discard_work_and_checkout_main(repo_path)


def _checkout_main(repo_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", "main"],
        capture_output=True, text=True, check=False,
    )


def _persist_pr_response_state(state_dir: Path, item: dict[str, Any]) -> None:
    """Write the updated item back into pr-response-queue.json."""
    from .steward import PR_RESPONSE_QUEUE

    path = state_dir / PR_RESPONSE_QUEUE
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = payload.get("items", [])
    for i, existing in enumerate(items):
        if existing.get("id") == item["id"]:
            items[i] = {**existing, **item, "updated_at": utc_now()}
            break
    payload["items"] = items
    write_json(path, payload)


def _park_unpublished_work(repo_path: Path, item: dict[str, Any], result: dict[str, Any]) -> None:
    """If generated work didn't become a PR, record why and park it on a branch.

    Leaving it in the working tree made later syncs skip the repo as dirty,
    so the content (and every later update to the repo) went nowhere.
    """
    pr = result.get("pr") or {}
    published = item["status"] == "merged" or pr.get("status") == "opened"
    if published:
        return
    if pr.get("status") in {"error", "skipped"}:
        item["last_failure"] = f"pr {pr['status']}: {pr.get('reason', '')}"[:500]
    from .gitops import GitOpsError, park_changes

    try:
        branch = park_changes(repo_path, item["work_id"])
    except GitOpsError as exc:
        result["park_error"] = str(exc)[:500]
        return
    if branch:
        item["parked_branch"] = branch
        result["parked_branch"] = branch


def _open_work_item_pr(
    repo_path: Path, plan: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    """Commit and open a PR for the just-verified work item.

    Failures bubble up as ``status=error`` with a stderr tail — the
    runner does not rollback the on-disk content if PR creation
    fails. Operators can re-run ``aicg pr --repo X --work-id Y``.
    """
    from .gitops import GitOpsError, prepare_pr

    # Re-audit the target so the PR body reflects post-generation
    # state, not the weekly-audit snapshot. The result is written to
    # .aicg/audit-report.json which prepare_pr reads.
    try:
        audit_report = audit_repo(
            workspace=repo_path.parent,
            repo_name=repo_path.name,
        )
    except Exception as exc:  # noqa: BLE001
        # Fall back to whatever audit-report.json existed before; we
        # never block PR creation on a stale audit.
        try:
            audit_report = read_state(repo_path, "audit-report.json")
        except FileNotFoundError:
            return {
                "status": "skipped",
                "reason": f"audit failed and no prior audit-report.json: {exc}",
            }

    try:
        validation_report = read_state(repo_path, "validation-report.json")
    except FileNotFoundError:
        validation_report = {"status": "not_run", "checks": []}

    try:
        pr_result = prepare_pr(
            repo_path,
            work_plan=plan,
            audit_report=audit_report,
            validation_report=validation_report,
            auto_merge=False,
            work_id=item["work_id"],
        )
    except GitOpsError as exc:
        return {"status": "error", "reason": str(exc)}

    return {
        "status": "opened",
        "pr_url": pr_result.get("pr_url"),
        "branch": pr_result.get("branch"),
        "changed_files": pr_result.get("changed_files", [])[:20],
    }


def _collect_pr_response_items(state_dir: Path, repo: str) -> list[dict[str, Any]]:
    """Read pr-response-queue.json from the org state and filter to ``repo``.

    Steward populates this queue when it finds CHANGES_REQUESTED reviews
    or unresolved review threads. Items with status='escalated' are
    skipped here — daily-issues handles those.
    """
    path = state_dir / "pr-response-queue.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    # Both 'ready' (work the daily loop will pick up) and 'escalated'
    # (work that daily-issues should open a tracking issue for) belong
    # in the queue — daily-remediate skips escalated items, daily-issues
    # acts on them.
    return [
        item
        for item in (payload.get("items") or [])
        if item.get("repo") == repo and item.get("status") in {"ready", "escalated"}
    ]


def _collect_freshness_items(repo_path: Path) -> list[dict[str, Any]]:
    """Read freshness reports from the repo and return their work items.

    Tolerates missing files (each report is optional) and malformed
    JSON (logs nothing, skips silently — the audit run shouldn't blow
    up because a stale report can't be parsed).
    """
    items: list[dict[str, Any]] = []
    state_dir = repo_path / ".aicg"
    for filename in (
        "freshness-links-report.json",
        "freshness-versions-report.json",
        "freshness-review-report.json",
    ):
        path = state_dir / filename
        if not path.exists():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in report.get("work_items", []) or []:
            if isinstance(entry, dict) and entry.get("id"):
                items.append(entry)
    return items


_BACKLOG_TYPES = {
    "exercise_depth_followup",
    # New broad-audit types — same priority tier as the existing
    # exercise_depth_followup backlog. They surface real gaps in the
    # learning side, nav docs, cross-repo pairing, and org profile.
    "learning_gap",
    "pairing_mismatch",
    "curriculum_nav_drift",
    "org_profile_stale",
}
_HIGH_PRIORITY_TYPES = {"respond_pr_review"}


def _severity_bias(item: dict[str, Any]) -> int:
    """Convert work item severity + type into a priority bias.

    Lower number = higher priority (queue is sorted ascending).

    - High-severity refresh items: -100000 (jumps above every
      structural gap regardless of role level).
    - Structural gaps (module_solution_gap, project_solution_gap): 0.
    - Medium-severity refresh + backlog (exercise_depth_followup): +5000
      (just below structural within the same role).
    - Low-severity refresh items: +20000 (deepest backlog).
    """
    severity = str(item.get("severity", "")).lower()
    work_type = str(item.get("type", ""))
    is_refresh = work_type.startswith("refresh_")
    is_backlog = work_type in _BACKLOG_TYPES
    is_high_priority = work_type in _HIGH_PRIORITY_TYPES
    if is_high_priority:
        # PR responses always jump structural gaps — keeping an open PR
        # moving beats opening another one.
        return -200_000
    if work_type == "learning_gap" and item.get("subtype") == "thin_module":
        # A skeleton module is structural content work. It goes just ahead of
        # its own role's solution gaps (solutions depend on the lesson); the
        # role-level term still puts entry roles first.
        return -500
    if not is_refresh and not is_backlog:
        return 0
    if severity == "high":
        return -100_000
    if severity == "medium":
        return 5_000
    return 20_000


def is_git_dirty(repo_path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path == ".aicg" or path.startswith(".aicg/"):
            continue
        return True
    return False


def git_tag_exists(repo_path: Path, tag: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def run_command(command: list[str]) -> dict[str, Any]:
    started = datetime.now().isoformat(timespec="seconds")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "command": shell_join(command),
        "started_at": started,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def shell_join(command: list[str]) -> str:
    return shlex.join(command)


def content_generation_command(manifest: OrgManifest) -> str | None:
    agent = manifest.content_generation.get("agent", {})
    command = agent.get("agent_command") or manifest.research.get("agent_command")
    return command or None


def content_generation_label(manifest: OrgManifest) -> str:
    agent = manifest.content_generation.get("agent", {})
    provider = agent.get("provider", "configured content agent")
    model = agent.get("model", "configured model")
    return f"{provider} {model}"
