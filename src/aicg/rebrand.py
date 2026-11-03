"""One-shot org-wide maintainership attribution.

Adds a small ``Maintained by ...`` footer to every repo's README.md
(and to ``.github/profile/README.md`` and ``.github/README.md`` when
they exist). The phrasing + URL come from the manifest's
``maintained_by`` block.

Idempotent: re-running detects the footer marker and skips. Each repo
gets its own branch + PR so the attribution can be reviewed per repo
before merging — no force-pushes, no rewriting of unrelated content.

This is deliberately a META-layer change (project attribution). It
does NOT change the source-policy rules for curriculum content; those
still apply.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .org_config import OrgManifest, state_dir_for_manifest
from .state import utc_now, write_json

REBRAND_REPORT = "rebrand-report.json"
DEFAULT_PROFILE_PATHS = (
    "profile/README.md",
    "README.md",
)


def rebrand_run(
    manifest: OrgManifest,
    workspace: Path,
    state_dir: Path | None = None,
    apply: bool = False,
    repos: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the maintainer footer across the org.

    ``apply=False`` is a dry-run that reports what would change.
    """
    state_root = state_dir_for_manifest(manifest, state_dir)
    state_root.mkdir(parents=True, exist_ok=True)

    mb = manifest.maintained_by or {}
    if not mb.get("name"):
        return {
            "status": "skipped",
            "reason": "manifest.maintained_by.name is not set",
        }

    target_repos = list(repos) if repos else list(manifest.repo_names)
    outcomes: list[dict[str, Any]] = []
    for repo in target_repos:
        repo_path = manifest.repo_path(workspace, repo)
        if not repo_path.exists():
            outcomes.append({"repo": repo, "status": "skipped", "reason": "missing"})
            continue
        outcomes.append(_rebrand_repo(repo_path, repo, mb, apply=apply))

    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "operation": "rebrand",
        "status": "applied" if apply else "dry_run",
        "maintained_by": dict(mb),
        "repos": outcomes,
    }
    write_json(state_root / REBRAND_REPORT, report)
    return report


def _rebrand_repo(
    repo_path: Path, repo: str, maintained_by: dict[str, Any], apply: bool
) -> dict[str, Any]:
    """Edit READMEs in this repo and (if apply) push a PR."""
    if apply:
        # Skip when the working tree is dirty (another job mid-flight).
        # Filter out .aicg/ paths explicitly — the runner writes its own
        # state files there (audit-report.json, work-plan.json, etc.) and
        # in some repos those paths are tracked, so --untracked-files=no
        # is not enough to exclude them.
        dirty = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain",
             "--untracked-files=no"],
            capture_output=True, text=True, check=False,
        )
        non_aicg = [
            line for line in dirty.stdout.splitlines()
            if line.strip() and not line[3:].startswith(".aicg/")
        ]
        if non_aicg:
            return {
                "repo": repo,
                "status": "skipped_dirty_tree",
                "changed_files": [],
                "reason": "tracked-file modifications present",
            }

    marker = str(maintained_by.get("footer_marker") or "<!-- aicg:maintained-by -->")
    phrasing = str(
        maintained_by.get("phrasing")
        or f"Maintained by {maintained_by.get('name')}"
    )

    files_targeted = [repo_path / "README.md"]
    # .github org profile carries an extra profile/README.md
    if repo == ".github":
        files_targeted = [
            repo_path / path for path in DEFAULT_PROFILE_PATHS
        ]

    changed_files: list[str] = []
    for path in files_targeted:
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
        # Idempotent on our marker (so re-runs are no-ops) AND defensive
        # against pre-existing 'Maintained by' lines in the README from
        # before we ever ran. If the doc already attributes maintainership,
        # skip — operators can adopt our marker by hand if they want the
        # auto-managed version.
        if marker in body:
            continue
        if _has_existing_maintainer_line(body, maintained_by):
            continue
        new_body = _append_maintainer_footer(body, marker, phrasing)
        if not apply:
            changed_files.append(str(path.relative_to(repo_path)))
            continue
        path.write_text(new_body, encoding="utf-8")
        changed_files.append(str(path.relative_to(repo_path)))

    if not changed_files:
        return {
            "repo": repo,
            "status": "already_branded",
            "changed_files": [],
        }
    if not apply:
        return {
            "repo": repo,
            "status": "would_change",
            "changed_files": changed_files,
        }

    pr_outcome = _open_rebrand_pr(repo_path, repo, changed_files, maintained_by)
    return {
        "repo": repo,
        "status": pr_outcome.get("status", "?"),
        "changed_files": changed_files,
        "pr": pr_outcome,
    }


def _has_existing_maintainer_line(
    body: str, maintained_by: dict[str, Any]
) -> bool:
    """Detect any 'Maintained by ...' line in the doc that ISN'T ours.

    Catches variants like ``*Maintained by ...*`` (italic),
    ``**Maintained by:** ...`` (bold-colon), or embedded inside a
    pipe-separated metadata line like
    ``*Last updated ... | Maintained by ...*``. Our own footer is
    detected via the marker (checked separately) so the runner is
    still idempotent.
    """
    import re as _re

    name = str(maintained_by.get("name") or "").lower()
    for line in body.splitlines():
        if "maintained by" not in line.lower():
            continue
        if name and name in line.lower():
            # Already attributes to us; don't bother adding a footer too.
            return True
        # Any other 'Maintained by' line counts — drop the operator a
        # hint by leaving it alone.
        if _re.search(r"maintained by", line, _re.IGNORECASE):
            return True
    return False


def _append_maintainer_footer(
    body: str, marker: str, phrasing: str
) -> str:
    """Append a small footer block. Preserves existing trailing newline."""
    footer = (
        f"\n\n---\n\n"
        f"{marker}\n"
        f"{phrasing}\n"
    )
    if not body.endswith("\n"):
        body += "\n"
    return body + footer


def _open_rebrand_pr(
    repo_path: Path, repo: str, changed_files: list[str], maintained_by: dict[str, Any]
) -> dict[str, Any]:
    """Create a branch, commit the changes, push, open PR."""
    today = utc_now()[:10]
    # git refuses branch components starting with '.'; replace dots so
    # '.github' becomes 'dot-github' in the branch path while the
    # working repo stays untouched.
    safe_repo = re.sub(r"^\.+", "dot-", repo).replace("/", "-")
    branch = f"aicg/{today}/{safe_repo}/maintainer-footer"
    title = (
        f"docs: add `Maintained by {maintained_by.get('name')}` footer"
    )
    body = (
        "## Subtle rebranding\n\n"
        f"This PR adds a small `Maintained by {maintained_by.get('name')}` "
        f"footer to the README(s) of this repo.\n\n"
        "- Attribution only — does not change curriculum content.\n"
        "- Marker comment makes the footer idempotent for future runs.\n"
        "- Source-policy rules for curriculum content remain unchanged.\n"
    )

    steps: list[tuple[str, list[str]]] = [
        ("fetch", ["git", "-C", str(repo_path), "fetch", "origin", "main"]),
        ("checkout_main", ["git", "-C", str(repo_path), "checkout", "main"]),
        ("pull", ["git", "-C", str(repo_path), "pull", "--ff-only"]),
        ("branch", ["git", "-C", str(repo_path), "checkout", "-B", branch]),
        ("add", ["git", "-C", str(repo_path), "add", *changed_files]),
        ("commit", ["git", "-C", str(repo_path), "commit", "-m", title]),
        ("push", ["git", "-C", str(repo_path), "push", "-u", "origin", branch]),
        (
            "pr_create",
            [
                "gh", "pr", "create",
                "--base", "main",
                "--title", title,
                "--body", body,
            ],
        ),
    ]

    outcomes: list[dict[str, Any]] = []
    pr_url = None
    for label, cmd in steps:
        completed = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True, text=True, check=False,
        )
        outcomes.append(
            {
                "step": label,
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-400:],
                "stderr_tail": completed.stderr[-400:],
            }
        )
        if label == "pr_create" and completed.returncode == 0:
            pr_url = completed.stdout.strip()
        if completed.returncode != 0 and label not in {"commit"}:
            # commit can return non-zero if nothing changed; everything
            # else is a hard stop. Reset to main so the repo is clean.
            # `git checkout main` alone preserves uncommitted README
            # modifications, which then break every subsequent rebrand
            # attempt (`pull --ff-only` refuses to clobber the dirty
            # file). Restore the files we touched from HEAD first so
            # the working tree is genuinely clean. We restore only
            # `changed_files` to avoid clobbering runner state in
            # other paths (e.g. .aicg/*.json).
            subprocess.run(
                ["git", "-C", str(repo_path), "checkout", "HEAD", "--",
                 *changed_files],
                capture_output=True, text=True, check=False,
            )
            subprocess.run(
                ["git", "-C", str(repo_path), "checkout", "main"],
                capture_output=True, text=True, check=False,
            )
            return {
                "status": "pr_failed",
                "failed_step": label,
                "branch": branch,
                "steps": outcomes,
            }
    # Always return to main when done so subsequent runs start clean.
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", "main"],
        capture_output=True, text=True, check=False,
    )
    return {
        "status": "pr_opened",
        "branch": branch,
        "pr_url": pr_url,
        "steps": outcomes,
    }
