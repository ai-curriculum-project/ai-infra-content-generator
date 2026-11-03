"""Tests for the curriculum manifest + canonical-source registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from aicg.manifest import (
    ManifestError,
    load_canonical_sources,
    load_curriculum_manifest,
)


CANONICAL_PATH = Path("manifest/canonical_sources.json")


def test_load_canonical_registry_seeded() -> None:
    """The committed canonical_sources.json loads and exposes known entries."""
    registry = load_canonical_sources(CANONICAL_PATH)

    assert registry.schema_version == 1
    assert len(registry.sources) >= 5
    names = [s.name for s in registry.sources]
    assert any("Bitnami" in n for n in names)
    assert any("exa" in n for n in names)
    assert any("Wellfound" in n for n in names) or any("AngelList" in n for n in names)


def test_canonical_lookup_bitnami() -> None:
    """Bitnami chart URL routes to the OCI registry, not Wayback."""
    registry = load_canonical_sources(CANONICAL_PATH)
    successor = registry.lookup_successor("https://charts.bitnami.com/bitnami")
    assert successor == "oci://registry-1.docker.io/bitnamicharts"


def test_canonical_lookup_exa_to_eza() -> None:
    """exa entry routes to eza."""
    registry = load_canonical_sources(CANONICAL_PATH)
    successor = registry.lookup_successor("https://the.exa.website/")
    assert successor == "https://github.com/eza-community/eza"
    # Also without trailing slash.
    assert (
        registry.lookup_successor("https://the.exa.website")
        == "https://github.com/eza-community/eza"
    )


def test_canonical_lookup_angellist_to_wellfound() -> None:
    registry = load_canonical_sources(CANONICAL_PATH)
    assert (
        registry.lookup_successor("https://angel.co/jobs")
        == "https://wellfound.com/jobs"
    )


def test_canonical_lookup_linuxjourney_to_labex() -> None:
    registry = load_canonical_sources(CANONICAL_PATH)
    assert (
        registry.lookup_successor("https://linuxjourney.com/")
        == "https://labex.io/linuxjourney"
    )


def test_canonical_lookup_unknown_returns_none() -> None:
    registry = load_canonical_sources(CANONICAL_PATH)
    assert registry.lookup_successor("https://some-random-blog.example.com/post") is None


def test_canonical_machine_endpoint_fulcio() -> None:
    """Fulcio is registered as machine_consumed=true — must be flagged."""
    registry = load_canonical_sources(CANONICAL_PATH)
    assert registry.is_known_machine_endpoint("https://fulcio.sigstore.dev")
    assert registry.is_known_machine_endpoint("https://fulcio.sigstore.dev/api/v2/configuration")


def test_canonical_machine_endpoint_negative() -> None:
    registry = load_canonical_sources(CANONICAL_PATH)
    assert not registry.is_known_machine_endpoint("https://kubernetes.io/docs/")


def test_canonical_missing_file_returns_empty_registry(tmp_path: Path) -> None:
    """Missing file should not crash callers — return an empty registry."""
    registry = load_canonical_sources(tmp_path / "missing.json")
    assert registry.sources == ()
    assert registry.lookup_successor("https://anything.example.com") is None


def test_canonical_schema_version_mismatch_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        '{"schema_version": 999, "updated_at": "2026-06-04", "sources": []}',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_canonical_sources(path)


# ---------- curriculum manifest ----------


def test_curriculum_manifest_loads_if_present(tmp_path: Path) -> None:
    """If the committed manifest is present, loader produces a structured object."""
    path = Path("manifest/curriculum.manifest.json")
    if not path.exists():
        pytest.skip("curriculum.manifest.json not built in this checkout")
    m = load_curriculum_manifest(path)
    assert m.schema_version == 1
    assert m.total_modules > 0
    assert m.total_exercises > 0
    # Junior-engineer track must be present and findable.
    junior = m.track("junior-engineer")
    assert junior is not None
    assert junior.level == 1
    assert len(junior.modules) >= 5


def test_curriculum_manifest_minimal_synth(tmp_path: Path) -> None:
    """Synthetic minimal manifest exercises the parser end-to-end."""
    path = tmp_path / "m.json"
    path.write_text(
        """
        {
          "schema_version": 1,
          "generated_at": "2026-06-04T00:00:00Z",
          "org": "ai-infra-curriculum",
          "tracks": [
            {
              "slug": "junior-engineer",
              "display_name": "Junior",
              "level": 1,
              "learning_repo": "ai-infra-junior-engineer-learning",
              "solutions_repo": "ai-infra-junior-engineer-solutions",
              "learning_repo_url": "https://github.com/x/y",
              "solutions_repo_url": "https://github.com/x/z",
              "modules": [
                {
                  "slug": "mod-001",
                  "number": 1,
                  "title": "Python Fundamentals",
                  "path": "lessons/mod-001",
                  "github_url": "https://github.com/x/y/blob/main/lessons/mod-001",
                  "exercises": []
                }
              ],
              "projects": [],
              "resources": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    m = load_curriculum_manifest(path)
    assert m.total_modules == 1
    junior = m.track("junior-engineer")
    assert junior is not None
    assert m.find_module("junior-engineer", "mod-001") is not None
    assert m.find_module("junior-engineer", "mod-999") is None


def test_extra_repo_org_override_changes_remote(tmp_path):
    import json

    from aicg.org_config import load_manifest

    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "org": "child-org",
                "default_remote": "git@github.com:{org}/{repo}.git",
                "roles": [
                    {"id": "r", "title": "R", "level": 10, "learning_repo": "r-learning",
                     "solution_repo": "r-solutions"}
                ],
                "extra_repos": [
                    {"name": "runner", "kind": "runner", "org": "parent-org"},
                    {"name": "local-extra", "kind": "extra"},
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert manifest.remote_for("runner") == "git@github.com:parent-org/runner.git"
    assert manifest.remote_for("local-extra") == "git@github.com:child-org/local-extra.git"
    assert manifest.remote_for("r-learning") == "git@github.com:child-org/r-learning.git"


def test_extra_repo_org_override_with_hardcoded_remote(tmp_path):
    import json

    from aicg.org_config import load_manifest

    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "org": "Child-Org",
                "default_remote": "git@github.com:Child-Org/{repo}.git",
                "roles": [
                    {"id": "r", "title": "R", "level": 10, "learning_repo": "r-learning",
                     "solution_repo": "r-solutions"}
                ],
                "extra_repos": [{"name": "runner", "kind": "runner", "org": "parent-org"}],
            }
        ),
        encoding="utf-8",
    )
    assert load_manifest(path).remote_for("runner") == "git@github.com:parent-org/runner.git"


def _dotgithub_manifest(tmp_path, org="AI-Infra-Curriculum", extra=None):
    import json

    from aicg.org_config import load_manifest

    path = tmp_path / f"{org}.json"
    path.write_text(
        json.dumps(
            {
                "org": org,
                "default_remote": "git@github.com:{org}/{repo}.git",
                "roles": [
                    {"id": "r", "title": "R", "level": 10, "learning_repo": "r-learning",
                     "solution_repo": "r-solutions"}
                ],
                "extra_repos": extra if extra is not None else [{"name": ".github", "kind": "org-profile"}],
            }
        ),
        encoding="utf-8",
    )
    return load_manifest(path)


def test_dotgithub_gets_a_per_org_local_dir(tmp_path):
    a = _dotgithub_manifest(tmp_path, "AI-Infra-Curriculum")
    b = _dotgithub_manifest(tmp_path, "ml-engineering-curriculum")
    assert a.local_dir(".github") == "ai-infra-curriculum-dotgithub"
    assert b.local_dir(".github") == "ml-engineering-curriculum-dotgithub"
    assert a.repo_path(tmp_path, ".github") != b.repo_path(tmp_path, ".github")


def test_other_repos_keep_their_name_as_local_dir(tmp_path):
    m = _dotgithub_manifest(tmp_path)
    assert m.local_dir("r-learning") == "r-learning"
    assert m.repo_path(tmp_path, "r-learning") == tmp_path / "r-learning"


def test_extra_repo_local_dir_override(tmp_path):
    m = _dotgithub_manifest(tmp_path, extra=[{"name": ".github", "kind": "org-profile", "local_dir": "profile-repo"}])
    assert m.local_dir(".github") == "profile-repo"


def test_sync_clones_dotgithub_into_per_org_dir(tmp_path):
    from aicg.org_runner import sync_repositories

    m = _dotgithub_manifest(tmp_path)
    report = sync_repositories(m, tmp_path / "ws", dry_run=True)
    clone = next(a for a in report["actions"] if a["repo"] == ".github")
    assert str(clone["command"]).endswith("/ws/ai-infra-curriculum-dotgithub")
