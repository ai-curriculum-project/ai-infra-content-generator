"""Manifest model for AI Infrastructure Curriculum org automation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_loader import ConfigError, load_config


class ManifestError(RuntimeError):
    """Raised when the org manifest is missing or malformed."""


@dataclass(frozen=True)
class RoleConfig:
    id: str
    title: str
    level: int
    learning_repo: str
    # ``solution_repo`` is ``None`` for single-repo curricula
    # (``repo_model == "single"``); required for the default pair model.
    solution_repo: str | None = None
    # Alternative/synonym job titles the research cycle should also search,
    # so sparse-title roles still clear the evidence gate.
    aliases: tuple[str, ...] = ()
    # ``"pair"`` (learning + solutions, the AI-domain default) or ``"single"``
    # (one ``<name>-curriculum`` repo, the startup-domain model).
    repo_model: str = "pair"

    @property
    def is_single(self) -> bool:
        return self.repo_model == "single"

    @property
    def repos(self) -> tuple[str, ...]:
        """The repo name(s) this role owns, in a stable order."""
        if self.is_single or not self.solution_repo:
            return (self.learning_repo,)
        return (self.learning_repo, self.solution_repo)


@dataclass(frozen=True)
class ExtraRepoConfig:
    name: str
    kind: str
    release: bool = False


@dataclass(frozen=True)
class OrgManifest:
    org: str
    default_remote: str
    roles: tuple[RoleConfig, ...]
    extra_repos: tuple[ExtraRepoConfig, ...]
    release: dict[str, Any]
    documentation: dict[str, Any]
    schedules: dict[str, str]
    automation: dict[str, Any]
    content_generation: dict[str, Any]
    quality_judge: dict[str, Any]
    pipeline: dict[str, Any]
    job_requirements: dict[str, Any]
    research: dict[str, Any]
    maintained_by: dict[str, Any]
    path: Path

    @property
    def known_org_references(self) -> set[str]:
        """Names + URL hosts the audits should treat as legitimate refs.

        Catches both the bare name (``VeriSwarm.ai``) and the URL host
        (``veriswarm.ai``), so the org-profile audit doesn't flag the
        maintainer attribution as an 'orphan reference'.
        """
        refs: set[str] = set()
        mb = self.maintained_by or {}
        if mb.get("name"):
            refs.add(str(mb["name"]))
            refs.add(str(mb["name"]).lower())
        url = str(mb.get("url") or "")
        if url:
            from urllib.parse import urlparse

            host = urlparse(url).netloc.lower().removeprefix("www.")
            if host:
                refs.add(host)
        return refs

    @property
    def repo_names(self) -> list[str]:
        names: list[str] = []
        for role in self.roles:
            names.extend(role.repos)
        names.extend(repo.name for repo in self.extra_repos)
        return list(dict.fromkeys(names))

    @property
    def learning_repo_names(self) -> list[str]:
        return [role.learning_repo for role in self.roles]

    @property
    def solution_repo_names(self) -> list[str]:
        return [role.solution_repo for role in self.roles if role.solution_repo]

    @property
    def release_repo_names(self) -> list[str]:
        names = self.learning_repo_names + self.solution_repo_names
        names.extend(repo.name for repo in self.extra_repos if repo.release)
        return list(dict.fromkeys(names))

    def remote_for(self, repo: str) -> str:
        return self.default_remote.format(org=self.org, repo=repo)

    def role_for_learning_repo(self, repo: str) -> RoleConfig | None:
        return next((role for role in self.roles if role.learning_repo == repo), None)


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "aicg-org.yaml"


def load_manifest(path: Path | None = None) -> OrgManifest:
    manifest_path = (path or default_manifest_path()).resolve()
    if not manifest_path.exists():
        raise ManifestError(f"Org manifest not found: {manifest_path}")
    try:
        raw = load_config(manifest_path)
    except ConfigError as exc:
        raise ManifestError(str(exc)) from exc
    if not isinstance(raw, dict):
        raise ManifestError(
            f"Org manifest at {manifest_path} must be a mapping; got {type(raw).__name__}."
        )
    def _role(item: dict[str, Any]) -> RoleConfig:
        model = item.get("repo_model", "pair")
        solution = item.get("solution_repo")
        if model not in ("pair", "single"):
            raise ManifestError(
                f"Role {item.get('id')!r}: repo_model must be 'pair' or 'single', got {model!r}."
            )
        if model == "single" and solution:
            raise ManifestError(
                f"Role {item.get('id')!r}: single repo_model must not set solution_repo."
            )
        if model == "pair" and not solution:
            raise ManifestError(
                f"Role {item.get('id')!r}: pair repo_model requires a solution_repo."
            )
        return RoleConfig(
            id=item["id"],
            title=item["title"],
            level=int(item["level"]),
            learning_repo=item["learning_repo"],
            solution_repo=solution,
            aliases=tuple(item.get("aliases", []) or ()),
            repo_model=model,
        )

    roles = tuple(_role(item) for item in raw.get("roles", []))
    if not roles:
        raise ManifestError("Org manifest must define at least one role.")
    extra_repos = tuple(
        ExtraRepoConfig(
            name=item["name"],
            kind=item.get("kind", "extra"),
            release=bool(item.get("release", False)),
        )
        for item in raw.get("extra_repos", [])
    )
    return OrgManifest(
        org=raw["org"],
        default_remote=raw["default_remote"],
        roles=roles,
        extra_repos=extra_repos,
        release=raw.get("release", {}),
        documentation=raw.get("documentation", {}),
        schedules=raw.get("schedules", {}),
        automation=raw.get("automation", {}),
        content_generation=raw.get("content_generation", {}),
        quality_judge=raw.get("quality_judge", {}),
        pipeline=raw.get("pipeline", {}),
        job_requirements=raw.get("job_requirements", {}),
        research=raw.get("research", {}),
        maintained_by=raw.get("maintained_by", {}),
        path=manifest_path,
    )


def state_dir_for_manifest(manifest: OrgManifest, override: Path | None = None) -> Path:
    if override is not None:
        return override.resolve()
    configured = Path(manifest.automation.get("state_dir", ".aicg/org"))
    if configured.is_absolute():
        return configured
    return (manifest.path.parent.parent / configured).resolve()
