"""Tests for single-repo curriculum support (repo_model: single).

The startup domain uses single `*-curriculum` repos instead of the
learning+solutions pairs the four AI domains use. These tests lock the
additive model change so the existing pair-based domains stay intact.
"""

from __future__ import annotations

import json

import pytest

from aicg.org_config import ManifestError, load_manifest

_BASE = {
    "org": "ai-startup-curriculum",
    "default_remote": "git@github.com:ai-startup-curriculum/{repo}.git",
    "extra_repos": [{"name": ".github", "kind": "org-profile"}],
    "release": {},
    "documentation": {},
    "schedules": {},
    "automation": {},
    "content_generation": {},
    "quality_judge": {},
    "pipeline": {},
    "job_requirements": {},
    "research": {},
    "maintained_by": {},
}


def _write(tmp_path, roles):
    cfg = dict(_BASE, roles=roles)
    p = tmp_path / "ai-startup.yaml"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_single_repo_role_loads(tmp_path):
    p = _write(
        tmp_path,
        [
            {
                "id": "founder-ceo",
                "title": "Founder / CEO",
                "level": 20,
                "learning_repo": "founder-ceo-curriculum",
                "repo_model": "single",
            }
        ],
    )
    m = load_manifest(p)
    r = m.roles[0]
    assert r.is_single is True
    assert r.solution_repo is None
    assert r.repos == ("founder-ceo-curriculum",)
    assert m.solution_repo_names == []
    assert m.repo_names == ["founder-ceo-curriculum", ".github"]


def test_pair_role_still_defaults(tmp_path):
    p = _write(
        tmp_path,
        [
            {
                "id": "engineer",
                "title": "Engineer",
                "level": 20,
                "learning_repo": "engineer-learning",
                "solution_repo": "engineer-solutions",
            }
        ],
    )
    m = load_manifest(p)
    r = m.roles[0]
    assert r.is_single is False
    assert r.repo_model == "pair"
    assert r.repos == ("engineer-learning", "engineer-solutions")
    assert m.solution_repo_names == ["engineer-solutions"]


def test_single_with_solution_repo_rejected(tmp_path):
    p = _write(
        tmp_path,
        [
            {
                "id": "x",
                "title": "X",
                "level": 1,
                "learning_repo": "x-curriculum",
                "solution_repo": "x-solutions",
                "repo_model": "single",
            }
        ],
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_pair_without_solution_repo_rejected(tmp_path):
    p = _write(
        tmp_path,
        [{"id": "x", "title": "X", "level": 1, "learning_repo": "x-learning"}],
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_bootstrap_single_repo_writes_one_tree(tmp_path):
    from aicg.bootstrap import bootstrap_role

    p = _write(
        tmp_path,
        [
            {
                "id": "founder-ceo",
                "title": "Founder / CEO",
                "level": 20,
                "learning_repo": "founder-ceo-curriculum",
                "repo_model": "single",
            }
        ],
    )
    m = load_manifest(p)
    ws = tmp_path / "ws"
    ws.mkdir()
    report = bootstrap_role(
        manifest=m,
        workspace=ws,
        role_id="founder-ceo",
        title="Founder / CEO",
        level=20,
        write_manifest=False,
        state_dir=tmp_path / "state",
    )
    curriculum = ws / "founder-ceo-curriculum"
    assert (curriculum / "README.md").exists()
    assert (curriculum / "CURRICULUM.md").exists()
    assert (curriculum / "exemplars" / "README.md").exists()
    assert (curriculum / "lessons" / "README.md").exists()
    assert not (ws / "founder-ceo-solutions").exists()
    assert report["plan"]["solution_repo"] is None
    assert report["plan"]["solution_path"] is None


def test_bootstrap_pair_repo_still_writes_two_trees(tmp_path):
    from aicg.bootstrap import bootstrap_role

    p = _write(
        tmp_path,
        [
            {
                "id": "engineer",
                "title": "Engineer",
                "level": 20,
                "learning_repo": "engineer-learning",
                "solution_repo": "engineer-solutions",
            }
        ],
    )
    m = load_manifest(p)
    ws = tmp_path / "ws"
    ws.mkdir()
    bootstrap_role(
        manifest=m,
        workspace=ws,
        role_id="engineer",
        title="Engineer",
        level=20,
        write_manifest=False,
        state_dir=tmp_path / "state",
    )
    assert (ws / "engineer-learning" / "README.md").exists()
    assert (ws / "engineer-solutions" / "README.md").exists()


def test_pairing_audit_skips_single_repo(tmp_path):
    from aicg.pairing_audit import audit_pairing

    p = _write(
        tmp_path,
        [
            {
                "id": "founder-ceo",
                "title": "Founder / CEO",
                "level": 20,
                "learning_repo": "founder-ceo-curriculum",
                "repo_model": "single",
            }
        ],
    )
    m = load_manifest(p)
    ws = tmp_path / "ws"
    ws.mkdir()
    report = audit_pairing(manifest=m, workspace=ws, state_dir=tmp_path / "state")
    roles = {r["role"]: r for r in report["roles"]}
    assert roles["founder-ceo"]["status"] == "skipped"
    assert "single-repo" in roles["founder-ceo"]["reason"]


def test_org_profile_renders_single_repo_row(tmp_path):
    from aicg.domain_provision import render_org_profile

    p = _write(
        tmp_path,
        [
            {
                "id": "founder-ceo",
                "title": "Founder / CEO",
                "level": 20,
                "learning_repo": "founder-ceo-curriculum",
                "repo_model": "single",
            }
        ],
    )
    m = load_manifest(p)
    md = render_org_profile(m, tagline=None)
    assert "founder-ceo-curriculum" in md
    # single repo → no bogus ".../None" solutions link
    assert "/None" not in md
