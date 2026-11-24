"""Fleet status — a read-only roll-up across every curriculum domain.

Roadmap §4 (control plane) starts here: now that the harness runs four
domains (ai-infra + ml-engineering + ai-engineering + ai-governance), an
operator needs one pane that answers "how live is each domain?" without
opening four manifests. This module is the read model; it makes no writes
and no network calls. The pure functions below take already-loaded
manifests so they're trivially testable; the CLI layer does the IO.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .judge import JudgeConfig
from .pipeline_config import PipelineConfig


@dataclass(frozen=True)
class DomainStatus:
    domain: str
    org: str
    role_count: int
    enabled_phases: tuple[str, ...]
    daily_budget: int
    judge_enabled: bool
    judge_flag_only: bool
    freshness_bar: int | None
    queue_depth: int | None  # None = no work-queue state file found

    @property
    def mode(self) -> str:
        """At-a-glance liveness: ACT (writing), OBSERVE (judging, flag-only),
        or INERT (judge off, no write-phases)."""
        if self.enabled_phases:
            return "ACT"
        if self.judge_enabled:
            return "OBSERVE"
        return "INERT"


def build_domain_status(
    domain: str, manifest: Any, queue_depth: int | None = None
) -> DomainStatus:
    """Derive a DomainStatus from a loaded manifest. Pure — no IO."""
    pc = PipelineConfig.from_manifest(manifest)
    jc = JudgeConfig.from_manifest(manifest)
    bar = None
    if jc.thresholds:
        bar = jc.thresholds.get("freshness", jc.thresholds.get("default"))
    return DomainStatus(
        domain=domain,
        org=manifest.org,
        role_count=len(manifest.roles),
        enabled_phases=tuple(pc.enabled_phases()),
        daily_budget=pc.daily_budget,
        judge_enabled=jc.enabled,
        judge_flag_only=jc.flag_only,
        freshness_bar=bar,
        queue_depth=queue_depth,
    )


def read_queue_depth(state_dir: Path) -> int | None:
    """Best-effort: count open items in ``<state_dir>/work-queue.json``.

    Returns None when the file is absent (a domain that hasn't run yet) or
    unreadable, so the fleet view degrades gracefully rather than erroring.
    """
    qpath = state_dir / "work-queue.json"
    if not qpath.is_file():
        return None
    try:
        data = json.loads(qpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    items = _queue_items(data)
    if items is None:
        return None
    counted = [
        it for it in items
        if not isinstance(it, dict) or it.get("status", "queued") in _OPEN_QUEUE_STATES
    ]
    return len(counted)


_OPEN_QUEUE_STATES = frozenset({"queued", "pending", "open", "in_progress", "ready"})


def _queue_items(data: Any) -> list | None:
    """Extract the item list from a work-queue payload.

    The runner writes ``work_items``; older/test payloads used ``items`` or a
    bare list. Returns None when no list is present.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("work_items", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return None


def render_fleet_table(statuses: list[DomainStatus]) -> str:
    """Render a fixed-width table of the fleet. Pure formatting."""
    header = (
        f"{'DOMAIN':<16} {'ORG':<26} {'ROLES':>5} {'MODE':<8} "
        f"{'PHASES':<22} {'JUDGE':<14} {'BAR':>4} {'BUDGET':>6} {'QUEUE':>6}"
    )
    lines = ["Fleet status — AI Career Curriculum ecosystem", "", header, "-" * len(header)]
    for s in sorted(statuses, key=lambda d: d.domain):
        phases = ",".join(s.enabled_phases) if s.enabled_phases else "—"
        judge = (
            f"{'on' if s.judge_enabled else 'off'}"
            + (" (flag)" if s.judge_enabled and s.judge_flag_only else "")
        )
        bar = str(s.freshness_bar) if s.freshness_bar is not None else "—"
        queue = "—" if s.queue_depth is None else str(s.queue_depth)
        lines.append(
            f"{s.domain:<16} {s.org:<26} {s.role_count:>5} {s.mode:<8} "
            f"{phases:<22} {judge:<14} {bar:>4} {s.daily_budget:>6} {queue:>6}"
        )
    total_roles = sum(s.role_count for s in statuses)
    act = sum(1 for s in statuses if s.mode == "ACT")
    observe = sum(1 for s in statuses if s.mode == "OBSERVE")
    inert = sum(1 for s in statuses if s.mode == "INERT")
    lines.append("")
    lines.append(
        f"{len(statuses)} domains · {total_roles} roles · "
        f"{act} ACT / {observe} OBSERVE / {inert} INERT"
    )
    return "\n".join(lines)


# --- Daily digest ----------------------------------------------------------
#
# The digest used to report only "roles with ≥1 authored module" — a one-way
# milestone that hit 100% and then read identically every day. It now reports
# flow: PR health from the steward, the last daily work item's outcome, and
# actionable queue depth, with an attention block for anything stuck.

STALE_REPORT_HOURS = 36  # steward runs several times a day; >1.5d means it stopped
OLD_PR_DAYS = 3  # an aicg PR open longer than this is not self-resolving
_HEAD_REF_DATE = re.compile(r"aicg/(\d{4}-\d{2}-\d{2})/")


@dataclass(frozen=True)
class DomainActivity:
    """What a domain did / has stuck, derived from its runner state files.

    ``None`` means "no report found" — distinct from zero, so a domain whose
    steward never ran is not mistaken for a healthy one with no PRs.
    """

    open_prs: int | None = None
    blocked_prs: int = 0
    ci_failed_prs: int = 0
    oldest_pr_days: int | None = None
    blocker_reasons: tuple[tuple[str, int], ...] = ()
    steward_age_hours: float | None = None
    queue_ready: int | None = None
    queue_deferred: int | None = None
    last_item: str | None = None
    last_item_repo: str | None = None
    last_item_age_days: int | None = None
    last_item_problem: bool = False


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _blocker_reason(text: str) -> str:
    """Collapse a blocker message to its category (drop file lists / periods)."""
    return text.split(":", 1)[0].strip().rstrip(".")


def _summarize_prs(steward: dict, now: datetime) -> dict:
    repos = [r for r in steward.get("repos") or [] if isinstance(r, dict)]
    prs = [p for r in repos for p in r.get("prs") or [] if isinstance(p, dict)]
    reasons: Counter[str] = Counter(
        _blocker_reason(b) for p in prs for b in p.get("blockers") or [] if isinstance(b, str)
    )
    pr_dates = [
        datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        for p in prs
        if (m := _HEAD_REF_DATE.search(str(p.get("head_ref", ""))))
    ]
    generated = _parse_ts(steward.get("generated_at"))
    return {
        "open_prs": sum(int(r.get("pr_count", 0)) for r in repos),
        "blocked_prs": sum(
            int(r.get("blocked_count", 0)) + int(r.get("review_blocked_count", 0)) for r in repos
        ),
        "ci_failed_prs": sum(int(r.get("ci_failed_count", 0)) for r in repos),
        "oldest_pr_days": (now - min(pr_dates)).days if pr_dates else None,
        "blocker_reasons": tuple(reasons.most_common()),
        "steward_age_hours": (now - generated).total_seconds() / 3600 if generated else None,
    }


def _summarize_queue(work_queue: Any) -> dict:
    items = _queue_items(work_queue)
    if items is None:
        return {}
    statuses = [it.get("status") for it in items if isinstance(it, dict)]
    return {
        "queue_ready": sum(1 for st in statuses if st in _OPEN_QUEUE_STATES),
        "queue_deferred": statuses.count("deferred"),
    }


def _last_item_outcome(item: dict) -> tuple[str, bool]:
    """Human outcome of the last daily work item, and whether it's a problem."""
    if (item.get("inline_merge") or {}).get("status") == "merged":
        return "merged", False
    if (item.get("pr") or {}).get("status") == "opened":
        return "PR opened", False
    if item.get("parked_branch"):
        why = (item.get("verify") or {}).get("status") or "parked"
        return f"parked ({why})", True
    status = str(item.get("status") or "unknown")
    if status == "deferred":
        return f"deferred: {item.get('defer_reason') or 'unknown'}", True
    return status, status in {"failed", "error", "verification_failed"}


def _summarize_last_item(item: Any, now: datetime) -> dict:
    if not isinstance(item, dict):
        return {}
    outcome, problem = _last_item_outcome(item)
    selected = item.get("selected") if isinstance(item.get("selected"), dict) else {}
    ts = _parse_ts(item.get("generated_at"))
    return {
        "last_item": outcome,
        "last_item_repo": selected.get("repo"),
        "last_item_age_days": (now - ts).days if ts else None,
        "last_item_problem": problem,
    }


def summarize_activity(
    *, steward: Any, work_queue: Any, last_item: Any, now: datetime
) -> DomainActivity:
    """Derive a DomainActivity from already-loaded state payloads. Pure."""
    fields: dict[str, Any] = {}
    if isinstance(steward, dict):
        fields.update(_summarize_prs(steward, now))
    fields.update(_summarize_queue(work_queue))
    fields.update(_summarize_last_item(last_item, now))
    return DomainActivity(**fields)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_domain_activity(state_dir: Path, now: datetime) -> DomainActivity:
    """Best-effort IO wrapper: load a domain's runner state and summarize it."""
    return summarize_activity(
        steward=_load_json(state_dir / "steward-report.json"),
        work_queue=_load_json(state_dir / "work-queue.json"),
        last_item=_load_json(state_dir / "daily-item-last.json"),
        now=now,
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def attention_flags(a: DomainActivity) -> list[str]:
    """Things an operator should act on. Empty list = domain is healthy."""
    flags: list[str] = []
    if a.blocked_prs:
        why = ", ".join(f"{r} ×{n}" for r, n in a.blocker_reasons)
        flags.append(f"{_plural(a.blocked_prs, 'PR')} blocked" + (f" ({why})" if why else ""))
    if a.ci_failed_prs:
        flags.append(f"{_plural(a.ci_failed_prs, 'PR')} failing CI")
    if a.oldest_pr_days is not None and a.oldest_pr_days > OLD_PR_DAYS:
        flags.append(f"oldest open PR {a.oldest_pr_days}d")
    if a.last_item_problem:
        repo = f" — {a.last_item_repo}" if a.last_item_repo else ""
        flags.append(f"last item {a.last_item}{repo}")
    if a.steward_age_hours is None and a.open_prs is None:
        flags.append("no steward report")
    elif a.steward_age_hours is not None and a.steward_age_hours > STALE_REPORT_HOURS:
        flags.append(f"steward report stale ({a.steward_age_hours / 24:.0f}d old)")
    return flags


@dataclass(frozen=True)
class DigestRow:
    domain: str
    mode: str
    bar: int | None
    filled: int  # roles with authored modules
    total: int
    activity: DomainActivity = field(default_factory=DomainActivity)


def _age(days: int | None) -> str:
    if days is None:
        return ""
    return " today" if days == 0 else f" {days}d ago"


def _domain_line(r: DigestRow) -> str:
    a = r.activity
    parts = []
    if a.open_prs is None:
        parts.append("PRs ?")
    else:
        bad = [f"{a.blocked_prs} blocked"] if a.blocked_prs else []
        bad += [f"{a.ci_failed_prs} CI-failing"] if a.ci_failed_prs else []
        parts.append(f"PRs {a.open_prs}" + (f" ({', '.join(bad)})" if bad else ""))
    if a.queue_ready is not None:
        deferred = f" (+{a.queue_deferred} deferred)" if a.queue_deferred else ""
        parts.append(f"queue {a.queue_ready}{deferred}")
    if a.last_item:
        repo = f" {a.last_item_repo}" if a.last_item_repo else ""
        parts.append(f"last: {a.last_item}{repo}{_age(a.last_item_age_days)}")
    if r.filled < r.total:  # only worth saying once it regresses / is incomplete
        parts.append(f"{r.filled}/{r.total} filled")
    if r.mode != "ACT":
        parts.append(r.mode)
    return f"{r.domain}: " + " · ".join(parts)


def render_fleet_digest(rows: list[DigestRow], *, date: str = "") -> str:
    """Daily ntfy digest: attention items first, then one line per domain. Pure."""
    rows = sorted(rows, key=lambda x: x.domain)
    lines = [f"AICG fleet{(' — ' + date) if date else ''}"]
    if not rows:  # a misconfigured runner must not read as an all-clear
        lines.append("⚠ no domains found — check config/domains/")
        return "\n".join(lines)
    attention = [
        f"⚠ {r.domain}: {flag}" for r in rows for flag in attention_flags(r.activity)
    ]
    lines.extend(attention or ["✅ nothing needs attention"])
    lines.append("")
    lines.extend(_domain_line(r) for r in rows)
    acts = [r.activity for r in rows]
    lines.append(
        f"Fleet: {sum(a.open_prs or 0 for a in acts)} open PRs "
        f"({sum(a.blocked_prs for a in acts)} blocked, "
        f"{sum(a.ci_failed_prs for a in acts)} CI-failing) · "
        f"queue {sum(a.queue_ready or 0 for a in acts)}"
    )
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_fleet_html(statuses: list[DomainStatus], *, generated_at: str = "") -> str:
    """Render the fleet status as a self-contained static HTML dashboard.

    The web form of the §4 control plane's read model — same data as
    ``render_fleet_table``, no external assets, no network. Read-only: it
    surfaces state; promotion/control writes stay in the CLI behind the
    operator. ``generated_at`` is passed in (no clock here).
    """
    mode_color = {"ACT": "#7ee787", "OBSERVE": "#f0883e", "INERT": "#8b949e"}
    rows = []
    for s in sorted(statuses, key=lambda d: d.domain):
        phases = ", ".join(s.enabled_phases) if s.enabled_phases else "—"
        judge = ("on" if s.judge_enabled else "off") + (
            " · flag-only" if s.judge_enabled and s.judge_flag_only else ""
        )
        bar = str(s.freshness_bar) if s.freshness_bar is not None else "—"
        queue = "—" if s.queue_depth is None else str(s.queue_depth)
        color = mode_color.get(s.mode, "#8b949e")
        rows.append(
            "<tr>"
            f'<td class="dom">{_esc(s.domain)}</td>'
            f'<td class="org">{_esc(s.org)}</td>'
            f'<td class="num">{s.role_count}</td>'
            f'<td><span class="badge" style="--c:{color}">{s.mode}</span></td>'
            f"<td>{_esc(phases)}</td><td>{_esc(judge)}</td>"
            f'<td class="num">{bar}</td><td class="num">{s.daily_budget}</td>'
            f'<td class="num">{queue}</td>'
            "</tr>"
        )
    total_roles = sum(s.role_count for s in statuses)
    act = sum(1 for s in statuses if s.mode == "ACT")
    observe = sum(1 for s in statuses if s.mode == "OBSERVE")
    inert = sum(1 for s in statuses if s.mode == "INERT")
    stamp = f"<p class='stamp'>Generated {_esc(generated_at)}</p>" if generated_at else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AICG Fleet Status</title>
<style>
  :root {{ --bg:#0f1419; --surface:#161b22; --ink:#e6edf3; --dim:#8b949e; --rule:#1f2630; --accent:#79c0ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.5 ui-monospace,'JetBrains Mono',Menlo,monospace; padding:32px; }}
  h1 {{ font-size:1.4rem; color:var(--accent); margin:0 0 4px; }}
  .summary {{ color:var(--dim); margin:0 0 24px; }}
  .stamp {{ color:var(--dim); font-size:12px; margin:0 0 24px; }}
  table {{ border-collapse:collapse; width:100%; background:var(--surface);
    border:1px solid var(--rule); border-radius:8px; overflow:hidden; }}
  th,td {{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--rule); }}
  th {{ color:var(--dim); text-transform:uppercase; letter-spacing:.08em; font-size:11px; }}
  tr:last-child td {{ border-bottom:none; }}
  td.num {{ text-align:right; }}
  td.dom {{ color:var(--accent); }}
  td.org {{ color:var(--dim); }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:999px;
    color:var(--c); border:1px solid var(--c); font-size:12px; }}
</style></head>
<body>
  <h1>AICG Fleet Status</h1>
  <p class="summary">{len(statuses)} domains · {total_roles} roles · {act} ACT / {observe} OBSERVE / {inert} INERT</p>
  {stamp}
  <table>
    <thead><tr>
      <th>Domain</th><th>Org</th><th>Roles</th><th>Mode</th>
      <th>Phases</th><th>Judge</th><th>BAR</th><th>Budget</th><th>Queue</th>
    </tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body></html>
"""
