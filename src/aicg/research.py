"""Process quarterly research packets into curriculum proposals.

:func:`generate_research_packets` (in :mod:`aicg.org_runner`) writes a
prompt packet per role describing what to research. This module is the
matching consumer: it invokes the configured research agent on each
packet and expects three outputs per role:

1. ``JOB_REQUIREMENTS.md`` — human-readable requirements roll-up
2. ``.aicg/job-requirements.json`` — machine-readable normalized
   requirements with evidence, owner role, and coverage status
3. ``.aicg/curriculum-plan-delta.json`` — proposed additions to
   the role's ``curriculum-plan.json``: new modules, new exercises,
   new projects. Strictly additive; the runner refuses to remove
   existing items via this channel.

The delta is NOT auto-merged. Instead :func:`validate_delta_against_caps`
applies per-run caps and evidence thresholds from the manifest, then
:func:`write_proposal` emits a human-readable ``RESEARCH_PROPOSAL_*.md``
plus the filtered delta into a branch and opens a PR for human review.
A separate command (:mod:`aicg.cli.cmd_org_promote_plan`) is what
actually merges an approved proposal into ``curriculum-plan.json``.

This split is intentional: research can propose, but the runner does
not add curriculum to the catalog autonomously. A human must approve
every new module / exercise / project.

The processor is subscription-limit-aware: when the agent hits a
five-hour or weekly cap it returns ``deferred`` for that role and the
next research run picks up where it left off.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .agent_cli import (
    AgentCommandResult,
    reclassify_with_response_file,
    run_agent_command,
)
from .org_config import OrgManifest, RoleConfig, state_dir_for_manifest
from .state import utc_now, write_json

RESEARCH_APPLY_REPORT = "research-apply-report.json"
CURRICULUM_PLAN_FILE = "curriculum-plan.json"
CURRICULUM_PLAN_DELTA_FILE = ".aicg/curriculum-plan-delta.json"
CURRICULUM_PLAN_DELTA_FILTERED = ".aicg/curriculum-plan-delta-filtered.json"
JOB_REQUIREMENTS_JSON = ".aicg/job-requirements.json"
JOB_REQUIREMENTS_MD = "JOB_REQUIREMENTS.md"

# W2.2b: per-role manifest format ("v2"). The agent writes a delta in the
# new shape at this path within the learning repo; research_apply
# validates against ``manifest/curriculum_plan.<role>.manifest.json`` in
# the content-generator repo, NOT the legacy curriculum-plan.json.
CURRICULUM_PLAN_DELTA_V2_FILE = ".aicg/curriculum-plan-delta-v2.json"

# Canary rollout — only these role IDs use the new v2 delta path. Other
# roles continue on the legacy code path (modules/exercises/projects
# additions) until we promote them after the canary stabilizes.
CANARY_ROLES_NEW_FORMAT: frozenset[str] = frozenset({"junior-engineer"})

# Path to per-role v2 manifests, relative to the content-generator root.
_PER_ROLE_MANIFEST_RELATIVE = "manifest/curriculum_plan.{role}.manifest.json"

# GitHub PR labels used on canary research proposals.
_LABEL_NEW_FORMAT = "curriculum-plan-v2"
_LABEL_REQUIRES_APPROVAL = "requires-explicit-approval"

# Skip-if-open-PR token-conservation: if a role already has an open
# research-proposal PR from a previous cycle, the human hasn't reviewed
# the prior work. Running again would waste a full agent invocation
# AND compete for the reviewer's attention. Skip with status
# ``skipped_pending_review`` until the existing PR is merged or closed.
#
# Branch prefix matches what `_open_proposal_pr` / `_open_proposal_pr_v2`
# create: ``aicg/research/<month>/<role>`` or ``research/<month>/<role>``.
_OPEN_PR_BRANCH_PREFIXES: tuple[str, ...] = (
    "aicg/research/",
    "research/",
)

DEFAULT_CAPS = {
    "max_modules_per_run": 1,
    "max_exercises_per_run": 3,
    "max_projects_per_run": 0,
}
DEFAULT_MIN_EVIDENCE_COUNT = 3


@dataclass(frozen=True)
class ResearchCaps:
    max_modules: int
    max_exercises: int
    max_projects: int
    min_evidence_count: int

    @classmethod
    def from_manifest(cls, manifest: OrgManifest) -> "ResearchCaps":
        research = manifest.research or {}
        caps = research.get("caps") or {}
        return cls(
            max_modules=int(caps.get("max_modules_per_run", DEFAULT_CAPS["max_modules_per_run"])),
            max_exercises=int(caps.get("max_exercises_per_run", DEFAULT_CAPS["max_exercises_per_run"])),
            max_projects=int(caps.get("max_projects_per_run", DEFAULT_CAPS["max_projects_per_run"])),
            min_evidence_count=int(
                research.get("min_evidence_count", DEFAULT_MIN_EVIDENCE_COUNT)
            ),
        )


class ResearchError(RuntimeError):
    """Raised when research processing cannot proceed."""


@dataclass(frozen=True)
class ResearchAgentConfig:
    enabled: bool
    agent_command: str | None
    timeout_seconds: int | None

    @classmethod
    def from_manifest(cls, manifest: OrgManifest) -> "ResearchAgentConfig":
        """Pull the research agent config from manifest.research.agent.

        Falls back to ``content_generation.agent`` when there is no
        explicit research block, since the research wrapper is just a
        sibling of the content wrapper that also allows web tools.
        """
        cfg = (manifest.research or {}).get("agent")
        if not cfg:
            cfg = (manifest.content_generation or {}).get("agent") or {}
        agent_command = cfg.get("agent_command") if isinstance(cfg, dict) else None
        return cls(
            enabled=bool(agent_command),
            agent_command=agent_command,
            timeout_seconds=(cfg.get("timeout_seconds") if isinstance(cfg, dict) else None),
        )


def research_apply(
    manifest: OrgManifest,
    workspace: Path,
    month: str | None = None,
    state_dir: Path | None = None,
    runner_root: Path | None = None,
    open_pr: bool = True,
    role_id: str | None = None,
) -> dict[str, Any]:
    """Process every role's research packet via the configured agent.

    Per role, this:
    1. Invokes the configured research agent on the prompt packet.
    2. Validates the resulting curriculum-plan-delta against the
       manifest's caps and evidence threshold.
    3. Writes a human-readable RESEARCH_PROPOSAL_<month>.md plus the
       filtered delta into a branch in the learning repo.
    4. Opens a GitHub PR (``open_pr=True``, default) requesting human
       approval. The runner never merges curriculum-plan.json
       autonomously.

    Set ``open_pr=False`` for dry-run / tests / first inspection.
    """
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    month = month or date.today().strftime("%Y-%m")
    prompt_dir = state_root / "research" / month
    if not prompt_dir.exists():
        raise ResearchError(
            f"No research packets at {prompt_dir}. Run `aicg org research` first."
        )

    config = ResearchAgentConfig.from_manifest(manifest)
    caps = ResearchCaps.from_manifest(manifest)
    runner_root = runner_root or Path(__file__).resolve().parents[2]

    roles = sorted(manifest.roles, key=lambda item: item.level)
    if role_id is not None:
        roles = [r for r in roles if r.id == role_id]
        if not roles:
            valid = ", ".join(sorted(r.id for r in manifest.roles))
            raise ResearchError(
                f"Unknown role {role_id!r}. Known roles: {valid}"
            )

    role_reports: list[dict[str, Any]] = []
    for role in roles:
        prompt_path = prompt_dir / f"{role.id}.md"
        if not prompt_path.exists():
            role_reports.append(_skip(role, "prompt_missing", str(prompt_path)))
            continue
        learning_path = workspace / role.learning_repo
        if not learning_path.exists():
            role_reports.append(_skip(role, "learning_repo_missing", str(learning_path)))
            continue
        if not config.enabled:
            role_reports.append(_skip(role, "agent_not_configured", None))
            continue

        # Bug 4 mitigation: skip-if-pending-review. If a prior cycle's
        # research-proposal PR is still open for this role, the human
        # has not approved the prior work. Running the agent again
        # would burn ~100-500K tokens AND compete for review attention
        # — both wasteful. Skip until the existing PR is merged/closed.
        pending = _pending_proposal_pr(role)
        if pending is not None:
            role_reports.append(
                {
                    "role": role.id,
                    "status": "skipped_pending_review",
                    "reason": (
                        f"Prior proposal PR still open ({pending}). "
                        "Skipping agent invocation to conserve tokens. "
                        "Merge or close the existing PR before re-running."
                    ),
                    "pending_pr": pending,
                }
            )
            continue

        result = _invoke_agent(
            config=config,
            prompt_path=prompt_path,
            learning_path=learning_path,
            role=role,
            runner_root=runner_root,
        )
        if result.limit_reached:
            role_reports.append(
                {
                    "role": role.id,
                    "status": "deferred",
                    "reason": (
                        f"Agent subscription limit ({result.limit_scope}); "
                        f"retry after {result.retry_after}."
                    ),
                    "retry_after": result.retry_after,
                }
            )
            continue
        if result.returncode != 0:
            role_reports.append(
                {
                    "role": role.id,
                    "status": "agent_failed",
                    "returncode": result.returncode,
                    "stderr_tail": result.stderr[-800:],
                }
            )
            continue

        outputs = _detect_outputs(learning_path)

        # ----- W2.2b canary: junior-engineer uses the new v2 delta path -----
        if role.id in CANARY_ROLES_NEW_FORMAT:
            v2_report = _handle_new_format_role(
                role=role,
                learning_path=learning_path,
                runner_root=runner_root,
                month=month,
                open_pr=open_pr,
                outputs=outputs,
            )
            role_reports.append(v2_report)
            continue
        # -------------------------------------------------------------------

        if not outputs["delta_present"]:
            role_reports.append(
                {
                    "role": role.id,
                    "status": "applied_no_delta",
                    "outputs": outputs,
                    "note": "Agent updated JOB_REQUIREMENTS only; no curriculum proposal.",
                }
            )
            continue

        delta_path = learning_path / CURRICULUM_PLAN_DELTA_FILE
        validation = validate_delta_against_caps(delta_path, caps)
        proposal = write_proposal(
            learning_path=learning_path,
            role=role,
            month=month,
            validation=validation,
            caps=caps,
        )
        pr_outcome: dict[str, Any] | None = None
        if open_pr and (validation["accepted"]["modules"]
                        or validation["accepted"]["exercises"]
                        or validation["accepted"]["projects"]
                        or validation["rejected"]):
            pr_outcome = _open_proposal_pr(
                learning_path=learning_path,
                role=role,
                month=month,
                proposal_paths=proposal["written"],
            )

        role_reports.append(
            {
                "role": role.id,
                "status": "proposal_ready",
                "outputs": outputs,
                "validation": _validation_summary(validation),
                "proposal_files": proposal["written"],
                "pr": pr_outcome,
                "approval_issue": _open_approval_issue(
                    manifest, role, month, validation, pr_outcome, proposal["written"]),
            }
        )

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "research_apply",
        "month": month,
        "caps": {
            "max_modules_per_run": caps.max_modules,
            "max_exercises_per_run": caps.max_exercises,
            "max_projects_per_run": caps.max_projects,
            "min_evidence_count": caps.min_evidence_count,
        },
        "roles": role_reports,
    }
    write_json(state_root / RESEARCH_APPLY_REPORT, report)
    return report


def validate_delta_against_caps(
    delta_path: Path, caps: ResearchCaps
) -> dict[str, Any]:
    """Apply per-run caps and evidence threshold to a delta.

    Returns ``{"accepted": {...}, "rejected": [...], "rationale": str}``.
    Items beyond the cap, or below the evidence threshold, are moved to
    ``rejected`` with a reason. Order matters: the highest-quality items
    (by evidence count) are accepted first.
    """
    if not delta_path.exists():
        return _empty_validation("delta file not present")
    try:
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _empty_validation(f"delta JSON invalid: {exc}")

    rationale = str(delta.get("rationale", "")).strip()
    raw_modules = delta.get("modules", []) or []
    raw_exercises = delta.get("exercises", []) or []
    raw_projects = delta.get("projects", []) or []

    accepted_modules, rejected = _filter_with_caps(
        raw_modules,
        cap=caps.max_modules,
        kind="module",
        min_evidence=caps.min_evidence_count,
    )
    accepted_exercises, rejected_ex = _filter_with_caps(
        raw_exercises,
        cap=caps.max_exercises,
        kind="exercise",
        min_evidence=caps.min_evidence_count,
    )
    accepted_projects, rejected_pj = _filter_with_caps(
        raw_projects,
        cap=caps.max_projects,
        kind="project",
        min_evidence=caps.min_evidence_count,
    )
    rejected.extend(rejected_ex)
    rejected.extend(rejected_pj)

    return {
        "rationale": rationale,
        "accepted": {
            "modules": accepted_modules,
            "exercises": accepted_exercises,
            "projects": accepted_projects,
        },
        "rejected": rejected,
        "input_counts": {
            "modules": len(raw_modules),
            "exercises": len(raw_exercises),
            "projects": len(raw_projects),
        },
    }


def _empty_validation(reason: str) -> dict[str, Any]:
    return {
        "rationale": "",
        "accepted": {"modules": [], "exercises": [], "projects": []},
        "rejected": [],
        "input_counts": {"modules": 0, "exercises": 0, "projects": 0},
        "note": reason,
    }


def _filter_with_caps(
    items: list[dict[str, Any]],
    cap: int,
    kind: str,
    min_evidence: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    qualified: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence") or item.get("postings") or []
        if not isinstance(evidence, list):
            evidence = []
        evidence_count = len(evidence)
        if evidence_count < min_evidence:
            rejected.append(
                {
                    "kind": kind,
                    "id": str(item.get("id") or item.get("slug") or "?"),
                    "reason": (
                        f"evidence_below_threshold: {evidence_count} "
                        f"< required {min_evidence}"
                    ),
                    "item": item,
                }
            )
            continue
        qualified.append((evidence_count, item))

    # Highest evidence count first.
    qualified.sort(key=lambda pair: pair[0], reverse=True)
    accepted = [item for _, item in qualified[:cap]]
    for _, item in qualified[cap:]:
        rejected.append(
            {
                "kind": kind,
                "id": str(item.get("id") or item.get("slug") or "?"),
                "reason": f"cap_exceeded: cap={cap}",
                "item": item,
            }
        )
    return accepted, rejected


def write_proposal(
    learning_path: Path,
    role: RoleConfig,
    month: str,
    validation: dict[str, Any],
    caps: ResearchCaps,
) -> dict[str, Any]:
    """Materialize the proposal as files inside the learning repo.

    Returns ``{"written": [<paths>]}``. Idempotent: rerunning overwrites
    the same proposal artifacts so a re-opened PR shows the latest.
    """
    md_path = learning_path / f"RESEARCH_PROPOSAL_{month}.md"
    filtered_path = learning_path / CURRICULUM_PLAN_DELTA_FILTERED
    filtered_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "role_id": role.id,
        "month": month,
        "rationale": validation["rationale"],
        "modules": validation["accepted"]["modules"],
        "exercises": validation["accepted"]["exercises"],
        "projects": validation["accepted"]["projects"],
    }
    filtered_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    md_path.write_text(_render_proposal_markdown(role, month, validation, caps), encoding="utf-8")
    return {"written": [str(md_path), str(filtered_path)]}


def _render_proposal_markdown(
    role: RoleConfig,
    month: str,
    validation: dict[str, Any],
    caps: ResearchCaps,
) -> str:
    accepted = validation["accepted"]
    rejected = validation["rejected"]
    lines: list[str] = [
        f"# Curriculum proposal: {role.title} — {month}",
        "",
        "Generated by `aicg org research --apply`. Caps:",
        "",
        f"- `max_modules_per_run`: {caps.max_modules}",
        f"- `max_exercises_per_run`: {caps.max_exercises}",
        f"- `max_projects_per_run`: {caps.max_projects}",
        f"- `min_evidence_count`: {caps.min_evidence_count} job postings per item",
        "",
        "## Rationale",
        "",
        validation["rationale"] or "_No rationale supplied by the research agent._",
        "",
        "## Accepted",
        "",
    ]
    for kind, items in (
        ("Modules", accepted["modules"]),
        ("Exercises", accepted["exercises"]),
        ("Projects", accepted["projects"]),
    ):
        lines.append(f"### {kind}")
        lines.append("")
        if not items:
            lines.append("_None._")
            lines.append("")
            continue
        for item in items:
            ident = item.get("id") or item.get("slug") or "?"
            title = item.get("title") or ""
            evidence = item.get("evidence") or item.get("postings") or []
            lines.append(f"- **`{ident}`** — {title}  ({len(evidence)} citations)")
            rationale = (item.get("rationale") or "").strip()
            if rationale:
                lines.append(f"  - rationale: {rationale}")
        lines.append("")

    lines.extend(["## Rejected", ""])
    if not rejected:
        lines.append("_None._")
    else:
        for entry in rejected:
            lines.append(
                f"- `{entry['kind']}` `{entry['id']}` — {entry['reason']}"
            )
    lines.append("")
    lines.extend(
        [
            "---",
            "",
            "## Approval",
            "",
            "**This proposal does NOT add anything to the curriculum automatically.**",
            "",
            "If you approve some/all of the accepted items, merge this PR. After merge,",
            "run `aicg org promote-plan --role <role>` (or wait for the next weekly",
            "audit) to apply the filtered delta to `curriculum-plan.json` and scaffold",
            "skeletons.",
            "",
            "If you reject everything, close the PR. No further action needed; the",
            "next research run will repropose anything still warranted by the job",
            "market.",
            "",
        ]
    )
    return "\n".join(lines)


def _validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    accepted = validation["accepted"]
    return {
        "rationale_present": bool(validation["rationale"]),
        "accepted_counts": {
            "modules": len(accepted["modules"]),
            "exercises": len(accepted["exercises"]),
            "projects": len(accepted["projects"]),
        },
        "rejected_count": len(validation["rejected"]),
        "input_counts": validation["input_counts"],
    }


def _open_approval_issue(
    manifest: OrgManifest,
    role: RoleConfig,
    month: str,
    validation: dict[str, Any],
    pr_outcome: dict[str, Any] | None,
    proposal_files: list[str] | None = None,
) -> str | None:
    """Open a content-proposal issue so the PR doesn't sit unnoticed.

    Only when something was accepted and the PR exists. Never raises:
    research must not fail because GitHub issue creation did.
    """
    accepted = validation.get("accepted", {})
    if not any(accepted.get(k) for k in ("modules", "exercises", "projects")):
        return None
    try:
        from .approvals import content_issue, open_content_issue, pr_url_from_outcome
        from .domain_provision import _org_display
        from .domains import domain_config_path, list_domains
        from .org_config import load_manifest
        from .role_discovery import default_gh

        pr_url = pr_url_from_outcome(pr_outcome)
        if not pr_url:
            return None
        domain = next((d for d in list_domains()
                       if load_manifest(domain_config_path(d)).org.lower() == manifest.org.lower()),
                      None)
        if domain is None:
            return None
        delta = None
        for path in proposal_files or []:
            if path.endswith(CURRICULUM_PLAN_DELTA_FILTERED):
                delta = json.loads(Path(path).read_text(encoding="utf-8"))
        title, body = content_issue(domain, _org_display(manifest.org), role.id, role.title, month,
                                    validation, pr_url, delta=delta)
        return open_content_issue(default_gh, title, body)
    except Exception as exc:  # noqa: BLE001 - best effort; the PR still exists
        return f"issue_failed: {type(exc).__name__}: {exc}"


def _open_proposal_pr(
    learning_path: Path,
    role: RoleConfig,
    month: str,
    proposal_paths: list[str],
) -> dict[str, Any]:
    """Push a branch with the proposal files and `gh pr create` it.

    Failures are reported but do not raise — the proposal files still
    exist locally so the human can open the PR manually.
    """
    branch = f"aicg/research/{month}/{role.id}"
    title = f"[aicg] curriculum proposal: {role.title} — {month}"
    body_path = learning_path / f"RESEARCH_PROPOSAL_{month}.md"

    commands: list[tuple[str, list[str]]] = [
        # Stash any residue the agent left behind — without this,
        # `git checkout -B` complains about uncommitted changes and the
        # subsequent `gh pr create` warns "Warning: N uncommitted
        # changes" which can disable some flag handling.
        (
            "stash_residue",
            [
                "git",
                "-C",
                str(learning_path),
                "stash",
                "push",
                "--include-untracked",
                "--message",
                f"aicg-research-residue-{month}-{role.id}",
            ],
        ),
        ("checkout", ["git", "-C", str(learning_path), "checkout", "-B", branch]),
        ("add", ["git", "-C", str(learning_path), "add", *proposal_paths]),
        (
            "commit",
            [
                "git",
                "-C",
                str(learning_path),
                "commit",
                "-m",
                f"aicg: curriculum proposal {month} ({role.id})",
            ],
        ),
        (
            "push",
            ["git", "-C", str(learning_path), "push", "-u", "origin", branch],
        ),
        # PR creation: do NOT pass --label flags. `gh pr create` hard-fails
        # on missing labels (which is how the entire research cycle has
        # been silently losing work — see Bug 1 in
        # project_research_cycle_bugs memory). Labels are added in a
        # follow-up step that tolerates failure.
        (
            "pr_create",
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body-file",
                str(body_path),
            ],
        ),
    ]
    # Best-effort labels — added AFTER the PR exists. Failures are
    # logged but never block the PR. Run with --add-label per label so
    # one missing label doesn't kill the others.
    _LEGACY_LABELS = ("aicg", "aicg:plan-proposal")
    outcomes: list[dict[str, Any]] = []
    for label, cmd in commands:
        completed = subprocess.run(
            cmd,
            cwd=learning_path,
            capture_output=True,
            text=True,
            check=False,
        )
        outcomes.append(
            {
                "step": label,
                "command": shlex.join(cmd),
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-400:],
                "stderr_tail": completed.stderr[-400:],
            }
        )
        if completed.returncode != 0 and label not in ("commit", "stash_residue"):
            # `git commit` returning non-zero is fine if nothing changed.
            # `git stash push` returns non-zero when there's nothing to
            # stash — that's the expected steady state, not a failure.
            break

    # Best-effort labels — added AFTER the PR exists so a missing label
    # can't block PR creation. Each label is tried individually;
    # failures are logged but never break the flow.
    pr_created = any(
        outcome["step"] == "pr_create" and outcome["returncode"] == 0
        for outcome in outcomes
    )
    if pr_created:
        for label_name in _LEGACY_LABELS:
            cmd = [
                "gh",
                "pr",
                "edit",
                branch,
                "--add-label",
                label_name,
            ]
            completed = subprocess.run(
                cmd,
                cwd=learning_path,
                capture_output=True,
                text=True,
                check=False,
            )
            outcomes.append(
                {
                    "step": f"add_label:{label_name}",
                    "command": shlex.join(cmd),
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-300:],
                    "stderr_tail": completed.stderr[-300:],
                }
            )

    # Restore main so subsequent agents start from a clean tree.
    subprocess.run(
        ["git", "-C", str(learning_path), "checkout", "main"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"branch": branch, "steps": outcomes}


def promote_plan(learning_path: Path) -> dict[str, Any]:
    """Apply a human-approved proposal to curriculum-plan.json.

    Reads ``.aicg/curriculum-plan-delta-filtered.json`` (written by
    :func:`write_proposal` and committed when a human merges the
    proposal PR) and merges it into ``curriculum-plan.json`` via
    :func:`merge_curriculum_plan_delta`. After a successful promotion
    the filtered file is renamed with a ``.promoted-<UTC>`` suffix so
    it doesn't get re-applied on the next pass.

    Returns a report describing what was promoted; raises
    ``ResearchError`` if no filtered proposal exists.
    """
    filtered_path = learning_path / CURRICULUM_PLAN_DELTA_FILTERED
    if not filtered_path.exists():
        raise ResearchError(
            f"No filtered proposal at {filtered_path}; run "
            "`aicg org research --apply` first and merge the PR."
        )

    # The filtered file has the same shape as a delta, so the existing
    # merger handles it. We temporarily write it as the delta input.
    plan_delta_path = learning_path / CURRICULUM_PLAN_DELTA_FILE
    plan_delta_path.parent.mkdir(parents=True, exist_ok=True)
    plan_delta_path.write_text(
        filtered_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    merge_report = merge_curriculum_plan_delta(learning_path)

    # Archive the consumed proposal so re-running promote is a no-op.
    stamp = utc_now().replace(":", "").replace("-", "")
    archived = filtered_path.with_suffix(f".promoted-{stamp}.json")
    filtered_path.rename(archived)
    # Also archive the now-merged delta file.
    if plan_delta_path.exists():
        plan_delta_path.rename(plan_delta_path.with_suffix(f".promoted-{stamp}.json"))

    return {
        "schema_version": 1,
        "operation": "promote_plan",
        "learning_path": str(learning_path),
        "archived_proposal": str(archived),
        "merge_report": merge_report,
    }


def merge_curriculum_plan_delta(learning_path: Path) -> dict[str, Any]:
    """Merge ``.aicg/curriculum-plan-delta.json`` into ``curriculum-plan.json``.

    Additive only — never removes existing modules, exercises, or
    projects. New items are deduplicated by stable id/slug. Returns a
    report describing what was added vs. skipped (already present).
    """
    delta_path = learning_path / CURRICULUM_PLAN_DELTA_FILE
    plan_path = learning_path / CURRICULUM_PLAN_FILE
    if not delta_path.exists():
        return {"present": False, "added": [], "skipped": []}

    try:
        delta = json.loads(delta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"present": True, "error": f"invalid JSON: {exc}"}

    plan: dict[str, Any]
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"present": True, "error": f"existing curriculum-plan.json invalid: {exc}"}
    else:
        plan = {"schema_version": 1, "modules": [], "projects": []}

    added: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    modules = plan.setdefault("modules", [])
    existing_module_ids = {str(m.get("id")) for m in modules if isinstance(m, dict)}
    for entry in delta.get("modules", []) or []:
        if not isinstance(entry, dict):
            continue
        mod_id = str(entry.get("id") or "")
        if not mod_id:
            continue
        if mod_id in existing_module_ids:
            skipped.append({"kind": "module", "id": mod_id})
            continue
        modules.append(entry)
        existing_module_ids.add(mod_id)
        added.append({"kind": "module", "id": mod_id})

    # Exercises live under their parent module.
    modules_by_id = {str(m.get("id")): m for m in modules if isinstance(m, dict)}
    for entry in delta.get("exercises", []) or []:
        if not isinstance(entry, dict):
            continue
        parent_id = str(entry.get("module_id") or entry.get("module") or "")
        exercise = entry.get("exercise") or entry
        exercise_slug = str(
            exercise.get("slug")
            or exercise.get("id")
            or ""
        )
        if not parent_id or not exercise_slug or parent_id not in modules_by_id:
            skipped.append({"kind": "exercise", "id": exercise_slug, "reason": "no_parent"})
            continue
        parent = modules_by_id[parent_id]
        parent_exercises = parent.setdefault("exercises", [])
        existing_slugs = {
            str((ex or {}).get("slug") or (ex or {}).get("id"))
            for ex in parent_exercises
            if isinstance(ex, dict)
        }
        if exercise_slug in existing_slugs:
            skipped.append({"kind": "exercise", "id": exercise_slug})
            continue
        parent_exercises.append(exercise)
        added.append({"kind": "exercise", "id": exercise_slug, "module_id": parent_id})

    projects = plan.setdefault("projects", [])
    existing_project_ids = {str(p.get("id")) for p in projects if isinstance(p, dict)}
    for entry in delta.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        proj_id = str(entry.get("id") or "")
        if not proj_id:
            continue
        if proj_id in existing_project_ids:
            skipped.append({"kind": "project", "id": proj_id})
            continue
        projects.append(entry)
        existing_project_ids.add(proj_id)
        added.append({"kind": "project", "id": proj_id})

    if added:
        plan.setdefault("research_provenance", []).append(
            {
                "applied_at": utc_now(),
                "added": added,
                "rationale": delta.get("rationale", ""),
            }
        )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    return {
        "present": True,
        "plan_path": str(plan_path),
        "added": added,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _skip(role: RoleConfig, status: str, detail: str | None) -> dict[str, Any]:
    return {
        "role": role.id,
        "status": status,
        "detail": detail,
    }


def _detect_outputs(learning_path: Path) -> dict[str, Any]:
    return {
        "job_requirements_md": (learning_path / JOB_REQUIREMENTS_MD).exists(),
        "job_requirements_json": (learning_path / JOB_REQUIREMENTS_JSON).exists(),
        "delta_present": (learning_path / CURRICULUM_PLAN_DELTA_FILE).exists(),
    }


def _invoke_agent(
    config: ResearchAgentConfig,
    prompt_path: Path,
    learning_path: Path,
    role: RoleConfig,
    runner_root: Path,
) -> AgentCommandResult:
    output_dir = learning_path / ".aicg" / "research" / "runs" / role.id
    output_dir.mkdir(parents=True, exist_ok=True)
    formatted = config.agent_command.format(  # type: ignore[union-attr]
        prompt=str(prompt_path),
        output_dir=str(output_dir),
        repo=str(learning_path),
        work_id=f"research:{role.id}",
        runner=str(runner_root),
    )
    result = run_agent_command(formatted, cwd=learning_path)
    # The wrapper redirects claude's stdout into response.md, so the
    # rate-limit JSONL line never reaches `result.stdout`. Peek into
    # the response file to catch the limit before it surfaces as
    # `agent_failed` (Bug 2).
    return reclassify_with_response_file(
        result, output_dir / "response.md", output_dir / "response.json"
    )


# =====================================================================
# W2.2b — new-format (v2) handler for canary roles
# =====================================================================


def _handle_new_format_role(
    *,
    role: "RoleConfig",
    learning_path: Path,
    runner_root: Path,
    month: str,
    open_pr: bool,
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Process a canary role's agent output as a curriculum_plan_delta v2.

    Looks for ``.aicg/curriculum-plan-delta-v2.json`` in the learning
    repo. Validates against the per-role manifest in the content-
    generator repo. Renders a proposal markdown summarizing the delta
    (including any validator flags). Opens a PR with the
    ``curriculum-plan-v2`` label, plus ``requires-explicit-approval`` if
    the validator flagged the delta.
    """
    from .curriculum_plan import load_curriculum_plan
    from .curriculum_plan_delta import (
        CurriculumPlanDeltaError,
        load_delta,
        validate_delta,
    )

    delta_path = learning_path / CURRICULUM_PLAN_DELTA_V2_FILE
    if not delta_path.exists():
        return {
            "role": role.id,
            "status": "applied_no_delta",
            "outputs": outputs,
            "note": (
                "Canary role: agent did not produce "
                f"`{CURRICULUM_PLAN_DELTA_V2_FILE}`. Either no proposal this "
                "cycle (the expected steady-state) or the agent's prompt did "
                "not request v2 output."
            ),
            "format": "v2",
        }

    baseline_path = runner_root / _PER_ROLE_MANIFEST_RELATIVE.format(role=role.id)
    if not baseline_path.exists():
        return {
            "role": role.id,
            "status": "baseline_missing",
            "outputs": outputs,
            "note": f"Per-role manifest not found at {baseline_path}",
            "format": "v2",
        }

    try:
        baseline = load_curriculum_plan(baseline_path)
        delta = load_delta(delta_path)
        validated = validate_delta(delta, baseline)
    except CurriculumPlanDeltaError as exc:
        return {
            "role": role.id,
            "status": "delta_rejected",
            "outputs": outputs,
            "format": "v2",
            "error": str(exc),
            "delta_path": str(delta_path),
        }

    proposal = _write_new_format_proposal(
        learning_path=learning_path,
        role=role,
        month=month,
        baseline=baseline,
        validated=validated,
    )

    pr_outcome: dict[str, Any] | None = None
    if open_pr and (validated.additions or validated.updates or validated.removals):
        labels = [_LABEL_NEW_FORMAT]
        if validated.requires_explicit_approval:
            labels.append(_LABEL_REQUIRES_APPROVAL)
        pr_outcome = _open_proposal_pr_v2(
            learning_path=learning_path,
            role=role,
            month=month,
            proposal_paths=proposal["written"],
            labels=labels,
        )

    return {
        "role": role.id,
        "status": "proposal_ready",
        "format": "v2",
        "outputs": outputs,
        "validation": {
            "additions": len(validated.additions),
            "updates": len(validated.updates),
            "removals": len(validated.removals),
            "requires_explicit_approval": validated.requires_explicit_approval,
            "notes": list(validated.validation_notes),
        },
        "proposal_files": proposal["written"],
        "pr": pr_outcome,
    }


def _write_new_format_proposal(
    *,
    learning_path: Path,
    role: "RoleConfig",
    month: str,
    baseline,  # CurriculumPlan
    validated,  # CurriculumPlanDelta
) -> dict[str, Any]:
    """Render the per-cycle proposal markdown for v2 canary deltas."""
    md_path = learning_path / f"RESEARCH_PROPOSAL_{month}.md"
    md_path.write_text(
        _render_new_format_proposal_markdown(role, month, baseline, validated),
        encoding="utf-8",
    )
    return {"written": [str(md_path)]}


def _render_new_format_proposal_markdown(
    role: "RoleConfig",
    month: str,
    baseline,  # CurriculumPlan
    validated,  # CurriculumPlanDelta
) -> str:
    flag_line = (
        "**Requires explicit approval — reviewer must confirm scope before merging.**"
        if validated.requires_explicit_approval
        else "Validator did not flag this delta for explicit approval."
    )
    notes_block = (
        "\n".join(f"- {n}" for n in validated.validation_notes) or "_(none)_"
    )

    lines = [
        f"# Research Proposal — {role.title} — {month}",
        "",
        f"Format: **curriculum-plan v2** (per-role manifest at "
        f"`manifest/curriculum_plan.{role.id}.manifest.json` in the "
        "content-generator repo).",
        "",
        f"## Summary",
        "",
        f"- Baseline requirement count: **{baseline.requirement_count}**",
        f"- Additions: **{len(validated.additions)}**",
        f"- Updates: **{len(validated.updates)}**",
        f"- Removals: **{len(validated.removals)}**",
        "",
        f"## Continuity check",
        "",
        flag_line,
        "",
        "Validator notes:",
        "",
        notes_block,
        "",
        "## Rationale",
        "",
        validated.rationale or "_(no rationale provided by agent)_",
        "",
    ]

    if validated.additions:
        lines += ["## Additions", ""]
        for a in validated.additions:
            req = a.requirement
            freq = f"{req.frequency:.0%}" if req.frequency is not None else "?"
            ev = len(req.evidence)
            lines.append(
                f"- `{req.id}` — **{req.label}** "
                f"(frequency {freq}, {ev} evidence item(s))"
            )
            if a.rationale:
                lines.append(f"  - Rationale: {a.rationale}")
        lines.append("")

    if validated.updates:
        lines += ["## Updates", ""]
        for u in validated.updates:
            parts: list[str] = []
            if u.label is not None:
                parts.append(f"label→`{u.label}`")
            if u.frequency is not None:
                parts.append(f"freq→{u.frequency:.0%}")
            if u.coverage_status is not None:
                parts.append(f"coverage→{u.coverage_status}")
            if u.evidence_add:
                parts.append(f"+{len(u.evidence_add)} evidence")
            if u.exercises_add or u.projects_add or u.solutions_add or u.tests_add:
                parts.append(
                    "+links "
                    f"(+{len(u.exercises_add)} ex, "
                    f"+{len(u.projects_add)} proj, "
                    f"+{len(u.solutions_add)} sol, "
                    f"+{len(u.tests_add)} tests)"
                )
            lines.append(f"- `{u.id}` — " + ("; ".join(parts) or "_(no fields changed)_"))
        lines.append("")

    if validated.removals:
        lines += ["## Removals", ""]
        for r in validated.removals:
            note = r.migration_note or "_(no migration note)_"
            lines.append(f"- `{r.id}` — {note}")
        lines.append("")

    lines += [
        "## How to apply (after approval)",
        "",
        "```sh",
        "aicg org plan-delta-apply \\",
        f"  --role {role.id} \\",
        f"  --delta .aicg/curriculum-plan-delta-v2.json",
        "```",
        "",
        "The CLI re-validates the delta against the current baseline. If the "
        "validator's flags or rejects have changed since this proposal was "
        "written (e.g., new postings landed in another role), the apply will "
        "surface them before writing.",
    ]
    return "\n".join(lines) + "\n"


def _open_proposal_pr_v2(
    *,
    learning_path: Path,
    role: "RoleConfig",
    month: str,
    proposal_paths: list[str],
    labels: list[str],
) -> dict[str, Any]:
    """Open a PR for a v2 canary research proposal. ``gh`` shell-outs only."""
    import shlex
    import subprocess

    branch = f"research/{month}/{role.id}"
    title = f"research: monthly cycle for {role.id} ({month})"
    body_lines = [
        f"Auto-generated v2 research proposal for **{role.title}** — {month}.",
        "",
        f"See `RESEARCH_PROPOSAL_{month}.md` for the diff summary, "
        "validator notes, and apply command.",
        "",
    ]
    if _LABEL_REQUIRES_APPROVAL in labels:
        body_lines += [
            "⚠️ **Validator auto-flagged this delta** "
            "(`requires_explicit_approval: true`). "
            "Reviewer must explicitly confirm scope before merging — see the "
            "validator notes in the proposal markdown.",
            "",
        ]

    body = "\n".join(body_lines)
    # PR creation does NOT pass --label flags (gh hard-fails on missing
    # labels). Labels are added in a follow-up step that tolerates
    # failure per label.
    steps = [
        # Stash residue first — without this, `git checkout -B` complains
        # about uncommitted changes and `gh pr create` warns about them.
        [
            "git",
            "-C",
            str(learning_path),
            "stash",
            "push",
            "--include-untracked",
            "--message",
            f"aicg-research-residue-{month}-{role.id}",
        ],
        ["git", "-C", str(learning_path), "checkout", "-B", branch],
        ["git", "-C", str(learning_path), "add", *proposal_paths, CURRICULUM_PLAN_DELTA_V2_FILE],
        ["git", "-C", str(learning_path), "commit", "-m", title],
        ["git", "-C", str(learning_path), "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--base", "main", "--title", title, "--body", body],
    ]
    trail: list[dict[str, Any]] = []
    pr_url: str | None = None
    pr_created = False
    for cmd in steps:
        completed = subprocess.run(
            cmd, cwd=learning_path, capture_output=True, text=True, check=False
        )
        is_stash = cmd[3:4] == ["stash"] if len(cmd) > 3 else False
        trail.append(
            {
                "step": cmd[1] if cmd[0] == "git" else cmd[0],
                "argv": shlex.join(cmd[-3:]),
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-300:],
                "stderr_tail": completed.stderr[-300:],
            }
        )
        if cmd[:3] == ["gh", "pr", "create"] and completed.returncode == 0:
            pr_url = completed.stdout.strip()
            pr_created = True
        # `git commit` returning non-zero is fine if nothing changed.
        # `git stash push` returning non-zero usually means "nothing to
        # stash", which is the expected steady state.
        if completed.returncode != 0 and cmd[1] != "commit" and not is_stash:
            return {"status": "pr_failed", "branch": branch, "steps": trail}

    # Best-effort labels — added per-label AFTER the PR is created so a
    # missing label can't block PR creation.
    if pr_created and labels:
        for label in labels:
            cmd = ["gh", "pr", "edit", branch, "--add-label", label]
            completed = subprocess.run(
                cmd, cwd=learning_path, capture_output=True, text=True, check=False
            )
            trail.append(
                {
                    "step": f"add_label:{label}",
                    "argv": shlex.join(cmd[-3:]),
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-300:],
                    "stderr_tail": completed.stderr[-300:],
                }
            )

    return {
        "status": "pr_opened",
        "branch": branch,
        "pr_url": pr_url,
        "labels": labels,
        "steps": trail,
    }


def _pending_proposal_pr(role: "RoleConfig") -> str | None:
    """Return the open proposal-PR URL for ``role``, or None.

    Queries GitHub via ``gh pr list`` (cheap — single API call per
    role). We match on branch name prefix rather than label so the
    check still works on repos that never had the aicg labels
    created. Returns the URL of the most-recently-updated open match.

    Failures (gh not on PATH, network error, repo missing) return
    ``None`` so the worst case is "we run the agent anyway."
    """
    import subprocess

    repo_full = role.learning_repo
    # Some manifests carry the repo as a slug; others as owner/slug.
    # `gh pr list -R` accepts either, but the search field below needs
    # just the branch substring.
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        f"ai-infra-curriculum/{repo_full}" if "/" not in repo_full else repo_full,
        "--state",
        "open",
        "--json",
        "url,headRefName,updatedAt",
        "--limit",
        "20",
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        rows = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return None

    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(
            str(row.get("headRefName", "")).startswith(prefix)
            for prefix in _OPEN_PR_BRANCH_PREFIXES
        )
        and f"/{role.id}" in str(row.get("headRefName", ""))
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("updatedAt", ""), reverse=True)
    return str(matches[0].get("url", "")) or None
