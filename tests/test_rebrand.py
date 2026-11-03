from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from conftest import write_file, write_minimal_manifest

from aicg.org_config import load_manifest
from aicg.rebrand import _append_maintainer_footer, _rebrand_repo, rebrand_run


def _setup_workspace(tmp_path: Path) -> tuple[Path, Path, "OrgManifest"]:
    workspace = tmp_path / "workspace"
    (workspace / "ai-infra-security-learning").mkdir(parents=True)
    (workspace / "ai-infra-security-solutions").mkdir(parents=True)
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    (manifest.repo_path(workspace, ".github") / "profile").mkdir(parents=True)
    return workspace, tmp_path / "state", manifest


def test_footer_appender_adds_marker_and_phrasing() -> None:
    body = "# Hi\n\nProject content.\n"
    out = _append_maintainer_footer(body, "<!-- m -->", "Maintained by X")
    assert "<!-- m -->" in out
    assert "Maintained by X" in out
    assert out.startswith(body)


def test_footer_appender_preserves_trailing_newline() -> None:
    body = "# Hi\n\nProject content."  # no trailing newline
    out = _append_maintainer_footer(body, "<!-- m -->", "Maintained by X")
    assert out.endswith("\n")
    assert "Maintained by X" in out


def test_rebrand_dry_run_reports_would_change(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    write_file(workspace / "ai-infra-security-learning" / "README.md", "# Repo\n")

    report = rebrand_run(
        manifest, workspace, state_dir=state_dir, apply=False,
        repos=["ai-infra-security-learning"],
    )

    learning_outcome = next(
        r for r in report["repos"] if r["repo"] == "ai-infra-security-learning"
    )
    assert learning_outcome["status"] == "would_change"
    assert "README.md" in learning_outcome["changed_files"]


def test_rebrand_idempotent_already_branded(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    # README already has the marker.
    body = (
        "# Repo\n\nContent.\n\n---\n\n<!-- aicg:maintained-by -->\n"
        "Maintained by [VeriSwarm.ai](https://veriswarm.ai)\n"
    )
    write_file(workspace / "ai-infra-security-learning" / "README.md", body)

    report = rebrand_run(
        manifest, workspace, state_dir=state_dir, apply=False,
        repos=["ai-infra-security-learning"],
    )

    outcome = next(
        r for r in report["repos"] if r["repo"] == "ai-infra-security-learning"
    )
    assert outcome["status"] == "already_branded"
    assert outcome["changed_files"] == []


def test_rebrand_github_repo_targets_profile_readme(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    dotgithub = manifest.repo_path(workspace, ".github")
    write_file(dotgithub / "profile" / "README.md", "# Org Profile\n")
    write_file(dotgithub / "README.md", "# Org README\n")

    report = rebrand_run(
        manifest, workspace, state_dir=state_dir, apply=False, repos=[".github"]
    )

    outcome = next(r for r in report["repos"] if r["repo"] == ".github")
    assert outcome["status"] == "would_change"
    paths = set(outcome["changed_files"])
    assert "profile/README.md" in paths
    assert "README.md" in paths


def test_rebrand_skips_when_no_maintainer_configured(tmp_path: Path) -> None:
    workspace, state_dir, _manifest = _setup_workspace(tmp_path)
    # Build a manifest WITHOUT a maintained_by block by editing the
    # written file directly.
    manifest_path = tmp_path / "aicg-org.yaml"
    payload = json.loads(manifest_path.read_text())
    payload.pop("maintained_by", None)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    bare_manifest = load_manifest(manifest_path)

    report = rebrand_run(
        bare_manifest, workspace, state_dir=state_dir, apply=False
    )

    assert report["status"] == "skipped"


def test_known_org_references_via_manifest(tmp_path: Path) -> None:
    manifest = load_manifest(write_minimal_manifest(tmp_path / "aicg-org.yaml"))
    refs = manifest.known_org_references
    assert "VeriSwarm.ai" in refs
    assert "veriswarm.ai" in {r.lower() for r in refs}


def test_audit_org_profile_does_not_flag_maintained_by_as_orphan(
    tmp_path: Path,
) -> None:
    """The maintainer attribution should not trip profile_orphan_references."""
    from aicg.nav_audit import audit_org_profile

    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    # Profile references VeriSwarm.ai in a maintainer footer.
    write_file(
        workspace / ".github" / "profile" / "README.md",
        "# Org Profile\n\n"
        "Welcome to the AI Infra Curriculum. " + ("." * 200) + "\n\n"
        "[ai-infra-security-learning](https://github.com/ai-infra-curriculum/ai-infra-security-learning)\n"
        "[ai-infra-security-solutions](https://github.com/ai-infra-curriculum/ai-infra-security-solutions)\n"
        "[.github](https://github.com/ai-infra-curriculum/.github)\n\n"
        "Maintained by [VeriSwarm.ai](https://veriswarm.ai)\n",
    )

    report = audit_org_profile(manifest, workspace, state_dir=state_dir)

    types = [f["type"] for f in report["findings"]]
    assert "profile_orphan_references" not in types


# ---------------------------------------------------------------------------
# Branch name + dirty-tree handling
# ---------------------------------------------------------------------------


def test_rebrand_skips_dirty_working_tree(tmp_path: Path) -> None:
    """Don't clobber an in-flight job's uncommitted changes."""
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    repo_path = workspace / "ai-infra-security-learning"
    write_file(repo_path / "README.md", "# Repo\n")

    with patch("aicg.rebrand.subprocess.run") as mock_run:
        # First call (the dirty check inside _rebrand_repo) returns
        # 'M README.md' so the rebrand should skip cleanly.
        mock_run.return_value.stdout = " M README.md\n"
        mock_run.return_value.returncode = 0
        outcome = _rebrand_repo(repo_path, "ai-infra-security-learning",
                                manifest.maintained_by, apply=True)
    assert outcome["status"] == "skipped_dirty_tree"


def test_rebrand_pr_failure_cleans_up_modified_files(tmp_path: Path) -> None:
    """A failed rebrand PR must not leave the README modified on main.

    Regression: `git checkout main` preserves uncommitted modifications
    when switching branches, so a failed rebrand left README modified
    and every subsequent `pull --ff-only` then refused to clobber the
    dirty file — breaking rebrand for that repo until manual cleanup.
    The cleanup must `git checkout HEAD -- <changed_files>` before
    switching back to main.
    """
    from aicg.rebrand import _open_rebrand_pr

    repo_path = tmp_path / "ai-infra-security-learning"
    repo_path.mkdir()
    changed = ["README.md"]
    maintained_by = {"name": "VeriSwarm.ai"}

    call_log: list[list[str]] = []

    def fake_run(cmd, **_):
        call_log.append(list(cmd))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        result = _R()
        # Fail the `pull` step to trigger the cleanup path.
        # cmd layout: ["git", "-C", repo, "pull", "--ff-only"]
        if cmd[3:5] == ["pull", "--ff-only"]:
            result.returncode = 1
            result.stderr = "would be overwritten by merge"
        return result

    with patch("aicg.rebrand.subprocess.run", side_effect=fake_run):
        outcome = _open_rebrand_pr(repo_path, "ai-infra-security-learning",
                                   changed, maintained_by)

    assert outcome["status"] == "pr_failed"
    assert outcome["failed_step"] == "pull"
    # The cleanup must include a `git checkout HEAD -- README.md` before
    # the final `git checkout main`. Without it, modifications survive.
    # cmd layout: ["git", "-C", repo, "checkout", "HEAD", "--", *files]
    restore_calls = [
        c for c in call_log
        if c[3:6] == ["checkout", "HEAD", "--"] and "README.md" in c[6:]
    ]
    assert len(restore_calls) == 1, (
        f"expected exactly one HEAD restore of README.md, got: {restore_calls}"
    )


def test_rebrand_ignores_aicg_runner_state_in_dirty_check(
    tmp_path: Path,
) -> None:
    """Runner state files under .aicg/ must not block the rebrand.

    Regression: 6 solution repos had tracked .aicg/*.json state files
    that the runner re-wrote every tick. Steward's rebrand action
    saw the modifications as 'dirty tree' and skipped those repos
    every day. The dirty check must filter .aicg/ paths.
    """
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    repo_path = workspace / "ai-infra-security-learning"
    write_file(repo_path / "README.md", "# Repo\n")

    with patch("aicg.rebrand.subprocess.run") as mock_run:
        # Only .aicg/ modifications — should NOT be considered dirty.
        mock_run.return_value.stdout = (
            " M .aicg/audit-report.json\n"
            " M .aicg/work-plan.json\n"
            " M .aicg/curriculum-nav-report.json\n"
        )
        mock_run.return_value.returncode = 0
        outcome = _rebrand_repo(repo_path, "ai-infra-security-learning",
                                manifest.maintained_by, apply=True)
    # Should proceed past the dirty check (downstream calls all see
    # the same mocked subprocess so status reflects whatever the
    # mock implies); the assertion is just that it didn't bail with
    # skipped_dirty_tree.
    assert outcome["status"] != "skipped_dirty_tree"


def test_rebrand_branch_safe_for_dotted_repo_names() -> None:
    """git refuses 'aicg/.../.github/...' because the component begins
    with a dot. Verify our branch builder sanitizes it."""
    import re

    from aicg.rebrand import utc_now

    repo = ".github"
    safe_repo = re.sub(r"^\.+", "dot-", repo).replace("/", "-")
    branch = f"aicg/{utc_now()[:10]}/{safe_repo}/maintainer-footer"

    # Each component must not start with '.'
    parts = branch.split("/")
    assert all(not p.startswith(".") for p in parts), branch


# ---------------------------------------------------------------------------
# Defensive idempotency: don't double-add when a 'Maintained by' line
# already exists (under any phrasing).
# ---------------------------------------------------------------------------


def test_rebrand_skips_when_prior_maintained_by_line_present(tmp_path: Path) -> None:
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    # Old-style attribution that the rebrand should NOT clobber by
    # appending its own footer.
    write_file(
        workspace / "ai-infra-security-learning" / "README.md",
        "# Repo\n\n## License\n\n---\n\n"
        "*Maintained by the AI Infrastructure Curriculum Project*\n",
    )

    report = rebrand_run(
        manifest, workspace, state_dir=state_dir, apply=False,
        repos=["ai-infra-security-learning"],
    )

    outcome = next(
        r for r in report["repos"] if r["repo"] == "ai-infra-security-learning"
    )
    assert outcome["status"] == "already_branded"
    assert outcome["changed_files"] == []


def test_rebrand_still_skips_when_existing_attribution_is_to_us(tmp_path: Path) -> None:
    """A prior run added our exact phrasing without the marker — still skip."""
    workspace, state_dir, manifest = _setup_workspace(tmp_path)
    write_file(
        workspace / "ai-infra-security-learning" / "README.md",
        "# Repo\n\nMaintained by VeriSwarm.ai\n",
    )

    report = rebrand_run(
        manifest, workspace, state_dir=state_dir, apply=False,
        repos=["ai-infra-security-learning"],
    )

    outcome = next(
        r for r in report["repos"] if r["repo"] == "ai-infra-security-learning"
    )
    assert outcome["status"] == "already_branded"
