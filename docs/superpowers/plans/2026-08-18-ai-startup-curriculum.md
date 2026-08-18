# ai-startup-curriculum Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fifth AICG domain — `ai-startup-curriculum` — as five single-repo `*-curriculum` repos driven by the existing nightly pipeline, with Founder/CEO seeded as the live-lab starter.

**Architecture:** Extend the harness additively to support single-repo curricula (`repo_model: single`, optional `solution_repo`) alongside today's learning/solutions pairs, add the domain config, scaffold the repos + org `.github`, hand-seed Founder/CEO, and wire per-role timers on pi-rook.

**Tech Stack:** Python 3.10 (`aicg` package, pytest, ruff), YAML/JSON configs, bash (`install-domain-timers.sh`), `gh` CLI, systemd --user timers on pi-rook.

## Global Constraints

- **Backward compatibility is absolute.** The four existing domains (`ai-infra`, `ml-engineering`, `ai-engineering`, `ai-governance`) must load and behave identically. Do not edit existing domain configs. Prove with a regression test.
- `solution_repo` becomes `str | None`; `repo_model` defaults to `"pair"`. A `pair` entry MUST have a `solution_repo`; a `single` entry MUST NOT.
- Single-repo unit name convention: `<name>-curriculum`; exemplar artifacts live in-repo under `exemplars/`.
- Org: `ai-startup-curriculum` (exists). Remote: `git@github.com:ai-startup-curriculum/{repo}.git`. Maintainer footer: VeriSwarm.ai. Agents/judge/research mirror `config/domains/ai-engineering.yaml`.
- Levels are path-order indices, not seniority: foundations 10, founder-ceo 20, product-gtm 30, finance-fundraising 40, operations-governance 50.
- Stage taxonomy: `IDEA → PRE-SEED → SEED → SERIES-A → GROWTH → MATURE`.
- `aicg` runs on pi-rook (Mac `.venv` interpreter path is broken). Repo creation + timers execute over SSH `rook@pi-rook`.
- Branch: `feat/ai-startup-domain` in ai-infra-content-generator.

---

## File Structure

- `src/aicg/org_config.py` — `RoleConfig`/`OrgManifest` model + loader (single-repo support).
- `src/aicg/bootstrap.py` — single-repo skeleton scaffolding.
- `src/aicg/pairing_audit.py`, `audit.py`, `org_runner.py`, `domain_provision.py` — single-aware guards.
- `src/aicg/cli.py` — `list-roles --with-repos` for the timer script.
- `scripts/install-domain-timers.sh` — single-repo timer selection.
- `config/domains/ai-startup.yaml` — the new domain config.
- `tests/test_single_repo_curriculum.py` — new tests.
- `tests/test_domains.py` — regression assertions for the four existing domains.
- Content (seed): `founder-ceo-curriculum` repo (authored at execution, Task 9).

---

### Task 1: Single/pair data model + loader validation

**Files:**
- Modify: `src/aicg/org_config.py` (`RoleConfig`, `load_manifest`, `OrgManifest` properties)
- Test: `tests/test_single_repo_curriculum.py`

**Interfaces:**
- Produces: `RoleConfig(id, title, level, learning_repo, solution_repo: str|None=None, aliases=(), repo_model: str="pair")`; `RoleConfig.is_single -> bool`; `RoleConfig.repos -> tuple[str,...]`. `OrgManifest.solution_repo_names`/`repo_names`/`release_repo_names` skip `None`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_single_repo_curriculum.py
import json
import pytest
from aicg.org_config import load_manifest, ManifestError

def _write(tmp_path, roles):
    cfg = {
        "org": "ai-startup-curriculum",
        "default_remote": "git@github.com:ai-startup-curriculum/{repo}.git",
        "roles": roles, "extra_repos": [{"name": ".github", "kind": "org-profile"}],
        "release": {}, "documentation": {}, "schedules": {}, "automation": {},
        "content_generation": {}, "quality_judge": {}, "pipeline": {},
        "job_requirements": {}, "research": {}, "maintained_by": {},
    }
    p = tmp_path / "ai-startup.yaml"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p

def test_single_repo_role_loads(tmp_path):
    p = _write(tmp_path, [{"id": "founder-ceo", "title": "Founder / CEO",
        "level": 20, "learning_repo": "founder-ceo-curriculum", "repo_model": "single"}])
    m = load_manifest(p)
    r = m.roles[0]
    assert r.is_single is True
    assert r.solution_repo is None
    assert r.repos == ("founder-ceo-curriculum",)
    assert m.solution_repo_names == []
    assert m.repo_names == ["founder-ceo-curriculum", ".github"]

def test_pair_role_still_defaults(tmp_path):
    p = _write(tmp_path, [{"id": "engineer", "title": "Engineer", "level": 20,
        "learning_repo": "engineer-learning", "solution_repo": "engineer-solutions"}])
    m = load_manifest(p)
    r = m.roles[0]
    assert r.is_single is False
    assert r.repos == ("engineer-learning", "engineer-solutions")

def test_single_with_solution_repo_rejected(tmp_path):
    p = _write(tmp_path, [{"id": "x", "title": "X", "level": 1,
        "learning_repo": "x-curriculum", "solution_repo": "x-solutions", "repo_model": "single"}])
    with pytest.raises(ManifestError):
        load_manifest(p)

def test_pair_without_solution_repo_rejected(tmp_path):
    p = _write(tmp_path, [{"id": "x", "title": "X", "level": 1, "learning_repo": "x-learning"}])
    with pytest.raises(ManifestError):
        load_manifest(p)
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest tests/test_single_repo_curriculum.py -v` (on pi-rook, or a working venv)
Expected: FAIL (`repo_model`/`is_single` unknown, no validation).

- [ ] **Step 3: Implement**

In `RoleConfig`: `solution_repo: str | None = None`; add `repo_model: str = "pair"`; add:
```python
    @property
    def is_single(self) -> bool:
        return self.repo_model == "single"

    @property
    def repos(self) -> tuple[str, ...]:
        return (self.learning_repo,) if self.is_single else (self.learning_repo, self.solution_repo)
```
In `load_manifest`, build each role with `solution_repo=item.get("solution_repo")`, `repo_model=item.get("repo_model", "pair")`, then validate:
```python
        model = item.get("repo_model", "pair")
        sol = item.get("solution_repo")
        if model == "single" and sol:
            raise ManifestError(f"Role {item['id']!r}: single repo_model must not set solution_repo.")
        if model == "pair" and not sol:
            raise ManifestError(f"Role {item['id']!r}: pair repo_model requires solution_repo.")
```
Null-guard the properties:
```python
    @property
    def repo_names(self):
        names = []
        for role in self.roles:
            names.extend(r for r in role.repos)
        names.extend(repo.name for repo in self.extra_repos)
        return list(dict.fromkeys(names))

    @property
    def solution_repo_names(self):
        return [r.solution_repo for r in self.roles if r.solution_repo]
```
Update `release_repo_names` to use the null-safe lists.

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/test_single_repo_curriculum.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/aicg/org_config.py tests/test_single_repo_curriculum.py
git commit -m "feat(org_config): support single-repo curricula (repo_model)"
```

---

### Task 2: Regression — existing domains unchanged

**Files:**
- Modify/Test: `tests/test_domains.py`

**Interfaces:** Consumes Task 1's model.

- [ ] **Step 1: Write failing test** (a golden snapshot of the four live domains)

```python
def test_existing_domains_repo_names_stable():
    from aicg.domains import domain_config_path
    from aicg.org_config import load_manifest
    expected_pairs = {  # each existing role must remain a pair with a solution repo
        "ai-engineering", "ai-governance", "ml-engineering",
    }
    for domain in expected_pairs:
        m = load_manifest(domain_config_path(domain))
        assert m.roles, domain
        for r in m.roles:
            assert r.is_single is False, f"{domain}:{r.id} must stay pair"
            assert r.solution_repo, f"{domain}:{r.id} lost solution_repo"
            assert r.repos == (r.learning_repo, r.solution_repo)
```

- [ ] **Step 2: Run — expect PASS** (proves additive change didn't alter existing behavior)

Run: `.venv/bin/pytest tests/test_domains.py -v`

- [ ] **Step 3: Commit**

```bash
git add tests/test_domains.py
git commit -m "test(domains): lock pair-model regression for existing domains"
```

---

### Task 3: Single-repo bootstrap skeleton

**Files:**
- Modify: `src/aicg/bootstrap.py` (`_resolve_repo_names`, `bootstrap_role`, `_create_github_repos`; add `_write_single_skeleton`)
- Test: `tests/test_single_repo_curriculum.py`

**Interfaces:**
- Produces: `bootstrap_role(..., create_remotes=False)` on a single-repo role writes only `<workspace>/<curriculum-repo>/` (README, CURRICULUM.md, VERSIONS.md, LICENSE, .gitignore, CI, and an `exemplars/.gitkeep`); no solutions path; `report["plan"]["solution_repo"]` is `None`.

- [ ] **Step 1: Write failing test**

```python
def test_bootstrap_single_repo_writes_one_tree(tmp_path):
    from aicg.org_config import load_manifest
    from aicg.bootstrap import bootstrap_role
    cfg = tmp_path / "ai-startup.yaml"
    cfg.write_text(json.dumps({
        "org": "ai-startup-curriculum",
        "default_remote": "git@github.com:ai-startup-curriculum/{repo}.git",
        "roles": [{"id": "founder-ceo", "title": "Founder / CEO", "level": 20,
                   "learning_repo": "founder-ceo-curriculum", "repo_model": "single"}],
        "extra_repos": [{"name": ".github", "kind": "org-profile"}],
        "release": {}, "documentation": {}, "schedules": {}, "automation": {},
        "content_generation": {}, "quality_judge": {}, "pipeline": {},
        "job_requirements": {}, "research": {}, "maintained_by": {},
    }), encoding="utf-8")
    m = load_manifest(cfg)
    ws = tmp_path / "ws"; ws.mkdir()
    report = bootstrap_role(manifest=m, workspace=ws, role_id="founder-ceo",
        title="Founder / CEO", level=20, write_manifest=False, state_dir=tmp_path/"state")
    assert (ws / "founder-ceo-curriculum" / "README.md").exists()
    assert (ws / "founder-ceo-curriculum" / "exemplars").is_dir()
    assert not (ws / "founder-ceo-solutions").exists()
    assert report["plan"]["solution_repo"] is None
```

- [ ] **Step 2: Run — expect FAIL** (`bootstrap_role` currently requires a solutions path)

- [ ] **Step 3: Implement**

- `_resolve_repo_names(manifest, role_id)`: return `(role.learning_repo, role.solution_repo)` where `solution_repo` may be `None`.
- In `bootstrap_role`: branch on `role.is_single` (look up via `manifest` roles). When single: create only `learning_path`, call new `_write_single_skeleton(learning_path, role_id, title, description, ctx)` (learning skeleton + `exemplars/.gitkeep`), set `solution_path=None`, skip the solutions existence check and `_write_solutions_skeleton`.
- `BootstrapPlan.to_dict()` must emit `solution_repo: None` / `solution_path: None` cleanly.
- `_create_github_repos`: create only the curriculum repo when single.

- [ ] **Step 4: Run — expect PASS.** Also run full bootstrap suite: `.venv/bin/pytest tests/test_bootstrap.py -v` (regression).

- [ ] **Step 5: Commit**

```bash
git add src/aicg/bootstrap.py tests/test_single_repo_curriculum.py
git commit -m "feat(bootstrap): single-repo curriculum skeleton"
```

---

### Task 4: Single-aware audits + org profile

**Files:**
- Modify: `src/aicg/pairing_audit.py`, `src/aicg/audit.py`, `src/aicg/org_runner.py`, `src/aicg/domain_provision.py`
- Test: `tests/test_single_repo_curriculum.py`, `tests/test_domain_provision.py`

**Interfaces:** Consumes Task 1 model. Produces: pairing audit skips `is_single` roles; org-profile renders a single-repo row.

- [ ] **Step 1: Write failing tests**

```python
def test_pairing_audit_skips_single(tmp_path):
    from aicg.org_config import load_manifest
    from aicg.pairing_audit import pairing_report  # adjust to actual entrypoint
    # single-repo manifest -> no pairing findings about a missing solutions repo
    ...
def test_org_profile_lists_single_repo(tmp_path):
    from aicg.domain_provision import render_org_profile
    from aicg.org_config import load_manifest
    m = load_manifest(<single-repo cfg>)
    md = render_org_profile(m, tagline=None)
    assert "founder-ceo-curriculum" in md
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — in each consumer, iterate `role.repos` (or guard `if role.solution_repo:`) instead of assuming a pair. In `pairing_audit`, `continue` when `role.is_single`. In `domain_provision._RoleRow`, handle a missing solution repo.

- [ ] **Step 4: Run — expect PASS**, plus `.venv/bin/pytest tests/test_domain_provision.py tests/test_broad_audits.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/aicg/pairing_audit.py src/aicg/audit.py src/aicg/org_runner.py src/aicg/domain_provision.py tests/
git commit -m "feat: single-repo aware audits + org profile"
```

---

### Task 5: `list-roles --with-repos` + timer script

**Files:**
- Modify: `src/aicg/cli.py` (`org_list_roles` parser + `cmd_org_list_roles`)
- Modify: `scripts/install-domain-timers.sh`
- Test: `tests/test_single_repo_curriculum.py`

**Interfaces:** Produces: `aicg org list-roles --with-repos` prints `id\tlearning_repo\tsolution_repo_or_-\tmodel` per line; bare `list-roles` output unchanged (ids only).

- [ ] **Step 1: Write failing test** — invoke CLI via `subprocess`/`capsys` asserting the tab format and that bare output stays ids-only.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — add `--with-repos` flag; when set, print `f"{r.id}\t{r.learning_repo}\t{r.solution_repo or '-'}\t{r.repo_model}"`.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Update `install-domain-timers.sh`** — read repo model via `list-roles --with-repos`; for `single` roles install research/generate/review timers targeting the curriculum repo only (no solutions review). Verify with `--dry-run --domain ai-startup`.

- [ ] **Step 6: Commit**

```bash
git add src/aicg/cli.py scripts/install-domain-timers.sh tests/test_single_repo_curriculum.py
git commit -m "feat(timers): single-repo aware list-roles + domain timers"
```

---

### Task 6: `config/domains/ai-startup.yaml`

**Files:**
- Create: `config/domains/ai-startup.yaml`
- Test: `tests/test_domains.py`

**Interfaces:** Consumes Tasks 1–5. Produces a loadable fifth domain.

- [ ] **Step 1: Write failing test**

```python
def test_ai_startup_domain_loads():
    from aicg.domains import list_domains, domain_config_path
    from aicg.org_config import load_manifest
    assert "ai-startup" in list_domains()
    m = load_manifest(domain_config_path("ai-startup"))
    assert m.org == "ai-startup-curriculum"
    ids = {r.id for r in m.roles}
    assert {"startup-foundations", "founder-ceo", "startup-product-gtm",
            "startup-finance-fundraising", "startup-operations-governance"} <= ids
    assert all(r.is_single for r in m.roles)
```

- [ ] **Step 2: Run — expect FAIL** (file absent).

- [ ] **Step 3: Create the config** — copy `ai-engineering.yaml`; change `org`, `default_remote`; replace `roles` with the five single entries (`repo_model: single`, `learning_repo: <name>-curriculum`, no `solution_repo`, levels 10/20/30/40/50, aliases each); keep `.github` extra_repo; mirror judge/pipeline/research/agents.

- [ ] **Step 4: Run — expect PASS**, plus `.venv/bin/aicg org list-roles --domain ai-startup`.

- [ ] **Step 5: Commit**

```bash
git add config/domains/ai-startup.yaml tests/test_domains.py
git commit -m "feat(domains): add ai-startup domain config"
```

---

### Task 7: Full suite + lint gate

- [ ] **Step 1:** `.venv/bin/pytest -q` — all green (backward-compat proven).
- [ ] **Step 2:** `.venv/bin/ruff check src tests` — clean.
- [ ] **Step 3:** Merge `feat/ai-startup-domain` → `main`; push. (Enables pi-rook `git pull`.)

---

### Task 8: Create remotes (5 repos + `.github`) — on pi-rook

- [ ] **Step 1:** SSH `rook@pi-rook`; `cd ~/ai-infra-curriculum/ai-infra-content-generator`; `git pull`.
- [ ] **Step 2:** `aicg org bootstrap-domain --domain ai-startup --create-remotes --tagline "An open-source startup operating school for technical founders."`
- [ ] **Step 3:** Verify: `gh repo list ai-startup-curriculum` shows the 5 curricula + `.github`.

---

### Task 9: Seed Founder/CEO + `.github` profile + cross-links

**Files (content, in the created repos):**
- `founder-ceo-curriculum`: `CURRICULUM.md` (module spine + stage tags + dependency links); `lessons/mod-001-customer-discovery/` authored fully; stubs for mod-002…006; `exemplars/` (interview guide, lean canvas, runway model, investor funnel, cap table, sales script).
- `startup-foundations`: `STARTUP_STAGES.md`, `FUNCTIONAL_CURRICULA.md` (the dependency graph + pillar map).
- `.github/profile/README.md`: three-axis model, five-repo table, stage taxonomy, ecosystem block linking the 4 sibling orgs.

- [ ] **Step 1:** Author mod-001 (learning content + real founder-artifact exercise + exemplar), stage-tagged (IDEA), `requires:` links. Commit/push `founder-ceo-curriculum`.
- [ ] **Step 2:** Author foundations docs (stages + functional graph). Commit/push `startup-foundations`.
- [ ] **Step 3:** Enrich `.github/profile/README.md`; commit/push.
- [ ] **Step 4:** Add `ai-startup-curriculum` to the ecosystem block in all four sibling `.github` profiles (reciprocal); commit/push each.

---

### Task 10: Wire timers on pi-rook + verify

- [ ] **Step 1:** `scripts/install-domain-timers.sh --domain ai-startup --hour-offset 8 --dry-run` (confirm 8 free; adjust if taken).
- [ ] **Step 2:** Run for real; `systemctl --user list-timers | grep aicg-ai-startup`.
- [ ] **Step 3:** Dry-run tick: `aicg org pipeline-status --domain ai-startup` and one `generate-role` unit `--dry-run` to confirm authoring path.
- [ ] **Step 4:** Update memory (`project_multidomain_harness`) → 5 orgs live; note single-repo model.

---

## Self-Review

- **Spec coverage:** three-axis model (Tasks 6, 9) · single-repo harness (1,3,4,5) · 5 repos (8) · seed founder-ceo (9) · cross-linking (9) · deploy/timers (10) · backward-compat (2,7). ✓
- **Placeholders:** Task 4 test bodies are sketched (`...`) because the exact `pairing_audit`/`domain_provision` entrypoints must be read at execution; every other code step is concrete. Flag: implementer reads those two modules first.
- **Type consistency:** `repo_model`, `is_single`, `repos`, `solution_repo: str|None` used consistently across tasks. ✓
