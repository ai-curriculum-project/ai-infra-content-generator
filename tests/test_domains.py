"""Tests for multi-tenant domain resolution (§2.2)."""

from pathlib import Path

from aicg.domains import (
    DEFAULT_DOMAIN,
    domain_config_path,
    domain_exists,
    list_domains,
)


def test_default_domain_maps_to_legacy_config(tmp_path: Path) -> None:
    # None and the default name both resolve to the legacy single-org config.
    assert domain_config_path(None, tmp_path) == tmp_path / "config" / "aicg-org.yaml"
    assert domain_config_path(DEFAULT_DOMAIN, tmp_path) == tmp_path / "config" / "aicg-org.yaml"


def test_named_domain_maps_under_domains_dir(tmp_path: Path) -> None:
    assert (
        domain_config_path("nursing", tmp_path)
        == tmp_path / "config" / "domains" / "nursing.yaml"
    )


def test_list_domains_includes_default_plus_registered(tmp_path: Path) -> None:
    d = tmp_path / "config" / "domains"
    d.mkdir(parents=True)
    (d / "nursing.yaml").write_text("{}", encoding="utf-8")
    (d / "accounting.yaml").write_text("{}", encoding="utf-8")
    assert list_domains(tmp_path) == ["ai-infra", "accounting", "nursing"]


def test_list_domains_default_only_when_no_dir(tmp_path: Path) -> None:
    assert list_domains(tmp_path) == ["ai-infra"]


def test_domain_exists(tmp_path: Path) -> None:
    d = tmp_path / "config" / "domains"
    d.mkdir(parents=True)
    (d / "nursing.yaml").write_text("{}", encoding="utf-8")
    assert domain_exists("ai-infra", tmp_path) is True  # always
    assert domain_exists("nursing", tmp_path) is True
    assert domain_exists("law", tmp_path) is False


def test_calibration_corpus_path_default_vs_domain(tmp_path):
    from aicg.domains import calibration_corpus_path
    # ai-infra (default) keeps the legacy path; siblings get a per-domain dir.
    assert calibration_corpus_path("ai-infra", tmp_path) == tmp_path / "calibration" / "corpus"
    assert calibration_corpus_path(None, tmp_path) == tmp_path / "calibration" / "corpus"
    assert calibration_corpus_path("ml-engineering", tmp_path) == tmp_path / "calibration" / "ml-engineering" / "corpus"


def test_ai_startup_domain_loads() -> None:
    from aicg.domains import list_domains
    from aicg.org_config import load_manifest

    assert "ai-startup" in list_domains()
    manifest = load_manifest(domain_config_path("ai-startup"))
    assert manifest.org == "ai-startup-curriculum"
    ids = {r.id for r in manifest.roles}
    assert {
        "startup-foundations",
        "founder-ceo",
        "cto",
        "startup-product-gtm",
        "cpo",
        "startup-finance-fundraising",
        "startup-operations-governance",
        "startup-exit",
    } == ids
    assert all(r.is_single for r in manifest.roles)
    assert manifest.solution_repo_names == []
    # curricula sorted by level: foundations(10), founder-ceo(20), cto(25)...
    assert manifest.learning_repo_names[1] == "founder-ceo-curriculum"
    cto = next(r for r in manifest.roles if r.id == "cto")
    assert cto.learning_repo == "cto-curriculum" and cto.is_single


def test_existing_domains_stay_pair_model() -> None:
    """Regression: the single-repo model change must not alter live domains.

    Every role in the four shipping domains must remain a learning+solutions
    pair with a real solution repo, so the nightly pipeline behaves identically.
    """
    from aicg.org_config import load_manifest

    for domain in ("ai-engineering", "ai-governance", "ml-engineering"):
        manifest = load_manifest(domain_config_path(domain))
        assert manifest.roles, domain
        for role in manifest.roles:
            assert role.is_single is False, f"{domain}:{role.id} became single"
            assert role.solution_repo, f"{domain}:{role.id} lost its solution repo"
            assert role.repos == (role.learning_repo, role.solution_repo)
        # solution repos still enumerated one-per-role
        assert len(manifest.solution_repo_names) == len(manifest.roles)
