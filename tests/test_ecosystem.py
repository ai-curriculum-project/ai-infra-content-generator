"""The org family is defined once (domain_provision.ECOSYSTEM); everything else derives from it."""

from __future__ import annotations

import json
from pathlib import Path

from aicg.bootstrap import _site_banner
from aicg.domain_provision import ECOSYSTEM, ecosystem_banner_body

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_banner_links_every_org_in_order():
    body = ecosystem_banner_body()
    positions = [body.index(f"https://github.com/{s.org})") for s in ECOSYSTEM]
    assert positions == sorted(positions)


def test_bootstrap_banner_uses_shared_body():
    banner = _site_banner("any-org")
    assert ecosystem_banner_body() in banner
    assert banner.startswith("<!-- aicg:site-banner -->")


def test_every_domain_config_org_is_in_ecosystem():
    orgs = {s.org.lower() for s in ECOSYSTEM}
    paths = [REPO_ROOT / "config" / "aicg-org.yaml", *sorted((REPO_ROOT / "config" / "domains").glob("*.yaml"))]
    for path in paths:
        org = json.loads(path.read_text(encoding="utf-8"))["org"].lower()
        assert org in orgs, f"{path.name}: org {org!r} missing from ECOSYSTEM"


def test_every_ecosystem_org_has_a_stance():
    stances = json.loads((REPO_ROOT / "config" / "stances.json").read_text(encoding="utf-8"))
    configs = {
        json.loads(p.read_text(encoding="utf-8"))["org"].lower(): p.stem
        for p in (REPO_ROOT / "config" / "domains").glob("*.yaml")
    }
    configs["ai-infra-curriculum"] = "ai-infra"
    for sibling in ECOSYSTEM:
        domain = configs.get(sibling.org.lower())
        assert domain in stances["domains"], f"no stance for {sibling.org}"
