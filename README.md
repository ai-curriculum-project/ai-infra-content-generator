# AI Infrastructure Content Generator

<!-- aicg:site-banner -->
> 🎓 Part of the free, open-source **AI Career Curriculum** ecosystem — [Infrastructure](https://github.com/ai-infra-curriculum) · [ML Engineering](https://github.com/ml-engineering-curriculum) · [AI Engineering](https://github.com/ai-engineering-curriculum) · [Governance](https://github.com/ai-governance-curriculum) · [Startup](https://github.com/ai-startup-curriculum).
<!-- /aicg:site-banner -->

`ai-infra-content-generator` is an executable curriculum runner for the AI
Infrastructure Curriculum workspace. It audits paired learning and solutions
repositories, turns objective gaps into deterministic work items, writes prompt
packets for a local generation agent, validates outputs, and prepares guarded
GitHub PRs.

The v1 pilot target is `ai-infra-security-solutions`, paired with
`ai-infra-security-learning`. The first loop closes module-level solution gaps:
learning exercises under `lessons/mod-*/exercises/` must have matching solution
directories under `modules/mod-*/exercise-*/SOLUTION.md`.

## Install

From this repository:

```bash
python3 -m pip install -e .
python3 -m pip install -e ".[dev]"
```

The editable install exposes the `aicg` command.

## Commands

```bash
aicg audit --workspace .. --repo ai-infra-security-solutions
aicg plan --repo ai-infra-security-solutions
aicg generate --repo ai-infra-security-solutions --module mod-001-ml-security-foundations
aicg generate --repo ai-infra-security-solutions --all
aicg verify --repo ai-infra-security-solutions
aicg diff --repo ai-infra-security-solutions --work-id fill-mod-001-ml-security-foundations-solutions
aicg validate --repo ai-infra-security-solutions
aicg pr --repo ai-infra-security-solutions --work-id fill-mod-001-ml-security-foundations-solutions
aicg run --repo ai-infra-security-solutions --mode pilot
aicg org sync
aicg org release
aicg org research
aicg org audit
aicg org daily
aicg org bootstrap-role --role data-engineer --title "Data Engineer" --level 25
aicg org execute-plan --role data-engineer
aicg propagate --repo ai-infra-security-solutions
aicg verify --repo ai-infra-security-solutions --with-quality-grade
aicg org issues           # dry-run
aicg org issues --apply   # open / comment / close tracking issues
aicg org steward          # dry-run
aicg org steward --apply  # real auto-merger
aicg org discussions      # always dry-run — flags items needing humans
```

If `--workspace` is omitted, `aicg` assumes it is running from this repo and uses
the parent directory as the curriculum workspace.

## What The Runner Checks

- Paired learning and solutions repo discovery.
- **Module-level exercise parity** — every learning exercise under
  `lessons/<mod>/exercises/` (file or directory) is matched against
  `modules/<mod>/exercise-NN*/{SOLUTION.md | README.md | STEP_BY_STEP.md}`.
  Module-level `modules/<mod>/SOLUTION.md` is recognised as covering the
  module's exercises with a warning-level depth follow-up rather than a
  blocking error.
- **Project-level parity** — every learning project under `projects/` is
  matched against the paired solutions repo's `projects/` directory using
  the same artifact rules.
- **Post-generation verification (`aicg verify`)** — after the agent
  writes artifacts, the runner walks each work item's actions, confirms
  the file exists, the heading structure matches the prompt's output
  contract (Overview, Implementation, Validation, Rubric, Common mistakes,
  References — relaxed for module-rationale docs), citations are
  classifiable against the source registry, and no
  `needs-research`/`manual-review` markers remain.
- Placeholder, `# manual-review`, and `needs-research` markers (cached
  per-file by `(mtime_ns, size)` under `.aicg/placeholder-cache.json`).
- Source policy warnings for practitioner references without official
  sources.
- Python syntax under the target repo.
- Markdown hygiene including GFM table column-count and separator-row
  validation.
- GitHub workflow presence.
- PR guardrails for direct main work, force-push, restricted files,
  unresolved research, manual-review markers, and non-green CI before
  auto-merge.

The old word-count completeness rule is retired. A solution is complete
when it matches the learning objective, includes the required artifacts,
verifies, validates, and has source-backed claims.

## State Files

Reports are written into the target repo:

```text
.aicg/run-state.json
.aicg/audit-report.json
.aicg/work-plan.json
.aicg/validation-report.json
.aicg/prompts/<work-id>.md
.aicg/generated/<work-id>/
```

These files make each loop inspectable and resumable.

## Generation Adapter

Generation is file-based in v1. `aicg generate` always writes a prompt packet and
invokes local subscription CLIs when configured. It does not call Anthropic or
OpenAI APIs.

The default org manifest uses:

- `scripts/run-claude-content.sh` for Claude Opus 4.7 content generation.
- `scripts/run-codex-control.sh` for Codex GPT-5.5 control-plane work.

To override the content command for one target repo, add `aicg.yaml`:

```yaml
generator_command: "/path/to/ai-infra-content-generator/scripts/run-claude-content.sh --model claude-opus-4.7 --prompt {prompt} --output-dir {output_dir} --repo {repo} --work-id {work_id}"
```

Supported placeholders are `{prompt}`, `{output_dir}`, `{repo}`, `{work_id}`, and
`{runner}`. If no command is configured, `generate` exits with a clear message
and leaves the prompt ready for manual execution.

If a local agent reports a subscription limit, AICG writes `agent_limit_reached`
to `.aicg/run-state.json`, records `retry_after`, and defers the queue item.

## Source Policy

The source registry lives at `config/source-registry.json`. Official standards
and official project docs are preferred. VeriSwarm references may be used only as
practitioner implementation examples for governance, trust gates, audit ledgers,
and agent-risk architecture. Unresolved facts must be marked with:

```markdown
<!-- needs-research: explain the unresolved claim -->
```

That marker blocks auto-merge.

## Runbook

Operational details are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

Org-level autonomous operation is described in
[docs/AUTONOMOUS_ORG_AUTOMATION.md](docs/AUTONOMOUS_ORG_AUTOMATION.md).

System architecture (Mermaid diagrams of every workflow, timer cadence,
state files, and external surface) is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

<!-- aicg:maintained-by -->
Maintained by [VeriSwarm.ai](https://veriswarm.ai)
