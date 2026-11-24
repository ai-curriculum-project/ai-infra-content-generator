from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from conftest import write_minimal_manifest

from aicg.org_config import load_manifest
from aicg.steward import (
    classify_rollup,
    evaluate_pr_guardrails,
    steward_run,
)


def _success_check(name: str) -> dict:
    return {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}


def _failure_check(name: str) -> dict:
    return {"name": name, "status": "COMPLETED", "conclusion": "FAILURE"}


def _pending_check(name: str) -> dict:
    return {"name": name, "status": "IN_PROGRESS", "conclusion": None}


def _commit_status(state: str) -> dict:
    return {"context": "ci/legacy", "state": state.upper()}


# ---------------------------------------------------------------------------
# classify_rollup
# ---------------------------------------------------------------------------


def test_rollup_all_success_is_success() -> None:
    assert classify_rollup([_success_check("ci")]) == "success"


def test_rollup_one_failure_is_failure() -> None:
    rollup = [_success_check("ci"), _failure_check("lint")]
    assert classify_rollup(rollup) == "failure"


def test_rollup_pending_with_success_is_pending() -> None:
    rollup = [_success_check("ci"), _pending_check("e2e")]
    assert classify_rollup(rollup) == "pending"


def test_rollup_skipped_treated_as_success_when_others_pass() -> None:
    rollup = [
        _success_check("ci"),
        {"name": "skip", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert classify_rollup(rollup) == "success"


def test_rollup_empty_is_pending() -> None:
    assert classify_rollup([]) == "pending"


def test_rollup_handles_commit_status_shape() -> None:
    assert classify_rollup([_commit_status("success")]) == "success"
    assert classify_rollup([_commit_status("pending")]) == "pending"
    assert classify_rollup([_commit_status("failure")]) == "failure"


# ---------------------------------------------------------------------------
# evaluate_pr_guardrails
# ---------------------------------------------------------------------------


def test_pr_guardrails_block_drafts(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={
            "isDraft": True,
            "headRefName": "aicg/feature",
            "files": [],
            "labels": [],
        },
    )
    assert not decision.allowed
    assert any("draft" in b.lower() for b in decision.blockers)


def test_pr_guardrails_block_do_not_merge_label(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={
            "isDraft": False,
            "headRefName": "aicg/feature",
            "files": [],
            "labels": [{"name": "do-not-merge"}],
        },
    )
    assert not decision.allowed
    assert any("do-not-merge" in b for b in decision.blockers)


def test_pr_guardrails_block_restricted_file(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={
            "isDraft": False,
            "headRefName": "aicg/feature",
            "files": [{"path": ".github/workflows/ci.yml"}],
            "labels": [],
        },
    )
    assert not decision.allowed
    assert any("restricted" in b.lower() for b in decision.blockers)


def test_pr_guardrails_block_main_head_ref(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={"isDraft": False, "headRefName": "main", "files": [], "labels": []},
    )
    assert not decision.allowed


def test_pr_guardrails_block_conflicting_mergeable(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={
            "isDraft": False,
            "headRefName": "aicg/feature",
            "files": [],
            "labels": [],
            "mergeable": "CONFLICTING",
        },
    )
    assert not decision.allowed
    assert any("conflict" in b.lower() for b in decision.blockers)


def test_pr_guardrails_allow_clean_pr(tmp_path: Path) -> None:
    decision = evaluate_pr_guardrails(
        repo_path=tmp_path,
        pr={
            "isDraft": False,
            "headRefName": "aicg/feature",
            "files": [{"path": "modules/mod-001/exercise-01/SOLUTION.md"}],
            "labels": [],
            "mergeable": "MERGEABLE",
        },
    )
    assert decision.allowed
    assert decision.blockers == ()


# ---------------------------------------------------------------------------
# steward_run end-to-end with mocked gh calls
# ---------------------------------------------------------------------------


def _setup_workspace(tmp_path: Path) -> tuple[Path, Path, "OrgManifest"]:
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning"
    solutions = workspace / "ai-infra-security-solutions"
    learning.mkdir(parents=True)
    solutions.mkdir(parents=True)
    (workspace / ".github").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    return workspace, tmp_path / "state", manifest


def test_steward_dry_run_reports_would_merge(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)

    # ai-infra-security-solutions has an open PR with green CI.
    open_prs = {
        "ai-infra-security-learning": [],
        "ai-infra-security-solutions": [
            {
                "number": 7,
                "title": "Fill mod-001 solutions",
                "headRefName": "aicg/2026-05-27/security-solutions/fill-mod-001",
                "isDraft": False,
                "labels": [],
            }
        ],
        ".github": [],
    }
    pr_views = {
        7: {
            "number": 7,
            "title": "Fill mod-001 solutions",
            "headRefName": "aicg/2026-05-27/security-solutions/fill-mod-001",
            "isDraft": False,
            "labels": [],
            "files": [{"path": "modules/mod-001/exercise-01/SOLUTION.md"}],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [_success_check("ci")],
        }
    }

    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view"
    ) as mock_view:
        mock_list.side_effect = lambda repo_path: open_prs.get(repo_path.name, [])
        mock_view.side_effect = lambda repo_path, number: pr_views[number]
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=False)

    assert report["status"] == "dry_run"
    solutions_report = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions_report["pr_count"] == 1
    assert solutions_report["prs"][0]["state"] == "would_merge"


def test_steward_apply_invokes_gh_pr_merge(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    pr_views = {
        7: {
            "number": 7,
            "title": "Fill mod-001",
            "headRefName": "aicg/2026-05-27/security/fill-mod-001",
            "isDraft": False,
            "labels": [],
            "files": [{"path": "modules/mod-001/exercise-01/SOLUTION.md"}],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [_success_check("ci")],
        }
    }

    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view"
    ) as mock_view, patch("aicg.steward.enable_auto_merge") as mock_merge, patch(
        "aicg.steward.pr_state", return_value="MERGED"
    ):
        mock_list.side_effect = lambda repo_path: (
            [
                {
                    "number": 7,
                    "title": "Fill mod-001",
                    "headRefName": "aicg/feature",
                    "isDraft": False,
                    "labels": [],
                }
            ]
            if repo_path.name == "ai-infra-security-solutions"
            else []
        )
        mock_view.side_effect = lambda repo_path, number: pr_views[number]
        mock_merge.return_value = {
            "command": "gh pr merge 7 --auto --squash --delete-branch",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=True)

    mock_merge.assert_called_once()
    solutions_report = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions_report["merged_count"] == 1
    assert solutions_report["prs"][0]["state"] == "merged"


def test_steward_blocks_when_ci_fails(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    pr_view_data = {
        "number": 7,
        "headRefName": "aicg/feature",
        "isDraft": False,
        "labels": [],
        "files": [],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [_failure_check("lint")],
    }

    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", return_value=pr_view_data
    ), patch("aicg.steward.enable_auto_merge") as mock_merge:
        mock_list.side_effect = lambda repo_path: (
            [{"number": 7, "headRefName": "aicg/feature", "isDraft": False, "labels": []}]
            if repo_path.name == "ai-infra-security-solutions"
            else []
        )
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=True)

    mock_merge.assert_not_called()
    solutions_report = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions_report["ci_failed_count"] == 1
    assert solutions_report["prs"][0]["state"] == "ci_failed"


def test_steward_blocks_restricted_file_changes(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    pr_view_data = {
        "number": 7,
        "headRefName": "aicg/feature",
        "isDraft": False,
        "labels": [],
        "files": [{"path": ".github/workflows/ci.yml"}],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [_success_check("ci")],
    }

    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", return_value=pr_view_data
    ), patch("aicg.steward.enable_auto_merge") as mock_merge:
        mock_list.side_effect = lambda repo_path: (
            [{"number": 7, "headRefName": "aicg/feature", "isDraft": False, "labels": []}]
            if repo_path.name == "ai-infra-security-solutions"
            else []
        )
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=True)

    mock_merge.assert_not_called()
    solutions_report = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions_report["blocked_count"] == 1


def test_steward_state_file_written(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    with patch("aicg.steward.list_open_prs", return_value=[]):
        steward_run(manifest, workspace, state_dir=state_dir, apply=False)
    report_path = state_dir / "steward-report.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text())
    assert payload["operation"] == "steward"
    assert payload["status"] == "dry_run"


# ---------------------------------------------------------------------------
# CI-absent short-circuit (wait_for_ci)
# ---------------------------------------------------------------------------


def test_wait_for_ci_short_circuits_when_no_rollup_after_grace(tmp_path: Path) -> None:
    from aicg.steward import wait_for_ci

    # pr_view returns empty rollup forever — simulates a repo with no CI.
    with patch("aicg.steward.pr_view", return_value={"statusCheckRollup": []}):
        result = wait_for_ci(
            repo_path=tmp_path,
            pr_number=1,
            timeout_seconds=120,
            poll_seconds=0,           # don't sleep in the test
            ci_absent_grace_seconds=0,  # short-circuit immediately
        )
    assert result["status"] == "no_ci_present"


def test_wait_for_ci_returns_success_when_rollup_passes(tmp_path: Path) -> None:
    from aicg.steward import wait_for_ci

    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    with patch(
        "aicg.steward.pr_view", return_value={"statusCheckRollup": rollup}
    ):
        result = wait_for_ci(
            repo_path=tmp_path,
            pr_number=1,
            timeout_seconds=10,
            poll_seconds=0,
        )
    assert result["status"] == "success"


def test_wait_for_ci_returns_failure_on_failed_check(tmp_path: Path) -> None:
    from aicg.steward import wait_for_ci

    rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
    with patch(
        "aicg.steward.pr_view", return_value={"statusCheckRollup": rollup}
    ):
        result = wait_for_ci(
            repo_path=tmp_path,
            pr_number=1,
            timeout_seconds=10,
            poll_seconds=0,
        )
    assert result["status"] == "failure"


# ---------------------------------------------------------------------------
# Stuck-PR remediation hooks
# ---------------------------------------------------------------------------


def test_rollup_ghost_in_progress_with_conclusion_is_terminal() -> None:
    """GitHub sometimes reports status=IN_PROGRESS on a check that already has
    a conclusion + completedAt (the run itself is completed). Waiting on it
    never ends, so the conclusion is authoritative."""
    ghost = {
        "name": "docker build (smoke)",
        "status": "IN_PROGRESS",
        "conclusion": "SUCCESS",
        "completedAt": "2026-11-11T21:01:10Z",
    }
    assert classify_rollup([_success_check("lint"), ghost]) == "success"
    assert classify_rollup([{**ghost, "conclusion": "FAILURE"}]) == "failure"


def test_rollup_genuinely_running_check_stays_pending() -> None:
    running = {"name": "e2e", "status": "IN_PROGRESS", "conclusion": "", "completedAt": "0001-01-01T00:00:00Z"}
    assert classify_rollup([running]) == "pending"


def _marker_pr() -> dict:
    return {
        "number": 7,
        "isDraft": False,
        "headRefName": "aicg/2026-11-23/r/fill",
        "files": [{"path": "lessons/mod-1/a.md"}],
        "labels": [],
        "mergeable": "MERGEABLE",
    }


def test_pr_guardrails_allow_marker_that_predates_the_pr(tmp_path: Path) -> None:
    (tmp_path / "lessons" / "mod-1").mkdir(parents=True)
    (tmp_path / "lessons" / "mod-1" / "a.md").write_text("<!-- needs-research: old -->\n")
    with patch("aicg.steward.introduced_marker_paths", return_value=[]):
        decision = evaluate_pr_guardrails(repo_path=tmp_path, pr=_marker_pr())
    assert decision.allowed, decision.blockers


def test_pr_guardrails_block_marker_the_pr_introduces(tmp_path: Path) -> None:
    with patch("aicg.steward.introduced_marker_paths", return_value=["lessons/mod-1/a.md"]):
        decision = evaluate_pr_guardrails(repo_path=tmp_path, pr=_marker_pr())
    assert not decision.allowed
    assert any("introduced" in b and "lessons/mod-1/a.md" in b for b in decision.blockers)


def test_pr_guardrails_fall_back_to_local_scan_when_git_unavailable(tmp_path: Path) -> None:
    """Fail closed: if the PR head can't be compared, keep the old scan."""
    (tmp_path / "lessons" / "mod-1").mkdir(parents=True)
    (tmp_path / "lessons" / "mod-1" / "a.md").write_text("<!-- needs-research: old -->\n")
    with patch("aicg.steward.introduced_marker_paths", return_value=None):
        decision = evaluate_pr_guardrails(repo_path=tmp_path, pr=_marker_pr())
    assert not decision.allowed


def test_steward_apply_closes_duplicates_before_merging(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    old = {"number": 14, "title": "gap", "headRefName": "aicg/2026-11-11/ops/learning-x", "isDraft": False,
           "labels": [], "isCrossRepository": False}
    new = {"number": 35, "title": "gap", "headRefName": "aicg/2026-11-24/ops/learning-x", "isDraft": False,
           "labels": [], "isCrossRepository": False}
    view = {
        "number": 35, "headRefName": new["headRefName"], "isDraft": False, "labels": [],
        "files": [], "mergeable": "MERGEABLE", "statusCheckRollup": [_success_check("ci")],
    }
    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", return_value=view
    ) as mock_view, patch("aicg.steward.enable_auto_merge", return_value={"returncode": 0}), patch(
        "aicg.steward.pr_state", return_value="MERGED"
    ), patch("aicg.steward.apply_action", side_effect=lambda rp, a, **kw: {**a.as_dict(), "result": "closed"}) as act:
        mock_list.side_effect = lambda repo_path: [old, new] if repo_path.name.endswith("solutions") else []
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=True)

    solutions = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert [a["pr_number"] for a in solutions["remediation"]] == [14]
    assert act.call_args.args[1].kind == "close_duplicate"
    assert [p["pr_number"] for p in solutions["prs"]] == [35]  # #14 was never stewarded
    assert all(call.args[1] == 35 for call in mock_view.call_args_list)


def test_steward_dry_run_plans_remediation_without_acting(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    prs = [
        {"number": 1, "headRefName": "aicg/2026-01-01/r/x", "isDraft": False, "labels": [], "isCrossRepository": False},
        {"number": 2, "headRefName": "aicg/2026-01-02/r/x", "isDraft": False, "labels": [], "isCrossRepository": False},
    ]
    view = {"number": 2, "headRefName": "aicg/2026-01-02/r/x", "isDraft": False, "labels": [],
            "files": [], "mergeable": "MERGEABLE", "statusCheckRollup": [_failure_check("lint")]}
    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", return_value=view
    ), patch("aicg.steward.apply_action") as act:
        mock_list.side_effect = lambda repo_path: prs if repo_path.name.endswith("solutions") else []
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=False)

    act.assert_not_called()
    solutions = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    planned = {(a["kind"], a["pr_number"], a["result"]) for a in solutions["remediation"]}
    assert planned == {("close_duplicate", 1, "planned"), ("close_stale", 2, "planned")}


def test_pr_guardrails_downgrade_introduced_markers_when_allowed(tmp_path: Path) -> None:
    """After marker resolution attempts are exhausted the PR may merge; the
    markers land on main where the daily resolve-markers job owns them."""
    with patch("aicg.steward.introduced_marker_paths", return_value=["lessons/mod-1/a.md"]):
        decision = evaluate_pr_guardrails(
            repo_path=tmp_path, pr=_marker_pr(), allow_introduced_markers=True
        )
    assert decision.allowed
    assert any("lessons/mod-1/a.md" in w for w in decision.warnings)


def _marker_blocked_setup(tmp_path: Path, numbers: list[int]):
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    prs = [{"number": n, "headRefName": f"aicg/2026-11-2{n % 10}/r/item-{n}", "isDraft": False, "labels": [],
            "isCrossRepository": False} for n in numbers]
    views = {n: {"number": n, "headRefName": f"aicg/2026-11-2{n % 10}/r/item-{n}", "isDraft": False,
                 "labels": [], "files": [{"path": "lessons/mod-1/a.md"}], "mergeable": "MERGEABLE",
                 "statusCheckRollup": [_success_check("ci")]} for n in numbers}
    return workspace, state_dir, manifest, prs, views


def test_steward_resolves_introduced_markers_within_budget(tmp_path: Path) -> None:
    workspace, state_dir, manifest, prs, views = _marker_blocked_setup(tmp_path, [1, 2, 3])
    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", side_effect=lambda rp, n: views[n]
    ), patch("aicg.steward.introduced_marker_paths", return_value=["lessons/mod-1/a.md"]), patch(
        "aicg.steward.apply_action", side_effect=lambda rp, a, **kw: {**a.as_dict(), "result": "pushed"}
    ) as act:
        mock_list.side_effect = lambda rp: prs if rp.name.endswith("solutions") else []
        steward_run(manifest, workspace, state_dir=state_dir, apply=True)

    resolved = [c.args[1].pr_number for c in act.call_args_list if c.args[1].kind == "resolve_markers"]
    assert len(resolved) == 2  # MAX_AGENT_RUNS_PER_PASS
    attempts = json.loads((state_dir / "marker-remediation.json").read_text())
    assert sorted(attempts["attempts"].values()) == [1, 1]


def test_steward_merges_after_marker_attempts_exhausted(tmp_path: Path) -> None:
    workspace, state_dir, manifest, prs, views = _marker_blocked_setup(tmp_path, [1])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "marker-remediation.json").write_text(
        json.dumps({"attempts": {"ai-infra-security-solutions#1": 3}})
    )
    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", side_effect=lambda rp, n: views[n]
    ), patch("aicg.steward.introduced_marker_paths", return_value=["lessons/mod-1/a.md"]), patch(
        "aicg.steward.apply_action"
    ) as act:
        mock_list.side_effect = lambda rp: prs if rp.name.endswith("solutions") else []
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=False)

    act.assert_not_called()
    solutions = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions["prs"][0]["state"] == "would_merge"


def test_pr_guardrails_block_fork_prs(tmp_path: Path) -> None:
    """Fork PRs never auto-merge: their head can change after review and the
    runner did not write them (ai-infra-engineer-learning#18 got auto-merge
    enabled before this)."""
    pr = {**_marker_pr(), "isCrossRepository": True}
    with patch("aicg.steward.introduced_marker_paths", return_value=[]):
        decision = evaluate_pr_guardrails(repo_path=tmp_path, pr=pr)
    assert not decision.allowed
    assert any("fork" in b.lower() for b in decision.blockers)


def test_pr_guardrails_compare_the_pr_head_object(tmp_path: Path) -> None:
    pr = {**_marker_pr(), "number": 7, "headRefOid": "abc123"}
    with patch("aicg.steward.introduced_marker_paths", return_value=[]) as cmp:
        evaluate_pr_guardrails(repo_path=tmp_path, pr=pr)
    cmp.assert_called_once_with(tmp_path, 7, ["lessons/mod-1/a.md"], expected_oid="abc123")


def test_steward_never_remediates_fork_prs(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    fork = {"number": 5, "headRefName": "aicg/2026-01-01/r/x", "isDraft": False, "labels": [], "isCrossRepository": True}
    view = {**fork, "files": [], "mergeable": "MERGEABLE", "statusCheckRollup": [_failure_check("lint")]}
    with patch("aicg.steward.list_open_prs") as mock_list, patch(
        "aicg.steward.pr_view", return_value=view
    ), patch("aicg.steward.apply_action") as act:
        mock_list.side_effect = lambda rp: [fork] if rp.name.endswith("solutions") else []
        report = steward_run(manifest, workspace, state_dir=state_dir, apply=True)
    act.assert_not_called()
    solutions = next(r for r in report["repos"] if r["repo"].endswith("solutions"))
    assert solutions["remediation"] == []
