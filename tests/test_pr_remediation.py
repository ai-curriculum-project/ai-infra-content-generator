"""Tests for automatic remediation of stuck aicg PRs."""
from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aicg.pr_remediation import (
    RemediationAction,
    apply_action,
    count_blocking_markers,
    introduced_marker_paths,
    item_work_key,
    marker_blocked_paths,
    plan_duplicate_closures,
    plan_post_steward,
    work_key,
)

TODAY = date(2026, 11, 24)


# ---------------------------------------------------------------------------
# work keys
# ---------------------------------------------------------------------------


def test_work_key_strips_aicg_date_prefix():
    assert work_key("aicg/2026-11-23/cto-curriculum/learning-gap-x") == "cto-curriculum/learning-gap-x"


@pytest.mark.parametrize("ref", ["main", "fix/requests-2.33.0", "aicg/feature", "aicg/parked/2026-11-24/x"])
def test_work_key_none_for_non_work_branches(ref):
    assert work_key(ref) is None


def test_item_work_key_matches_the_branch_the_runner_creates():
    # org_runner: branch = aicg/{today}/{ref_segment(repo)}/{_slug_for_branch(work_id)}
    key = item_work_key("cto-curriculum", "learning-placeholder-exercise-lessons-mod-106-X.md")
    assert key == work_key("aicg/2026-11-24/cto-curriculum/learning-placeholder-exercise-lessons-mod-106-x-md")


# ---------------------------------------------------------------------------
# duplicate PRs for the same work item
# ---------------------------------------------------------------------------


def _open(n: int, ref: str, *, fork: bool = False) -> dict:
    return {"number": n, "headRefName": ref, "isCrossRepository": fork}


def test_plan_duplicate_closures_keeps_newest_pr_per_work_item():
    prs = [
        _open(14, "aicg/2026-11-11/ops/learning-x"),
        _open(22, "aicg/2026-11-18/ops/learning-x"),
        _open(35, "aicg/2026-11-24/ops/learning-x"),
        _open(30, "aicg/2026-11-20/ops/learning-y"),
        _open(18, "fix/requests-2.33.0"),
    ]
    actions = plan_duplicate_closures(prs)
    assert {(a.kind, a.pr_number, a.superseded_by) for a in actions} == {
        ("close_duplicate", 14, 35),
        ("close_duplicate", 22, 35),
    }


def test_plan_duplicate_closures_ignores_fork_prs_posing_as_work_branches():
    """A fork PR named like a work branch must never get our PR closed."""
    prs = [_open(14, "aicg/2026-11-11/ops/learning-x"), _open(99, "aicg/2026-11-24/ops/learning-x", fork=True)]
    assert plan_duplicate_closures(prs) == []


def test_plan_duplicate_closures_fails_closed_without_repo_provenance():
    prs = [{"number": 1, "headRefName": "aicg/2026-11-11/r/x"}, {"number": 2, "headRefName": "aicg/2026-11-12/r/x"}]
    assert plan_duplicate_closures(prs) == []


def test_plan_duplicate_closures_none_when_unique():
    assert plan_duplicate_closures([_open(1, "aicg/2026-11-11/r/a"), _open(2, "aicg/2026-11-11/r/b")]) == []


# ---------------------------------------------------------------------------
# post-steward remediation (rebase conflicts, close long-stuck PRs)
# ---------------------------------------------------------------------------


def _outcome(n: int, state: str, ref_date: str, blockers: list[str] | None = None) -> dict:
    return {
        "pr_number": n,
        "state": state,
        "head_ref": f"aicg/{ref_date}/repo/item-{n}",
        "blockers": blockers or [],
    }


def test_plan_post_steward_rebases_conflicting_prs():
    actions = plan_post_steward(
        [_outcome(13, "guardrails_blocked", "2026-11-22", ["PR has merge conflicts."])], today=TODAY
    )
    assert [(a.kind, a.pr_number) for a in actions] == [("rebase", 13)]


def test_plan_post_steward_closes_prs_stuck_past_the_limit():
    actions = plan_post_steward(
        [
            _outcome(1, "ci_failed", "2026-11-10"),
            _outcome(2, "ci_timeout", "2026-11-12"),
            _outcome(3, "guardrails_blocked", "2026-11-01", ["Marker remains in changed file(s): a.md"]),
            _outcome(4, "ci_failed", "2026-11-20"),  # young: give it time
            _outcome(5, "merged", "2026-11-01"),
            _outcome(6, "merge_pending", "2026-11-01"),
        ],
        today=TODAY,
    )
    assert sorted((a.kind, a.pr_number) for a in actions) == [
        ("close_stale", 1),
        ("close_stale", 2),
        ("close_stale", 3),
    ]
    assert all("14d" in a.reason or "12d" in a.reason or "23d" in a.reason for a in actions)


def test_plan_post_steward_never_closes_human_review_or_non_aicg_prs():
    outcomes = [
        _outcome(1, "review_blocked", "2026-10-01"),
        {"pr_number": 2, "state": "ci_failed", "head_ref": "fix/requests-2.33.0", "blockers": []},
    ]
    assert plan_post_steward(outcomes, today=TODAY) == []


def test_plan_post_steward_old_conflicting_pr_is_rebased_not_closed():
    actions = plan_post_steward(
        [_outcome(13, "guardrails_blocked", "2026-11-01", ["PR has merge conflicts."])], today=TODAY
    )
    assert [(a.kind, a.pr_number) for a in actions] == [("rebase", 13)]


# ---------------------------------------------------------------------------
# apply_action (gh / git IO is mocked)
# ---------------------------------------------------------------------------


def test_apply_close_duplicate_comments_and_deletes_branch(tmp_path):
    action = RemediationAction("close_duplicate", 14, "aicg/2026-11-11/r/x", "duplicate", superseded_by=35)
    done = subprocess.CompletedProcess([], 0, "", "")
    with patch("aicg.pr_remediation._run_gh", return_value=done) as gh:
        result = apply_action(tmp_path, action)
    args = gh.call_args.args[1]
    assert args[:4] == ["gh", "pr", "close", "14"]
    assert "--delete-branch" in args
    assert "#35" in args[args.index("--comment") + 1]
    assert result["result"] == "closed"


def test_apply_close_reports_gh_failure(tmp_path):
    action = RemediationAction("close_stale", 3, "aicg/2026-11-01/r/x", "stuck 23d")
    failed = subprocess.CompletedProcess([], 1, "", "boom")
    with patch("aicg.pr_remediation._run_gh", return_value=failed):
        result = apply_action(tmp_path, action)
    assert result["result"] == "error"
    assert "boom" in result["detail"]


def test_apply_rebase_delegates_to_runner_auto_rebase(tmp_path):
    action = RemediationAction("rebase", 13, "aicg/2026-11-22/r/x", "merge conflicts")
    with patch("aicg.org_runner._auto_rebase_branch_on_main", return_value={"status": "merged_clean"}) as rb:
        result = apply_action(tmp_path, action)
    rb.assert_called_once_with(repo_path=tmp_path, branch="aicg/2026-11-22/r/x", pr_number=13)
    assert result["result"] == "merged_clean"


# ---------------------------------------------------------------------------
# marker guardrail: only markers the PR introduces
# ---------------------------------------------------------------------------


def test_count_blocking_markers_ignores_todo_and_placeholder():
    text = "<!-- needs-research: x -->\nTODO later\n# manual-review\nplaceholder\n"
    assert count_blocking_markers(text) == 2


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def pr_repo(tmp_path: Path) -> Path:
    """A clone whose origin has main + a PR branch.

    main: a.md carries one pre-existing marker; b.md is clean.
    PR:   a.md keeps that marker (and gains prose); b.md gains a NEW marker.
    """
    origin = tmp_path / "origin"
    work = tmp_path / "author"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    for k, v in {"user.email": "t@t", "user.name": "t", "commit.gpgsign": "false"}.items():
        _git(work, "config", k, v)
    (work / "a.md").write_text("intro\n<!-- needs-research: cite -->\n")
    (work / "b.md").write_text("clean\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "aicg/2026-11-23/r/fill")
    (work / "a.md").write_text("intro\nnew prose\n<!-- needs-research: cite -->\n")
    (work / "b.md").write_text("clean\n<!-- needs-research: new gap -->\n")
    _git(work, "commit", "-q", "-am", "fill")
    _git(work, "push", "-q", "origin", "aicg/2026-11-23/r/fill")
    _git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head")  # what GitHub exposes

    runner = tmp_path / "runner-clone"
    _git(tmp_path, "clone", "-q", str(origin), str(runner))
    for k, v in {"user.email": "t@t", "user.name": "t", "commit.gpgsign": "false"}.items():
        _git(runner, "config", k, v)
    return runner


def test_introduced_marker_paths_ignores_markers_already_on_main(pr_repo: Path):
    head = _git(pr_repo.parent / "origin", "rev-parse", "refs/pull/7/head")
    assert introduced_marker_paths(pr_repo, 7, ["a.md", "b.md"], expected_oid=head) == ["b.md"]


def test_introduced_marker_paths_none_when_head_unreachable(pr_repo: Path):
    assert introduced_marker_paths(pr_repo, 999, ["a.md"]) is None


def test_introduced_marker_paths_none_when_head_is_not_the_pr_head(pr_repo: Path):
    """Compare the object GitHub will merge, nothing else: a mismatched
    headRefOid means we fetched something other than the PR's head."""
    main = _git(pr_repo.parent / "origin", "rev-parse", "main")
    assert introduced_marker_paths(pr_repo, 7, ["b.md"], expected_oid=main) is None


def test_open_pr_work_keys_none_for_pseudo_repo_without_checkout(tmp_path):
    from aicg.pr_remediation import open_pr_work_keys

    assert open_pr_work_keys(tmp_path / "_pairing") is None


# ---------------------------------------------------------------------------
# resolve introduced markers on the PR branch
# ---------------------------------------------------------------------------

MARKER_BLOCKER = "Marker introduced in changed file(s): lessons/mod-1/a.md, lessons/mod-1/b.md"


def test_marker_blocked_paths_only_for_marker_only_blocks():
    assert marker_blocked_paths({"blockers": [MARKER_BLOCKER]}) == ["lessons/mod-1/a.md", "lessons/mod-1/b.md"]
    assert marker_blocked_paths({"blockers": [MARKER_BLOCKER, "PR has merge conflicts."]}) == []
    assert marker_blocked_paths({"blockers": ["Marker remains in changed file(s): x.md"]}) == []
    assert marker_blocked_paths({"blockers": []}) == []


def test_plan_post_steward_resolves_introduced_markers_instead_of_closing():
    old_marker_pr = _outcome(9, "guardrails_blocked", "2026-11-01", [MARKER_BLOCKER])
    actions = plan_post_steward([old_marker_pr], today=TODAY, marker_attempts={})
    assert [(a.kind, a.pr_number, a.paths) for a in actions] == [
        ("resolve_markers", 9, ("lessons/mod-1/a.md", "lessons/mod-1/b.md"))
    ]


def test_plan_post_steward_stops_resolving_after_max_attempts():
    pr = _outcome(9, "guardrails_blocked", "2026-11-01", [MARKER_BLOCKER])
    assert plan_post_steward([pr], today=TODAY, marker_attempts={9: 3}) == []


def _resolver_that_edits(path: str, accepted: bool = True):
    from aicg.marker_resolution import Verdict

    def fake(manifest, repo, repo_name, paths, **kw):
        if accepted:
            (repo / path).write_text("clean\ncited (https://example.org)\n")
            return Verdict(True, 1, [path])
        return Verdict(False, 0, [path], "markers added in: b.md")

    return fake


def _origin_file(pr_repo: Path, ref: str, path: str) -> str:
    return _git(pr_repo.parent / "origin", "show", f"{ref}:{path}")


def test_apply_resolve_markers_commits_and_pushes_to_the_pr_branch(pr_repo: Path):
    action = RemediationAction("resolve_markers", 7, "aicg/2026-11-23/r/fill", "markers", paths=("b.md",))
    with patch("aicg.marker_resolution.resolve_file_markers", side_effect=_resolver_that_edits("b.md")):
        result = apply_action(pr_repo, action, manifest=object(), repo_name="r")
    assert result["result"] == "pushed", result
    assert "needs-research" not in _origin_file(pr_repo, "aicg/2026-11-23/r/fill", "b.md")
    assert _git(pr_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(pr_repo, "status", "--porcelain") == ""


def test_apply_resolve_markers_rejected_pushes_nothing(pr_repo: Path):
    before = _git(pr_repo.parent / "origin", "rev-parse", "aicg/2026-11-23/r/fill")
    action = RemediationAction("resolve_markers", 7, "aicg/2026-11-23/r/fill", "markers", paths=("b.md",))
    with patch("aicg.marker_resolution.resolve_file_markers", side_effect=_resolver_that_edits("b.md", accepted=False)):
        result = apply_action(pr_repo, action, manifest=object(), repo_name="r")
    assert result["result"] == "rejected"
    assert "markers added" in result["detail"]
    assert _git(pr_repo.parent / "origin", "rev-parse", "aicg/2026-11-23/r/fill") == before
    assert _git(pr_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_apply_resolve_markers_refuses_dirty_checkout(pr_repo: Path):
    (pr_repo / "a.md").write_text("local edit\n")
    action = RemediationAction("resolve_markers", 7, "aicg/2026-11-23/r/fill", "markers", paths=("b.md",))
    with patch("aicg.marker_resolution.resolve_file_markers") as resolver:
        result = apply_action(pr_repo, action, manifest=object(), repo_name="r")
    resolver.assert_not_called()
    assert result["result"] == "skipped_dirty"


def test_apply_resolve_markers_agent_limit_is_reported_not_raised(pr_repo: Path):
    from aicg.agent_cli import AgentLimitReached

    action = RemediationAction("resolve_markers", 7, "aicg/2026-11-23/r/fill", "markers", paths=("b.md",))
    with patch("aicg.marker_resolution.resolve_file_markers", side_effect=AgentLimitReached(SimpleNamespace(retry_after=None))):
        result = apply_action(pr_repo, action, manifest=object(), repo_name="r")
    assert result["result"] == "agent_limit"
    assert _git(pr_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_open_pr_work_keys_ignores_fork_prs(tmp_path):
    import json

    from aicg.pr_remediation import open_pr_work_keys

    listing = json.dumps([
        {"headRefName": "aicg/2026-11-11/r/ours", "isCrossRepository": False},
        {"headRefName": "aicg/2026-11-11/r/theirs", "isCrossRepository": True},
    ])
    with patch("aicg.pr_remediation._run_gh", return_value=subprocess.CompletedProcess([], 0, listing, "")) as gh:
        keys = open_pr_work_keys(tmp_path)
    assert keys == {"r/ours"}
    assert "isCrossRepository" in gh.call_args.args[1][gh.call_args.args[1].index("--json") + 1]


def test_marker_mentions_in_code_are_not_markers():
    """Prose that *documents* the marker (inline code or a fenced example) is
    not an unresolved claim — PR #35 stayed blocked on four such mentions."""
    text = (
        "See the `<!-- needs-research -->` marker in chapter 03.\n"
        "```md\n<!-- needs-research: example -->\n```\n"
        "Real claim. <!-- needs-research: cite it -->\n"
    )
    assert count_blocking_markers(text) == 1
