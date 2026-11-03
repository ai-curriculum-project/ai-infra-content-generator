"""Approval loop: proposals open issues; an `approved` label makes the runner act."""

from __future__ import annotations

import json
from pathlib import Path

from aicg.approvals import (
    APPLIED_LABEL,
    APPROVED_LABEL,
    CONTENT_LABEL,
    Context,
    actions_for,
    content_issue,
    parse_meta,
    pr_url_from_outcome,
    process_approvals,
    proposal_meta,
)


def _ctx(tmp_path: Path) -> Context:
    return Context(runner_dir=tmp_path / "gen", workspace=tmp_path,
                   manifest_for={"ai-engineering": tmp_path / "gen/config/domains/ai-engineering.yaml"},
                   learning_repo_for={("ai-engineering", "rag-engineer"): "rag-engineer-learning"})


def test_meta_round_trips_through_an_issue_body():
    body = "Some text\n\n" + proposal_meta("content", domain="ai-engineering", role_id="rag") + "\n"
    assert parse_meta(body) == {"kind": "content", "domain": "ai-engineering", "role_id": "rag"}
    assert parse_meta("no block here") is None


def test_pr_url_comes_from_the_pr_create_step():
    outcome = {"steps": [{"step": "push", "returncode": 0, "stdout_tail": ""},
                         {"step": "pr_create", "returncode": 0,
                          "stdout_tail": "https://github.com/o/r/pull/12\n"}]}
    assert pr_url_from_outcome(outcome) == "https://github.com/o/r/pull/12"
    assert pr_url_from_outcome({"steps": []}) is None
    assert pr_url_from_outcome(None) is None


def test_content_issue_summarizes_accepted_items_and_embeds_meta():
    validation = {"accepted": {"modules": [{"id": "mod-113-mcp-evals", "title": "MCP tool evals"}],
                               "exercises": [{"module": "mod-103", "slug": "long-horizon"}],
                               "projects": []}}
    title, body = content_issue("ai-engineering", "AI Engineering", "rag-engineer", "RAG Engineer",
                                "2026-11", validation, "https://github.com/o/r/pull/12")
    assert title == "Curriculum proposal: RAG Engineer (AI Engineering) — 2026-11"
    assert "mod-113-mcp-evals" in body and "long-horizon" in body
    assert "https://github.com/o/r/pull/12" in body
    meta = parse_meta(body)
    assert meta["kind"] == "content" and meta["pr_url"].endswith("/pull/12")


def test_content_actions_merge_promote_then_author(tmp_path: Path):
    meta = {"kind": "content", "domain": "ai-engineering", "role_id": "rag-engineer",
            "pr_url": "https://github.com/o/r/pull/12"}
    steps = [a.argv for a in actions_for(meta, _ctx(tmp_path))]
    assert steps[0][:3] == ["gh", "pr", "merge"]
    assert any("promote-plan" in s for s in steps)
    assert "generate-role" in steps[-1] and "rag-engineer" in steps[-1]
    assert steps[-1][:2] == ["systemd-run", "--user"]


def test_role_actions_bootstrap_with_remotes_then_author(tmp_path: Path):
    meta = {"kind": "role", "domain": "ai-engineering", "role_id": "agent-sre", "title": "Agent SRE",
            "level": 35, "aliases": ["AI Agent SRE"], "focus": "Agent SLOs, incidents"}
    actions = actions_for(meta, _ctx(tmp_path))
    argvs = [a.argv for a in actions if a.argv]
    boot = next(a for a in argvs if "bootstrap-role" in a)
    assert "--create-remotes" in boot and "agent-sre" in boot and "--domain" in boot
    assert any(a.python for a in actions)  # aliases + focus are written into config
    assert "generate-role" in argvs[-1]


def test_unknown_kind_or_domain_yields_no_actions(tmp_path: Path):
    assert actions_for({"kind": "weird"}, _ctx(tmp_path)) == []
    assert actions_for({"kind": "content", "domain": "nope", "role_id": "x"}, _ctx(tmp_path)) == []


class FakeGh:
    def __init__(self, issues):
        self.issues = issues
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            want = args[args.index("--label") + 1]
            return json.dumps([i for i in self.issues if want in [lb["name"] for lb in i["labels"]]])
        return ""


def _issue(meta: dict, labels=(APPROVED_LABEL, CONTENT_LABEL), number=7):
    return {"number": number, "title": "t", "body": proposal_meta(**meta),
            "labels": [{"name": n} for n in labels]}


def test_process_runs_actions_comments_and_closes_on_success(tmp_path: Path):
    meta = {"kind": "content", "domain": "ai-engineering", "role_id": "rag-engineer",
            "pr_url": "https://github.com/o/r/pull/12"}
    gh = FakeGh([_issue(meta)])
    ran: list[list[str]] = []
    report = process_approvals(_ctx(tmp_path), gh=gh, run=lambda argv: ran.append(argv) or (0, "ok"),
                               apply=True)
    assert report[0]["status"] == "applied"
    assert ran and ran[0][:3] == ["gh", "pr", "merge"]
    flat = [" ".join(c) for c in gh.calls]
    assert any(c.startswith("issue comment 7") for c in flat)
    assert any(c.startswith("issue close 7") for c in flat)
    assert any(f"--add-label {APPLIED_LABEL}" in c for c in flat)


def test_process_stops_and_flags_on_failure_without_retrying_forever(tmp_path: Path):
    meta = {"kind": "content", "domain": "ai-engineering", "role_id": "rag-engineer",
            "pr_url": "https://github.com/o/r/pull/12"}
    gh = FakeGh([_issue(meta)])
    report = process_approvals(_ctx(tmp_path), gh=gh, run=lambda argv: (1, "merge conflict"), apply=True)
    assert report[0]["status"] == "failed"
    flat = [" ".join(c) for c in gh.calls]
    assert any(f"--remove-label {APPROVED_LABEL}" in c for c in flat)
    assert not any(c.startswith("issue close") for c in flat)


def test_process_skips_issues_already_applied_and_dry_run_does_nothing(tmp_path: Path):
    meta = {"kind": "content", "domain": "ai-engineering", "role_id": "rag-engineer", "pr_url": "u"}
    gh = FakeGh([_issue(meta, labels=(APPROVED_LABEL, CONTENT_LABEL, APPLIED_LABEL))])
    assert process_approvals(_ctx(tmp_path), gh=gh, run=lambda a: (0, ""), apply=True) == []
    gh2 = FakeGh([_issue(meta)])
    ran: list = []
    report = process_approvals(_ctx(tmp_path), gh=gh2, run=lambda a: ran.append(a) or (0, ""), apply=False)
    assert report[0]["status"] == "would_apply" and ran == []


def test_issue_without_meta_is_flagged_for_a_human(tmp_path: Path):
    gh = FakeGh([{"number": 9, "title": "t", "body": "hand-written",
                  "labels": [{"name": APPROVED_LABEL}, {"name": CONTENT_LABEL}]}])
    report = process_approvals(_ctx(tmp_path), gh=gh, run=lambda a: (0, ""), apply=True)
    assert report[0]["status"] == "unparseable"


def test_apply_role_config_adds_aliases_focus_and_band(tmp_path: Path):
    from aicg.approvals import apply_role_config

    runner = tmp_path / "gen"
    (runner / "config" / "profiles").mkdir(parents=True)
    (runner / "config" / "aicg-org.yaml").write_text(json.dumps({"org": "o", "roles": [
        {"id": "mlops", "title": "MLOps", "level": 30},
        {"id": "architect", "title": "Architect", "level": 45},
        {"id": "agent-sre", "title": "Agent SRE", "level": 35},
    ]}), encoding="utf-8")
    (runner / "config" / "profiles" / "ai-infra.json").write_text(json.dumps({
        "focus": {"mlops": "x"},
        "bands": [{"title": "Mid", "roles": ["mlops"]}, {"title": "Adv", "roles": ["architect"]}],
    }), encoding="utf-8")
    apply_role_config(runner, "ai-infra", {"role_id": "agent-sre", "title": "Agent SRE", "level": 33,
                                           "aliases": ["AI Agent SRE", "Agent SRE"], "focus": "Agent SLOs"})
    cfg = json.loads((runner / "config" / "aicg-org.yaml").read_text())
    assert next(r for r in cfg["roles"] if r["id"] == "agent-sre")["aliases"] == ["AI Agent SRE"]
    prof = json.loads((runner / "config" / "profiles" / "ai-infra.json").read_text())
    assert prof["focus"]["agent-sre"] == "Agent SLOs"
    assert "agent-sre" in prof["bands"][0]["roles"]


def test_new_pending_announces_each_undecided_proposal_once():
    from aicg.approvals import ROLE_LABEL, new_pending

    issues = [
        {"number": 1, "title": "a", "url": "u1", "labels": [{"name": CONTENT_LABEL}]},
        {"number": 2, "title": "b", "url": "u2", "labels": [{"name": ROLE_LABEL}]},
        {"number": 3, "title": "c", "url": "u3", "labels": [{"name": ROLE_LABEL}, {"name": APPROVED_LABEL}]},
    ]
    gh = FakeGh(issues)
    assert [i["number"] for i in new_pending(gh, seen=set())] == [1, 2]
    assert [i["number"] for i in new_pending(gh, seen={1})] == [2]


def test_content_approval_applies_the_delta_carried_by_the_issue(tmp_path: Path):
    delta = {"modules": [{"id": "mod-113-mcp-evals"}], "exercises": [], "projects": []}
    _, body = content_issue("ai-engineering", "AI Engineering", "rag-engineer", "RAG Engineer", "2026-11",
                            {"accepted": {"modules": delta["modules"]}}, "https://github.com/o/r/pull/12",
                            delta=delta)
    actions = actions_for(parse_meta(body), _ctx(tmp_path))
    stage = next(a for a in actions if a.python)
    stage.python()
    written = tmp_path / "rag-engineer-learning" / ".aicg" / "curriculum-plan-delta-filtered.json"
    assert json.loads(written.read_text()) == delta
    labels = [a.label for a in actions]
    assert labels.index("stage the approved plan change") < labels.index("apply to the curriculum plan")
