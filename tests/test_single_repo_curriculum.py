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
