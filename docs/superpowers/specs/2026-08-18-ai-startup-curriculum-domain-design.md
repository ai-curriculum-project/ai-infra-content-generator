# ai-startup-curriculum — Fifth Domain Design

**Date:** 2026-08-18
**Status:** Approved (design) — implementation pending
**Author:** Joshua Ferguson (with Claude Code)
**Repo:** ai-infra-content-generator (AICG harness)

## 1. Context & Goal

AICG is a multi-tenant curriculum-generation harness driving four sibling GitHub
orgs today: `ai-infra-curriculum`, `ml-engineering-curriculum`,
`ai-engineering-curriculum`, `ai-governance-curriculum`. Each is a "domain"
(`config/domains/<domain>.yaml`) with a role ladder; the nightly pipeline on
pi-rook researches job evidence, authors curriculum content, and runs a quality
judge per role.

This adds a **fifth domain**, `ai-startup-curriculum`: an open-source startup
operating school aimed at technical founders. It applies the same engineering
mentality as the infra curricula — *what does someone in this role need to
understand, in what dependency order, and what should they be able to do after* —
to the job of building and operating a company.

The founder is the first student: the Founder/CEO curriculum is built roughly one
level above where the author currently is at each moment, turning a live startup
(incorporation, fundraising, partnerships, runway) into a systematic path.

## 2. Non-Goals & Hard Constraints

- **Backward compatibility is mandatory.** The four existing domains run nightly
  on pi-rook. Every harness change is strictly additive; the pair-based model
  (`learning_repo` + `solution_repo`) must behave exactly as today. Existing
  domain configs are not edited. Regression tests must prove this.
- No MBA case-study fluff. Exercises are real founder artifacts.
- Not fifteen repos. Start with five canonical repos; split later when a body of
  material outgrows its home.

## 3. The Three-Axis Model

Startup knowledge is organized on three axes, not one:

### Axis 1 — Functional curricula (bodies of knowledge, authored once)
The deep, reusable subjects wired as an explicit **dependency graph**:

```
STARTUP FOUNDATIONS
   ├─► Customer Discovery ─► Product-Market Fit ─► GTM ─► Sales ─► Growth
   ├─► Corporate Structure ─► {Equity, Governance, Fundraising}
   └─► Startup Economics ─► {Financial Modeling, Runway, Unit Economics, Capital Allocation}
```

### Axis 2 — Role pathways (curated views, not duplicated content)
A role is a **learning path over functional curricula**, expressed as target
coverage — so the cap-table lesson is written once and referenced everywhere.

- **CEO:** Foundations 100 · Strategy 100 · Finance 80 · Fundraising 80 · Sales 60 · Marketing 40 · Product 60 · People 80 · Legal 50 · Governance 80 · Technical 20
- **CTO:** Foundations 100 · Product 70 · Technical Leadership 100 · Finance 30 · Sales 30 · People 70 · Governance 30

### Axis 3 — Startup stages (tags on everything)
`IDEA → PRE-SEED → SEED → SERIES-A → GROWTH → MATURE`. Lets the curriculum say
"you're pre-seed: do these 17 modules, ignore those 43." Prevents teaching
transfer pricing to a seed-stage founder.

## 4. Repo Structure — Five Canonical Repos

Org `ai-startup-curriculum` (already created; author is admin). **Single
`*-curriculum` repos**, not learning/solutions pairs. Exemplar founder artifacts
live in-repo under `exemplars/`.

| Repo | Core question |
|---|---|
| `startup-foundations` | How does a startup work as a system? (shared root + dependency graph + stage taxonomy) |
| `founder-ceo-curriculum` | Idea → company → functioning organization; capital/attention/people allocation |
| `startup-product-gtm-curriculum` | Discovery, PMF, product, positioning, marketing, sales, growth |
| `startup-finance-fundraising-curriculum` | Runway, models, unit economics, capitalization, angels→Series A |
| `startup-operations-governance-curriculum` | Corp ops, vendors, metrics, legal, boards, fiduciary duty, risk |

Plus `.github` (org profile). **founder-ceo-curriculum is built first** (live lab).

## 5. Harness Extension — Single-Repo Curricula (additive)

### 5.1 Data model (`org_config.py`)
- `RoleConfig.solution_repo: str | None = None` (was required `str`).
- `RoleConfig.repo_model: str = "pair"` — `"pair"` | `"single"`.
- `@property RoleConfig.is_single -> bool` and `RoleConfig.repos -> tuple[str, ...]`.
- For a single curriculum, `learning_repo` holds the one repo name
  (e.g. `founder-ceo-curriculum`), `solution_repo=None`, `repo_model="single"`.
- Null-guard `OrgManifest` properties: `repo_names`, `solution_repo_names`,
  `release_repo_names` skip `None` solution repos.
- Loader: `solution_repo=item.get("solution_repo")`,
  `repo_model=item.get("repo_model", "pair")`. Validation: `single` requires no
  `solution_repo`; `pair` requires one (fail fast otherwise).

### 5.2 Consumers to make single-aware (guard on `is_single` / `solution_repo is None`)
- **`bootstrap.py`** — `_resolve_repo_names`, `bootstrap_role`, `_create_github_repos`:
  scaffold a single-repo skeleton (learning skeleton + `exemplars/` dir; no
  solutions repo, no solutions CI). New helper `_write_single_skeleton`.
- **`pairing_audit.py`** — skip single-repo roles (no pair to audit).
- **`domain_provision.py`** — org-profile rows render single repos.
- **`audit.py`, `org_runner.py`** — solution-repo audit/generate steps guarded.
- **`learning_audit.py`, `learning_content.py`, `research.py`, `referential.py`,
  `fleet.py`, `discussions_index.py`** — verified None-safe; adjust as tests find.
- **`scripts/install-domain-timers.sh`** — for single-repo roles, install only the
  learning/curriculum-targeted timers (no solutions generate/review). Repo model
  surfaced via `aicg org list-roles` output.

### 5.3 Stage tags & dependency graph representation (no schema break)
- Canonical taxonomy + graph live in `startup-foundations` and
  `.github/STARTUP_STAGES.md` / `.github/FUNCTIONAL_CURRICULA.md`.
- Each module README carries YAML front-matter: `stage:`/`stages:`, `pillar:`,
  `requires:` (prereq module ids). The curriculum-plan manifest tolerates extra
  keys (frozen dataclass ignores them), so tags may also live there without
  breaking `load_curriculum_plan`. Markdown front-matter is the source of truth.

## 6. Domain Config — `config/domains/ai-startup.yaml`

Mirrors `ai-engineering.yaml` (agents: Claude for content/research, Codex for
control; judge flag-only; VeriSwarm.ai maintainer footer; research caps
1 module / 3 exercises / 0 projects, min 3 evidence, 90-day window). Differences:

- `org: ai-startup-curriculum`, `default_remote: git@github.com:ai-startup-curriculum/{repo}.git`.
- Five entries with `repo_model: single`, `learning_repo: <name>-curriculum`,
  no `solution_repo`. `level` = path/order index (not seniority):
  foundations 10, founder-ceo 20, product-gtm 30, finance-fundraising 40,
  operations-governance 50.
- Aliases per entry for research matching (e.g. founder-ceo ← "Founder",
  "Startup CEO", "Founding CEO", "Solo Founder").
- `pipeline.phases`: mirror ai-engineering (`author: true`, rest false).
- `extra_repos`: `.github` (org-profile).

## 7. Founder/CEO Seed Content (hand-authored now)

`founder-ceo-curriculum`, stage-tagged, dependency-linked, with real artifacts:

- **mod-001 Customer Discovery & Idea Validation** (IDEA) — exercise: 20-interview
  discovery plan + synthesis; exemplar: filled interview guide + insight memo.
- **mod-002 Lean Business Modeling & Unit Economics** (IDEA→PRE-SEED) — exercise:
  build a lean canvas + unit-economics sheet; exemplar model.
- **mod-003 Startup Economics: Runway & Financial Modeling** (SEED) — exercise:
  $310k cash, $42k/mo burn, hiring 2 engineers → 18-month operating plan;
  exemplar spreadsheet + narrative.
- **mod-004 Fundraising: Pre-seed → Seed mechanics** (PRE-SEED→SEED) — exercise:
  investor funnel from 200 targets with conversion assumptions; exemplar funnel.
- **mod-005 Equity, SAFEs & Cap Tables** (PRE-SEED→SEED) — exercise: founder +
  cofounder + 10% option pool + SAFE + priced seed → everyone's ownership;
  exemplar cap table.
- **mod-006 Founder-led Sales v0** (SEED) — exercise: discovery → qualification →
  pilot → enterprise contract; exemplar call script + pilot agreement outline.

Module 001 authored fully now (learning content + exercise + exemplar). The rest
land as plan + stubs for the pipeline to complete. CTO/product-gtm/finance/ops
repos are scaffold-only in wave one.

## 8. `.github` Org Profile & Cross-Linking

- `profile/README.md`: what the org is, the three-axis model, the five repos,
  stage taxonomy, functional-pillar map, and an **ecosystem block** linking the
  other four orgs.
- `STARTUP_STAGES.md`, `FUNCTIONAL_CURRICULA.md`, `CAREER_PROGRESSION.md` analog,
  `FUNDING.yml`, avatar.
- **Bidirectional:** add `ai-startup-curriculum` to the ecosystem block in all
  four sibling `.github` profiles too.

## 9. Deployment (pi-rook)

`aicg` runs on pi-rook (the Mac `.venv` is broken — wrong interpreter path).

1. Push the content-generator branch; on pi-rook `git pull`.
2. `aicg org bootstrap-domain --domain ai-startup --create-remotes` (org exists;
   creates + pushes the 5 repos + `.github`). May run from Mac via `gh` too, but
   the venv/aicg path makes pi-rook the natural host.
3. Seed founder-ceo content; commit/push that repo.
4. `scripts/install-domain-timers.sh --domain ai-startup --hour-offset 8`
   (siblings use 2/4/6; confirm 8 free on-host). Verify timers + dry-run tick.

## 10. Testing Strategy (TDD)

- **Regression first:** golden test that the four existing domain configs load
  and produce identical `repo_names`/`release_repo_names`/pairing behavior after
  the model change.
- Unit tests for `RoleConfig` single/pair validation and `OrgManifest` null-safe
  properties.
- `bootstrap` single-repo skeleton test (files written, no solutions repo).
- `pairing_audit` skips single-repo roles.
- `ai-startup.yaml` loads, `list-roles`/`list-domains` include it, 80%+ coverage
  on new/changed code.

## 11. Build Order

1. Harness extension (model + consumers + timers) — TDD, backward-compat green.
2. Write `config/domains/ai-startup.yaml`; validate load + list.
3. `bootstrap-domain --create-remotes` (5 repos + `.github`).
4. Enrich `.github` + reciprocal ecosystem links across all 5 orgs.
5. Seed founder-ceo content.
6. Deploy timers on pi-rook; verify.

## 12. Risks & Open Items

- **Production pipeline risk** — mitigated by additive changes + regression tests;
  keep new domain `pipeline` phases conservative until content settles.
- **Timer script** is shell (not Python) — single-repo handling needs care and a
  `--dry-run` verification before enabling.
- **Model IDs** mirror siblings (`claude-opus-4-7` / `codex-gpt-5.5`); bump later.
- **Public repos in a real org** — creation is outward-facing; do it once the
  skeletons + seed content are ready to avoid churn.
- Deeper cross-repo module reuse (true shared modules across repos) is future
  work; wave one uses front-matter `requires:` + a linked pillar map.
