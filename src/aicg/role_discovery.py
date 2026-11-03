"""Role discovery: find job titles no existing role covers and propose them.

Monthly per-role research keeps existing roles current, but nothing looked for
*new* roles. This module does: per domain, the web-enabled research agent
searches recent postings in that domain's space and lists titles that match
no role title or alias in any domain. The runner then filters hard (evidence,
recency, overlap, caps) and opens a "New role proposal" issue in the parent
org for each survivor. A human decides; approving one is just running the
``aicg org bootstrap-role`` command in the issue, after which the existing
fill timers write the content.

Validation and rendering are pure; the agent call and GitHub calls are
injected so they can be tested without a network.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .proposals import PARENT_REPO, clean, issue_number, record

PROPOSAL_LABEL = "role-proposal"
WINDOW_DAYS = 60
MIN_EVIDENCE = 10
MAX_OVERLAP = 0.5
MAX_PER_RUN = 3
_SENIORITY = {"senior", "sr", "staff", "principal", "lead", "junior", "jr", "associate", "distinguished"}

Gh = Callable[[list[str]], str]


# --- names -------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Compare titles by role, not by seniority, punctuation, or location tags."""
    text = re.sub(r"\([^)]*\)", " ", title.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(w for w in text.split() if w not in _SENIORITY)


def known_names(manifests: Iterable[Any]) -> set[str]:
    """Normalized titles and aliases of every role in every domain."""
    names: set[str] = set()
    for manifest in manifests:
        for role in manifest.roles:
            names.add(normalize_title(role.title))
            names.update(normalize_title(a) for a in role.aliases)
    names.discard("")
    return names


# --- validation --------------------------------------------------------------------


def _recent_distinct(evidence: Any, today: date, window_days: int) -> int:
    if not isinstance(evidence, list):
        return 0
    cutoff = today - timedelta(days=window_days)
    urls = set()
    for item in evidence:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        try:
            posted = date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        if cutoff <= posted <= today:
            urls.add(str(item["url"]).rstrip("/"))
    return len(urls)


def _shape_error(c: Any) -> str | None:
    if not isinstance(c, dict):
        return "malformed: not an object"
    if not str(c.get("title", "")).strip():
        return "malformed: missing title"
    if not isinstance(c.get("suggested_level"), int):
        return "malformed: suggested_level must be an integer"
    return None


def validate_candidates(
    candidates: Any,
    known: set[str],
    *,
    today: date,
    domains: set[str] | None = None,
    window_days: int = WINDOW_DAYS,
    min_evidence: int = MIN_EVIDENCE,
    max_overlap: float = MAX_OVERLAP,
    max_per_run: int = MAX_PER_RUN,
) -> tuple[list[dict], list[dict]]:
    """Return (accepted, rejected). Accepted are strongest-evidence first, capped."""
    rejected: list[dict] = []
    qualified: list[tuple[int, dict]] = []
    seen: set[str] = set()

    def reject(c: Any, reason: str) -> None:
        rejected.append({"title": c.get("title") if isinstance(c, dict) else None, "reason": reason})

    for c in candidates if isinstance(candidates, list) else []:
        if (err := _shape_error(c)) is not None:
            reject(c, err)
            continue
        names = {normalize_title(c["title"]), *(normalize_title(a) for a in c.get("aliases") or [])}
        names.discard("")
        if hit := sorted(names & known):
            reject(c, f"covered_by_existing_role: {hit[0]}")
            continue
        if domains is not None and c.get("suggested_domain") not in domains:
            reject(c, f"unknown_domain: {c.get('suggested_domain')}")
            continue
        count = _recent_distinct(c.get("evidence"), today, window_days)
        if count < min_evidence:
            reject(c, f"evidence_below_threshold: {count} recent distinct postings < {min_evidence}")
            continue
        overlap = c.get("overlap_estimate")
        if not isinstance(overlap, (int, float)) or overlap > max_overlap:
            reject(c, f"overlaps_existing_role: {overlap} > {max_overlap}")
            continue
        key = normalize_title(c["title"])
        if key in seen:
            reject(c, "duplicate_in_run")
            continue
        seen.add(key)
        qualified.append((count, {**c, "evidence_count": count}))

    qualified.sort(key=lambda pair: -pair[0])
    accepted = [c for _, c in qualified[:max_per_run]]
    rejected += [{"title": c["title"], "reason": "cap_exceeded"} for _, c in qualified[max_per_run:]]
    return accepted, rejected


# --- prompt ------------------------------------------------------------------------


def build_prompt(
    domain: str,
    manifest: Any,
    all_manifests: list[Any],
    stance: dict[str, Any],
    *,
    output_file: Path,
    today: date,
) -> str:
    start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    titles = sorted({r.title for m in all_manifests for r in m.roles}
                    | {a for m in all_manifests for r in m.roles for a in r.aliases})
    own = "\n".join(f"- {r.title} (L{r.level})" for r in sorted(manifest.roles, key=lambda r: r.level))
    return f"""# Role discovery: {domain}

You are researching the job market for **new roles** this curriculum doesn't cover yet.

## The curriculum's focus
{stance.get("headline", "")} Search for jobs whose core work matches that relationship to the model.

Current roles in this curriculum:
{own}

## Already covered (do NOT propose these, or any seniority variant of them)
{", ".join(titles)}

## Task
1. Search live job postings published between {start} and {today.isoformat()} (use WebSearch/WebFetch;
   company career pages, LinkedIn, Greenhouse, Lever, Ashby, Workday, Indeed).
2. Find job titles that recur across employers and whose work isn't covered by any role above. A new
   title that is just an existing role at a different seniority, or a renamed existing role, does NOT count.
3. For each candidate, collect at least {MIN_EVIDENCE} distinct postings from different employers, with the URL
   and posting date of each. Fewer is fine to record, but say so; the runner will drop it.
4. Estimate overlap (0-1) with the nearest existing role: the share of the candidate's core skills an
   existing role's curriculum already teaches.
5. PREFER AN EMPTY LIST over weak candidates. Most months should find nothing.

## Output contract
Write exactly one file: `{output_file}`. It must be a JSON array (possibly empty) of:

```json
{{
  "title": "Canonical job title",
  "aliases": ["Other titles employers use for the same job"],
  "suggested_domain": "{domain}",
  "suggested_level": 30,
  "evidence": [{{"url": "https://...", "company": "...", "title": "posting title", "date": "YYYY-MM-DD"}}],
  "nearest_roles": ["existing-role-id"],
  "overlap_estimate": 0.3,
  "rationale": "Why this is a distinct role now, in 2-4 sentences.",
  "draft_modules": [{{"title": "...", "objectives": ["..."]}}]
}}
```

`suggested_domain` may name a sibling domain if the work fits there better. Levels follow this curriculum's
ladder (10 entry, 20, 30, 40 senior/lead, 50+ principal/executive). Do not write any other file.
"""


# --- issues ------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def role_meta(c: dict, domain: str) -> dict:
    """What the approvals job acts on. Recorded by the runner, never read back from the issue."""
    return {"kind": "role", "domain": domain, "role_id": _slug(c["title"]), "title": c["title"],
            "level": c["suggested_level"], "aliases": [str(a) for a in c.get("aliases") or []],
            "focus": _focus(c), "description": _description(c)}


def render_issue(c: dict, domain: str, domain_display: str) -> tuple[str, str]:
    """Title and body. Every agent-supplied field is cleaned before rendering."""
    name = clean(c["title"], 80)
    title = f"New role proposal: {name} ({domain_display})"
    evidence = "\n".join(
        f"- [{clean(e.get('company', '?'), 80)}: {clean(e.get('title', name), 120)}]"
        f"({clean(e['url'], 300)}) ({clean(e.get('date', '?'), 10)})"
        for e in (c.get("evidence") or []) if isinstance(e, dict) and e.get("url")
    )
    modules = "\n".join(
        f"{i}. **{clean(m.get('title', '?'), 120)}**"
        + "".join(f"\n   - {clean(o, 200)}" for o in m.get("objectives") or [])
        for i, m in enumerate(c.get("draft_modules") or [], start=1) if isinstance(m, dict)
    ) or "_None drafted._"
    aliases = ", ".join(clean(a, 80) for a in c.get("aliases") or []) or "none"
    nearest = ", ".join(f"`{clean(r, 60)}`" for r in c.get("nearest_roles") or []) or "none"
    body = f"""Found by the monthly role-discovery run for **{domain_display}**.

| | |
|---|---|
| Suggested level | L{int(c['suggested_level'])} |
| Also posted as | {aliases} |
| Nearest existing roles | {nearest} |
| Estimated overlap | {clean(c.get('overlap_estimate'), 10)} |
| Recent distinct postings | {c.get('evidence_count', len(c.get('evidence') or []))} (last {WINDOW_DAYS} days) |

## Why it's a distinct role
{clean(c.get('rationale', ''), 1500)}

## Draft modules
{modules}

## Evidence
{evidence}

## What approval does
Creates the role `{_slug(c['title'])}` at L{int(c['suggested_level'])} in `{domain}`: config, learning and
solutions repos, timers. Then the content pipeline writes it.

**To approve**, add the `approved` label. **To decline**, close this issue; discovery won't propose
this title again.

🤖 Generated by `aicg org discover-roles`
"""
    return title, body


def _focus(c: dict) -> str:
    titles = [m.get("title", "") for m in c.get("draft_modules") or [] if isinstance(m, dict)]
    return ", ".join(t for t in titles if t)[:150] or c["title"]


def _description(c: dict) -> str:
    first = re.split(r"(?<=[.!?])\s", str(c.get("rationale", "")).strip(), maxsplit=1)[0]
    return first[:240] or c["title"]


def default_gh(args: list[str]) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def _proposed_titles(gh: Gh, repo: str) -> set[str]:
    raw = gh(["issue", "list", "-R", repo, "--label", PROPOSAL_LABEL, "--state", "all",
              "--limit", "500", "--json", "title"])
    titles = set()
    for item in json.loads(raw or "[]"):
        m = re.match(r"New role proposal: (.+) \([^)]*\)$", item.get("title", ""))
        if m:
            titles.add(normalize_title(m.group(1)))
    return titles


def open_proposals(
    accepted: list[dict],
    domain_display: dict[str, str],
    *,
    gh: Gh = default_gh,
    apply: bool = False,
    repo: str = PARENT_REPO,
    store: Path | None = None,
) -> list[dict]:
    """Open one issue per accepted candidate, skipping titles proposed before (open or closed).

    Each opened proposal is recorded in ``store``; the approvals job acts only on records.
    """
    already = _proposed_titles(gh, repo)
    if apply and accepted:
        gh(["label", "create", PROPOSAL_LABEL, "-R", repo, "--color", "0E8A16",
            "--description", "Proposed new curriculum role", "--force"])
    results = []
    for c in accepted:
        domain = c.get("suggested_domain", "")
        if normalize_title(c["title"]) in already:
            results.append({"title": c["title"], "status": "already_proposed"})
            continue
        title, body = render_issue(c, domain, domain_display.get(domain, domain))
        if not apply:
            results.append({"title": c["title"], "status": "would_open"})
            continue
        url = gh(["issue", "create", "-R", repo, "--title", title, "--body", body,
                  "--label", PROPOSAL_LABEL]).strip()
        number = issue_number(url)
        if store is not None and number is not None:
            record(store, number, role_meta(c, domain))
        results.append({"title": c["title"], "status": "opened", "url": url})
    return results


# --- running a discovery pass ------------------------------------------------------

# An agent takes (prompt_path, workdir) and returns "ok", "limit", or "failed".
Agent = Callable[[Path, Path], str]


def research_agent(manifest: Any, runner_root: Path) -> Agent:
    """The domain's web-enabled research agent (same one per-role research uses)."""
    from .agent_cli import reclassify_with_response_file, run_agent_command

    template = (manifest.research.get("agent") or {}).get("agent_command") or manifest.research.get(
        "agent_command")
    if not template:
        raise RuntimeError(f"{manifest.org}: no research agent_command configured")

    def run(prompt_path: Path, workdir: Path) -> str:
        command = template.format(prompt=str(prompt_path), output_dir=str(workdir), repo=str(workdir),
                                  work_id=f"discover-roles:{manifest.org}", runner=str(runner_root))
        result = reclassify_with_response_file(
            run_agent_command(command, cwd=workdir), workdir / "response.md", workdir / "response.json")
        if result.limit_reached:
            return "limit"
        return "ok" if result.returncode == 0 else "failed"

    return run


def run_discovery(
    domain: str,
    manifest: Any,
    all_manifests: list[Any],
    stance: dict[str, Any],
    *,
    state_dir: Path,
    today: date,
    domains: set[str],
    agent: Agent,
) -> dict[str, Any]:
    """One discovery pass for a domain: prompt the agent, then validate what it found."""
    workdir = Path(state_dir) / "role-discovery" / today.strftime("%Y-%m")
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "candidates.json"
    output.unlink(missing_ok=True)
    prompt = workdir / "prompt.md"
    prompt.write_text(build_prompt(domain, manifest, all_manifests, stance, output_file=output,
                                   today=today), encoding="utf-8")
    outcome = agent(prompt, workdir)
    report: dict[str, Any] = {"domain": domain, "month": today.strftime("%Y-%m"), "accepted": [],
                              "rejected": []}
    if outcome == "limit":
        report["status"] = "deferred"
    elif not output.exists():
        report["status"] = "no_output" if outcome == "ok" else "agent_failed"
    else:
        try:
            candidates = json.loads(output.read_text(encoding="utf-8"))
        except ValueError:
            candidates = None
        accepted, rejected = validate_candidates(candidates, known_names(all_manifests), today=today,
                                                 domains=domains)
        report.update(status="ok", accepted=accepted, rejected=rejected)
    (workdir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
