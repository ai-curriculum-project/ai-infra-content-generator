"""Human approval loop for proposals.

Two things need a human yes before the runner acts on them:

- **content proposals**: research found new modules/exercises a role should
  teach (opened by ``aicg org research --apply`` alongside its proposal PR);
- **role proposals**: discovery found a job title no role covers (opened by
  ``aicg org discover-roles``).

Each opens an issue in the parent org's ``.github`` repo carrying a hidden
machine-readable block. A maintainer approves by adding the ``approved``
label (labels need triage rights, so a drive-by comment can't approve
anything) or declines by closing the issue. The daily ``approvals`` job
then does the work: merge + promote + author for content; create the role,
its repos, config, and timers, then author, for a role.

Command plans are pure (``actions_for``); execution and GitHub calls are
injected so the loop is testable offline.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .proposals import PARENT_REPO, clean, issue_number, load, record, validate

APPROVED_LABEL = "approved"
APPLIED_LABEL = "applied"
FAILED_LABEL = "approval-failed"
CONTENT_LABEL = "content-proposal"
ROLE_LABEL = "role-proposal"
GENERATOR_REPO = "ai-curriculum-project/ai-infra-content-generator"
HOUR_OFFSETS = {"ml-engineering": 2, "ai-engineering": 4, "ai-governance": 6,
                "ai-startup": 8, "ai-workforce": 10}

Gh = Callable[[list[str]], str]
Run = Callable[[list[str]], tuple[int, str]]


# --- content proposals (from research) ------------------------------------------------


def pr_url_from_outcome(outcome: dict[str, Any] | None) -> str | None:
    for step in (outcome or {}).get("steps", []):
        if step.get("step") == "pr_create" and step.get("returncode") == 0:
            found = re.search(r"https://github\.com/\S+/pull/\d+", step.get("stdout_tail", ""))
            if found:
                return found.group(0)
    return None


def _item_label(item: dict[str, Any]) -> str:
    ident = item.get("id") or "/".join(str(item[k]) for k in ("module", "slug") if item.get(k)) or "?"
    title = item.get("title")
    return f"`{clean(ident, 100)}`" + (f": {clean(title, 200)}" if title else "")


def content_issue(domain: str, domain_display: str, role_id: str, role_title: str, month: str,
                  validation: dict[str, Any], pr_url: str,
                  delta: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
    accepted = validation.get("accepted", {})
    sections = []
    for kind in ("modules", "exercises", "projects"):
        items = accepted.get(kind) or []
        if items:
            sections.append(f"**{kind.title()}**\n" + "\n".join(f"- {_item_label(i)}" for i in items))
    title = f"Curriculum proposal: {role_title} ({domain_display}) — {month}"
    body = (
        f"This month's job-market research for **{role_title}** found content the track should add.\n\n"
        + "\n\n".join(sections)
        + f"\n\nEvidence, rationale, and the exact plan changes: {pr_url}\n\n"
        "**To approve**, add the `approved` label. The runner merges the PR, applies it to the "
        "curriculum plan, and writes the new content. **To decline**, close this issue; the PR "
        "is closed too.\n"
    )
    meta = {"kind": "content", "domain": domain, "role_id": role_id, "month": month,
            "pr_url": pr_url, "delta": delta}
    return title, body, meta


def open_content_issue(gh: Gh, title: str, body: str, meta: dict[str, Any], store: Path,
                       repo: str = PARENT_REPO) -> str:
    """Open the issue and record what it proposes (the approvals job trusts only this record)."""
    gh(["label", "create", CONTENT_LABEL, "-R", repo, "--color", "1D76DB",
        "--description", "Proposed curriculum additions from research", "--force"])
    url = gh(["issue", "create", "-R", repo, "--title", title, "--body", body,
              "--label", CONTENT_LABEL]).strip()
    number = issue_number(url)
    if number is not None:
        record(store, number, meta)
    return url


# --- planning the work ----------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    label: str
    argv: list[str] | None = None
    fallback: list[str] | None = None  # tried when argv fails (e.g. squash when merges are off)
    python: Callable[[], None] | None = None
    check: Callable[[Run], str | None] | None = None  # returns why to stop, or None


@dataclass(frozen=True)
class Context:
    runner_dir: Path
    workspace: Path
    manifest_for: dict[str, Path]
    learning_repo_for: dict[tuple[str, str], str] = field(default_factory=dict)
    orgs: dict[str, str] = field(default_factory=dict)  # domain -> GitHub org
    roles: dict[str, set[str]] = field(default_factory=dict)  # domain -> role ids
    store: Path | None = None  # the runner's proposal records
    runner_login: str | None = None  # only issues this account opened are acted on

    @property
    def aicg(self) -> str:
        return str(self.runner_dir / ".venv" / "bin" / "aicg")

    def state_dir(self, domain: str) -> Path:
        return self.runner_dir / ".aicg" / ("org" if domain == "ai-infra" else domain)


def _detached(ctx: Context, unit: str, lock_domain: str, job: list[str]) -> list[str]:
    """A systemd-run command that waits for the domain's lock, then runs ``job``.

    The job wrapper exits (rather than waits) when its lock is held, and the
    approvals job itself holds the ai-infra lock, so a plain launch of an
    ai-infra job from here would quit immediately.
    """
    lock = ctx.state_dir(lock_domain) / "aicg-org.lock"
    script = f"while [ -d {shlex.quote(str(lock))} ]; do sleep 60; done; {shlex.join(job)}"
    return ["systemd-run", "--user", f"--unit={unit}", f"--working-directory={ctx.runner_dir}",
            "bash", "-c", script]


def _job(ctx: Context, name: str, *args: str, domain: str | None = None) -> list[str]:
    cmd = ["bash", str(ctx.runner_dir / "scripts" / "aicg-org-job.sh"), name, *args,
           "--workspace", str(ctx.workspace)]
    if domain is not None:
        cmd += ["--manifest", str(ctx.manifest_for[domain]), "--state-dir", str(ctx.state_dir(domain))]
    return cmd


def _author(ctx: Context, domain: str, role_id: str) -> Action:
    unit = f"aicg-approved-{role_id}-{int(time.time())}"
    return Action("start authoring", _detached(ctx, unit, domain,
                                                _job(ctx, "generate-role", role_id, domain=domain)))


def _refresh_pages(ctx: Context) -> Action:
    """Re-render profiles, Module 00, and the sites so a new role shows up the same day."""
    unit = f"aicg-publish-{int(time.time())}"
    return Action("refresh pages and sites", _detached(ctx, unit, "ai-infra", _job(ctx, "publish")))


def _content_actions(meta: dict[str, Any], ctx: Context) -> list[Action]:
    domain, role_id, pr = meta["domain"], meta["role_id"], meta.get("pr_url", "")
    learning = ctx.learning_repo_for.get((domain, role_id))
    expected_branch = f"aicg/research/{meta['month']}/{role_id}"

    def is_research_pr(run: Run) -> str | None:
        rc, out = run(["gh", "pr", "view", pr, "--json", "headRefName,baseRefName,state"])
        try:
            info = json.loads(out) if rc == 0 else {}
        except ValueError:
            info = {}
        if info.get("headRefName") != expected_branch or info.get("baseRefName") != "main":
            return f"PR is not the research proposal branch {expected_branch} -> main"
        if info.get("state") not in {"OPEN", "MERGED"}:
            return f"PR is {info.get('state')}"
        return None

    actions = [Action("verify the proposal PR", check=is_research_pr),
               Action("merge the proposal PR", ["gh", "pr", "merge", pr, "--merge", "--delete-branch"],
                      fallback=["gh", "pr", "merge", pr, "--squash", "--delete-branch"])]
    if learning:
        actions.append(Action("pull the learning repo",
                              ["git", "-C", str(ctx.workspace / learning), "pull", "-q", "--ff-only"]))
        if isinstance(meta.get("delta"), dict):
            # Apply exactly what the issue showed, not whatever a later research
            # run left in the (gitignored) local file.
            target = ctx.workspace / learning / ".aicg" / "curriculum-plan-delta-filtered.json"
            actions.append(Action("stage the approved plan change",
                                  python=lambda: _write_json(target, meta["delta"])))
    actions.append(Action("apply to the curriculum plan", [
        ctx.aicg, "org", "promote-plan", "--workspace", str(ctx.workspace),
        "--manifest", str(ctx.manifest_for[domain]), "--role", role_id]))
    return actions + [_author(ctx, domain, role_id)]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def apply_role_config(runner_dir: Path, domain: str, meta: dict[str, Any]) -> None:
    """Add the new role's aliases to its domain config and a focus line to its profile."""
    from .domains import domain_config_path

    def edit(path: Path, change: Callable[[dict], None]) -> None:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        change(data)
        path.write_text(json.dumps(data, indent=2, ensure_ascii="\\u" in raw) + "\n", encoding="utf-8")

    def add_aliases(data: dict) -> None:
        for role in data["roles"]:
            if role["id"] == meta["role_id"]:
                role["aliases"] = [a for a in meta.get("aliases", []) if a != meta["title"]]

    def add_focus(data: dict) -> None:
        data["focus"][meta["role_id"]] = meta.get("focus") or meta["title"]
        # Bands that list roles explicitly: join the band holding the nearest-level role.
        explicit = [b for b in data.get("bands", []) if "roles" in b]
        if explicit and not any(meta["role_id"] in b["roles"] for b in explicit):
            levels = {r["id"]: r["level"] for r in json.loads(
                domain_config_path(domain, runner_dir).read_text(encoding="utf-8"))["roles"]}
            best = min(explicit, key=lambda b: min(
                (abs(levels.get(r, 999) - meta["level"]) for r in b["roles"]), default=999))
            best["roles"].append(meta["role_id"])

    edit(domain_config_path(domain, runner_dir), add_aliases)
    edit(runner_dir / "config" / "profiles" / f"{domain}.json", add_focus)


def _role_actions(meta: dict[str, Any], ctx: Context) -> list[Action]:
    domain, role_id = meta["domain"], meta["role_id"]
    runner = str(ctx.runner_dir)
    branch = f"aicg/approve-role-{role_id}"
    pr_body = (f"Adds **{meta['title']}** (L{meta['level']}) to `{domain}`, approved in "
               f"{meta.get('issue_url', 'the role-proposal issue')}.\n\n🤖 Generated by `aicg org approvals`")
    timers = (["bash", f"{runner}/scripts/install-schedules.sh"] if domain == "ai-infra" else
              ["bash", f"{runner}/scripts/install-domain-timers.sh", "--domain", domain,
               "--hour-offset", str(HOUR_OFFSETS.get(domain, 0))])
    return [
        Action("sync the runner", ["git", "-C", runner, "pull", "-q", "--ff-only"]),
        Action("create the role and its repos", [
            ctx.aicg, "org", "bootstrap-role", "--workspace", str(ctx.workspace), "--domain", domain,
            "--role", role_id, "--title", meta["title"], "--level", str(meta["level"]),
            "--description", meta.get("description") or meta["title"], "--create-remotes"]),
        Action("add aliases and focus line", python=lambda: apply_role_config(ctx.runner_dir, domain, meta)),
        Action("branch", ["git", "-C", runner, "checkout", "-q", "-B", branch]),
        Action("stage config", ["git", "-C", runner, "add", "config"]),
        Action("commit config", ["git", "-C", runner, "commit", "-q", "-m",
                                 f"feat: add {role_id} role (approved proposal)"]),
        Action("push", ["git", "-C", runner, "push", "-q", "-u", "origin", branch]),
        Action("open config PR", ["gh", "pr", "create", "--repo", GENERATOR_REPO, "--head", branch,
                                  "--title", f"feat: add {meta['title']} role", "--body", pr_body]),
        Action("merge config PR", ["gh", "pr", "merge", branch, "--repo", GENERATOR_REPO, "--merge",
                                   "--delete-branch"]),
        Action("back to main", ["git", "-C", runner, "checkout", "-q", "main"]),
        Action("pull main", ["git", "-C", runner, "pull", "-q", "--ff-only"]),
        Action("install timers", timers),
        _author(ctx, domain, role_id),
        _refresh_pages(ctx),
    ]


def actions_for(meta: dict[str, Any], ctx: Context) -> list[Action]:
    if meta.get("domain") not in ctx.manifest_for:
        return []
    if meta.get("kind") == "content" and meta.get("role_id"):
        return _content_actions(meta, ctx)
    if meta.get("kind") == "role" and meta.get("role_id") and meta.get("title"):
        return _role_actions(meta, ctx)
    return []


# --- running the loop -----------------------------------------------------------------


def default_run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr)[-1500:]


def _execute(action: Action, run: Run) -> tuple[bool, str]:
    if action.check is not None:
        reason = action.check(run)
        return reason is None, reason or ""
    if action.python is not None:
        try:
            action.python()
            return True, ""
        except Exception as exc:  # noqa: BLE001 - surfaced in the issue comment
            return False, f"{type(exc).__name__}: {exc}"
    rc, out = run(action.argv or [])
    if rc != 0 and action.fallback:
        rc, out = run(action.fallback)
    return rc == 0, out


def _approved_issues(gh: Gh, repo: str) -> list[dict[str, Any]]:
    raw = gh(["issue", "list", "-R", repo, "--label", APPROVED_LABEL, "--state", "open",
              "--limit", "100", "--json", "number,title,author,labels"])
    issues = json.loads(raw or "[]")
    kinds = {CONTENT_LABEL, ROLE_LABEL}
    out = []
    for issue in issues:
        names = {lb.get("name") for lb in issue.get("labels", [])}
        if names & kinds and APPLIED_LABEL not in names:
            out.append(issue)
    return out


def _untrusted_reason(issue: dict[str, Any], ctx: Context) -> str | None:
    if ctx.store is None:
        return "no proposal store configured"
    author = (issue.get("author") or {}).get("login", "")
    if not ctx.runner_login or author.lower() != ctx.runner_login.lower():
        return f"issue was opened by {author or 'unknown'}, not the runner"
    return None


def process_approvals(ctx: Context, *, gh: Gh, run: Run = default_run, apply: bool = False,
                      repo: str = PARENT_REPO) -> list[dict[str, Any]]:
    """Act on every open, approved, not-yet-applied proposal issue."""
    results: list[dict[str, Any]] = []
    for issue in _approved_issues(gh, repo):
        number = str(issue["number"])
        reason = _untrusted_reason(issue, ctx)
        meta = None if reason else load(ctx.store, issue["number"])  # type: ignore[arg-type]
        reason = reason or validate(meta, orgs=ctx.orgs, roles=ctx.roles,
                                    learning_repo_for=ctx.learning_repo_for)
        actions = [] if reason else actions_for(meta, ctx)  # type: ignore[arg-type]
        if not actions:
            reason = reason or "nothing to do for this proposal"
            results.append({"issue": number, "status": "refused", "reason": reason})
            if apply:
                gh(["issue", "comment", number, "-R", repo, "--body",
                    f"The runner refused to act on this: {reason}. Only proposals the runner "
                    "opened itself, with a matching record, are applied automatically."])
                gh(["issue", "edit", number, "-R", repo, "--remove-label", APPROVED_LABEL,
                    "--add-label", FAILED_LABEL])
            continue
        if not apply:
            results.append({"issue": number, "status": "would_apply",
                            "steps": [a.label for a in actions]})
            continue
        done: list[str] = []
        for action in actions:
            ok, out = _execute(action, run)
            if not ok:
                gh(["issue", "comment", number, "-R", repo, "--body",
                    f"Approval failed at **{action.label}**. Completed before it: "
                    f"{', '.join(done) or 'nothing'}.\n\n```\n{out.strip()[-1200:]}\n```\n\n"
                    f"Fix the cause, then re-add the `{APPROVED_LABEL}` label to retry."])
                gh(["issue", "edit", number, "-R", repo, "--remove-label", APPROVED_LABEL,
                    "--add-label", FAILED_LABEL])
                results.append({"issue": number, "status": "failed", "step": action.label})
                break
            done.append(action.label)
        else:
            gh(["issue", "comment", number, "-R", repo, "--body",
                "Approved and applied: " + ", ".join(done) + ". Content is being written now; "
                "progress shows up in the role's COMPLETENESS_MATRIX.md."])
            gh(["issue", "edit", number, "-R", repo, "--add-label", APPLIED_LABEL])
            gh(["issue", "close", number, "-R", repo, "--reason", "completed"])
            results.append({"issue": number, "status": "applied", "steps": done})
    return results


def close_declined(gh: Gh, ctx: Context, *, run: Run = default_run, apply: bool = False,
                   repo: str = PARENT_REPO) -> list[str]:
    """Close the proposal PR of every content issue that was closed without being applied."""
    raw = gh(["issue", "list", "-R", repo, "--label", CONTENT_LABEL, "--state", "closed",
              "--limit", "100", "--json", "number,labels"])
    closed: list[str] = []
    for issue in json.loads(raw or "[]"):
        if APPLIED_LABEL in {lb.get("name") for lb in issue.get("labels", [])}:
            continue
        meta = load(ctx.store, issue["number"]) if ctx.store else None
        if validate(meta, orgs=ctx.orgs, roles=ctx.roles, learning_repo_for=ctx.learning_repo_for):
            continue
        pr = meta["pr_url"]  # type: ignore[index]
        rc, state = run(["gh", "pr", "view", pr, "--json", "state", "--jq", ".state"])
        if rc == 0 and state.strip() == "OPEN":
            if apply:
                run(["gh", "pr", "close", pr, "--comment",
                     f"Declined in {repo}#{issue['number']}.", "--delete-branch"])
            closed.append(pr)
    return closed


def describe(actions: list[Action]) -> str:
    return "\n".join(f"- {a.label}: {shlex.join(a.argv) if a.argv else '(python)'}" for a in actions)


def new_pending(gh: Gh, seen: set[int], repo: str = PARENT_REPO) -> list[dict[str, Any]]:
    """Open proposal issues awaiting a decision that haven't been announced yet."""
    fresh = []
    for label in (CONTENT_LABEL, ROLE_LABEL):
        raw = gh(["issue", "list", "-R", repo, "--label", label, "--state", "open",
                  "--limit", "100", "--json", "number,title,url,labels"])
        for issue in json.loads(raw or "[]"):
            names = {lb.get("name") for lb in issue.get("labels", [])}
            if issue["number"] not in seen and not names & {APPROVED_LABEL, APPLIED_LABEL}:
                fresh.append(issue)
    return fresh
