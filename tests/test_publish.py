"""Keeping generated pages current: org profiles, Module 00, and the sites."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_security_workspace, write_file, write_minimal_manifest

from aicg.org_config import load_manifest


def _profile() -> dict:
    return {"tagline": "t", "overview": "o", "focus": {"security": "f"}, "quick_start_role": "security"}


def test_publish_profiles_writes_only_changed_pages(tmp_path: Path):
    from aicg.publish import publish_profiles

    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "ai-infra.json").write_text(json.dumps(_profile()), encoding="utf-8")
    ws = tmp_path / "ws"
    dotgithub = manifest.repo_path(ws, ".github")
    dotgithub.mkdir(parents=True)

    first = publish_profiles({"ai-infra": manifest}, profiles, ws)
    assert first == [dotgithub / "profile" / "README.md"]
    assert "# AI Infrastructure Curriculum" in first[0].read_text()
    assert publish_profiles({"ai-infra": manifest}, profiles, ws) == []


def test_publish_profiles_skips_missing_checkouts_and_profiles(tmp_path: Path):
    from aicg.publish import publish_profiles

    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    assert publish_profiles({"ai-infra": manifest}, tmp_path / "none", tmp_path / "ws") == []


def test_weekly_audit_no_longer_queues_agent_profile_edits(tmp_path: Path):
    from aicg.org_runner import run_org_audit

    workspace = make_security_workspace(tmp_path)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    dotgithub = manifest.repo_path(workspace, ".github")
    write_file(dotgithub / "profile" / "README.md", "# Hi")  # thin + missing repos -> stale
    queue = run_org_audit(manifest, workspace, state_dir=tmp_path / "state")
    assert not [i for i in queue["work_items"] if i["type"] == "org_profile_stale"]


def _approvals_ctx(tmp_path: Path):
    from aicg.approvals import Context

    return Context(
        runner_dir=tmp_path / "gen", workspace=tmp_path,
        manifest_for={"ai-infra": tmp_path / "gen/config/aicg-org.yaml"},
        orgs={"ai-infra": "ai-infra-curriculum"}, roles={"ai-infra": set()},
        store=tmp_path / "store", runner_login="bot",
    )


def test_launched_jobs_wait_for_the_lock_instead_of_bailing(tmp_path: Path):
    from aicg.approvals import actions_for

    meta = {"kind": "role", "domain": "ai-infra", "role_id": "agent-sre", "title": "Agent SRE",
            "level": 35, "aliases": [], "focus": "f", "description": "d"}
    actions = actions_for(meta, _approvals_ctx(tmp_path))
    author = next(a for a in actions if a.label == "start authoring")
    script = author.argv[-1]
    assert author.argv[:2] == ["systemd-run", "--user"]
    assert ".aicg/org/aicg-org.lock" in script and "while" in script and "generate-role" in script


def test_new_role_approval_refreshes_the_pages(tmp_path: Path):
    from aicg.approvals import actions_for

    meta = {"kind": "role", "domain": "ai-infra", "role_id": "agent-sre", "title": "Agent SRE",
            "level": 35, "aliases": [], "focus": "f", "description": "d"}
    labels = [a.label for a in actions_for(meta, _approvals_ctx(tmp_path))]
    assert labels[-1] == "refresh pages and sites"
    publish = actions_for(meta, _approvals_ctx(tmp_path))[-1].argv[-1]
    assert "aicg-org-job.sh publish" in publish
