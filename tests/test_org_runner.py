from __future__ import annotations

import subprocess
from datetime import date

from conftest import make_security_workspace, write_minimal_manifest

from aicg.cli import main
from aicg.org_config import load_manifest
from aicg.org_runner import (
    generate_research_packets,
    is_git_dirty,
    plan_monthly_release,
    release_tag,
    run_org_audit,
    sync_repositories,
)


def test_manifest_loads_role_and_agent_policy(tmp_path):
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")

    manifest = load_manifest(manifest_path)

    assert manifest.repo_names == [
        "ai-infra-security-learning",
        "ai-infra-security-solutions",
        ".github",
    ]
    assert manifest.release_repo_names == [
        "ai-infra-security-learning",
        "ai-infra-security-solutions",
    ]
    assert manifest.automation["agent"]["model"] == "codex-gpt-5.5"
    assert manifest.automation["agent"]["interface"] == "local_cli_subscription"
    assert manifest.content_generation["agent"]["model"] == "claude-opus-4-7"
    assert manifest.content_generation["agent"]["interface"] == "local_cli_subscription"


def test_release_tag_uses_date_based_versioning(tmp_path):
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    assert release_tag(manifest, date(2026, 5, 27)) == "v2026.05"


def test_sync_repositories_dry_run_plans_clones(tmp_path):
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    workspace = tmp_path / "workspace"

    report = sync_repositories(manifest, workspace, dry_run=True)

    assert len(report["actions"]) == 3
    assert all(action["status"] == "planned" for action in report["actions"])
    assert "git clone" in report["actions"][0]["command"]


def test_monthly_release_dry_run_reports_missing_repos(tmp_path):
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = plan_monthly_release(manifest, tmp_path / "workspace", today=date(2026, 5, 27))

    assert report["tag"] == "v2026.05"
    assert len(report["actions"]) == 2
    assert {action["status"] for action in report["actions"]} == {"missing"}


def test_research_packets_include_claude_policy(tmp_path):
    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    report = generate_research_packets(
        manifest,
        workspace,
        month="2026-05",
        state_dir=tmp_path / "state",
    )

    prompt = report["packets"][0]["prompt_path"]
    assert report["packets"][0]["role"] == "security"
    with open(prompt, encoding="utf-8") as prompt_file:
        assert "claude-opus-4-7" in prompt_file.read()


def test_org_audit_writes_queue_from_solution_gaps(tmp_path):
    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))

    queue = run_org_audit(manifest, workspace, state_dir=tmp_path / "state")

    # The structural solution gap must be present at minimum; broader
    # audits (learning, pairing, nav, profile) may add more.
    assert queue["work_item_count"] >= 1
    solution_items = [
        item for item in queue["work_items"]
        if item.get("repo") == "ai-infra-security-solutions"
        and item.get("type") == "module_solution_gap"
    ]
    assert len(solution_items) == 1
    assert solution_items[0]["status"] == "ready"


def test_cli_org_research(tmp_path):
    workspace = make_security_workspace(tmp_path)
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    state_dir = tmp_path / "state"

    rc = main(
        [
            "org",
            "research",
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest_path),
            "--state-dir",
            str(state_dir),
            "--month",
            "2026-05",
        ]
    )

    assert rc == 0
    assert (state_dir / "job-research-plan.json").exists()


def test_aicg_state_does_not_make_repo_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / ".aicg").mkdir()
    (repo / ".aicg" / "audit-report.json").write_text("{}\n", encoding="utf-8")

    assert not is_git_dirty(repo)


def test_daily_remediation_defers_opaque_failure_with_retry_count(tmp_path):
    from aicg.org_runner import run_daily_remediation, run_org_audit

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"
    generator = tmp_path / "opaque.sh"
    generator.write_text(
        "#!/usr/bin/env bash\necho 'unknown error' >&2\nexit 1\n",
        encoding="utf-8",
    )
    generator.chmod(0o755)

    import aicg.org_runner as org_runner

    original = org_runner.content_generation_command
    org_runner.content_generation_command = lambda _m: str(generator)
    try:
        run_org_audit(manifest, workspace, state_dir=state_dir)
        summary = run_daily_remediation(
            manifest, workspace, state_dir=state_dir, drain_until_empty=False
        )
    finally:
        org_runner.content_generation_command = original

    first = summary["items"][0]
    assert first["status"] == "deferred"
    assert first["defer_reason"] == "opaque_generator_failure"
    assert first["retry_count"] == 1
    assert first.get("retry_after", "").endswith("Z")

    import json

    queue = json.loads((state_dir / "work-queue.json").read_text())
    item = next(item for item in queue["work_items"] if item.get("retry_count"))
    assert item["status"] == "deferred"
    assert item["retry_count"] == 1


def test_daily_remediation_fails_permanently_after_max_retries(tmp_path):
    import json

    from aicg.org_runner import run_daily_remediation, run_org_audit

    workspace = make_security_workspace(tmp_path)
    manifest_path = write_minimal_manifest(tmp_path / "aicg-org.yaml")
    payload = json.loads(manifest_path.read_text())
    payload["automation"]["opaque_retry"] = {
        "max_retries": 2,
        "retry_delay_minutes": 0,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    manifest = load_manifest(manifest_path)
    state_dir = tmp_path / "state"
    generator = tmp_path / "opaque.sh"
    generator.write_text(
        "#!/usr/bin/env bash\necho 'broken' >&2\nexit 1\n",
        encoding="utf-8",
    )
    generator.chmod(0o755)

    import aicg.org_runner as org_runner

    original = org_runner.content_generation_command
    org_runner.content_generation_command = lambda _m: str(generator)
    try:
        run_org_audit(manifest, workspace, state_dir=state_dir)
        first_summary = run_daily_remediation(
            manifest, workspace, state_dir=state_dir, drain_until_empty=False
        )
        # Manually flip the deferred item to ready so the second daily
        # picks it up immediately (retry_after has already passed
        # because we set retry_delay_minutes=0).
        queue_path = state_dir / "work-queue.json"
        queue = json.loads(queue_path.read_text())
        for item in queue["work_items"]:
            if item.get("status") == "deferred":
                item["status"] = "ready"
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        second_summary = run_daily_remediation(
            manifest, workspace, state_dir=state_dir, drain_until_empty=False
        )
    finally:
        org_runner.content_generation_command = original

    first = first_summary["items"][0]
    second = second_summary["items"][0]
    assert first["status"] == "deferred"
    assert second["status"] == "failed_permanently"
    assert second["retry_count"] == 2


def test_daily_remediation_skips_unhandled_work_types(tmp_path):
    """Unhandled types in the queue must not starve handled items.

    Regression: 289 ``refresh_links`` items sat at top priority with no
    handler, so the selector always picked one and deferred it,
    blocking the items that did have handlers from ever running.
    (Uses ``refresh_versions`` here because ``refresh_links`` is now
    handled by the deterministic link-refresh handler.)
    """
    import json as _json

    from aicg.org_runner import (
        HANDLED_WORK_TYPES,
        run_daily_remediation,
    )

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Hand-craft a queue with one unhandled item at top priority. Nothing
    # handled exists — selector should return the supplemental packet
    # and report skipped_unhandled_types=1, instead of picking the
    # unhandled item.
    assert "refresh_versions" not in HANDLED_WORK_TYPES
    queue = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "operation": "org_audit",
        "workspace": str(workspace),
        "work_item_count": 1,
        "repo_reports": [],
        "work_items": [
            {
                "id": "fake-unhandled-1",
                "type": "refresh_versions",
                "repo": "ai-infra-security-solutions",
                "status": "ready",
                "priority": -89900,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "title": "Refresh broken links",
                "work_id": "fake-unhandled-1",
            }
        ],
    }
    (state_dir / "work-queue.json").write_text(_json.dumps(queue))

    summary = run_daily_remediation(
        manifest, workspace, state_dir=state_dir, drain_until_empty=False
    )

    # Item must remain in the queue (not deferred, not consumed).
    queue_after = _json.loads(
        (state_dir / "work-queue.json").read_text(encoding="utf-8")
    )
    assert queue_after["work_items"][0]["status"] == "ready"
    # The supplemental-packet branch is what fires when nothing is
    # handled — it returns ``status: no_supplement`` because the empty
    # workspace has no supplemental work either. Either way, the
    # important contract is: skipped_unhandled_types == 1.
    assert summary.get("skipped_unhandled_types") == 1


def test_daily_remediation_picks_handled_over_higher_priority_unhandled(tmp_path):
    """Handled item must win even when an unhandled item outranks it."""
    import json as _json

    from aicg.org_runner import HANDLED_WORK_TYPES, run_daily_remediation

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    handled_type = next(
        t for t in ("pairing_mismatch", "curriculum_nav_drift") if t in HANDLED_WORK_TYPES
    )
    queue = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "operation": "org_audit",
        "workspace": str(workspace),
        "work_item_count": 2,
        "repo_reports": [],
        "work_items": [
            {
                "id": "unhandled-top",
                "type": "refresh_versions",
                "repo": "ai-infra-security-solutions",
                "status": "ready",
                "priority": -89900,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "title": "Refresh broken links",
                "work_id": "unhandled-top",
            },
            {
                "id": "handled-lower",
                "type": handled_type,
                "repo": "ai-infra-security-solutions",
                "status": "ready",
                "priority": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "title": "Cross-repo handled item",
                "work_id": "handled-lower",
                # Provide minimal fields the cross-repo dispatcher reads,
                # but we don't actually need to drive it to completion —
                # this test only asserts that the handled item is SELECTED.
                "subtype": "project_only_in_solutions",
            },
        ],
    }
    (state_dir / "work-queue.json").write_text(_json.dumps(queue))

    summary = run_daily_remediation(
        manifest, workspace, state_dir=state_dir, drain_until_empty=False
    )

    # Selector must have moved past the unhandled item.
    assert summary["items_processed"] == 1
    assert summary["items"][0]["selected"]["id"] == "handled-lower"
    assert summary.get("skipped_unhandled_types") == 1


def test_org_audit_preserves_refresh_links_details_when_promoting(tmp_path):
    """Regression: queue promotion stripped ``details`` so the link
    handler had no URLs to resolve."""
    import json as _json

    workspace = make_security_workspace(tmp_path)
    repo_path = workspace / "ai-infra-security-solutions"
    state_dir = tmp_path / "state"

    # Drop a freshness-links report into the repo's .aicg state.
    aicg = repo_path / ".aicg"
    aicg.mkdir(exist_ok=True)
    (aicg / "freshness-links-report.json").write_text(_json.dumps({
        "work_items": [
            {
                "id": "refresh-links-docs-foo-md",
                "type": "refresh_links",
                "severity": "high",
                "path": "docs/foo.md",
                "broken_count": 2,
                "title": "Fix 2 broken link(s) in docs/foo.md",
                "details": [
                    {"url": "https://gone.example/a", "status": 404, "reason": "Not Found"},
                    {"url": "https://gone.example/b", "status": 404, "reason": "Not Found"},
                ],
            }
        ],
    }))

    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    report = run_org_audit(manifest, workspace, state_dir=state_dir)

    promoted = [
        it for it in report["work_items"] if it["type"] == "refresh_links"
    ]
    assert promoted, "refresh_links should be promoted to the org queue"
    item = promoted[0]
    # The bug: these fields were missing from the promoted item.
    assert "details" in item
    assert len(item["details"]) == 2
    assert item["details"][0]["url"] == "https://gone.example/a"
    assert item.get("broken_count") == 2


def test_daily_remediation_verifies_after_generate(tmp_path):
    import textwrap

    from aicg.org_runner import run_daily_remediation

    workspace = make_security_workspace(tmp_path)
    solutions = workspace / "ai-infra-security-solutions"
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"

    # Configure a generator script that produces a complete SOLUTION.md.
    generator = tmp_path / "fake-claude.sh"
    target_file = (
        solutions
        / "modules"
        / "mod-001-ml-security-foundations"
        / "exercise-01-threat-model-a-small-ml-system"
        / "SOLUTION.md"
    )
    body = textwrap.dedent(
        """\
        # Threat Model Solution

        ## Overview
        Brief walkthrough.

        ## Implementation
        Build it.

        ## Validation
        Run tests.

        ## Rubric
        Graded.

        ## Common mistakes
        Listed.

        ## References
        - https://www.nist.gov/itl/ai-risk-management-framework
        """
    )
    target_file.parent.mkdir(parents=True, exist_ok=True)
    generator.write_text(
        "#!/usr/bin/env bash\n"
        f'cat > "{target_file}" <<EOF\n{body}EOF\n',
        encoding="utf-8",
    )
    generator.chmod(0o755)

    # Override the manifest's configured agent command for this run.
    import aicg.org_runner as org_runner

    original = org_runner.content_generation_command
    org_runner.content_generation_command = lambda _m: str(generator)
    try:
        # First run org audit to populate the work queue.
        org_runner.run_org_audit(manifest, workspace, state_dir=state_dir)
        summary = run_daily_remediation(
            manifest, workspace, state_dir=state_dir, drain_until_empty=False
        )
    finally:
        org_runner.content_generation_command = original

    result = summary["items"][0]
    assert result["status"] in {"generated", "merged"}
    assert result.get("verify", {}).get("status") == "verified"
    # propagate runs after a verified item — VERSIONS.md is appended.
    propagate = result.get("propagate", {})
    assert propagate.get("status") == "updated"
    assert propagate.get("updated_count", 0) >= 1
    versions = (
        workspace / "ai-infra-security-solutions" / "VERSIONS.md"
    ).read_text(encoding="utf-8")
    assert "fill-mod-001-ml-security-foundations-solutions" in versions


def test_daily_remediation_skips_items_that_already_have_an_open_pr(tmp_path):
    """Regression: the audit resets gaps to ``ready`` from main, so an item
    whose PR was still open got picked again and a duplicate PR opened
    (startup-operations-governance had 3 PRs for one exercise)."""
    import json as _json
    from unittest.mock import patch

    from aicg.org_runner import HANDLED_WORK_TYPES, run_daily_remediation
    from aicg.pr_remediation import item_work_key

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    handled = next(t for t in ("learning_gap", "module_solution_gap") if t in HANDLED_WORK_TYPES)
    repo = "ai-infra-security-solutions"

    def item(work_id: str, priority: int) -> dict:
        return {
            "id": work_id, "work_id": work_id, "type": handled, "repo": repo,
            "status": "ready", "priority": priority, "title": work_id,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }

    queue = {"schema_version": 1, "work_item_count": 2, "repo_reports": [],
             "work_items": [item("gap-with-pr", 0), item("gap-fresh", 1)]}
    (state_dir / "work-queue.json").write_text(_json.dumps(queue))

    picked: list[str] = []

    def fake_process(**kwargs):
        picked.append(kwargs["item"]["work_id"])
        return {"status": "generated"}

    with patch(
        "aicg.pr_remediation.open_pr_work_keys", return_value={item_work_key(repo, "gap-with-pr")}
    ), patch("aicg.org_runner._process_one_item", side_effect=fake_process):
        run_daily_remediation(manifest, workspace, state_dir=state_dir)

    assert picked == ["gap-fresh"]
    after = {i["work_id"]: i for i in _json.loads((state_dir / "work-queue.json").read_text())["work_items"]}
    assert after["gap-with-pr"]["status"] == "pr_open"


def test_daily_remediation_unchanged_when_open_prs_unknown(tmp_path):
    """gh unavailable -> don't filter (no worse than before)."""
    import json as _json
    from unittest.mock import patch

    from aicg.org_runner import HANDLED_WORK_TYPES, run_daily_remediation

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    handled = next(t for t in ("learning_gap", "module_solution_gap") if t in HANDLED_WORK_TYPES)
    queue = {"schema_version": 1, "work_item_count": 1, "repo_reports": [], "work_items": [{
        "id": "g", "work_id": "g", "type": handled, "repo": "ai-infra-security-solutions",
        "status": "ready", "priority": 0, "title": "g",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}]}
    (state_dir / "work-queue.json").write_text(_json.dumps(queue))
    picked: list[str] = []
    with patch("aicg.pr_remediation.open_pr_work_keys", return_value=None), patch(
        "aicg.org_runner._process_one_item",
        side_effect=lambda **kw: picked.append(kw["item"]["work_id"]) or {"status": "generated"},
    ):
        run_daily_remediation(manifest, workspace, state_dir=state_dir)
    assert picked == ["g"]
