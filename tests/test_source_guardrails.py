from __future__ import annotations

from conftest import write_file

from aicg.guardrails import evaluate_guardrails, parse_status_paths
from aicg.source_registry import SourceRegistry


def test_source_registry_classifies_official_and_practitioner_sources():
    registry = SourceRegistry.load()

    atlas = registry.classify_url("https://atlas.mitre.org/techniques/AML.T0015/")
    veriswarm = registry.classify_url("https://veriswarm.ai/docs/gate#risk")

    assert atlas is not None
    assert atlas.is_official
    assert veriswarm is not None
    assert veriswarm.is_practitioner_reference
    preferred = registry.preferred_sources(["governance"])
    assert preferred[0].is_official


def test_guardrails_block_main_needs_research_and_restricted_auto_merge(tmp_path):
    repo = tmp_path / "repo"
    write_file(
        repo / "modules" / "mod-001" / "exercise-01" / "SOLUTION.md",
        "# Solution\n\n<!-- needs-research: source -->\n",
    )

    decision = evaluate_guardrails(
        repo,
        branch="main",
        changed_files=[".github/workflows/ci.yml", "modules/mod-001/exercise-01/SOLUTION.md"],
        ci_status="failure",
        auto_merge=True,
    )

    assert not decision.allowed
    assert any("main/master" in blocker for blocker in decision.blockers)
    assert any("needs-research" in blocker for blocker in decision.blockers)
    assert any("Restricted files" in blocker for blocker in decision.blockers)
    assert any("green CI" in blocker for blocker in decision.blockers)


def test_git_status_parser_includes_untracked_and_renamed_files():
    output = "?? modules/mod-001/exercise-01/SOLUTION.md\nR  old.md -> new.md\n M README.md\n"

    assert parse_status_paths(output) == [
        "modules/mod-001/exercise-01/SOLUTION.md",
        "new.md",
        "README.md",
    ]


def test_markers_in_unchanged_files_do_not_block_a_pr(tmp_path):
    """A marker committed long ago elsewhere must not block every future PR."""
    repo = tmp_path / "repo"
    write_file(repo / "modules" / "mod-310" / "SOLUTION.md", "<!-- needs-research: old -->\n")
    write_file(repo / "modules" / "mod-301" / "SOLUTION.md", "# New, clean solution\n")

    decision = evaluate_guardrails(
        repo,
        branch="aicg/fill-mod-301",
        changed_files=["modules/mod-301/SOLUTION.md"],
    )

    assert decision.allowed, decision.blockers


def test_markers_in_changed_files_still_block(tmp_path):
    repo = tmp_path / "repo"
    write_file(repo / "modules" / "mod-301" / "SOLUTION.md", "<!-- manual-review: check -->\n")

    decision = evaluate_guardrails(
        repo, branch="aicg/fill-mod-301", changed_files=["modules/mod-301/SOLUTION.md"]
    )

    assert not decision.allowed
    assert any("Manual-review" in b for b in decision.blockers)


def test_changed_directories_are_scanned_recursively(tmp_path):
    # `git status --porcelain` reports a brand-new directory as one entry.
    repo = tmp_path / "repo"
    write_file(repo / "modules" / "mod-301" / "exercise-1" / "SOLUTION.md",
               "<!-- needs-research: cite -->\n")

    decision = evaluate_guardrails(
        repo, branch="aicg/fill-mod-301", changed_files=["modules/mod-301/"]
    )

    assert any("needs-research" in b for b in decision.blockers)


def test_deleted_changed_files_are_ignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    decision = evaluate_guardrails(
        repo, branch="aicg/x", changed_files=["modules/gone/SOLUTION.md"]
    )

    assert decision.allowed
