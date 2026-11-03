from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_security_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    learning = workspace / "ai-infra-security-learning"
    solutions = workspace / "ai-infra-security-solutions"
    write_file(
        learning
        / "lessons"
        / "mod-001-ml-security-foundations"
        / "exercises"
        / "exercise-01-threat-model.md",
        "# Exercise 01 - Threat Model\n\nBuild a threat model.\n",
    )
    write_file(
        learning / "lessons" / "mod-001-ml-security-foundations" / "README.md",
        "# Module 01\n",
    )
    # A written lecture chapter, so the module reads as authored rather than a
    # skeleton (see aicg.depth.MIN_MODULE_WORDS).
    write_file(
        learning / "lessons" / "mod-001-ml-security-foundations" / "01-foundations.md",
        "# Foundations\n\n" + "Threats, assets, and trust boundaries in ML systems. " * 450,
    )
    write_file(solutions / "modules" / "README.md", "# Modules\n")
    write_file(solutions / ".github" / "workflows" / "ci.yml", "name: CI\n")
    return workspace


def write_minimal_manifest(path: Path) -> Path:
    data = {
        "org": "AI-Infra-Curriculum",
        "default_remote": "git@github.com:AI-Infra-Curriculum/{repo}.git",
        "release": {"tag_format": "v%Y.%m", "branch": "main"},
        "extra_repos": [{"name": ".github", "kind": "org-profile", "release": False}],
        "documentation": {
            "format_guard_files": [
                "CURRICULUM.md",
                "CURRICULUM_INDEX.md",
                "README.md",
                "VERSIONS.md",
            ],
            "org_readme_repo": ".github",
            "org_readme_files": ["README.md", "profile/README.md"],
        },
        "schedules": {},
        "automation": {
            "state_dir": ".aicg/org",
            "agent": {
                "provider": "openai",
                "model": "codex-gpt-5.5",
                "interface": "local_cli_subscription",
                "agent_command": "{runner}/scripts/run-codex-control.sh --model codex-gpt-5.5 --prompt {prompt} --output-dir {output_dir} --repo {repo} --work-id {work_id}",
            },
        },
        "content_generation": {
            "agent": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "interface": "local_cli_subscription",
                "agent_command": "{runner}/scripts/run-claude-content.sh --model claude-opus-4-7 --prompt {prompt} --output-dir {output_dir} --repo {repo} --work-id {work_id}",
            }
        },
        "job_requirements": {
            "ownership_strategy": "lowest_level_role",
            "markdown_file": "JOB_REQUIREMENTS.md",
            "structured_file": ".aicg/job-requirements.json",
            "supplemental_dir": "supplemental",
        },
        "research": {"minimum_postings_per_role": 25, "source_window_days": 45},
        "maintained_by": {
            "name": "VeriSwarm.ai",
            "url": "https://veriswarm.ai",
            "phrasing": "Maintained by [VeriSwarm.ai](https://veriswarm.ai)",
            "footer_marker": "<!-- aicg:maintained-by -->",
        },
        "quality_judge": {
            "enabled": False,
            "agent_command": "{runner}/scripts/run-claude-judge.sh --prompt {prompt} --output-dir {output_dir} --repo {repo} --work-id {work_id} --artifact {artifact}",
            "thresholds": {"default": 70},
        },
        "roles": [
            {
                "id": "security",
                "title": "AI Infrastructure Security Engineer",
                "level": 35,
                "learning_repo": "ai-infra-security-learning",
                "solution_repo": "ai-infra-security-solutions",
            }
        ],
    }
    return write_file(path, json.dumps(data, indent=2) + "\n")
