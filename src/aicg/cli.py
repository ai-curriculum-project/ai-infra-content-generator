"""Command-line interface for AICG."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_cli import AgentLimitReached
from .audit import AuditError, audit_repo
from .bootstrap import (
    BootstrapError,
    CurriculumPlanError,
    bootstrap_role,
    execute_curriculum_plan,
)
from .diff import diff_repo
from .generator import GenerationNotConfigured, generate_all_pending, generate_from_plan
from .gitops import GitOpsError, prepare_pr
from .inventory import InventoryError, WorkspaceInventory, default_workspace
from .org_config import ManifestError, load_manifest, state_dir_for_manifest
from .org_runner import (
    OrgRunnerError,
    generate_research_packets,
    plan_monthly_release,
    run_daily_remediation,
    run_org_audit,
    steward_report,
    sync_repositories,
)
from .planner import plan_from_audit
from .propagate import PropagateError, propagate_repo
from .state import read_state
from .validator import validate_repo
from .verify import VerifyError, verify_repo


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        AuditError,
        BootstrapError,
        CurriculumPlanError,
        GitOpsError,
        InventoryError,
        ManifestError,
        OrgRunnerError,
        PropagateError,
        VerifyError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicg", description="AI curriculum runner")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace containing paired curriculum repos. Defaults to the parent of this repo.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="Audit curriculum repo structure and guardrails")
    add_repo_args(audit_parser)
    audit_parser.set_defaults(func=cmd_audit)

    plan_parser = subparsers.add_parser("plan", help="Turn audit gaps into deterministic work items")
    add_repo_args(plan_parser)
    plan_parser.set_defaults(func=cmd_plan)

    generate_parser = subparsers.add_parser("generate", help="Write prompt packet and invoke local agent")
    add_repo_args(generate_parser)
    generate_parser.add_argument("--work-id", default=None, help="Specific work item to generate")
    generate_parser.add_argument(
        "--all",
        action="store_true",
        help="Drain every pending work item. Stops on subscription-limit or missing-config.",
    )
    generate_parser.add_argument(
        "--config",
        type=Path,
        action="append",
        default=[],
        help="Additional aicg.yaml/json config path containing generator_command.",
    )
    generate_parser.set_defaults(func=cmd_generate)

    validate_parser = subparsers.add_parser("validate", help="Run objective validation checks")
    add_repo_args(validate_parser)
    validate_parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0 after writing the validation report.",
    )
    validate_parser.set_defaults(func=cmd_validate)

    audit_links_parser = subparsers.add_parser(
        "audit-links",
        help="HEAD-ping every external URL in committed markdown; emit refresh_links work items",
    )
    add_repo_args(audit_links_parser)
    audit_links_parser.add_argument(
        "--timeout", type=float, default=8.0, help="Per-URL HEAD timeout seconds (default 8)."
    )
    audit_links_parser.add_argument(
        "--workers", type=int, default=16, help="Concurrent HEAD requests (default 16)."
    )
    audit_links_parser.set_defaults(func=cmd_audit_links)

    audit_versions_parser = subparsers.add_parser(
        "audit-versions",
        help="Scan committed markdown for stale version pins from a curated registry",
    )
    add_repo_args(audit_versions_parser)
    audit_versions_parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to version-targets.yaml. Defaults to config/version-targets.yaml.",
    )
    audit_versions_parser.set_defaults(func=cmd_audit_versions)

    propagate_parser = subparsers.add_parser(
        "propagate",
        help="Append shipped work items to VERSIONS.md (and suggest CURRICULUM.md edits)",
    )
    add_repo_args(propagate_parser)
    propagate_parser.add_argument(
        "--work-id",
        default=None,
        help="Limit propagation to a single work item.",
    )
    propagate_parser.set_defaults(func=cmd_propagate)

    diff_parser = subparsers.add_parser(
        "diff",
        help="Show what the agent changed for a work item",
    )
    add_repo_args(diff_parser)
    diff_parser.add_argument(
        "--work-id",
        default=None,
        help="Limit expected-path matching to one work item.",
    )
    diff_parser.add_argument(
        "--full",
        action="store_true",
        help="Include the unified `git diff` output in the report.",
    )
    diff_parser.set_defaults(func=cmd_diff)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Confirm generated artifacts match the plan's output contract",
    )
    add_repo_args(verify_parser)
    verify_parser.add_argument(
        "--work-id",
        default=None,
        help="Verify a specific work item id; defaults to every item in the plan.",
    )
    verify_parser.add_argument(
        "--with-quality-grade",
        action="store_true",
        help="Invoke the configured judge to grade artifact quality.",
    )
    verify_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Org manifest path (only needed when --with-quality-grade is set).",
    )
    verify_parser.set_defaults(func=cmd_verify)

    pr_drive_parser = subparsers.add_parser(
        "pr-drive",
        help=(
            "Run the inline-merge loop against an existing PR: CI wait → "
            "self-heal failed checks → guardrails → reviews → merge."
        ),
    )
    add_repo_args(pr_drive_parser)
    pr_drive_parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="PR number to drive to merge.",
    )
    pr_drive_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Org manifest path. Defaults to config/aicg-org.yaml.",
    )
    pr_drive_parser.set_defaults(func=cmd_pr_drive)

    pr_parser = subparsers.add_parser("pr", help="Create guarded branch, commit, and GitHub PR")
    add_repo_args(pr_parser)
    pr_parser.add_argument(
        "--work-id",
        default=None,
        help="Specific work item id to PR. Defaults to the highest-priority item.",
    )
    pr_parser.add_argument("--auto-merge", action="store_true", help="Require auto-merge guardrails")
    pr_parser.set_defaults(func=cmd_pr)

    run_parser = subparsers.add_parser("run", help="Run a predefined orchestration loop")
    add_repo_args(run_parser)
    run_parser.add_argument("--mode", choices=["pilot"], default="pilot")
    run_parser.set_defaults(func=cmd_run)

    org_parser = subparsers.add_parser("org", help="Run org-level automation operations")
    org_subparsers = org_parser.add_subparsers(dest="org_command", required=True)

    org_sync = org_subparsers.add_parser("sync", help="Clone or fast-forward all manifest repos")
    add_org_args(org_sync)
    org_sync.add_argument("--dry-run", action="store_true", help="Print actions without running git.")
    org_sync.set_defaults(func=cmd_org_sync)

    org_release = org_subparsers.add_parser("release", help="Create monthly release tags")
    add_org_args(org_release)
    org_release.add_argument(
        "--apply",
        action="store_true",
        help="Actually create and push tags. Omit for dry-run planning.",
    )
    org_release.set_defaults(func=cmd_org_release)

    org_research = org_subparsers.add_parser(
        "research",
        help="Create monthly job-requirements research packets",
    )
    add_org_args(org_research)
    org_research.add_argument("--month", default=None, help="Month key such as 2026-05.")
    org_research.add_argument(
        "--apply",
        action="store_true",
        help="After writing prompts, invoke the configured agent on each role's packet so it updates JOB_REQUIREMENTS + curriculum-plan-delta in the learning repo.",
    )
    org_research.add_argument(
        "--no-pr",
        action="store_true",
        help="Skip opening proposal PRs. Useful for first-cycle inspection and tests; the proposal files still get written.",
    )
    org_research.add_argument(
        "--role",
        default=None,
        help=(
            "Process only this role id. Omit to process every role in the manifest. "
            "Used by per-role systemd timers (aicg-research-role@<slug>.timer)."
        ),
    )
    org_research.set_defaults(func=cmd_org_research)

    org_promote = org_subparsers.add_parser(
        "promote-plan",
        help="Apply a human-approved research proposal to curriculum-plan.json (run AFTER merging the proposal PR)",
    )
    add_org_args(org_promote)
    org_promote.add_argument(
        "--role",
        default=None,
        help="Role id to promote. Omit to promote every role with a pending proposal.",
    )
    org_promote.set_defaults(func=cmd_org_promote_plan)

    org_audit = org_subparsers.add_parser("audit", help="Audit all solution repos and write queue")
    add_org_args(org_audit)
    org_audit.set_defaults(func=cmd_org_audit)

    org_daily = org_subparsers.add_parser(
        "daily",
        help=(
            "Drive ready work items end-to-end. Drain mode is read from "
            "manifest.automation.daily_drain by default; override with "
            "--drain / --no-drain."
        ),
    )
    add_org_args(org_daily)
    org_daily.add_argument(
        "--drain",
        dest="drain",
        action="store_true",
        default=None,
        help="Process items until the queue is empty (or wall-clock cap fires).",
    )
    org_daily.add_argument(
        "--no-drain",
        dest="drain",
        action="store_false",
        help="Process exactly one item then exit (legacy mode).",
    )
    org_daily.add_argument(
        "--wall-clock-cap-seconds",
        type=int,
        default=None,
        help="Override the per-tick wall-clock cap (default 7200).",
    )
    org_daily.set_defaults(func=cmd_org_daily)

    org_execute_plan = org_subparsers.add_parser(
        "execute-plan",
        help="Scaffold module + project skeletons from a curriculum-plan.json",
    )
    add_org_args(org_execute_plan)
    org_execute_plan.add_argument(
        "--role", required=True, help="Role id whose plan should be executed."
    )
    org_execute_plan.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Optional explicit path to curriculum-plan.json (defaults to .aicg/ in the learning repo).",
    )
    org_execute_plan.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-scaffold modules/projects that already have on-disk content.",
    )
    org_execute_plan.set_defaults(func=cmd_org_execute_plan)

    org_generate_learning = org_subparsers.add_parser(
        "generate-learning",
        help="Author lecture + exercise content for a role's modules from its curriculum plan (no postings evidence required)",
    )
    add_org_args(org_generate_learning)
    org_generate_learning.add_argument(
        "--role", required=True, help="Role id whose learning content should be generated."
    )
    org_generate_learning.add_argument(
        "--module",
        default=None,
        help="Optional single module id (e.g. mod-101-foundations). Defaults to all modules in the plan.",
    )
    org_generate_learning.set_defaults(func=cmd_org_generate_learning)

    org_bootstrap = org_subparsers.add_parser(
        "bootstrap-role",
        help="Scaffold a new role's learning + solutions repos and research prompt",
    )
    add_org_args(org_bootstrap)
    org_bootstrap.add_argument(
        "--role",
        required=True,
        help="Role id (lowercase, hyphenated, e.g. 'data-engineer').",
    )
    org_bootstrap.add_argument("--title", required=True, help="Human-readable role title.")
    org_bootstrap.add_argument(
        "--level", type=int, required=True, help="Numeric seniority level (e.g. 25)."
    )
    org_bootstrap.add_argument(
        "--description",
        default=None,
        help="One-line description used inside the bootstrap prompt and READMEs.",
    )
    org_bootstrap.add_argument(
        "--overwrite",
        action="store_true",
        help="Scaffold even when the target directories already exist with content.",
    )
    org_bootstrap.add_argument(
        "--no-update-manifest",
        action="store_true",
        help="Skip appending the role to the org manifest (print a YAML snippet instead).",
    )
    org_bootstrap.add_argument(
        "--create-remotes",
        action="store_true",
        help="Create the matching GitHub repos via `gh repo create --push`.",
    )
    org_bootstrap.set_defaults(func=cmd_org_bootstrap_role)

    org_bootstrap_domain = org_subparsers.add_parser(
        "bootstrap-domain",
        help="Scaffold a whole domain: every role's repos + the org-profile README",
    )
    add_org_args(org_bootstrap_domain)
    org_bootstrap_domain.add_argument(
        "--tagline",
        default=None,
        help="One-line tagline for the org-profile README (defaults to a generated one).",
    )
    org_bootstrap_domain.add_argument(
        "--create-remotes",
        action="store_true",
        help="Create the GitHub repos (roles + .github profile) via `gh repo create --push`. "
        "The org itself must already exist.",
    )
    org_bootstrap_domain.set_defaults(func=cmd_org_bootstrap_domain)

    org_bootstrap_prompt = org_subparsers.add_parser(
        "bootstrap-prompt",
        help="(Re)write a role's research+plan prompt packet (no scaffold) for seeding",
    )
    add_org_args(org_bootstrap_prompt)
    org_bootstrap_prompt.add_argument("--role", required=True, help="Role id (must be in the manifest).")
    org_bootstrap_prompt.set_defaults(func=cmd_org_bootstrap_prompt)

    org_issues = org_subparsers.add_parser(
        "issues",
        help="Reconcile GitHub issues with the work-queue state (open / comment / close)",
    )
    add_org_args(org_issues)
    org_issues.add_argument(
        "--apply",
        action="store_true",
        help="Actually open / comment / close issues. Omit for dry-run.",
    )
    org_issues.add_argument(
        "--stuck-after",
        type=float,
        default=None,
        help="Hours an item must be deferred before it gets an issue (default 24).",
    )
    org_issues.set_defaults(func=cmd_org_issues)

    org_steward = org_subparsers.add_parser(
        "steward",
        help="Inspect open PRs and merge ones that clear CI + guardrails",
    )
    add_org_args(org_steward)
    org_steward.add_argument(
        "--apply",
        action="store_true",
        help="Actually merge eligible PRs. Omit for dry-run / planning.",
    )
    org_steward.add_argument(
        "--ci-timeout",
        type=int,
        default=600,
        help="Per-PR CI rollup timeout in seconds (default 600).",
    )
    org_steward.add_argument(
        "--ci-poll",
        type=int,
        default=30,
        help="Per-PR CI rollup poll interval in seconds (default 30).",
    )
    org_steward.add_argument(
        "--merge-method",
        choices=["squash", "merge", "rebase"],
        default="squash",
        help="Merge strategy for `gh pr merge --auto` (default squash).",
    )
    org_steward.set_defaults(func=cmd_org_steward)

    org_discussions = org_subparsers.add_parser(
        "discussions",
        help="Summarize open GitHub Discussions across the org (dry-run only)",
    )
    add_org_args(org_discussions)
    org_discussions.set_defaults(func=cmd_org_discussions)

    org_list_roles = org_subparsers.add_parser(
        "list-roles",
        help="Print role ids from the manifest, one per line (for shell loops).",
    )
    add_org_args(org_list_roles)
    org_list_roles.add_argument(
        "--with-repos",
        action="store_true",
        help="Print tab-separated 'id<TAB>learning_repo<TAB>solution_repo|-<TAB>repo_model' "
        "per role (for the timer installer). Solution column is '-' for single-repo curricula.",
    )
    org_list_roles.set_defaults(func=cmd_org_list_roles)

    org_audit_learning = org_subparsers.add_parser(
        "audit-learning",
        help="Structural audit of every learning repo in the manifest",
    )
    add_org_args(org_audit_learning)
    org_audit_learning.set_defaults(func=cmd_org_audit_learning)

    org_audit_pairing = org_subparsers.add_parser(
        "audit-pairing",
        help="Compare each learning repo to its paired solutions repo for alignment",
    )
    add_org_args(org_audit_pairing)
    org_audit_pairing.set_defaults(func=cmd_org_audit_pairing)

    org_audit_curriculum = org_subparsers.add_parser(
        "audit-curriculum",
        help="Walk every repo's CURRICULUM.md / CURRICULUM_INDEX.md for completeness",
    )
    add_org_args(org_audit_curriculum)
    org_audit_curriculum.set_defaults(func=cmd_org_audit_curriculum)

    org_audit_profile = org_subparsers.add_parser(
        "audit-profile",
        help="Audit the .github org-profile docs for staleness vs the manifest",
    )
    add_org_args(org_audit_profile)
    org_audit_profile.set_defaults(func=cmd_org_audit_profile)

    org_rebrand = org_subparsers.add_parser(
        "rebrand",
        help=(
            "One-shot org-wide maintainer footer (driven by "
            "manifest.maintained_by). Idempotent; opens one PR per "
            "repo so each footer addition can be reviewed."
        ),
    )
    add_org_args(org_rebrand)
    org_rebrand.add_argument(
        "--apply",
        action="store_true",
        help="Actually write files + commit + open PRs. Omit for dry-run.",
    )
    org_rebrand.add_argument(
        "--repo",
        action="append",
        default=None,
        help=(
            "Limit the rebrand to one or more specific repos (repeat "
            "the flag). Default: every manifest repo."
        ),
    )
    org_rebrand.set_defaults(func=cmd_org_rebrand)

    org_dependabot = org_subparsers.add_parser(
        "dependabot",
        help=(
            "Sweep Dependabot PRs across org repos: enable auto-merge on "
            "clean ones, post @dependabot rebase on stale ones, escalate "
            "after 3 fruitless rebase requests."
        ),
    )
    add_org_args(org_dependabot)
    org_dependabot.add_argument(
        "--apply",
        action="store_true",
        help="Actually post comments and enable auto-merge. Omit for dry-run.",
    )
    org_dependabot.set_defaults(func=cmd_org_dependabot)

    org_audit_links = org_subparsers.add_parser(
        "audit-links",
        help="Run the link checker against every solution repo in the manifest",
    )
    add_org_args(org_audit_links)
    org_audit_links.add_argument("--timeout", type=float, default=8.0)
    org_audit_links.add_argument("--workers", type=int, default=16)
    org_audit_links.set_defaults(func=cmd_org_audit_links)

    org_audit_versions = org_subparsers.add_parser(
        "audit-versions",
        help="Run the version-pin scanner against every solution repo in the manifest",
    )
    add_org_args(org_audit_versions)
    org_audit_versions.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="version-targets.yaml. Defaults to config/version-targets.yaml.",
    )
    org_audit_versions.set_defaults(func=cmd_org_audit_versions)

    org_review = org_subparsers.add_parser(
        "review",
        help="LLM freshness review of committed artifacts across all solution repos",
    )
    add_org_args(org_review)
    org_review.add_argument(
        "--max-artifacts",
        type=int,
        default=None,
        help="Cap per-repo artifacts reviewed in this run (default: unlimited).",
    )
    org_review.add_argument(
        "--role",
        default=None,
        help=(
            "Process only this role's solution repo. Omit to review every "
            "solution repo. Used by per-role systemd timers "
            "(aicg-review-role@<slug>.timer)."
        ),
    )
    org_review.set_defaults(func=cmd_org_review)

    org_calibrate = org_subparsers.add_parser(
        "calibrate-judge",
        help="Score a labeled good/bad corpus to choose the eval-gate BAR empirically (P0)",
    )
    add_org_args(org_calibrate)
    org_calibrate.add_argument(
        "--corpus",
        default=None,
        help="Path to the calibration corpus dir (contains good/ and bad/ *.md artifacts). "
        "Defaults to calibration/<domain>/corpus (calibration/corpus for ai-infra).",
    )
    org_calibrate.add_argument(
        "--out",
        default=None,
        help="Optional path to write the full calibration report as JSON.",
    )
    org_calibrate.set_defaults(func=cmd_org_calibrate_judge)

    org_pipeline_status = org_subparsers.add_parser(
        "pipeline-status",
        help="Show which autonomous-pipeline phases (P0-P6) are enabled (read-only)",
    )
    add_org_args(org_pipeline_status)
    org_pipeline_status.set_defaults(func=cmd_org_pipeline_status)

    org_pipeline_tick = org_subparsers.add_parser(
        "pipeline-tick",
        help="Run every pipeline phase (P2-P5) in OBSERVE mode on real data — writes nothing",
    )
    add_org_args(org_pipeline_tick)
    org_pipeline_tick.add_argument(
        "--role", default=None, help="Role to load plan nodes from for the P2 re-audit slice."
    )
    org_pipeline_tick.set_defaults(func=cmd_org_pipeline_tick)

    org_list_domains = org_subparsers.add_parser(
        "list-domains",
        help="List registered domains/tenants (multi-tenant §2.2)",
    )
    org_list_domains.set_defaults(func=cmd_org_list_domains)

    org_plan_delta_apply = org_subparsers.add_parser(
        "plan-delta-apply",
        help="Apply a curriculum-plan delta to a per-role manifest (validates first; flags large changes for human approval).",
    )
    org_plan_delta_apply.add_argument(
        "--role",
        required=True,
        help="Role slug, e.g. junior-engineer. Selects which curriculum_plan.<slug>.manifest.json to mutate.",
    )
    org_plan_delta_apply.add_argument(
        "--delta",
        type=Path,
        required=True,
        help="Path to the curriculum-plan delta JSON to apply.",
    )
    org_plan_delta_apply.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional explicit path to the per-role manifest. Defaults to manifest/curriculum_plan.<role>.manifest.json next to the content-generator repo root.",
    )
    org_plan_delta_apply.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional explicit output path. Defaults to overwriting the baseline.",
    )
    org_plan_delta_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + print the rewritten manifest summary; do NOT write the file.",
    )
    org_plan_delta_apply.set_defaults(func=cmd_org_plan_delta_apply)

    org_plan_coverage = org_subparsers.add_parser(
        "plan-coverage",
        help="Print coverage breakdown for a per-role curriculum-plan manifest (text or JSON).",
    )
    org_plan_coverage.add_argument(
        "--role",
        required=True,
        help="Role slug, e.g. junior-engineer.",
    )
    org_plan_coverage.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional explicit path to the per-role manifest. Defaults to manifest/curriculum_plan.<role>.manifest.json next to the content-generator repo root.",
    )
    org_plan_coverage.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    org_plan_coverage.set_defaults(func=cmd_org_plan_coverage)

    org_discussions_refresh = org_subparsers.add_parser(
        "discussions-refresh",
        help="Fetch GitHub Discussions for a role and populate discussion_topics in its curriculum-plan manifest.",
    )
    org_discussions_refresh.add_argument(
        "--role",
        required=True,
        help="Role slug, e.g. junior-engineer.",
    )
    org_discussions_refresh.add_argument(
        "--repo",
        default=None,
        help="Override the learning repo to fetch (defaults to ai-infra-<role>-learning).",
    )
    org_discussions_refresh.add_argument(
        "--owner",
        default="ai-infra-curriculum",
        help="GitHub org. Default: ai-infra-curriculum.",
    )
    org_discussions_refresh.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional explicit per-role manifest path. Defaults to manifest/curriculum_plan.<role>.manifest.json.",
    )
    org_discussions_refresh.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache dir. Defaults to manifest/.cache/discussions/ (gitignored).",
    )
    org_discussions_refresh.add_argument(
        "--use-cache-only",
        action="store_true",
        help="Do not call gh; map purely from the cached threads file.",
    )
    org_discussions_refresh.add_argument(
        "--no-auto-enable",
        action="store_true",
        help="Do NOT auto-enable Discussions on the target repo if disabled.",
    )
    org_discussions_refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute mappings + print summary, but do not write the manifest.",
    )
    org_discussions_refresh.set_defaults(func=cmd_org_discussions_refresh)

    # Fleet: cross-domain roll-up (roadmap §4 control-plane, read-only).
    fleet_parser = subparsers.add_parser(
        "fleet", help="Cross-domain fleet operations (read-only roll-up)"
    )
    fleet_subparsers = fleet_parser.add_subparsers(dest="fleet_command", required=True)
    fleet_status = fleet_subparsers.add_parser(
        "status", help="Status roll-up across every registered domain"
    )
    fleet_status.set_defaults(func=cmd_fleet_status)
    fleet_digest = fleet_subparsers.add_parser(
        "digest", help="Compact daily fill-progress summary (for an ntfy push)"
    )
    fleet_digest.add_argument("--date", default="", help="Date label for the digest header.")
    fleet_digest.set_defaults(func=cmd_fleet_digest)

    return parser


def add_repo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        type=Path,
        default=argparse.SUPPRESS,
        help="Workspace containing paired curriculum repos.",
    )
    parser.add_argument("--repo", required=True, help="Target curriculum repository name")
    parser.add_argument("--module", default=None, help="Optional module id such as mod-001-...")


def add_org_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        type=Path,
        default=argparse.SUPPRESS,
        help="Workspace containing curriculum repos.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Org manifest path. Overrides --domain. Defaults to config/aicg-org.yaml.",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Domain/tenant to operate on (default: ai-infra). Resolves to "
        "config/domains/<domain>.yaml. See `aicg org list-domains`.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override org state directory.",
    )


def resolve_workspace(args: argparse.Namespace) -> Path:
    return (getattr(args, "workspace", None) or default_workspace()).resolve()


def target_repo_path(workspace: Path, repo_name: str) -> Path:
    return WorkspaceInventory(workspace).require(repo_name).path


def resolve_manifest(args: argparse.Namespace):
    if getattr(args, "manifest", None):
        return load_manifest(args.manifest)
    from .domains import domain_config_path

    return load_manifest(domain_config_path(getattr(args, "domain", None)))


def resolve_org_state_dir(args: argparse.Namespace, manifest):
    return state_dir_for_manifest(manifest, args.state_dir)


def cmd_audit(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    report = audit_repo(workspace, args.repo, module=args.module)
    print_audit_summary(report)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    audit = audit_repo(workspace, args.repo, module=args.module)
    repo_path = target_repo_path(workspace, args.repo)
    plan = plan_from_audit(audit, repo_path=repo_path)
    print(f"Wrote work plan for {args.repo}: {plan['work_item_count']} item(s)")
    for item in plan["work_items"]:
        print(f"- {item['id']} ({len(item['exercises'])} exercise solution(s))")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    try:
        plan = read_state(repo_path, "work-plan.json")
    except FileNotFoundError:
        audit = audit_repo(workspace, args.repo, module=args.module)
        plan = plan_from_audit(audit, repo_path=repo_path)

    if args.all and args.work_id:
        print("error: --all and --work-id are mutually exclusive", file=sys.stderr)
        return 2

    if args.all:
        try:
            batch = generate_all_pending(
                repo_path,
                plan,
                module=args.module,
                config_paths=args.config,
            )
        except GenerationNotConfigured as exc:
            print("Prompt packet ready; no generator command configured.")
            print(f"Prompt: {exc.prompt_path}")
            print(f"Expected output directory: {exc.output_dir}")
            return 2
        print(
            f"Generated {batch['completed_count']}/{batch['pending_count']} pending work item(s)."
        )
        if batch.get("deferred"):
            print(
                f"Stopped at {batch['deferred']['work_id']}: subscription "
                f"limit ({batch['deferred']['limit_scope']}); retry after "
                f"{batch['deferred']['retry_after']}."
            )
            return 75
        return 0

    try:
        state = generate_from_plan(
            repo_path,
            plan,
            module=args.module,
            work_id=args.work_id,
            config_paths=args.config,
        )
    except GenerationNotConfigured as exc:
        print("Prompt packet ready; no generator command configured.")
        print(f"Prompt: {exc.prompt_path}")
        print(f"Expected output directory: {exc.output_dir}")
        return 2
    except AgentLimitReached as exc:
        print("Agent subscription limit reached; work deferred.")
        print(f"Retry after: {exc.result.retry_after}")
        return 75
    print(f"Generated work item {state['work_id']} with status {state['status']}")
    return 0


def cmd_audit_links(args: argparse.Namespace) -> int:
    from .freshness import audit_links

    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    report = audit_links(
        repo_path,
        timeout=args.timeout,
        max_workers=args.workers,
    )
    print(
        f"Link audit for {args.repo}: {report['broken_count']} broken / "
        f"{report['urls_unique']} unique URLs across {report['files_scanned']} file(s)"
    )
    for item in report["work_items"][:8]:
        print(f"  ! {item['severity']:>6}  {item['broken_count']:>2}  {item['path']}")
    return 0 if report["broken_count"] == 0 else 1


def cmd_audit_versions(args: argparse.Namespace) -> int:
    from .freshness import audit_versions

    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    registry = args.registry or _default_version_registry()
    report = audit_versions(repo_path, registry_path=registry)
    print(
        f"Version audit for {args.repo}: {report['finding_count']} stale ref(s) "
        f"across {report['target_count']} target(s)"
    )
    for item in report["work_items"][:8]:
        print(f"  ! {item['severity']:>6}  {item['target']:>14}  {item['path']}")
    return 0 if report["finding_count"] == 0 else 1


def _default_version_registry() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "version-targets.yaml"


def cmd_validate(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    report = validate_repo(workspace, args.repo, module=args.module)
    print(f"Validation {report['status']} for {args.repo}")
    for check in report["checks"]:
        print(f"- {check['name']}: {check['status']} ({check['finding_count']} finding(s))")
    if args.report_only:
        return 0
    return 0 if report["status"] == "passed" else 1


def cmd_propagate(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    report = propagate_repo(workspace, args.repo, work_id=args.work_id)
    if report["status"] == "no_items":
        print(f"Propagate noop: no verified work items in plan for {args.repo}.")
        return 0
    print(
        f"Propagated {len(report['updated'])} work item(s) to "
        f"{report['versions_path']}; {len(report['already_present'])} already present."
    )
    for entry in report["updated"][:8]:
        print(f"  + {entry['date']} {entry['work_id']} ({entry.get('module') or entry.get('project') or entry.get('type')})")
    for suggestion in report["curriculum_suggestions"][:4]:
        scope = suggestion.get("scope")
        if scope:
            print(f"  ~ CURRICULUM.md: review the row for `{scope}`.")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    report = diff_repo(repo_path, work_id=args.work_id, show_full=args.full)
    summary = report["summary"]
    print(
        f"Diff for {args.repo}: {summary['added']} added, "
        f"{summary['modified']} modified, {summary['deleted']} deleted, "
        f"{summary['untracked']} untracked "
        f"({summary['unexpected']} unexpected)"
    )
    for entry in report["entries"][:25]:
        tag = "  " if entry["expected"] else " !"
        print(
            f"{tag} {entry['status']:>9}  {entry['path']}"
            + (f"  ({entry['line_count']} lines)" if entry['line_count'] else "")
        )
        for line in entry["preview_head"][:6]:
            print(f"    | {line}")
        if entry["preview_tail"]:
            print("    | ...")
            for line in entry["preview_tail"][:4]:
                print(f"    | {line}")
    if args.full and report.get("full_diff"):
        print("\n--- full diff ---")
        print(report["full_diff"])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    judge_config = None
    if getattr(args, "with_quality_grade", False):
        from .judge import JudgeConfig

        manifest = load_manifest(args.manifest)
        judge_config = JudgeConfig.from_manifest(manifest)
        if not judge_config.enabled:
            print(
                "warning: --with-quality-grade requested but manifest's "
                "quality_judge.enabled is false; skipping judge invocation.",
                file=sys.stderr,
            )
    report = verify_repo(
        workspace,
        args.repo,
        work_id=args.work_id,
        judge_config=judge_config,
    )
    print(f"Verify {report['status']} for {args.repo} ({report['work_item_count']} item(s))")
    for item in report["work_items"]:
        print(f"- {item['work_id']}: {item['status']} ({item['finding_count']} finding(s))")
        for action in item["actions"][:4]:
            quality = action.get("quality") or {}
            quality_tag = (
                f"  q={quality['score']}/100"
                if isinstance(quality.get("score"), int)
                else ""
            )
            print(
                f"    {action['status']:>10}  {action['type']:<22}  "
                f"{action['path']}{quality_tag}"
            )
        if len(item["actions"]) > 4:
            print(f"    ... {len(item['actions']) - 4} more action(s) in .aicg/verify-report.json")
    return 0 if report["status"] in {"verified", "no_items"} else 1


def cmd_pr_drive(args: argparse.Namespace) -> int:
    """Run the inline-merge loop (with CI self-heal) on an existing PR."""
    import json as _json
    import subprocess as _sp

    from .org_runner import _drive_pr_to_merge

    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    manifest = load_manifest(args.manifest)

    # Look up the PR's url + head branch via gh.
    completed = _sp.run(
        [
            "gh",
            "pr",
            "view",
            str(args.pr),
            "--json",
            "url,headRefName,state",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(f"error: gh pr view {args.pr} failed: {completed.stderr.strip()}", file=sys.stderr)
        return 1
    try:
        pr = _json.loads(completed.stdout)
    except _json.JSONDecodeError as exc:
        print(f"error: could not parse gh pr view output: {exc}", file=sys.stderr)
        return 1

    if pr.get("state") != "OPEN":
        print(f"PR #{args.pr} is not OPEN (state={pr.get('state')}); nothing to do.")
        return 0

    print(
        f"Driving PR #{args.pr} ({pr['url']}) — branch `{pr['headRefName']}`. "
        "This may invoke the agent and push commits."
    )
    outcome = _drive_pr_to_merge(
        manifest=manifest,
        repo_path=repo_path,
        pr_url=pr["url"],
        branch=pr["headRefName"],
    )
    print(f"Outcome: {outcome.get('status')}")
    for entry in outcome.get("history", [])[-10:]:
        print(f"  - {entry}")
    if outcome.get("status") in {"merged", "auto_merge_enabled"}:
        return 0
    if outcome.get("status") in {"review_blocked", "guardrails_blocked"}:
        return 0  # not a failure — surfaces to the operator's attention
    return 1


def cmd_pr(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    repo_path = target_repo_path(workspace, args.repo)
    work_plan = read_state(repo_path, "work-plan.json")
    audit = read_state(repo_path, "audit-report.json")
    validation = read_state(repo_path, "validation-report.json")
    result = prepare_pr(
        repo_path,
        work_plan=work_plan,
        audit_report=audit,
        validation_report=validation,
        auto_merge=args.auto_merge,
        work_id=args.work_id,
    )
    print(f"Created PR: {result['pr_url']}")
    print(f"Branch: {result['branch']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args)
    audit = audit_repo(workspace, args.repo, module=args.module)
    repo_path = target_repo_path(workspace, args.repo)
    plan = plan_from_audit(audit, repo_path=repo_path)
    print_audit_summary(audit)
    print(f"Planned {plan['work_item_count']} work item(s).")
    if not plan["work_items"]:
        validate_report = validate_repo(workspace, args.repo, module=args.module)
        print(f"Validation {validate_report['status']}.")
        return 0 if validate_report["status"] == "passed" else 1
    try:
        run_state = generate_from_plan(repo_path, plan, module=args.module)
    except AgentLimitReached as exc:
        print("Pilot stopped because the content agent hit a subscription limit.")
        print(f"Retry after: {exc.result.retry_after}")
        return 75
    except GenerationNotConfigured as exc:
        print("Pilot stopped before content mutation because no generator command is configured.")
        print(f"Prompt: {exc.prompt_path}")
        return 2
    verify_report = verify_repo(
        workspace, args.repo, work_id=run_state.get("work_id")
    )
    print(f"Verify {verify_report['status']} ({verify_report['work_item_count']} item(s)).")
    if verify_report["status"] == "verified":
        propagate_report = propagate_repo(
            workspace, args.repo, work_id=run_state.get("work_id")
        )
        print(
            f"Propagate {propagate_report['status']}: "
            f"{len(propagate_report['updated'])} VERSIONS.md row(s) added."
        )
    return 0 if verify_report["status"] in {"verified", "no_items"} else 1


def cmd_org_sync(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    report = sync_repositories(manifest, resolve_workspace(args), dry_run=args.dry_run)
    print(f"Org sync {'dry-run' if args.dry_run else 'completed'}: {len(report['actions'])} repo(s)")
    for action in report["actions"]:
        print(f"- {action['repo']}: {action['status']} ({action['command']})")
    return 0


def cmd_org_release(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    report = plan_monthly_release(manifest, resolve_workspace(args), apply=args.apply)
    state_dir = resolve_org_state_dir(args, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    from .state import write_json

    write_json(state_dir / "monthly-release-plan.json", report)
    print(
        f"Monthly release {'applied' if args.apply else 'planned'} for tag {report['tag']}: "
        f"{len(report['actions'])} repo(s)"
    )
    for action in report["actions"]:
        print(f"- {action['repo']}: {action['status']}")
    return 0 if all(action["status"] != "failed" for action in report["actions"]) else 1


def cmd_org_research(args: argparse.Namespace) -> int:
    from .research import ResearchError, research_apply

    manifest = resolve_manifest(args)
    state_dir = resolve_org_state_dir(args, manifest)
    workspace = resolve_workspace(args)
    role_id = getattr(args, "role", None)
    try:
        report = generate_research_packets(
            manifest,
            workspace,
            month=args.month,
            state_dir=state_dir,
            role_id=role_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    scope = f" (role={role_id})" if role_id else ""
    print(
        f"Research packets ready for {report['month']}{scope}: "
        f"{len(report['packets'])} role(s)"
    )
    for packet in report["packets"]:
        print(f"- {packet['role']}: {packet['prompt_path']}")

    if not getattr(args, "apply", False):
        return 0

    try:
        apply_report = research_apply(
            manifest,
            workspace,
            month=args.month,
            state_dir=state_dir,
            open_pr=not getattr(args, "no_pr", False),
            role_id=role_id,
        )
    except ResearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    proposal_ready = sum(1 for r in apply_report["roles"] if r["status"] == "proposal_ready")
    no_delta = sum(1 for r in apply_report["roles"] if r["status"] == "applied_no_delta")
    deferred = sum(1 for r in apply_report["roles"] if r["status"] == "deferred")
    failed = sum(1 for r in apply_report["roles"] if r["status"] == "agent_failed")
    skipped = sum(
        1
        for r in apply_report["roles"]
        if r["status"] not in {"proposal_ready", "applied_no_delta", "deferred", "agent_failed"}
    )
    print(
        f"Research apply: {proposal_ready} proposal(s), {no_delta} no-delta, "
        f"{deferred} deferred, {failed} failed, {skipped} skipped."
    )
    for role in apply_report["roles"]:
        if role["status"] == "proposal_ready":
            summary = role.get("validation", {})
            counts = summary.get("accepted_counts", {})
            rejected = summary.get("rejected_count", 0)
            print(
                f"  + {role['role']}: proposal "
                f"(mods={counts.get('modules', 0)} "
                f"ex={counts.get('exercises', 0)} "
                f"proj={counts.get('projects', 0)} "
                f"rejected={rejected})"
            )
        elif role["status"] == "applied_no_delta":
            print(f"  = {role['role']}: requirements updated, no curriculum additions")
        elif role["status"] == "deferred":
            print(f"  ~ {role['role']}: deferred ({role.get('reason', '')})")
        elif role["status"] == "agent_failed":
            print(f"  ! {role['role']}: agent_failed (rc={role.get('returncode')})")
        else:
            print(f"  - {role['role']}: {role['status']}")
    return 0 if failed == 0 else 1


def cmd_org_promote_plan(args: argparse.Namespace) -> int:
    from .research import ResearchError, promote_plan

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    roles_to_promote = (
        [next((r for r in manifest.roles if r.id == args.role), None)]
        if args.role
        else list(manifest.roles)
    )
    if args.role and roles_to_promote[0] is None:
        print(f"error: role not in manifest: {args.role}", file=sys.stderr)
        return 1

    promoted_count = 0
    skipped_count = 0
    for role in roles_to_promote:
        if role is None:
            continue
        learning_path = workspace / role.learning_repo
        try:
            report = promote_plan(learning_path)
        except ResearchError:
            skipped_count += 1
            continue
        added = report.get("merge_report", {}).get("added", [])
        print(
            f"+ {role.id}: promoted ({len(added)} item(s) added to curriculum-plan.json)"
        )
        promoted_count += 1
    if skipped_count:
        print(f"({skipped_count} role(s) had no pending proposal — skipped)")
    return 0


def cmd_org_audit(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    queue = run_org_audit(
        manifest,
        resolve_workspace(args),
        state_dir=resolve_org_state_dir(args, manifest),
    )
    print(f"Org audit queued {queue['work_item_count']} work item(s)")
    for repo in queue["repo_reports"]:
        print(f"- {repo['repo']}: {repo['status']}")
    return 0


def cmd_org_daily(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    # Drain default: CLI flag wins, else manifest's automation.daily_drain,
    # else False (single-item per tick).
    drain = args.drain
    if drain is None:
        automation = manifest.automation or {}
        drain = bool(automation.get("daily_drain", False))
    kwargs = {"drain_until_empty": drain}
    if args.wall_clock_cap_seconds is not None:
        kwargs["wall_clock_cap_seconds"] = args.wall_clock_cap_seconds

    report = run_daily_remediation(
        manifest,
        resolve_workspace(args),
        state_dir=resolve_org_state_dir(args, manifest),
        **kwargs,
    )
    status = report.get("status", "no_items")
    items_processed = report.get("items_processed", 0)
    exit_reason = report.get("exit_reason", "n/a")
    print(
        f"Daily remediation: status={status} items_processed={items_processed} "
        f"exit_reason={exit_reason} (drain={drain})"
    )
    for item in report.get("items", [])[:10]:
        selected = item.get("selected") or {}
        print(
            f"  - {selected.get('repo','?')}/{selected.get('work_id','?')}: "
            f"{item.get('status','?')}"
        )
    return 0


def cmd_org_execute_plan(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    report = execute_curriculum_plan(
        manifest=manifest,
        workspace=resolve_workspace(args),
        role_id=args.role,
        plan_path=args.plan,
        overwrite=args.overwrite,
        state_dir=resolve_org_state_dir(args, manifest),
    )
    print(
        f"Executed curriculum plan for {report['role_id']}: "
        f"{len(report['modules_created'])} module(s) scaffolded, "
        f"{len(report['modules_skipped'])} skipped; "
        f"{len(report['projects_created'])} project(s) scaffolded, "
        f"{len(report['projects_skipped'])} skipped; "
        f"{report['files_written_count']} file(s) written."
    )
    if report["modules_created"]:
        print("Modules scaffolded:")
        for mod in report["modules_created"][:12]:
            print(f"  - {mod}")
        if len(report["modules_created"]) > 12:
            print(f"  ... {len(report['modules_created']) - 12} more")
    return 0


def cmd_org_generate_learning(args: argparse.Namespace) -> int:
    from .learning_content import LearningContentError, generate_role_learning_content

    manifest = resolve_manifest(args)
    try:
        report = generate_role_learning_content(
            manifest=manifest,
            workspace=resolve_workspace(args),
            role_id=args.role,
            module=args.module,
            state_dir=resolve_org_state_dir(args, manifest),
        )
    except LearningContentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Generated learning content for {report['role_id']}: "
        f"{len(report['modules_generated'])} module(s) authored, "
        f"{len(report['modules_failed'])} failed"
        + (f", deferred at {report['deferred_module']} (agent limit)" if report["deferred_module"] else "")
        + "."
    )
    for mod in report["modules_generated"]:
        print(f"  ✓ {mod}")
    for mod in report["modules_failed"]:
        print(f"  ✗ {mod}")
    return 0


def cmd_org_bootstrap_role(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    report = bootstrap_role(
        manifest=manifest,
        workspace=resolve_workspace(args),
        role_id=args.role,
        title=args.title,
        level=args.level,
        description=args.description,
        overwrite=args.overwrite,
        write_manifest=not args.no_update_manifest,
        create_remotes=args.create_remotes,
        state_dir=resolve_org_state_dir(args, manifest),
    )
    plan = report["plan"]
    print(
        f"Bootstrapped role '{plan['role_id']}' ({plan['title']}, level "
        f"{plan['level']})"
    )
    print(f"- Learning repo: {plan['learning_path']}")
    print(f"- Solutions repo: {plan['solution_path']}")
    print(f"- Prompt packet: {plan['prompt_path']}")
    manifest_update = report.get("manifest_update")
    if manifest_update and manifest_update.get("status") == "yaml_manifest_manual_update_required":
        print("Manifest is YAML; append this snippet under `roles:`:")
        print(manifest_update["snippet"])
    elif manifest_update and manifest_update.get("status") == "appended_json":
        print(f"- Manifest updated: {manifest_update['manifest_path']}")
    remotes = report.get("remotes")
    if remotes:
        for action in remotes.get("actions", []):
            outcome = "ok" if action["returncode"] == 0 else "failed"
            print(f"- gh repo create {action['repo']}: {outcome}")
    return 0


def cmd_org_bootstrap_prompt(args: argparse.Namespace) -> int:
    """(Re)write a role's bootstrap research+plan prompt packet (no scaffold)."""
    from .bootstrap import write_bootstrap_prompt

    manifest = resolve_manifest(args)
    path = write_bootstrap_prompt(
        manifest, args.role, state_dir=resolve_org_state_dir(args, manifest)
    )
    print(str(path))
    return 0


def cmd_org_bootstrap_domain(args: argparse.Namespace) -> int:
    """Scaffold an entire domain: every role's repos + the org-profile README.

    The one-command form of standing up a sibling org (roadmap §3). The GitHub
    org must already exist (org creation needs admin:org / the web UI). Roles
    come from the resolved domain manifest; each is scaffolded via the
    domain-aware bootstrap_role. With --create-remotes, repos and the .github
    profile are created and pushed; otherwise everything is written locally for
    review. Authoring CAREER_PROGRESSION.md is left as a follow-up (printed).
    """
    import subprocess

    from .bootstrap import _git_init_commit, bootstrap_role
    from .domain_provision import render_org_profile

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)

    print(f"Provisioning domain → org '{manifest.org}' ({len(manifest.roles)} roles)")
    for role in sorted(manifest.roles, key=lambda r: r.level):
        report = bootstrap_role(
            manifest=manifest,
            workspace=workspace,
            role_id=role.id,
            title=role.title,
            level=role.level,
            description=None,
            overwrite=False,
            write_manifest=False,  # role already in the domain config
            create_remotes=args.create_remotes,
            state_dir=state_dir,
        )
        remotes = report.get("remotes") or {}
        outcome = ""
        if args.create_remotes:
            oks = sum(1 for a in remotes.get("actions", []) if a["returncode"] == 0)
            outcome = f"  [remotes: {oks}/{len(remotes.get('actions', []))} ok]"
        print(f"  ✓ {role.id} (L{role.level}){outcome}")

    # Org-profile README under a local .github/profile/ tree.
    profile = render_org_profile(manifest, tagline=args.tagline)
    dotgithub = workspace / f"{manifest.org}-dotgithub"
    profile_path = dotgithub / "profile" / "README.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile, encoding="utf-8")
    print(f"  ✓ org profile written: {profile_path}")

    if args.create_remotes:
        init_err = _git_init_commit(dotgithub)
        if init_err is None:
            res = subprocess.run(
                ["gh", "repo", "create", f"{manifest.org}/.github", "--public",
                 "--source", str(dotgithub), "--remote", "origin", "--push"],
                capture_output=True, text=True, check=False,
            )
            print(f"  {'✓' if res.returncode == 0 else '✗'} gh repo create {manifest.org}/.github")
        else:
            print(f"  ✗ git init for .github failed: {init_err.stderr[-200:]}")

    print("\nNext steps:")
    print(f"  • Author {manifest.org}/.github/CAREER_PROGRESSION.md (the central ladder doc).")
    print("  • Add a per-domain calibration corpus to enable the quality judge (§2.3).")
    print("  • Leave pipeline phases OFF (observe-only) until the content settles.")
    return 0


def cmd_org_issues(args: argparse.Namespace) -> int:
    from .issues import IssuesError, issues_run

    manifest = resolve_manifest(args)
    try:
        report = issues_run(
            manifest=manifest,
            workspace=resolve_workspace(args),
            state_dir=resolve_org_state_dir(args, manifest),
            apply=args.apply,
            stuck_after_hours=args.stuck_after,
        )
    except IssuesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    mode = "apply" if args.apply else "dry-run"
    opened = sum(repo.get("opened", 0) for repo in report["repos"])
    commented = sum(repo.get("commented", 0) for repo in report["repos"])
    closed = sum(repo.get("closed", 0) for repo in report["repos"])
    print(
        f"Issues {mode}: {opened} opened, {commented} commented, {closed} closed."
    )
    for repo in report["repos"]:
        for decision in repo.get("decisions", [])[:6]:
            print(
                f"- {repo['repo']}/{decision['work_id']}: "
                f"{decision['action']} — {decision['reason']}"
            )
    return 0


def cmd_org_steward(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    report = steward_report(
        manifest,
        resolve_workspace(args),
        state_dir=resolve_org_state_dir(args, manifest),
        apply=args.apply,
        ci_timeout_seconds=args.ci_timeout,
        ci_poll_seconds=args.ci_poll,
        merge_method=args.merge_method,
    )
    merged = sum(repo.get("merged_count", 0) for repo in report.get("repos", []))
    blocked = sum(repo.get("blocked_count", 0) for repo in report.get("repos", []))
    failed = sum(repo.get("ci_failed_count", 0) for repo in report.get("repos", []))
    pr_count = sum(repo.get("pr_count", 0) for repo in report.get("repos", []))
    mode = "apply" if args.apply else "dry-run"
    print(
        f"Steward {mode}: {pr_count} open PR(s); merged={merged} "
        f"blocked={blocked} ci_failed={failed}"
    )
    for repo in report["repos"]:
        if not repo.get("pr_count"):
            continue
        for pr in repo.get("prs", []):
            print(f"- {repo['repo']}#{pr['pr_number']}: {pr['state']} — {pr.get('title', '')}")
    return 0


def cmd_org_audit_links(args: argparse.Namespace) -> int:
    from .freshness import audit_links
    from .state import write_json

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)

    all_work_items: list = []
    repo_summaries: list = []
    for repo in manifest.repo_names:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        report = audit_links(
            repo_path, timeout=args.timeout, max_workers=args.workers
        )
        all_work_items.extend(report["work_items"])
        repo_summaries.append(
            {
                "repo": repo,
                "files_scanned": report["files_scanned"],
                "urls_unique": report["urls_unique"],
                "broken_count": report["broken_count"],
                "work_items": len(report["work_items"]),
            }
        )

    org_report = {
        "schema_version": 1,
        "operation": "org_audit_links",
        "repo_count": len(repo_summaries),
        "broken_total": sum(r["broken_count"] for r in repo_summaries),
        "work_item_total": len(all_work_items),
        "repos": repo_summaries,
        "work_items": all_work_items,
    }
    write_json(state_dir / "freshness-links-report.json", org_report)
    print(
        f"Link audit: {org_report['broken_total']} broken across "
        f"{org_report['repo_count']} repo(s); {org_report['work_item_total']} work item(s)"
    )
    for repo in repo_summaries:
        if repo["broken_count"]:
            print(f"  ! {repo['repo']}: {repo['broken_count']} broken")
    return 0


def cmd_org_audit_versions(args: argparse.Namespace) -> int:
    from .freshness import audit_versions
    from .state import write_json

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = args.registry or _default_version_registry()

    all_work_items: list = []
    repo_summaries: list = []
    for repo in manifest.repo_names:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        report = audit_versions(repo_path, registry_path=registry)
        all_work_items.extend(report["work_items"])
        repo_summaries.append(
            {
                "repo": repo,
                "finding_count": report["finding_count"],
                "work_items": len(report["work_items"]),
            }
        )

    org_report = {
        "schema_version": 1,
        "operation": "org_audit_versions",
        "repo_count": len(repo_summaries),
        "finding_total": sum(r["finding_count"] for r in repo_summaries),
        "work_item_total": len(all_work_items),
        "repos": repo_summaries,
        "work_items": all_work_items,
    }
    write_json(state_dir / "freshness-versions-report.json", org_report)
    print(
        f"Version audit: {org_report['finding_total']} stale ref(s) across "
        f"{org_report['repo_count']} repo(s); {org_report['work_item_total']} work item(s)"
    )
    for repo in repo_summaries:
        if repo["finding_count"]:
            print(f"  ! {repo['repo']}: {repo['finding_count']} ref(s)")
    return 0


def cmd_org_list_domains(args: argparse.Namespace) -> int:
    """List registered domains/tenants (the default plus config/domains/*)."""
    from .domains import domain_config_path, list_domains

    domains = list_domains()
    print(f"Registered domains ({len(domains)}):")
    for d in domains:
        path = domain_config_path(d)
        mark = "✓" if path.exists() else "✗ missing"
        print(f"  {d:<28} {mark}  {path}")
    return 0


def cmd_fleet_status(args: argparse.Namespace) -> int:
    """Read-only liveness roll-up across every registered domain.

    Roadmap §4 (control plane) observability slice: one pane answering
    "how live is each domain?" — mode (ACT/OBSERVE/INERT), enabled phases,
    judge state + BAR, daily budget, and best-effort work-queue depth.
    Makes no writes and no network calls.
    """
    from .domains import domain_config_path, list_domains
    from .fleet import build_domain_status, read_queue_depth, render_fleet_table
    from .org_config import load_manifest, state_dir_for_manifest

    statuses = []
    for domain in list_domains():
        path = domain_config_path(domain)
        if not path.exists():
            continue
        manifest = load_manifest(path)
        queue = None
        try:
            queue = read_queue_depth(state_dir_for_manifest(manifest))
        except Exception:
            queue = None
        statuses.append(build_domain_status(domain, manifest, queue_depth=queue))

    print(render_fleet_table(statuses))
    return 0


def cmd_fleet_digest(args: argparse.Namespace) -> int:
    """Compact fill-progress digest across all domains (one line per domain).

    Counts roles whose learning repo has authored modules (lessons/mod-* or
    modules/mod-*). Read-only; meant to be pushed once daily via ntfy.
    """
    from .domains import domain_config_path, list_domains
    from .fleet import DigestRow, build_domain_status, render_fleet_digest
    from .org_config import load_manifest

    workspace = resolve_workspace(args)
    rows: list = []
    for domain in list_domains():
        path = domain_config_path(domain)
        if not path.exists():
            continue
        manifest = load_manifest(path)
        status = build_domain_status(domain, manifest)
        filled = 0
        for role in manifest.roles:
            lr = workspace / role.learning_repo
            if list(lr.glob("lessons/mod-*")) or list(lr.glob("modules/mod-*")):
                filled += 1
        rows.append(
            DigestRow(
                domain=domain,
                mode=status.mode,
                bar=status.freshness_bar,
                filled=filled,
                total=len(manifest.roles),
            )
        )
    print(render_fleet_digest(rows, date=args.date))
    return 0


def cmd_org_pipeline_tick(args: argparse.Namespace) -> int:
    """Run every pipeline phase's decision in OBSERVE mode (writes nothing).

    Loads real data (plan nodes, git changes) and reports what each phase WOULD
    do — the design's staged first form for the write phases (P4 is explicitly
    dry-run-first). Safe to run anytime; makes no autonomous changes.
    """
    import datetime
    import subprocess

    from .packager import RepoChange
    from .pipeline_config import PipelineConfig
    from .pipeline_tick import TickInputs, run_pipeline_tick
    from .rotation import ScanNode

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    pc = PipelineConfig.from_manifest(manifest)
    today = datetime.date.today().isoformat()
    version = "v" + datetime.date.today().strftime("%Y.%m")

    # P2 scan nodes: a role's plan modules (last_scan not tracked yet -> all eligible).
    scan_nodes: list[ScanNode] = []
    role_id = getattr(args, "role", None)
    if role_id:
        role = next((r for r in manifest.roles if r.id == role_id), None)
        if role is not None:
            plan_path = workspace / role.learning_repo / ".aicg" / "curriculum-plan.json"
            if plan_path.exists():
                import json

                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                scan_nodes = [ScanNode(m.get("id", ""), None) for m in plan.get("modules", [])]

    # P5 repo changes: changed-since-last-tag across all manifest repos.
    repo_changes: list[RepoChange] = []
    repos = list(manifest.learning_repo_names) + list(manifest.solution_repo_names)
    for repo in repos:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        try:
            tag = subprocess.run(
                ["git", "-C", str(repo_path), "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            if not tag:
                changed = True
            else:
                count = subprocess.run(
                    ["git", "-C", str(repo_path), "rev-list", f"{tag}..HEAD", "--count"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                changed = count not in ("", "0")
        except Exception:  # noqa: BLE001
            changed = False
        repo_changes.append(RepoChange(repo, changed_since_last_tag=changed))

    inputs = TickInputs(
        scan_nodes=scan_nodes, repo_changes=repo_changes, today=today, version=version
    )
    report = run_pipeline_tick(config=pc, inputs=inputs)

    print(f"Pipeline tick (OBSERVE — no writes) — {today}")
    for name, ph in report["phases"].items():
        flag = "ON" if ph.get("enabled") else "observe"
        print(f"  {name} [{flag}]")
        for k, v in ph.items():
            if k == "enabled":
                continue
            print(f"      {k}: {v}")
    return 0


def cmd_org_pipeline_status(args: argparse.Namespace) -> int:
    """Report the autonomous pipeline's staged-rollout state (read-only).

    Shows which P0-P6 phases are enabled (all autonomous-write phases default
    OFF), the budget/rotation knobs, and the P0 quality-judge state. Makes no
    changes — the operator's at-a-glance view of how 'live' the system is.
    """
    from .judge import JudgeConfig
    from .pipeline_config import PipelineConfig

    manifest = resolve_manifest(args)
    pc = PipelineConfig.from_manifest(manifest)
    jc = JudgeConfig.from_manifest(manifest)

    enabled = pc.enabled_phases()
    print("Autonomous pipeline — staged-rollout status")
    print(f"  enabled phases : {', '.join(enabled) if enabled else '(none — inert / observe-only)'}")
    print(f"  daily budget   : {pc.daily_budget} items")
    print(f"  rotation       : {pc.rotation_days}d   re-audit slice: {pc.reaudit_slice}")
    print(f"  budget shares  : {pc.budget_shares}")
    print(f"  heartbeat      : {pc.heartbeat_url or '(not configured — C-B2)'}")
    print(f"  quality_judge  : enabled={jc.enabled}  flag_only={jc.flag_only}")
    if not enabled and not jc.enabled:
        print("\n  Pipeline is fully inert. Next: run `aicg org calibrate-judge` to pick BAR,")
        print("  then enable quality_judge in flag-only mode (P0).")
    return 0


def cmd_org_calibrate_judge(args: argparse.Namespace) -> int:
    """Score a labeled known-good/known-bad corpus to choose BAR empirically.

    This is P0 of the autonomous pipeline: the eval-gate's safety case rests
    on the pass threshold being measured against ground truth, not guessed.
    It reads the corpus and reports the confusion + a suggested BAR; it makes
    no autonomous writes. The judge is force-enabled for the run regardless of
    the live ``quality_judge.enabled`` flag, since calibrating is the explicit
    point of the command.
    """
    import dataclasses

    from .calibration import CalibrationLimitError, run_calibration
    from .judge import JudgeConfig
    from .state import write_json

    manifest = resolve_manifest(args)
    judge_config = JudgeConfig.from_manifest(manifest)
    if not judge_config.agent_command:
        print(
            "error: quality_judge.agent_command is not configured; cannot "
            "calibrate (the judge needs a command to invoke).",
            file=sys.stderr,
        )
        return 1

    if args.corpus:
        corpus = Path(args.corpus)
    else:
        from .domains import calibration_corpus_path

        corpus = calibration_corpus_path(getattr(args, "domain", None))
    if not corpus.exists():
        print(f"error: corpus directory not found: {corpus}", file=sys.stderr)
        print(
            "  (author good/ and bad/ exemplar artifacts there, or pass --corpus)",
            file=sys.stderr,
        )
        return 1

    run_config = dataclasses.replace(judge_config, enabled=True)
    runner_root = Path(__file__).resolve().parents[2]
    try:
        report = run_calibration(corpus, judge_config=run_config, runner_root=runner_root)
    except CalibrationLimitError as exc:
        print(f"rate-limited: {exc}", file=sys.stderr)
        return 2  # distinct code: transient, retry after the limit resets
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    bar = report.threshold
    print(f"Calibration over {len(report.rows)} artifacts (current BAR={bar}):")
    print(f"  good scores: {sorted(report.good_scores, reverse=True)}")
    print(f"  bad scores:  {sorted(report.bad_scores, reverse=True)}")
    print(
        f"  confusion @BAR={bar}: true_pass={report.true_pass} "
        f"false_fail={report.false_fail} true_fail={report.true_fail} "
        f"false_pass={report.false_pass}"
    )
    print(
        f"  accuracy={report.accuracy:.0%}  separable={report.separable}  "
        f"suggested_BAR={report.suggested_bar}"
    )
    if report.false_pass:
        print(
            f"  WARNING: {report.false_pass} bad artifact(s) PASSED at BAR={bar} — "
            f"raise BAR toward {report.suggested_bar} before trusting the gate.",
            file=sys.stderr,
        )
    if getattr(args, "out", None):
        write_json(Path(args.out), report.as_dict())
        print(f"  wrote report → {args.out}")
    return 0


def cmd_org_review(args: argparse.Namespace) -> int:
    from .freshness import review_existing_artifacts
    from .judge import JudgeConfig
    from .state import write_json

    manifest = resolve_manifest(args)
    judge_config = JudgeConfig.from_manifest(manifest)
    if not judge_config.enabled:
        print(
            "warning: quality_judge.enabled is false; review will mark every "
            "artifact 'skipped'. Enable judge in the manifest to actually scan.",
            file=sys.stderr,
        )

    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)

    role_id = getattr(args, "role", None)
    if role_id is not None:
        role = next((r for r in manifest.roles if r.id == role_id), None)
        if role is None:
            valid = ", ".join(sorted(r.id for r in manifest.roles))
            print(
                f"error: Unknown role {role_id!r}. Known roles: {valid}",
                file=sys.stderr,
            )
            return 1
        # Both learning and solutions repos (or the single curriculum repo for
        # single-repo curricula — `role.repos` yields the right set). The
        # freshness rubric (api_currency / version_currency / citation_validity
        # / hardware_currency) matters more for learner-facing content (lecture
        # notes, exercise prompts) than for solutions, but both should be judged.
        repos_to_review = list(role.repos)
    else:
        # Legacy org-wide mode (called by aicg-monthly-review.timer,
        # which is now disabled in favor of per-role timers). Walk
        # both repo lists so the legacy invocation matches the new
        # per-role semantics.
        repos_to_review = (
            manifest.learning_repo_names + manifest.solution_repo_names
        )

    all_work_items: list = []
    repo_summaries: list = []
    for repo in repos_to_review:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        report = review_existing_artifacts(
            repo_path,
            judge_config=judge_config,
            max_artifacts=args.max_artifacts,
        )
        all_work_items.extend(report["work_items"])
        repo_summaries.append(
            {
                "repo": repo,
                "artifacts_reviewed": report["artifacts_reviewed"],
                "stale_count": report["stale_count"],
                "deferred": bool(report.get("deferred")),
                "work_items": len(report["work_items"]),
            }
        )

    org_report = {
        "schema_version": 1,
        "operation": "org_review",
        "role": role_id,
        "repo_count": len(repo_summaries),
        "stale_total": sum(r["stale_count"] for r in repo_summaries),
        "work_item_total": len(all_work_items),
        "repos": repo_summaries,
        "work_items": all_work_items,
    }
    # Per-role runs land in their own file so adjacent nightly timers
    # don't clobber each other. The unscoped (org-wide) file name is
    # preserved when --role is omitted.
    report_name = (
        f"freshness-review-report.{role_id}.json"
        if role_id
        else "freshness-review-report.json"
    )
    write_json(state_dir / report_name, org_report)
    scope = f" (role={role_id})" if role_id else ""
    print(
        f"Freshness review{scope}: {org_report['stale_total']} stale artifact(s) across "
        f"{org_report['repo_count']} repo(s); {org_report['work_item_total']} work item(s)"
    )
    for repo in repo_summaries:
        if repo["stale_count"]:
            print(
                f"  ! {repo['repo']}: {repo['stale_count']} stale of "
                f"{repo['artifacts_reviewed']} reviewed"
            )
    return 0


def cmd_org_audit_learning(args: argparse.Namespace) -> int:
    from .learning_audit import audit_learning_repo

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    total_gaps = 0
    repos_with_gaps = 0
    for repo in manifest.learning_repo_names:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        report = audit_learning_repo(repo_path)
        n = report["summary"]["gap_count"]
        total_gaps += n
        if n:
            repos_with_gaps += 1
            print(
                f"  ! {repo}: {n} gap(s) "
                f"({report['summary']['error_count']} error, "
                f"{report['summary']['warning_count']} warning)"
            )
    print(
        f"Learning audit: {total_gaps} gap(s) across "
        f"{repos_with_gaps} repo(s)"
    )
    return 0


def cmd_org_audit_pairing(args: argparse.Namespace) -> int:
    from .pairing_audit import audit_pairing

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    report = audit_pairing(manifest, workspace, state_dir=state_dir)
    print(
        f"Pairing audit: {report['finding_count']} mismatch(es) across "
        f"{report['role_count']} role(s) "
        f"(errors={report['by_severity']['error']}, "
        f"warnings={report['by_severity']['warning']})"
    )
    for role_report in report["roles"]:
        if role_report.get("finding_count"):
            print(f"  ! {role_report['role']}: {role_report['finding_count']}")
    return 0


def cmd_org_audit_curriculum(args: argparse.Namespace) -> int:
    from .nav_audit import audit_curriculum_nav

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    total_gaps = 0
    for repo in manifest.repo_names:
        repo_path = workspace / repo
        if not repo_path.exists():
            continue
        report = audit_curriculum_nav(repo_path)
        n = report["gap_count"]
        total_gaps += n
        if n:
            print(f"  ! {repo}: {n} nav drift(s)")
    print(f"Curriculum-nav audit: {total_gaps} drift(s)")
    return 0


def cmd_org_rebrand(args: argparse.Namespace) -> int:
    from .rebrand import rebrand_run

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    report = rebrand_run(
        manifest=manifest,
        workspace=workspace,
        state_dir=state_dir,
        apply=args.apply,
        repos=args.repo,
    )
    if report.get("status") == "skipped":
        print(f"Rebrand skipped: {report.get('reason')}")
        return 0
    mode = "apply" if args.apply else "dry-run"
    by_status: dict[str, int] = {}
    for r in report.get("repos", []):
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(
        f"Rebrand {mode}: "
        + ", ".join(f"{s}={n}" for s, n in sorted(by_status.items()))
    )
    for r in report.get("repos", [])[:20]:
        if r["status"] in {"already_branded", "skipped"}:
            continue
        print(f"  {r['repo']}: {r['status']}")
        if r.get("pr", {}).get("pr_url"):
            print(f"    {r['pr']['pr_url']}")
    return 0


def cmd_org_dependabot(args: argparse.Namespace) -> int:
    from .dependabot import dependabot_run

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    report = dependabot_run(
        manifest=manifest,
        workspace=workspace,
        state_dir=state_dir,
        apply=args.apply,
    )
    mode = "apply" if args.apply else "dry-run"
    total_prs = sum(r["pr_count"] for r in report["repos"])
    auto_merged = sum(r["auto_merged"] for r in report["repos"])
    rebased = sum(r["rebase_requested"] for r in report["repos"])
    escalated = sum(r["escalated"] for r in report["repos"])
    print(
        f"Dependabot sweep {mode}: {total_prs} PR(s) total; "
        f"auto-merge enabled on {auto_merged}, rebase requested on "
        f"{rebased}, escalated {escalated}."
    )
    for repo in report["repos"]:
        if not repo["pr_count"]:
            continue
        print(f"  {repo['repo']}:")
        for pr in repo["prs"][:5]:
            print(
                f"    #{pr['pr_number']} {pr['title'][:60]} → "
                f"action={pr.get('action')} status={pr.get('status')}"
            )
    return 0


def cmd_org_audit_profile(args: argparse.Namespace) -> int:
    from .nav_audit import audit_org_profile

    manifest = resolve_manifest(args)
    workspace = resolve_workspace(args)
    state_dir = resolve_org_state_dir(args, manifest)
    report = audit_org_profile(manifest, workspace, state_dir=state_dir)
    print(f"Org-profile audit: {report['gap_count']} finding(s)")
    for finding in report["findings"][:5]:
        print(f"  ! {finding['severity']:>7}  {finding['type']}: {finding['message'][:80]}")
    return 0


def cmd_org_list_roles(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args)
    with_repos = getattr(args, "with_repos", False)
    for role in sorted(manifest.roles, key=lambda item: item.level):
        if with_repos:
            print(
                f"{role.id}\t{role.learning_repo}\t{role.solution_repo or '-'}\t{role.repo_model}"
            )
        else:
            print(role.id)
    return 0


def cmd_org_discussions(args: argparse.Namespace) -> int:
    from .discussions import DiscussionsError, discussions_run

    manifest = resolve_manifest(args)
    try:
        report = discussions_run(
            manifest=manifest,
            workspace=resolve_workspace(args),
            state_dir=resolve_org_state_dir(args, manifest),
        )
    except DiscussionsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    totals = report["totals"]
    print(
        f"Discussions: {totals['discussion_count']} open across "
        f"{totals['repos_with_content']} repo(s); "
        f"{totals['needs_attention_count']} needing attention"
        + (
            f"; {totals['repos_with_errors']} fetch error(s)"
            if totals["repos_with_errors"]
            else ""
        )
    )
    for repo in report["repos"]:
        if not repo.get("needs_attention"):
            continue
        print(f"- {repo['repo']}: {repo['needs_attention_count']} flagged")
        for item in repo["needs_attention"][:5]:
            reasons = "; ".join(item.get("reasons", []))[:160]
            print(f"    #{item['number']} {item['title']} — {reasons}")
    return 0


def print_audit_summary(report: dict) -> None:
    summary = report["summary"]
    print(
        f"Audit {summary['status']} for {report['repo']['name']}: "
        f"{summary['error_count']} error(s), {summary['warning_count']} warning(s), "
        f"{summary['module_count']} module(s)"
    )
    for gap in report.get("gaps", [])[:12]:
        location = gap.get("expected_path") or gap.get("path") or gap.get("learning_path") or ""
        suffix = f" [{location}]" if location else ""
        print(f"- {gap['severity']}: {gap['type']}: {gap['message']}{suffix}")
    if len(report.get("gaps", [])) > 12:
        print(f"- ... {len(report['gaps']) - 12} more gap(s) in .aicg/audit-report.json")


def cmd_org_plan_delta_apply(args: argparse.Namespace) -> int:
    from .curriculum_plan import load_curriculum_plan
    from .curriculum_plan_delta import (
        CurriculumPlanDeltaError,
        apply_delta,
        load_delta,
        validate_delta,
    )

    repo_root = Path(__file__).resolve().parent.parent.parent
    manifest_dir = repo_root / "manifest"
    baseline_path = (
        args.baseline
        or manifest_dir / f"curriculum_plan.{args.role}.manifest.json"
    )
    out_path = args.out or baseline_path

    try:
        baseline = load_curriculum_plan(baseline_path)
    except Exception as exc:
        print(f"failed to load baseline {baseline_path}: {exc}", file=sys.stderr)
        return 2

    try:
        delta = load_delta(args.delta)
    except CurriculumPlanDeltaError as exc:
        print(f"failed to load delta {args.delta}: {exc}", file=sys.stderr)
        return 2

    if delta.role != args.role:
        print(
            f"delta.role={delta.role!r} does not match --role={args.role!r}",
            file=sys.stderr,
        )
        return 2

    try:
        validated = validate_delta(delta, baseline)
    except CurriculumPlanDeltaError as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 3

    new_plan = apply_delta(validated, baseline)
    diff = {
        "before": baseline.coverage_breakdown(),
        "after": new_plan.coverage_breakdown(),
        "added": len(validated.additions),
        "updated": len(validated.updates),
        "removed": len(validated.removals),
        "requires_explicit_approval": validated.requires_explicit_approval,
        "validation_notes": list(validated.validation_notes),
    }

    if args.dry_run:
        print("dry-run: would apply delta to", baseline_path)
        print(json.dumps(diff, indent=2))
        return 0

    from .curriculum_plan import write_curriculum_plan

    write_curriculum_plan(new_plan, out_path)
    print(f"applied delta to {out_path}")
    print(json.dumps(diff, indent=2))
    return 0


def cmd_org_plan_coverage(args: argparse.Namespace) -> int:
    from .curriculum_plan import load_curriculum_plan
    from .plan_coverage import coverage_report, render_text

    repo_root = Path(__file__).resolve().parent.parent.parent
    baseline_path = (
        args.baseline
        or repo_root / "manifest" / f"curriculum_plan.{args.role}.manifest.json"
    )

    try:
        plan = load_curriculum_plan(baseline_path)
    except Exception as exc:
        print(f"failed to load baseline {baseline_path}: {exc}", file=sys.stderr)
        return 2

    report = coverage_report(plan)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        sys.stdout.write(render_text(report))
    return 0


def cmd_org_discussions_refresh(args: argparse.Namespace) -> int:
    from .discussions_index import refresh_role_discussions

    repo_root = Path(__file__).resolve().parent.parent.parent
    baseline_path = (
        args.baseline
        or repo_root / "manifest" / f"curriculum_plan.{args.role}.manifest.json"
    )
    cache_dir = (
        args.cache_dir or repo_root / "manifest" / ".cache" / "discussions"
    )
    learning_repo = args.repo or f"ai-infra-{args.role}-learning"

    report = refresh_role_discussions(
        role=args.role,
        learning_repo=learning_repo,
        baseline_path=baseline_path,
        cache_dir=cache_dir,
        owner=args.owner,
        auto_enable=not args.no_auto_enable,
        use_cache_only=args.use_cache_only,
        write=not args.dry_run,
    )
    print(json.dumps(report, indent=2))
    if report.get("fetch", {}).get("error"):
        return 3
    return 0
