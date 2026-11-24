"""Tests for the fleet status read model (roadmap §4)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aicg.fleet import (
    attention_flags,
    build_domain_status,
    read_queue_depth,
    render_fleet_table,
    summarize_activity,
)
from aicg.org_config import load_manifest


def _write(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _manifest(org: str, roles: int, *, judge=True, flag_only=True, author=False) -> dict:
    return {
        "org": org,
        "default_remote": f"git@github.com:{org}/{{repo}}.git",
        "quality_judge": {
            "enabled": judge,
            "flag_only": flag_only,
            "thresholds": {"default": 70, "freshness": 76},
        },
        "pipeline": {"phases": {"author": author}, "daily_budget": 8},
        "roles": [
            {
                "id": f"role-{i}",
                "title": f"Role {i}",
                "level": 10 * (i + 1),
                "learning_repo": f"role-{i}-learning",
                "solution_repo": f"role-{i}-solutions",
            }
            for i in range(roles)
        ],
    }


def test_mode_act_observe_inert(tmp_path: Path) -> None:
    act = build_domain_status(
        "d-act", load_manifest(_write(tmp_path / "a.yaml", _manifest("a", 2, author=True)))
    )
    observe = build_domain_status(
        "d-obs", load_manifest(_write(tmp_path / "b.yaml", _manifest("b", 3)))
    )
    inert = build_domain_status(
        "d-inert", load_manifest(_write(tmp_path / "c.yaml", _manifest("c", 1, judge=False)))
    )
    assert act.mode == "ACT"
    assert observe.mode == "OBSERVE"
    assert inert.mode == "INERT"


def test_domain_status_fields(tmp_path: Path) -> None:
    m = load_manifest(_write(tmp_path / "m.yaml", _manifest("ml-engineering-curriculum", 4)))
    s = build_domain_status("ml-engineering", m, queue_depth=5)
    assert s.org == "ml-engineering-curriculum"
    assert s.role_count == 4
    assert s.freshness_bar == 76
    assert s.judge_enabled and s.judge_flag_only
    assert s.queue_depth == 5


def test_read_queue_depth_counts_open_items(tmp_path: Path) -> None:
    (tmp_path / "work-queue.json").write_text(
        json.dumps(
            {
                "items": [
                    {"id": "1", "status": "queued"},
                    {"id": "2", "status": "in_progress"},
                    {"id": "3", "status": "done"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert read_queue_depth(tmp_path) == 2


def test_read_queue_depth_reads_real_work_items_schema(tmp_path: Path) -> None:
    """The runner writes ``work_items`` (not ``items``); ready = open."""
    (tmp_path / "work-queue.json").write_text(
        json.dumps(
            {
                "work_item_count": 3,
                "work_items": [
                    {"id": "1", "status": "ready"},
                    {"id": "2", "status": "deferred"},
                    {"id": "3", "status": "merged"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert read_queue_depth(tmp_path) == 1


def test_read_queue_depth_missing_returns_none(tmp_path: Path) -> None:
    assert read_queue_depth(tmp_path) is None


def test_render_table_includes_every_domain(tmp_path: Path) -> None:
    statuses = [
        build_domain_status("ai-infra", load_manifest(_write(tmp_path / "i.yaml", _manifest("ai-infra-curriculum", 11, author=True)))),
        build_domain_status("ml-engineering", load_manifest(_write(tmp_path / "m.yaml", _manifest("ml-engineering-curriculum", 4)))),
    ]
    out = render_fleet_table(statuses)
    assert "ai-infra" in out
    assert "ml-engineering" in out
    assert "2 domains · 15 roles" in out
    assert "1 ACT / 1 OBSERVE / 0 INERT" in out


NOW = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)


def _steward(*repos: dict, generated_at: str = "2026-11-24T15:00:00Z") -> dict:
    return {"generated_at": generated_at, "repos": list(repos)}


def _repo(prs: list[dict], *, blocked=0, ci_failed=0, review_blocked=0) -> dict:
    return {
        "pr_count": len(prs),
        "blocked_count": blocked,
        "ci_failed_count": ci_failed,
        "review_blocked_count": review_blocked,
        "merged_count": 0,
        "prs": prs,
    }


def _pr(n: int, date: str, blockers: list[str] | None = None, repo: str = "r") -> dict:
    pr = {"pr_number": n, "repo": repo, "head_ref": f"aicg/{date}/{repo}/fill-x"}
    if blockers is not None:
        pr["blockers"] = blockers
    return pr


def test_summarize_activity_counts_prs_and_groups_blockers():
    steward = _steward(
        _repo(
            [
                _pr(1, "2026-11-23", ["Marker remains in changed file(s): a.md"]),
                _pr(2, "2026-11-20", ["Marker remains in changed file(s): b.md", "PR has merge conflicts."]),
            ],
            blocked=2,
        ),
        _repo([_pr(3, "2026-11-24", repo="nlp")], ci_failed=1),
    )
    a = summarize_activity(steward=steward, work_queue=None, last_item=None, now=NOW)
    assert a.open_prs == 3
    assert a.blocked_prs == 2
    assert a.ci_failed_prs == 1
    assert a.oldest_pr_days == 4
    assert a.blocker_reasons == (("Marker remains in changed file(s)", 2), ("PR has merge conflicts", 1))


def test_summarize_activity_queue_ready_and_deferred():
    wq = {"work_items": [{"status": "ready"}, {"status": "ready"}, {"status": "deferred"}, {"status": "merged"}]}
    a = summarize_activity(steward=None, work_queue=wq, last_item=None, now=NOW)
    assert a.queue_ready == 2
    assert a.queue_deferred == 1
    assert a.open_prs is None  # no steward report -> unknown, not zero


@pytest.mark.parametrize(
    "last_item, outcome, problem",
    [
        ({"status": "generated", "inline_merge": {"status": "merged"}, "pr": {"status": "opened"}}, "merged", False),
        ({"status": "generated", "pr": {"status": "opened"}}, "PR opened", False),
        (
            {"status": "generated", "parked_branch": "aicg/parked/x", "verify": {"status": "verification_failed"}},
            "parked (verification_failed)",
            True,
        ),
        ({"status": "deferred", "defer_reason": "opaque_generator_failure"}, "deferred: opaque_generator_failure", True),
    ],
)
def test_summarize_activity_last_item_outcome(last_item, outcome, problem):
    item = {"generated_at": "2026-11-22T16:00:00Z", "selected": {"repo": "foo-solutions"}, **last_item}
    a = summarize_activity(steward=None, work_queue=None, last_item=item, now=NOW)
    assert a.last_item == outcome
    assert a.last_item_repo == "foo-solutions"
    assert a.last_item_age_days == 2
    assert a.last_item_problem is problem


def test_attention_flags_blocked_ci_stale_and_problem_items():
    a = summarize_activity(
        steward=_steward(
            _repo([_pr(1, "2026-11-10", ["PR has merge conflicts."])], blocked=1),
            _repo([_pr(2, "2026-11-24")], ci_failed=1),
            generated_at="2026-11-22T00:00:00Z",
        ),
        work_queue=None,
        last_item={"generated_at": "2026-11-24T10:00:00Z", "status": "deferred", "defer_reason": "x", "selected": {"repo": "r"}},
        now=NOW,
    )
    flags = attention_flags(a)
    assert "1 PR blocked (PR has merge conflicts ×1)" in flags
    assert "1 PR failing CI" in flags
    assert "oldest open PR 14d" in flags
    assert "last item deferred: x — r" in flags
    assert any(f.startswith("steward report stale") for f in flags)


def test_attention_quiet_for_healthy_domain():
    a = summarize_activity(
        steward=_steward(_repo([_pr(1, "2026-11-24")])),
        work_queue={"work_items": [{"status": "ready"}]},
        last_item={"generated_at": "2026-11-24T10:00:00Z", "status": "generated", "inline_merge": {"status": "merged"}},
        now=NOW,
    )
    assert attention_flags(a) == []


def test_render_fleet_digest_shows_activity_and_attention():
    from aicg.fleet import DigestRow, render_fleet_digest

    busy = summarize_activity(
        steward=_steward(_repo([_pr(i, "2026-11-23", ["Marker remains in changed file(s): a"]) for i in range(3)], blocked=3)),
        work_queue={"work_items": [{"status": "ready"}] * 5},
        last_item=None,
        now=NOW,
    )
    calm = summarize_activity(
        steward=_steward(_repo([])),
        work_queue={"work_items": [{"status": "ready"}] * 2 + [{"status": "deferred"}]},
        last_item={"generated_at": "2026-11-24T10:00:00Z", "status": "generated", "inline_merge": {"status": "merged"}, "selected": {"repo": "x-solutions"}},
        now=NOW,
    )
    rows = [
        DigestRow("ai-startup", "ACT", 72, 8, 8, activity=busy),
        DigestRow("ai-infra", "ACT", 76, 12, 13, activity=calm),
    ]
    out = render_fleet_digest(rows, date="2026-11-24")
    lines = out.splitlines()
    assert lines[0] == "AICG fleet — 2026-11-24"
    assert "⚠ ai-startup: 3 PRs blocked (Marker remains in changed file(s) ×3)" in out
    assert "ai-infra: PRs 0 · queue 2 (+1 deferred) · last: merged x-solutions today · 12/13 filled" in out
    assert "ai-startup: PRs 3 (3 blocked) · queue 5" in out
    assert "Fleet: 3 open PRs (3 blocked, 0 CI-failing) · queue 7" in out
    # attention block precedes the per-domain lines
    assert lines.index(next(ln for ln in lines if ln.startswith("⚠"))) < lines.index(next(ln for ln in lines if ln.startswith("ai-infra:")))
    # a fully-filled domain no longer repeats the stale fill counter
    assert "8/8 filled" not in out


def test_render_fleet_digest_all_clear():
    from aicg.fleet import DigestRow, render_fleet_digest

    rows = [DigestRow("ai-infra", "ACT", 76, 1, 1, activity=summarize_activity(steward=_steward(_repo([])), work_queue=None, last_item=None, now=NOW))]
    out = render_fleet_digest(rows, date="d")
    assert "✅ nothing needs attention" in out


def test_render_fleet_digest_no_domains_is_not_an_all_clear():
    from aicg.fleet import render_fleet_digest

    out = render_fleet_digest([], date="d")
    assert "✅" not in out
    assert "no domains found" in out
