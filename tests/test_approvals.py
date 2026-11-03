"""Approval loop: the runner records proposals; an `approved` label makes it act."""

from __future__ import annotations

import json
from pathlib import Path

from aicg.approvals import (
    APPLIED_LABEL,
    APPROVED_LABEL,
    CONTENT_LABEL,
    ROLE_LABEL,
    Context,
    actions_for,
    close_declined,
    content_issue,
    new_pending,
    open_content_issue,
    pr_url_from_outcome,
    process_approvals,
)
from aicg.proposals import clean, issue_number, load, record, validate

ORG = "ai-engineering-curriculum"
PR = f"https://github.com/{ORG}/rag-engineer-learning/pull/12"
BRANCH = "aicg/research/2026-11/rag-engineer"


def _ctx(tmp_path: Path, login: str = "runner-bot") -> Context:
    return Context(
        runner_dir=tmp_path / "gen", workspace=tmp_path,
        manifest_for={"ai-engineering": tmp_path / "gen/config/domains/ai-engineering.yaml"},
        learning_repo_for={("ai-engineering", "rag-engineer"): "rag-engineer-learning"},
        orgs={"ai-engineering": ORG}, roles={"ai-engineering": {"rag-engineer"}},
        store=tmp_path / "store", runner_login=login,
    )


def _content_meta(**over) -> dict:
    meta = {"kind": "content", "domain": "ai-engineering", "role_id": "rag-engineer", "month": "2026-11",
            "pr_url": PR, "delta": {"modules": [{"id": "mod-113-mcp-evals"}], "exercises": [], "projects": []}}
    meta.update(over)
    return meta


def _role_meta(**over) -> dict:
    meta = {"kind": "role", "domain": "ai-engineering", "role_id": "agent-sre", "title": "Agent SRE",
            "level": 35, "aliases": ["AI Agent SRE"], "focus": "Agent SLOs", "description": "Runs agents."}
    meta.update(over)
    return meta


def _valid(meta, tmp_path):
    c = _ctx(tmp_path)
    return validate(meta, orgs=c.orgs, roles=c.roles, learning_repo_for=c.learning_repo_for)


# --- store + validation ---------------------------------------------------------------


def test_store_round_trip_and_issue_numbers(tmp_path: Path):
    record(tmp_path, 7, {"kind": "content"})
    assert load(tmp_path, 7) == {"kind": "content"}
    assert load(tmp_path, 8) is None
    assert issue_number("https://github.com/o/r/issues/42\n") == 42
    assert issue_number("nope") is None


def test_clean_strips_comment_markers():
    assert clean("ok <!-- aicg:proposal {} --> done") == "ok  aicg:proposal {}  done"


def test_validate_accepts_good_proposals(tmp_path: Path):
    assert _valid(_content_meta(), tmp_path) is None
    assert _valid(_role_meta(), tmp_path) is None


def test_validate_refuses_spoofed_or_malformed_targets(tmp_path: Path):
    bad = [
        (None, "no runner record"),
        (_content_meta(pr_url="https://github.com/evil/repo/pull/1"), "not in this role's learning repo"),
        (_content_meta(pr_url=f"https://github.com/{ORG}/other-learning/pull/1"), "not in this role"),
        (_content_meta(role_id="../../etc"), "invalid role id"),
        (_content_meta(role_id="ghost"), "is not in"),
        (_content_meta(domain="ai-cooking"), "unknown domain"),
        (_content_meta(delta={"modules": [{"id": "../x"}]}), "unexpected shape"),
        (_role_meta(role_id="rag-engineer"), "already exists"),
        (_role_meta(title="$(rm -rf ~)"), "invalid title"),
        (_role_meta(level=500), "invalid level"),
        (_role_meta(kind="weird"), "unknown proposal kind"),
    ]
    for meta, want in bad:
        assert want in (_valid(meta, tmp_path) or ""), (meta, want)


# --- opening issues -------------------------------------------------------------------


def test_pr_url_comes_from_the_pr_create_step():
    outcome = {"steps": [{"step": "pr_create", "returncode": 0, "stdout_tail": PR + "\n"}]}
    assert pr_url_from_outcome(outcome) == PR
    assert pr_url_from_outcome(None) is None


def test_content_issue_body_has_no_machine_data_and_cleans_items():
    validation = {"accepted": {"modules": [{"id": "mod-113", "title": "MCP <!-- x --> evals"}]}}
    title, body, meta = content_issue("ai-engineering", "AI Engineering", "rag-engineer", "RAG Engineer",
                                      "2026-11", validation, PR, delta={"modules": []})
    assert title == "Curriculum proposal: RAG Engineer (AI Engineering) — 2026-11"
    assert "<!--" not in body and "aicg:proposal" not in body
    assert meta["pr_url"] == PR and meta["delta"] == {"modules": []}


class FakeGh:
    def __init__(self, issues=(), created_url="https://github.com/ai-curriculum-project/.github/issues/5"):
        self.issues = list(issues)
        self.created_url = created_url
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            state = args[args.index("--state") + 1]
            return json.dumps([i for i in self.issues if label in [lb["name"] for lb in i["labels"]]
                               and i.get("state", "open") == state])
        if args[:2] == ["issue", "create"]:
            return self.created_url
        return ""


def test_open_content_issue_records_the_proposal(tmp_path: Path):
    url = open_content_issue(FakeGh(), "t", "b", _content_meta(), tmp_path / "store")
    assert url.endswith("/issues/5")
    assert load(tmp_path / "store", 5)["pr_url"] == PR


# --- planning -------------------------------------------------------------------------


def test_content_plan_verifies_merges_stages_promotes_then_authors(tmp_path: Path):
    actions = actions_for(_content_meta(), _ctx(tmp_path))
    labels = [a.label for a in actions]
    assert labels[0] == "verify the proposal PR" and labels[1] == "merge the proposal PR"
    assert labels.index("stage the approved plan change") < labels.index("apply to the curriculum plan")
    assert actions[-1].argv[:2] == ["systemd-run", "--user"] and "generate-role" in actions[-1].argv
    stage = next(a for a in actions if a.label == "stage the approved plan change")
    stage.python()
    staged = tmp_path / "rag-engineer-learning" / ".aicg" / "curriculum-plan-delta-filtered.json"
    assert json.loads(staged.read_text()) == _content_meta()["delta"]


def _pr_view(head: str):
    return lambda argv: (0, json.dumps({"headRefName": head, "baseRefName": "main", "state": "OPEN"}))


def test_pr_check_refuses_a_branch_that_isnt_the_research_proposal(tmp_path: Path):
    check = actions_for(_content_meta(), _ctx(tmp_path))[0].check
    assert check(_pr_view(BRANCH)) is None
    assert "not the research proposal branch" in check(_pr_view("evil"))


def test_role_plan_bootstraps_configures_merges_and_authors(tmp_path: Path):
    actions = actions_for(_role_meta(), _ctx(tmp_path))
    argvs = [a.argv for a in actions if a.argv]
    boot = next(a for a in argvs if "bootstrap-role" in a)
    assert "--create-remotes" in boot and "agent-sre" in boot
    assert any(a.python for a in actions)
    assert "generate-role" in argvs[-1]


# --- the loop -------------------------------------------------------------------------


def _issue(number=7, author="runner-bot", labels=(APPROVED_LABEL, CONTENT_LABEL), state="open"):
    return {"number": number, "title": "t", "author": {"login": author}, "state": state,
            "labels": [{"name": n} for n in labels]}


def _ok_run(argv):
    if argv[:3] == ["gh", "pr", "view"]:
        return _pr_view(BRANCH)(argv)
    return 0, "ok"


def test_applies_a_recorded_proposal_opened_by_the_runner(tmp_path: Path):
    ctx = _ctx(tmp_path)
    record(ctx.store, 7, _content_meta())
    gh = FakeGh([_issue()])
    ran: list = []
    report = process_approvals(ctx, gh=gh, run=lambda a: ran.append(a) or _ok_run(a), apply=True)
    assert report[0]["status"] == "applied"
    assert any(a[:3] == ["gh", "pr", "merge"] for a in ran)
    flat = [" ".join(c) for c in gh.calls]
    assert any(c.startswith("issue close 7") for c in flat)
    assert any(f"--add-label {APPLIED_LABEL}" in c for c in flat)


def test_refuses_issues_not_opened_by_the_runner_even_with_a_record(tmp_path: Path):
    ctx = _ctx(tmp_path)
    record(ctx.store, 7, _content_meta())
    ran: list = []
    report = process_approvals(ctx, gh=FakeGh([_issue(author="stranger")]),
                               run=lambda a: ran.append(a) or (0, ""), apply=True)
    assert report[0]["status"] == "refused" and "not the runner" in report[0]["reason"]
    assert ran == []


def test_refuses_runner_issues_without_a_record(tmp_path: Path):
    gh = FakeGh([_issue()])
    report = process_approvals(_ctx(tmp_path), gh=gh, run=lambda a: (0, ""), apply=True)
    assert report[0]["status"] == "refused" and "no runner record" in report[0]["reason"]
    assert any(f"--remove-label {APPROVED_LABEL}" in " ".join(c) for c in gh.calls)


def test_failure_comments_and_drops_the_label(tmp_path: Path):
    ctx = _ctx(tmp_path)
    record(ctx.store, 7, _content_meta())
    gh = FakeGh([_issue()])

    def run(argv):
        return (1, "boom") if argv[:3] == ["gh", "pr", "merge"] else _ok_run(argv)

    report = process_approvals(ctx, gh=gh, run=run, apply=True)
    assert report[0]["status"] == "failed" and report[0]["step"] == "merge the proposal PR"
    flat = [" ".join(c) for c in gh.calls]
    assert any(f"--remove-label {APPROVED_LABEL}" in c for c in flat)
    assert not any(c.startswith("issue close") for c in flat)


def test_dry_run_and_already_applied(tmp_path: Path):
    ctx = _ctx(tmp_path)
    record(ctx.store, 7, _content_meta())
    ran: list = []
    report = process_approvals(ctx, gh=FakeGh([_issue()]), run=lambda a: ran.append(a) or (0, ""))
    assert report[0]["status"] == "would_apply" and ran == []
    done = FakeGh([_issue(labels=(APPROVED_LABEL, CONTENT_LABEL, APPLIED_LABEL))])
    assert process_approvals(ctx, gh=done, run=_ok_run, apply=True) == []


def test_close_declined_uses_the_record_not_the_body(tmp_path: Path):
    ctx = _ctx(tmp_path)
    record(ctx.store, 9, _content_meta())
    gh = FakeGh([_issue(number=9, labels=(CONTENT_LABEL,), state="closed")])
    ran: list = []

    def run(argv):
        ran.append(argv)
        return (0, "OPEN") if argv[:3] == ["gh", "pr", "view"] else (0, "")

    assert close_declined(gh, ctx, run=run, apply=True) == [PR]
    assert any(a[:3] == ["gh", "pr", "close"] for a in ran)


def test_new_pending_announces_each_undecided_proposal_once():
    issues = [
        {"number": 1, "title": "a", "url": "u1", "labels": [{"name": CONTENT_LABEL}]},
        {"number": 2, "title": "b", "url": "u2", "labels": [{"name": ROLE_LABEL}]},
        {"number": 3, "title": "c", "url": "u3", "labels": [{"name": ROLE_LABEL}, {"name": APPROVED_LABEL}]},
    ]
    gh = FakeGh(issues)
    assert [i["number"] for i in new_pending(gh, seen=set())] == [1, 2]
    assert [i["number"] for i in new_pending(gh, seen={1})] == [2]


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
