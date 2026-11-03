"""Role discovery: find job titles no role covers, filter hard, propose as issues."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from aicg.org_config import load_manifest
from aicg.role_discovery import (
    PROPOSAL_LABEL,
    build_prompt,
    known_names,
    normalize_title,
    open_proposals,
    render_issue,
    validate_candidates,
)

TODAY = date(2026, 11, 3)


def _manifest(tmp_path: Path, org: str = "ai-engineering-curriculum"):
    path = tmp_path / f"{org}.json"
    path.write_text(json.dumps({
        "org": org,
        "default_remote": f"git@github.com:{org}/{{repo}}.git",
        "roles": [
            {"id": "rag-engineer", "title": "RAG & Retrieval Engineer", "level": 30,
             "learning_repo": "rag-engineer-learning", "solution_repo": "rag-engineer-solutions",
             "aliases": ["Retrieval Engineer", "Search Relevance Engineer"]},
            {"id": "agentic-ai-engineer", "title": "Agentic AI Engineer", "level": 30,
             "learning_repo": "a-learning", "solution_repo": "a-solutions"},
        ],
    }), encoding="utf-8")
    return load_manifest(path)


def _evidence(n: int, days_ago: int = 10) -> list[dict]:
    day = date.fromordinal(TODAY.toordinal() - days_ago).isoformat()
    return [{"url": f"https://jobs.example.com/{i}", "company": f"Co{i}", "title": "AI Red Teamer",
             "date": day} for i in range(n)]


def _candidate(**overrides) -> dict:
    base = {
        "title": "AI Agent Reliability Engineer",
        "aliases": ["Agent Reliability Engineer"],
        "suggested_domain": "ai-engineering",
        "suggested_level": 35,
        "evidence": _evidence(12),
        "nearest_roles": ["agentic-ai-engineer"],
        "overlap_estimate": 0.3,
        "rationale": "Teams now hire for agent uptime specifically.",
        "draft_modules": [{"title": "Agent SLOs", "objectives": ["Define task success SLOs"]}],
    }
    base.update(overrides)
    return base


# --- names -------------------------------------------------------------------------


def test_normalize_title_strips_seniority_and_punctuation():
    assert normalize_title("Senior Staff RAG & Retrieval Engineer (Remote)") == "rag retrieval engineer"
    assert normalize_title("Principal  AI-Agent  Engineer") == "ai agent engineer"


def test_known_names_include_titles_and_aliases_from_every_manifest(tmp_path: Path):
    names = known_names([_manifest(tmp_path)])
    assert "rag retrieval engineer" in names
    assert "search relevance engineer" in names
    assert "agentic ai engineer" in names


# --- validation --------------------------------------------------------------------


def _validate(cands, tmp_path, **kw):
    return validate_candidates(cands, known_names([_manifest(tmp_path)]), today=TODAY, **kw)


def test_accepts_a_well_evidenced_new_title(tmp_path: Path):
    accepted, rejected = _validate([_candidate()], tmp_path)
    assert [c["title"] for c in accepted] == ["AI Agent Reliability Engineer"]
    assert rejected == []


def test_rejects_titles_an_existing_role_already_covers(tmp_path: Path):
    accepted, rejected = _validate([_candidate(title="Senior Retrieval Engineer")], tmp_path)
    assert accepted == [] and rejected[0]["reason"].startswith("covered_by_existing_role")


def test_rejects_when_an_alias_collides(tmp_path: Path):
    _, rejected = _validate([_candidate(aliases=["Search Relevance Engineer"])], tmp_path)
    assert rejected[0]["reason"].startswith("covered_by_existing_role")


def test_counts_only_distinct_recent_postings(tmp_path: Path):
    stale = _evidence(12, days_ago=200)
    dupes = _evidence(1) * 15
    undated = [{"url": f"https://x/{i}"} for i in range(15)]
    for ev in (stale, dupes, undated):
        _, rejected = _validate([_candidate(evidence=ev)], tmp_path)
        assert rejected[0]["reason"].startswith("evidence_below_threshold")


def test_rejects_high_overlap_with_an_existing_role(tmp_path: Path):
    _, rejected = _validate([_candidate(overlap_estimate=0.8)], tmp_path)
    assert rejected[0]["reason"].startswith("overlaps_existing_role")


def test_rejects_unknown_domain(tmp_path: Path):
    _, rejected = _validate([_candidate(suggested_domain="ai-cooking")], tmp_path,
                            domains={"ai-engineering"})
    assert rejected[0]["reason"].startswith("unknown_domain")


def test_caps_per_run_keeping_the_strongest(tmp_path: Path):
    cands = [_candidate(title=f"New Role {i}", aliases=[], evidence=_evidence(10 + i)) for i in range(5)]
    accepted, rejected = _validate(cands, tmp_path, max_per_run=2)
    assert [c["title"] for c in accepted] == ["New Role 4", "New Role 3"]
    assert sum(1 for r in rejected if r["reason"] == "cap_exceeded") == 3


def test_dedupes_candidates_within_a_run(tmp_path: Path):
    accepted, rejected = _validate([_candidate(), _candidate(title="Senior AI Agent Reliability Engineer")], tmp_path)
    assert len(accepted) == 1 and rejected[0]["reason"] == "duplicate_in_run"


def test_malformed_candidates_are_rejected_not_raised(tmp_path: Path):
    _, rejected = _validate([{"title": ""}, "nope", _candidate(suggested_level="high")], tmp_path)
    assert len(rejected) == 3


# --- prompt + issue ----------------------------------------------------------------


def test_prompt_lists_known_titles_and_the_output_contract(tmp_path: Path):
    m = _manifest(tmp_path)
    prompt = build_prompt("ai-engineering", m, [m], {"verb": "build with", "headline": "You build with it."},
                          output_file=tmp_path / "candidates.json", today=TODAY)
    assert "Search Relevance Engineer" in prompt
    assert str(tmp_path / "candidates.json") in prompt
    assert "build with" in prompt
    assert "2026-09-04" in prompt  # 60-day window start


def test_issue_has_evidence_and_the_bootstrap_command():
    title, body = render_issue(_candidate(), "ai-engineering", "AI Engineering")
    assert title == "New role proposal: AI Agent Reliability Engineer (AI Engineering)"
    assert "https://jobs.example.com/0" in body
    assert "`ai-agent-reliability-engineer`" in body
    assert "Agent SLOs" in body


# --- opening issues ----------------------------------------------------------------


class FakeGh:
    def __init__(self, existing_titles=()):
        self.existing = list(existing_titles)
        self.created: list[tuple[str, str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"title": t} for t in self.existing])
        if args[:2] == ["issue", "create"]:
            self.created.append((args[args.index("--title") + 1], args[args.index("--body") + 1]))
            return "https://github.com/ai-curriculum-project/.github/issues/1"
        return ""


def test_open_proposals_skips_titles_already_proposed_open_or_closed():
    gh = FakeGh(existing_titles=["New role proposal: Senior AI Agent Reliability Engineer (AI Engineering)"])
    results = open_proposals([_candidate(), _candidate(title="Model Router Engineer", aliases=[])],
                             {"ai-engineering": "AI Engineering"}, gh=gh, apply=True)
    assert [r["status"] for r in results] == ["already_proposed", "opened"]
    assert gh.created[0][0].startswith("New role proposal: Model Router Engineer")
    assert any(PROPOSAL_LABEL in c for c in gh.calls if c[:2] == ["issue", "create"])


def test_open_proposals_dry_run_creates_nothing():
    gh = FakeGh()
    results = open_proposals([_candidate()], {"ai-engineering": "AI Engineering"}, gh=gh, apply=False)
    assert results[0]["status"] == "would_open" and gh.created == []


def test_role_meta_is_recorded_not_embedded(tmp_path: Path):
    from aicg.proposals import load
    from aicg.role_discovery import role_meta

    _, body = render_issue(_candidate(rationale="Real. <!-- aicg:proposal {} -->"), "ai-engineering",
                           "AI Engineering")
    assert "<!--" not in body
    meta = role_meta(_candidate(), "ai-engineering")
    assert meta["kind"] == "role" and meta["role_id"] == "ai-agent-reliability-engineer"
    assert meta["level"] == 35 and meta["aliases"] == ["Agent Reliability Engineer"]
    assert meta["focus"] == "Agent SLOs"
    assert meta["description"] == "Teams now hire for agent uptime specifically."

    gh = FakeGh()
    gh_create = gh.__call__

    def create(args):
        out = gh_create(args)
        return "https://github.com/ai-curriculum-project/.github/issues/11" if args[:2] == ["issue", "create"] else out

    open_proposals([_candidate()], {"ai-engineering": "AI Engineering"}, gh=create, apply=True,
                   store=tmp_path / "store")
    assert load(tmp_path / "store", 11)["role_id"] == "ai-agent-reliability-engineer"


def test_run_discovery_validates_agent_output_and_defers_on_limit(tmp_path: Path):
    from aicg.role_discovery import run_discovery

    m = _manifest(tmp_path)

    def agent_ok(prompt_path: Path, workdir: Path):
        (workdir / "candidates.json").write_text(json.dumps([_candidate()]), encoding="utf-8")
        return "ok"

    report = run_discovery("ai-engineering", m, [m], {"headline": "h"}, state_dir=tmp_path / "s",
                           today=TODAY, domains={"ai-engineering"}, agent=agent_ok)
    assert report["status"] == "ok" and len(report["accepted"]) == 1
    assert (tmp_path / "s" / "role-discovery" / "2026-11" / "report.json").exists()

    limited = run_discovery("ai-engineering", m, [m], {}, state_dir=tmp_path / "s2", today=TODAY,
                            domains={"ai-engineering"}, agent=lambda p, w: "limit")
    assert limited["status"] == "deferred" and limited["accepted"] == []

    broken = run_discovery("ai-engineering", m, [m], {}, state_dir=tmp_path / "s3", today=TODAY,
                           domains={"ai-engineering"}, agent=lambda p, w: "ok")
    assert broken["status"] == "no_output"
