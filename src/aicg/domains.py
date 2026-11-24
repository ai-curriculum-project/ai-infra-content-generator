"""Multi-tenant domain resolution (roadmap §2.2).

The harness was single-org (one `config/aicg-org.yaml`). Multi-tenancy makes
"domain" the boundary above "role": each domain is a full org manifest pointing
at its own GitHub org + repos + role ladder + rubric. The existing AI-infra
config is the default domain (`ai-infra`), so every current command keeps working
unchanged; additional domains live under `config/domains/<domain>.yaml`.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_DOMAIN = "ai-infra"


def _runner_root(runner_root: Path | None = None) -> Path:
    return runner_root or Path(__file__).resolve().parents[2]


def domains_dir(runner_root: Path | None = None) -> Path:
    return _runner_root(runner_root) / "config" / "domains"


def domain_config_path(domain: str | None, runner_root: Path | None = None) -> Path:
    """Resolve a domain name to its manifest path.

    The default domain (``ai-infra`` / None) maps to the legacy
    ``config/aicg-org.yaml``; any other domain to ``config/domains/<domain>.yaml``.
    """
    root = _runner_root(runner_root)
    if not domain or domain == DEFAULT_DOMAIN:
        return root / "config" / "aicg-org.yaml"
    return domains_dir(runner_root) / f"{domain}.yaml"


def domain_state_dir(domain: str | None, runner_root: Path | None = None) -> Path:
    """Resolve a domain's runner state dir, as the timers actually write it.

    Mirrors ``scripts/install-domain-timers.sh`` (``STATE_DIR=.aicg/$DOMAIN``);
    the default domain keeps the legacy ``.aicg/org``. Don't use
    ``state_dir_for_manifest`` for cross-domain reads: every domain manifest
    sets ``automation.state_dir: .aicg/org`` and the timers override it with
    ``--state-dir``, so the manifest value would point all domains at ai-infra.
    """
    root = _runner_root(runner_root)
    if not domain or domain == DEFAULT_DOMAIN:
        return root / ".aicg" / "org"
    return root / ".aicg" / domain


def list_domains(runner_root: Path | None = None) -> list[str]:
    """All registered domains: the default plus any under config/domains/."""
    domains = [DEFAULT_DOMAIN]
    d = domains_dir(runner_root)
    if d.is_dir():
        domains += sorted(p.stem for p in d.glob("*.yaml"))
    return domains


def domain_exists(domain: str, runner_root: Path | None = None) -> bool:
    return domain == DEFAULT_DOMAIN or domain_config_path(domain, runner_root).exists()


def calibration_corpus_path(domain: str | None, runner_root: Path | None = None) -> Path:
    """Resolve a domain's calibration corpus dir (roadmap §2.3).

    The default domain keeps the legacy ``calibration/corpus`` so the AI-infra
    BAR calibration is unchanged; any other domain uses
    ``calibration/<domain>/corpus`` so each tenant picks its own BAR from its
    own good/bad exemplars.
    """
    root = _runner_root(runner_root)
    if not domain or domain == DEFAULT_DOMAIN:
        return root / "calibration" / "corpus"
    return root / "calibration" / domain / "corpus"
