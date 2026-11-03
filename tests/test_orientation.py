"""Tests for the per-role 'Your Relationship to the Model' orientation module."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aicg.org_config import load_manifest
from aicg.orientation import (
    BLOCK_END,
    BLOCK_START,
    ORIENTATION_SLUG,
    StanceError,
    load_stances,
    render_orientation,
    upsert_curriculum_block,
    write_orientation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAINS = ["ai-infra", "ml-engineering", "ai-engineering", "ai-governance", "ai-startup"]


def _stances() -> dict:
    hands = {d: {o: f"{d}->{o} handoff" for o in DOMAINS if o != d} for d in DOMAINS}
    return {
        "order": list(DOMAINS),
        "scenario": {
            "title": "One upgrade",
            "intro": "Intro.",
            "steps": {d: f"{d} step" for d in DOMAINS},
            "close": "Close.",
        },
        "domains": {
            d: {
                "verb": f"verb-{d}",
                "headline": f"Headline {d}.",
                "stance": f"Stance {d}.",
                "owns": [f"{d} owns A", f"{d} owns B"],
                "not_owns": f"{d} does not own X.",
                "hands_to": hands[d],
            }
            for d in DOMAINS
        },
    }


def _manifest(tmp_path: Path, org: str, roles: list[dict]):
    path = tmp_path / f"{org}.json"
    path.write_text(
        json.dumps(
            {
                "org": org,
                "default_remote": f"git@github.com:{org}/{{repo}}.git",
                "roles": roles,
            }
        ),
        encoding="utf-8",
    )
    return load_manifest(path)


def _role(rid: str, title: str, level: int, single: bool = False) -> dict:
    if single:
        return {
            "id": rid,
            "title": title,
            "level": level,
            "learning_repo": f"{rid}-curriculum",
            "solution_repo": None,
            "repo_model": "single",
        }
    return {
        "id": rid,
        "title": title,
        "level": level,
        "learning_repo": f"{rid}-learning",
        "solution_repo": f"{rid}-solutions",
    }


@pytest.fixture
def family(tmp_path: Path) -> dict:
    return {
        "ai-infra": _manifest(
            tmp_path,
            "ai-infra-curriculum",
            [_role("junior", "Junior Infra", 10), _role("infra", "Infra Engineer", 20),
             _role("senior-infra", "Senior Infra", 30)],
        ),
        "ml-engineering": _manifest(
            tmp_path, "ml-engineering-curriculum",
            [_role("mle", "ML Engineer", 20), _role("staff-mle", "Staff MLE", 40)],
        ),
        "ai-engineering": _manifest(
            tmp_path, "ai-engineering-curriculum",
            [_role("dev", "AI Developer", 20), _role("eng", "AI Engineer", 30)],
        ),
        "ai-governance": _manifest(
            tmp_path, "ai-governance-curriculum",
            [_role("analyst", "Gov Analyst", 15), _role("risk", "Risk Engineer", 25)],
        ),
        "ai-startup": _manifest(
            tmp_path, "ai-startup-curriculum",
            [_role("founder", "Founder / CEO", 20, single=True)],
        ),
    }


# --- stances ---------------------------------------------------------------------


def test_real_stances_file_is_valid():
    from aicg.domains import list_domains

    stances = load_stances(REPO_ROOT / "config" / "stances.json")
    assert set(stances["domains"]) == set(list_domains(REPO_ROOT))


def test_load_stances_rejects_missing_handoff(tmp_path: Path):
    data = _stances()
    del data["domains"]["ai-infra"]["hands_to"]["ml-engineering"]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StanceError, match="ai-infra.*ml-engineering"):
        load_stances(path)


def test_load_stances_rejects_missing_scenario_step(tmp_path: Path):
    data = _stances()
    del data["scenario"]["steps"]["ai-startup"]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StanceError, match="ai-startup"):
        load_stances(path)


def test_load_stances_rejects_domain_missing_from_order(tmp_path: Path):
    data = _stances()
    data["order"] = data["order"][:-1]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StanceError, match="order"):
        load_stances(path)


# --- render ----------------------------------------------------------------------


def test_render_places_role_in_its_org(family):
    files = render_orientation("infra", "ai-infra", family, _stances())
    readme = files["README.md"]
    assert readme.startswith("# Module 00: Your Relationship to the Model")
    assert "Infra Engineer" in readme
    assert "Headline ai-infra." in readme
    assert "ai-infra owns A" in readme
    assert "ai-infra does not own X." in readme
    assert "*(you are here)*" in readme


def test_render_lists_handoffs_both_directions(family):
    readme = render_orientation("infra", "ai-infra", family, _stances())["README.md"]
    assert "ai-infra->ml-engineering handoff" in readme
    assert "ml-engineering->ai-infra handoff" in readme
    # Never lists your own org as a counterpart.
    assert "ai-infra->ai-infra" not in readme


def test_render_picks_nearest_level_counterpart(family):
    # Infra Engineer is L20 -> ML Engineer (20), AI Developer (20), Risk Engineer (25 beats 15? no:
    # distance 5 vs 5 -> tie broken toward the lower level, Gov Analyst).
    readme = render_orientation("infra", "ai-infra", family, _stances())["README.md"]
    assert "[ML Engineer](https://github.com/ml-engineering-curriculum/mle-learning)" in readme
    assert "[AI Developer](https://github.com/ai-engineering-curriculum/dev-learning)" in readme
    assert "[Gov Analyst](https://github.com/ai-governance-curriculum/analyst-learning)" in readme
    assert "[Founder / CEO](https://github.com/ai-startup-curriculum/founder-curriculum)" in readme


def test_render_shows_ladder_neighbours(family):
    readme = render_orientation("infra", "ai-infra", family, _stances())["README.md"]
    assert "Junior Infra" in readme
    assert "Senior Infra" in readme


def test_render_handles_top_and_bottom_of_ladder(family):
    bottom = render_orientation("junior", "ai-infra", family, _stances())["README.md"]
    top = render_orientation("senior-infra", "ai-infra", family, _stances())["README.md"]
    assert "the entry point" in bottom
    assert "the top of this ladder" in top


def test_render_highlights_own_scenario_step(family):
    readme = render_orientation("infra", "ai-infra", family, _stances())["README.md"]
    assert "**ai-infra step** ← your part" in readme
    assert "ml-engineering step" in readme


def test_render_includes_exercise(family):
    files = render_orientation("infra", "ai-infra", family, _stances())
    exercise = files["exercises/exercise-01-map-your-model-boundary.md"]
    assert "Infra Engineer" in exercise
    assert len(exercise) > 200


def test_render_unknown_role_raises(family):
    with pytest.raises(KeyError):
        render_orientation("nope", "ai-infra", family, _stances())


def test_render_is_deterministic(family):
    a = render_orientation("infra", "ai-infra", family, _stances())
    b = render_orientation("infra", "ai-infra", family, copy.deepcopy(_stances()))
    assert a == b


# --- CURRICULUM.md block --------------------------------------------------------


def test_upsert_inserts_after_h1():
    text = "# My Curriculum\n\nIntro.\n"
    out = upsert_curriculum_block(text)
    assert out.startswith("# My Curriculum\n\n" + BLOCK_START)
    assert f"lessons/{ORIENTATION_SLUG}/README.md" in out
    assert out.rstrip().endswith("Intro.")


def test_upsert_is_idempotent():
    once = upsert_curriculum_block("# T\n\nBody\n")
    assert upsert_curriculum_block(once) == once


def test_upsert_replaces_existing_block():
    stale = f"# T\n\n{BLOCK_START}\nold\n{BLOCK_END}\n\nBody\n"
    out = upsert_curriculum_block(stale)
    assert "old" not in out
    assert out.count(BLOCK_START) == 1


def test_upsert_without_h1_prepends():
    out = upsert_curriculum_block("Body only\n")
    assert out.startswith(BLOCK_START)


def test_upsert_uses_given_container():
    out = upsert_curriculum_block("# T\n", container="modules")
    assert f"modules/{ORIENTATION_SLUG}/README.md" in out


# --- write -----------------------------------------------------------------------


def test_write_creates_files_and_links_curriculum(tmp_path: Path, family):
    repo = tmp_path / "ws" / "infra-learning"
    (repo / "lessons").mkdir(parents=True)
    (repo / "CURRICULUM.md").write_text("# Infra\n\nBody\n", encoding="utf-8")
    written = write_orientation(tmp_path / "ws", "infra", "ai-infra", family, _stances())
    base = repo / "lessons" / ORIENTATION_SLUG
    assert (base / "README.md").exists()
    assert (base / "exercises" / "exercise-01-map-your-model-boundary.md").exists()
    assert BLOCK_START in (repo / "CURRICULUM.md").read_text(encoding="utf-8")
    assert len(written) == 3


def test_write_uses_modules_container_when_present(tmp_path: Path, family):
    repo = tmp_path / "ws" / "infra-learning"
    (repo / "modules").mkdir(parents=True)
    write_orientation(tmp_path / "ws", "infra", "ai-infra", family, _stances())
    assert (repo / "modules" / ORIENTATION_SLUG / "README.md").exists()


def test_write_skips_missing_repo(tmp_path: Path, family):
    assert write_orientation(tmp_path, "infra", "ai-infra", family, _stances()) == []


def test_orientation_dir_is_invisible_to_module_audits():
    # The module audits only consider directories matching ^mod-\d+; the
    # orientation must not be treated as a planned module (no solutions pair).
    import re

    assert not re.match(r"^mod-\d+", ORIENTATION_SLUG)


def _path_stances() -> dict:
    data = _stances()
    data["domains"]["ai-startup"]["path_levels"] = True
    data["domains"]["ai-startup"]["counterpart_for"] = {"ai-infra": "founder"}
    return data


def test_path_level_sibling_uses_declared_counterpart(family):
    readme = render_orientation("junior", "ai-infra", family, _path_stances())["README.md"]
    assert "[Founder / CEO](https://github.com/ai-startup-curriculum/founder-curriculum)" in readme


def test_path_level_role_pairs_with_most_senior_sibling(family):
    readme = render_orientation("founder", "ai-startup", family, _path_stances())["README.md"]
    assert "[Senior Infra](https://github.com/ai-infra-curriculum/senior-infra-learning)" in readme
    assert "[Staff MLE](https://github.com/ml-engineering-curriculum/staff-mle-learning)" in readme
    assert "[Risk Engineer](https://github.com/ai-governance-curriculum/risk-learning)" in readme


def test_load_stances_rejects_unknown_counterpart_domain(tmp_path: Path):
    data = _path_stances()
    data["domains"]["ai-startup"]["counterpart_for"]["nope"] = "x"
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StanceError, match="nope"):
        load_stances(path)


def test_family_heading_counts_orgs(family):
    readme = render_orientation("infra", "ai-infra", family, _stances())["README.md"]
    assert "## The five relationships" in readme
