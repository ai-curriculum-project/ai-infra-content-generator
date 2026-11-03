"""Tests for the rich, profile-driven org README renderer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aicg.org_config import load_manifest
from aicg.org_profile import ProfileError, load_profile, render_rich_profile

ROLES = [
    {
        "id": "staff",
        "title": "Staff ML Engineer",
        "level": 40,
        "learning_repo": "staff-learning",
        "solution_repo": "staff-solutions",
    },
    {
        "id": "mle",
        "title": "ML Engineer",
        "level": 20,
        "learning_repo": "mle-learning",
        "solution_repo": "mle-solutions",
    },
    {
        "id": "ft",
        "title": "Fine-Tuning Engineer",
        "level": 30,
        "learning_repo": "ft-learning",
        "solution_repo": "ft-solutions",
    },
]


def _manifest(tmp_path: Path, roles: list[dict] | None = None):
    data = {
        "org": "ml-engineering-curriculum",
        "default_remote": "git@github.com:ml-engineering-curriculum/{repo}.git",
        "maintained_by": {"phrasing": "Maintained by [VeriSwarm.ai](https://veriswarm.ai)"},
        "roles": roles or ROLES,
    }
    path = tmp_path / "m.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_manifest(path)


def _profile(**overrides) -> dict:
    base = {
        "tagline": "Build and train the models.",
        "overview": "This org covers *building & training* models.",
        "highlights": ["🧪 Every exercise has a reference solution"],
        "bands": [
            {"title": "🟢 Foundation (L20)", "min_level": 0, "max_level": 29},
            {"title": "🔵 Specialist (L30)", "min_level": 30, "max_level": 39},
            {"title": "⭐ Staff+ (L40+)", "min_level": 40, "max_level": 99},
        ],
        "focus": {
            "mle": "Data, classical ML, deep learning",
            "ft": "SFT, PEFT/LoRA, preference optimization",
            "staff": "Multi-team ML architecture",
        },
        "quick_start_role": "mle",
        "technologies": {"Training": "PyTorch, FSDP, DeepSpeed"},
        "discussions": True,
        "career_progression": True,
    }
    base.update(overrides)
    return base


def test_rich_profile_has_all_sections_in_order(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## ⚠️ AI-Generated Content Disclaimer",
        "## 🎯 Overview",
        "## 🔗 Curriculum Family",
        "## 📚 Learning Tracks",
        "## 🚀 Quick Start",
        "## 🛠️ Technologies Covered",
        "## 📈 Career Progression",
        "## 🤝 Contributing",
        "## 📜 License",
        "## 📞 Support",
    ]


def test_roles_are_grouped_into_bands_with_focus(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    foundation = text.index("### 🟢 Foundation (L20)")
    specialist = text.index("### 🔵 Specialist (L30)")
    staff = text.index("### ⭐ Staff+ (L40+)")
    assert foundation < text.index("**ML Engineer**") < specialist
    assert specialist < text.index("**Fine-Tuning Engineer**") < staff
    assert "| **Fine-Tuning Engineer** | SFT, PEFT/LoRA, preference optimization |" in text
    assert "[📘 Learning](https://github.com/ml-engineering-curriculum/ft-learning)" in text
    assert "[✅ Solutions](https://github.com/ml-engineering-curriculum/ft-solutions)" in text


def test_at_a_glance_counts_come_from_the_manifest(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    assert "**3 role tracks**" in text
    assert "**6 curriculum repositories** (3 learning + 3 solutions)" in text
    assert "[![Repositories: 6]" in text
    assert "🧪 Every exercise has a reference solution" in text


def test_quick_start_clones_the_chosen_role(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    assert "git clone https://github.com/ml-engineering-curriculum/mle-learning.git" in text


def test_self_is_marked_in_curriculum_family(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    assert "**ML Engineering Curriculum** *(you are here)*" in text
    assert "[AI Infrastructure Curriculum](https://github.com/ai-infra-curriculum)" in text


def test_support_links_follow_discussions_flag(tmp_path: Path) -> None:
    with_d = render_rich_profile(_manifest(tmp_path), _profile(discussions=True))
    without = render_rich_profile(_manifest(tmp_path), _profile(discussions=False))
    assert "https://github.com/orgs/ml-engineering-curriculum/discussions" in with_d
    assert "/discussions" not in without


def test_career_progression_section_is_optional(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile(career_progression=False))
    assert "## 📈 Career Progression" not in text
    assert "CAREER_PROGRESSION.md" not in text


def test_custom_sections_render_after_overview(tmp_path: Path) -> None:
    profile = _profile(sections=[{"heading": "🧭 The Three-Axis Model", "body": "Three axes."}])
    text = render_rich_profile(_manifest(tmp_path), profile)
    assert text.index("## 🎯 Overview") < text.index("## 🧭 The Three-Axis Model")
    assert text.index("## 🧭 The Three-Axis Model") < text.index("## 🔗 Curriculum Family")


def test_single_repo_roles_render_one_curriculum_link(tmp_path: Path) -> None:
    roles = [
        {
            "id": "ceo",
            "title": "Founder / CEO",
            "level": 20,
            "learning_repo": "founder-ceo-curriculum",
            "repo_model": "single",
        }
    ]
    profile = _profile(
        bands=None,
        focus={"ceo": "Discovery, runway, fundraising"},
        quick_start_role="ceo",
        tracks_heading="📚 Curricula",
    )
    text = render_rich_profile(_manifest(tmp_path, roles), profile)
    assert "[📗 Curriculum](https://github.com/ai-infra-curriculum" not in text
    assert (
        "[📗 Curriculum](https://github.com/ml-engineering-curriculum/founder-ceo-curriculum)"
        in text
    )
    assert "**1 curriculum repository**" in text
    assert "## 📚 Curricula" in text
    assert "| Role | Focus |" in text  # default label


def test_role_column_label_is_configurable(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile(role_column="Curriculum"))
    assert "| Curriculum | Focus | Repositories |" in text


def test_missing_focus_for_a_role_is_an_error(tmp_path: Path) -> None:
    profile = _profile(focus={"mle": "x", "ft": "y"})
    with pytest.raises(ProfileError, match="staff"):
        render_rich_profile(_manifest(tmp_path), profile)


def test_role_outside_every_band_is_an_error(tmp_path: Path) -> None:
    profile = _profile(bands=[{"title": "Only L20", "min_level": 0, "max_level": 29}])
    with pytest.raises(ProfileError, match="band"):
        render_rich_profile(_manifest(tmp_path), profile)


def test_unknown_quick_start_role_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="quick_start_role"):
        render_rich_profile(_manifest(tmp_path), _profile(quick_start_role="nope"))


def test_load_profile_reads_json_and_rejects_unknown_keys(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_profile()), encoding="utf-8")
    assert load_profile(good)["tagline"] == "Build and train the models."
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_profile(taglne="typo")), encoding="utf-8")
    with pytest.raises(ProfileError, match="taglne"):
        load_profile(bad)


def test_maintained_by_footer_is_kept(tmp_path: Path) -> None:
    text = render_rich_profile(_manifest(tmp_path), _profile())
    assert text.rstrip().endswith(
        "<!-- aicg:maintained-by -->\nMaintained by [VeriSwarm.ai](https://veriswarm.ai)"
    )


def test_shipped_profiles_render_for_every_domain() -> None:
    """Every config/profiles/<domain>.json must render against its manifest."""
    from aicg.domains import domain_config_path

    root = Path(__file__).resolve().parents[1]
    profiles = sorted((root / "config" / "profiles").glob("*.json"))
    assert profiles, "no shipped profiles found"
    for path in profiles:
        manifest = load_manifest(domain_config_path(path.stem))
        text = render_rich_profile(manifest, load_profile(path))
        assert "## 📚" in text and "(you are here)" in text


def test_bands_can_list_roles_explicitly(tmp_path: Path) -> None:
    profile = _profile(
        bands=[
            {"title": "Track A", "roles": ["ft", "mle"]},
            {"title": "Track B", "roles": ["staff"]},
        ]
    )
    text = render_rich_profile(_manifest(tmp_path), profile)
    a, b = text.index("### Track A"), text.index("### Track B")
    assert a < text.index("**ML Engineer**") < b
    assert a < text.index("**Fine-Tuning Engineer**") < b
    assert b < text.index("**Staff ML Engineer**")


def test_role_missing_from_explicit_bands_is_an_error(tmp_path: Path) -> None:
    profile = _profile(bands=[{"title": "Only some", "roles": ["mle", "ft"]}])
    with pytest.raises(ProfileError, match="staff"):
        render_rich_profile(_manifest(tmp_path), profile)


def test_profile_links_org_site_and_umbrella(tmp_path):
    text = render_rich_profile(_manifest(tmp_path), _profile())
    assert "https://ml-engineering-curriculum.github.io/" in text
    assert "https://ai-curriculum-project.github.io/" in text
    assert "([site](https://ai-infra-curriculum.github.io/))" in text
