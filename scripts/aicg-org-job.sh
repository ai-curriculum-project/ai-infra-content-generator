#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly RUNNER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKSPACE="${AICG_WORKSPACE:-$(cd "$RUNNER_DIR/.." && pwd)}"
MANIFEST="${AICG_MANIFEST:-$RUNNER_DIR/config/aicg-org.yaml}"
STATE_DIR="${AICG_STATE_DIR:-$RUNNER_DIR/.aicg/org}"
LOG_DIR="${AICG_LOG_DIR:-$RUNNER_DIR/.aicg/logs}"
AICG_BIN="${AICG_BIN:-$RUNNER_DIR/.venv/bin/aicg}"

# Claude CLI auth. The systemd timers run with a bare environment (no
# CLAUDE_CODE_OAUTH_TOKEN), so timer-driven content runs saw "Not logged in"
# even with a valid token — only hand-sourced manual runs worked. Source +
# EXPORT the token here (set -a) so every run, timer or manual, is authed.
if [[ -f "$HOME/.config/aicg/claude-auth.env" ]]; then
  set -a; . "$HOME/.config/aicg/claude-auth.env"; set +a
fi

# ntfy observability (optional). Topic comes from an env file so it stays out
# of git; if unset, every notify is a no-op.
[[ -f "$HOME/.config/aicg/ntfy.env" ]] && . "$HOME/.config/aicg/ntfy.env"
NTFY_TOPIC="${AICG_NTFY_TOPIC:-}"
NTFY_BASE="${AICG_NTFY_URL:-https://ntfy.sh}"

# notify_ntfy <title> <tags> <priority> <message>  — best-effort; never fails the job.
notify_ntfy() {
  [[ -n "$NTFY_TOPIC" ]] || return 0
  curl -fsS -m 10 \
    -H "Title: $1" -H "Tags: $2" -H "Priority: $3" \
    -d "$4" "$NTFY_BASE/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

# If an agent output shows the CLI token lapsed, fire a distinct, urgent 🔑
# alert (debounced to once/day so a down token doesn't spam every run) and
# return 1 so callers can skip the rest. This is the recurring failure mode:
# the subscription token gets invalidated and every content run silently caps.
notify_reauth_if_needed() {
  local out="$1"
  [[ -f "$out" ]] || return 0
  grep -qiE "Not logged in|Please run /login" "$out" 2>/dev/null || return 0
  local marker="${AICG_LOG_DIR:-$RUNNER_DIR/.aicg/logs}/.reauth-alerted-$(date +%F)"
  if [[ ! -f "$marker" ]]; then
    : > "$marker" 2>/dev/null || true
    notify_ntfy "🔑 AICG: re-auth needed on $(hostname 2>/dev/null)" "key,warning" "urgent" \
      "The Claude CLI token is invalid/expired — content is stalled. Fix: run 'claude setup-token' and replace CLAUDE_CODE_OAUTH_TOKEN in ~/.config/aicg/claude-auth.env."
  fi
  return 1
}

usage() {
  cat <<EOF
Usage: $SCRIPT_NAME JOB [OPTIONS]

Jobs:
  sync
  monthly-release
  monthly-research      (quarterly: writes proposals, opens PRs, NO auto-merge)
  monthly-review        (quarterly: LLM freshness review of existing content)
  promote-plan          (on-demand: applies approved research proposals after PR merge)
  research-role ROLE    (nightly per-role research; replaces monthly-research)
  review-role ROLE      (nightly per-role freshness review; replaces monthly-review)
  weekly-audit
  daily-remediate
  daily-issues
  daily-steward
  steward               (per domain, twice daily: merge open PRs whose CI passed; no Claude calls)
  daily-discussions
  backfill-assessments  (daily per domain: author quizzes/labs for ONE module still on placeholders)
  resolve-markers       (daily: resolve a batch of needs-research markers in ONE module)

Options:
  --workspace PATH   Curriculum workspace. Default: parent of runner repo.
  --manifest PATH    Org manifest. Default: config/aicg-org.yaml.
  --state-dir PATH   Org state directory. Default: .aicg/org.
  --aicg-bin PATH    aicg executable. Default: .venv/bin/aicg.
  -h, --help         Show this help.
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

parse_args() {
  if [[ $# -eq 0 ]]; then
    usage
    exit 2
  fi

  JOB="$1"
  shift

  # research-role / review-role take a positional ROLE argument. Capture
  # it before the option loop so the rest of parsing stays uniform.
  ROLE=""
  case "$JOB" in
    research-role|review-role|generate-role|seed-role)
      if [[ $# -eq 0 || "$1" == --* ]]; then
        die "$JOB requires a ROLE argument (e.g. junior-engineer)"
      fi
      ROLE="$1"
      shift
      ;;
  esac

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace)
        WORKSPACE="${2:-}"
        shift 2
        ;;
      --manifest)
        MANIFEST="${2:-}"
        shift 2
        ;;
      --state-dir)
        STATE_DIR="${2:-}"
        shift 2
        ;;
      --aicg-bin)
        AICG_BIN="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

run_aicg_org() {
  "$AICG_BIN" org "$@" --workspace "$WORKSPACE" --manifest "$MANIFEST" --state-dir "$STATE_DIR"
}

main() {
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local log_slug="$JOB"
  if [[ -n "$ROLE" ]]; then
    log_slug="${JOB}-${ROLE}"
  fi
  local log_file="$LOG_DIR/${log_slug}-$(date '+%Y%m%d').log"
  exec >>"$log_file" 2>&1

  [[ -x "$AICG_BIN" ]] || die "aicg executable not found or not executable: $AICG_BIN"
  [[ -f "$MANIFEST" ]] || die "manifest not found: $MANIFEST"

  # Lock + OWN_LOCK are intentionally global so the EXIT trap, which fires
  # after main() returns, can still see them under `set -u`.
  LOCK_DIR="$STATE_DIR/aicg-org.lock"
  # A nested invocation (fill-next -> generate-role -> seed-role) inherits
  # AICG_LOCK_HELD from the parent that already owns the org-job lock. It must
  # NOT re-acquire (that self-deadlocked the fill timers) or remove the parent's
  # lock on exit.
  OWN_LOCK=1
  [[ -n "${AICG_LOCK_HELD:-}" ]] && OWN_LOCK=""
  local lock_ttl="${AICG_LOCK_TTL:-10800}"  # 3h grace before a held lock is suspect
  if [[ -n "$OWN_LOCK" ]]; then
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      # Stale-lock recovery (C-M2): break the lock only if its owner PID is dead
      # AND it has been held past the TTL. A slow-but-live holder is never broken.
      local broke=0
      if [[ -f "$LOCK_DIR/owner" ]]; then
        local lpid lts age
        lpid="$(sed -n '1p' "$LOCK_DIR/owner" 2>/dev/null || true)"
        lts="$(sed -n '2p' "$LOCK_DIR/owner" 2>/dev/null || true)"
        if [[ "$lpid" =~ ^[0-9]+$ && "$lts" =~ ^[0-9]+$ ]]; then
          age=$(( $(date +%s) - lts ))
          if (( age > lock_ttl )) && ! kill -0 "$lpid" 2>/dev/null; then
            log "Breaking stale lock: pid $lpid dead, held ${age}s > ${lock_ttl}s."
            rm -rf "$LOCK_DIR" 2>/dev/null || true
            mkdir "$LOCK_DIR" 2>/dev/null && broke=1
          fi
        fi
      fi
      if (( broke == 0 )); then
        log "Another AICG org job is running; exiting."
        exit 0
      fi
    fi
    printf '%s\n%s\n' "$$" "$(date +%s)" > "$LOCK_DIR/owner" 2>/dev/null || true
  fi
  # On exit: drop the lock IF we own it, and (on a hard failure) push an ntfy
  # alert so a silent crash is visible. A clean exit or the "already running"
  # path (rc 0) stays quiet.
  _on_exit() {
    local rc=$?
    [[ -n "$OWN_LOCK" ]] && rm -rf "$LOCK_DIR" 2>/dev/null
    if [[ "$rc" -ne 0 ]]; then
      notify_ntfy "🔴 AICG job failed: $JOB${ROLE:+ ($ROLE)}" "rotating_light" "high" \
        "domain=$(basename "$MANIFEST" .yaml) job=$JOB exit=$rc host=$(hostname 2>/dev/null)"
    fi
    return "$rc"
  }
  trap _on_exit EXIT

  log "Starting job: $JOB"
  case "$JOB" in
    sync)
      run_aicg_org sync
      ;;
    monthly-release)
      run_aicg_org sync
      run_aicg_org release --apply
      ;;
    monthly-research)
      # Quarterly research. The runner does NOT scaffold new modules
      # autonomously — `research --apply` opens proposal PRs for human
      # review. After a human merges a proposal PR, run
      # `aicg org promote-plan --role <role>` (or the next
      # weekly-audit) to apply the approved delta and scaffold
      # skeletons.
      run_aicg_org research --apply
      ;;
    research-role)
      # Per-role nightly research. One role per night at midnight, ordered
      # lowest-level-first across the month, opens a proposal PR if the
      # delta passes caps. Replaces the org-wide monthly-research job —
      # spreads token usage across 13 nights and bounds blast-radius
      # of any single failure to one role.
      [[ -n "$ROLE" ]] || die "research-role requires ROLE"
      # Pull every repo to current main first — research reads the role's
      # curriculum-plan.json and branches from local main, so a stale clone
      # would analyze old content and base the proposal PR on an old commit.
      run_aicg_org sync
      run_aicg_org research --apply --role "$ROLE"
      # Research refreshes JOB_REQUIREMENTS.md and may write a
      # RESEARCH_PROPOSAL_<month>.md without opening a PR (no delta, or nothing
      # passed the caps). Nothing committed those, so the learning repo stayed
      # dirty and every later sync/release skipped it (8 ai-infra repos stuck
      # Aug-Nov 2026). Commit exactly those files on main.
      RR_LREPO=$(python3 -c "
import json
d=json.load(open('$MANIFEST'))
print(next((r['learning_repo'] for r in d['roles'] if r['id']=='$ROLE'),''))" 2>/dev/null || true)
      if [[ -n "$RR_LREPO" && -d "$WORKSPACE/$RR_LREPO/.git" ]]; then
        ( cd "$WORKSPACE/$RR_LREPO"
          RR_BRANCH=$(git branch --show-current)
          if [[ "$RR_BRANCH" != "main" ]]; then
            log "research-role: $RR_LREPO is on '$RR_BRANCH', not main; not committing research files"
            exit 0
          fi
          # Runner state must never make the repo look dirty: older repos
          # don't ignore .aicg/, so add the rule (and commit it) if missing.
          RR_FILES=(JOB_REQUIREMENTS.md)
          if [[ -e .aicg ]] && ! grep -qE '^\.aicg/?$' .gitignore 2>/dev/null; then
            echo ".aicg/" >>.gitignore
            RR_FILES+=(.gitignore)
          fi
          shopt -s nullglob
          RR_FILES+=(RESEARCH_PROPOSAL_*.md)
          RR_EXISTING=()
          for f in "${RR_FILES[@]}"; do [[ -f "$f" ]] && RR_EXISTING+=("$f"); done
          [[ ${#RR_EXISTING[@]} -gt 0 ]] && git add -- "${RR_EXISTING[@]}"
          if ! git diff --cached --quiet 2>/dev/null; then
            git -c user.email="aicg@veriswarm.ai" -c user.name="AICG Runner" \
              commit -q -m "research: refresh job requirements for $ROLE (aicg research-role)" \
              && git push -q origin HEAD \
              && log "research-role: committed research files for $ROLE"
          fi ) || log "research-role: commit/push of research files failed for $ROLE"
      fi
      ;;
    seed-role)
      # One-time plan SEEDING for a fresh role. Curriculum-plan changes are
      # human-gated by design (research opens proposal PRs; the runner never
      # auto-merges curriculum-plan.json), so a brand-new repo has no plan for
      # generate-role to author from. This bypasses that gate for the INITIAL
      # seed only: (re)write the role's bootstrap prompt, run the content agent
      # to research + author curriculum-plan.json + JOB_REQUIREMENTS.md into the
      # learning repo, then commit + push just those plan files. Idempotent:
      # skips if a non-empty plan already exists.
      [[ -n "$ROLE" ]] || die "seed-role requires ROLE"
      run_aicg_org sync
      LREPO=$(PYTHONPATH="$RUNNER_DIR/src" python3 -c "
import json,sys
d=json.load(open('$MANIFEST'))
print(next((r['learning_repo'] for r in d['roles'] if r['id']=='$ROLE'),''))" 2>/dev/null || true)
      [[ -n "$LREPO" ]] || die "no learning repo for role $ROLE in $MANIFEST"
      LPATH="$WORKSPACE/$LREPO"
      [[ -d "$LPATH" ]] || die "learning repo not cloned: $LPATH (run sync)"
      if [[ -s "$LPATH/.aicg/curriculum-plan.json" ]]; then
        log "plan already exists for $ROLE; skipping seed"
      else
        PROMPT=$(run_aicg_org bootstrap-prompt --role "$ROLE" | tail -1)
        case "$PROMPT" in /*) ;; *) PROMPT="$RUNNER_DIR/$PROMPT" ;; esac
        [[ -f "$PROMPT" ]] || die "bootstrap prompt not written: $PROMPT"
        # Absolute output dir: run-claude-content.sh cd's into --repo before
        # writing response.md, so a relative path would resolve under the repo.
        SEED_OUT="$STATE_DIR/seed/$ROLE"
        case "$SEED_OUT" in /*) ;; *) SEED_OUT="$RUNNER_DIR/$SEED_OUT" ;; esac
        mkdir -p "$SEED_OUT"
        log "seeding plan for $ROLE via content agent (prompt=$PROMPT)"
        "$RUNNER_DIR/scripts/run-claude-content.sh" \
          --prompt "$PROMPT" --repo "$WORKSPACE" --output-dir "$SEED_OUT" \
          --work-id "seed-$ROLE" || log "content agent returned non-zero for $ROLE"
        # Surface an expired/invalid token loudly (recurring failure mode).
        notify_reauth_if_needed "$SEED_OUT/response.md" || log "re-auth needed (token lapsed)"
        if [[ -s "$LPATH/.aicg/curriculum-plan.json" ]]; then
          # The plan lives at .aicg/ which is gitignored by design (per-repo
          # runner state, never pushed — generate-role reads it locally). So
          # success = the local plan exists. Only the PUBLIC artifacts the
          # agent may also write (JOB_REQUIREMENTS.md, supplemental/) get
          # committed; git add'ing the ignored plan would fail the whole add.
          log "seeded plan present for $ROLE ($(wc -c <"$LPATH/.aicg/curriculum-plan.json") bytes, local runner state)"
          ( cd "$LPATH"
            git add JOB_REQUIREMENTS.md supplemental 2>/dev/null || true
            if ! git diff --cached --quiet 2>/dev/null; then
              git -c user.email="aicg@veriswarm.ai" -c user.name="AICG Runner" \
                  commit -m "seed: job requirements from initial research" \
              && git push origin HEAD && echo "  pushed public seed artifacts"
            fi ) || log "seed: public-artifact commit skipped for $ROLE"
        else
          log "seed produced NO plan for $ROLE (see $SEED_OUT/response.md)"
        fi
      fi
      ;;
    generate-role)
      # Per-role nightly CONTENT AUTHORING — the autonomous fill step that
      # research/daily-remediate alone don't cover for a fresh repo. Reads the
      # role's seeded curriculum-plan.json, scaffolds any missing module/project
      # skeletons, then authors the learning content for those modules directly
      # (learning_content.generate_role_learning_content bypasses the
      # evidence-gated research-PR flow — it authors from the plan). Solutions
      # are filled by daily-remediate from audit-queued solution gaps afterward.
      # No-op-safe: execute-plan + generate-learning are idempotent per module.
      [[ -n "$ROLE" ]] || die "generate-role requires ROLE"
      run_aicg_org sync
      # Self-seed: generate-learning authors from curriculum-plan.json, which a
      # fresh role lacks (plan changes are human-gated by design). Seed it first
      # if missing — seed-role no-ops when a plan already exists, so this is the
      # autonomous full chain: seed (once) -> author. Heavy LLM steps; the
      # shared-subscription session cap naturally spreads them across the
      # staggered per-role schedule.
      GR_LREPO=$(PYTHONPATH="$RUNNER_DIR/src" python3 -c "
import json
d=json.load(open('$MANIFEST'))
print(next((r['learning_repo'] for r in d['roles'] if r['id']=='$ROLE'),''))" 2>/dev/null || true)
      GR_SREPO=$(python3 -c "
import json
d=json.load(open('$MANIFEST'))
print(next((r.get('solution_repo') or '' for r in d['roles'] if r['id']=='$ROLE'),''))" 2>/dev/null || true)
      if [[ -n "$GR_LREPO" && ! -s "$WORKSPACE/$GR_LREPO/.aicg/curriculum-plan.json" ]]; then
        log "no plan for $ROLE; seeding before generate"
        # generate-role holds the org-job lock here, so tell the nested
        # seed-role not to re-acquire it (else it bails and no plan is written).
        AICG_LOCK_HELD=1 bash "$0" seed-role "$ROLE" --workspace "$WORKSPACE" \
          --manifest "$MANIFEST" --state-dir "$STATE_DIR" || log "seed step failed/limited for $ROLE"
      fi
      run_aicg_org execute-plan --role "$ROLE" 2>&1 | tail -3 || true
      # Whole modules run 15-25 min; the 20-min default cut most off mid-wrap-up.
      AICG_AGENT_TIMEOUT="${AICG_AGENT_TIMEOUT:-1800}" run_aicg_org generate-learning --role "$ROLE" || true
      # Commit + push the authored content. generate-learning edits the working
      # tree in place and leaves committing to the caller — without this the
      # content never reaches GitHub (the "no commits" symptom) and the dirty
      # tree makes the next sync dirty_skip. execute-plan also scaffolds the
      # solutions repo, so commit that too (it used to sit uncommitted).
      for GR_REPO in "$GR_LREPO" "$GR_SREPO"; do
      [[ -n "$GR_REPO" ]] || continue
      if [[ "$GR_REPO" == "$GR_SREPO" ]]; then
        GR_MSG="content: scaffold $ROLE solution modules (aicg generate-role)"
      else
        GR_MSG="content: author $ROLE learning modules (aicg generate-role)"
      fi
      if [[ -d "$WORKSPACE/$GR_REPO/.git" ]]; then
        ( cd "$WORKSPACE/$GR_REPO"
          # Guarantee runner scratch is never committed: older repos don't
          # gitignore .aicg/, so add -A would otherwise stage local state.
          # Self-heal the ignore rule so add -A only stages curriculum files.
          if [[ -e .aicg ]] && ! grep -qE '^\.aicg/?$' .gitignore 2>/dev/null; then
            echo ".aicg/" >>.gitignore
          fi
          git add -A 2>/dev/null || true
          if ! git diff --cached --quiet 2>/dev/null; then
            git -c user.email="aicg@veriswarm.ai" -c user.name="AICG Runner" \
              commit -q -m "$GR_MSG" \
              && git push origin HEAD && echo "  pushed $GR_REPO for $ROLE"
          fi ) || log "content commit/push failed for $GR_REPO"
      fi
      done
      # Observability: report the outcome by observable state (modules present
      # or not) — no agent-output parsing. One push per role-fill, so the
      # daily fill timer yields ~1 notify/domain/day, not spam.
      GR_DOM=$(basename "$MANIFEST" .yaml)
      GR_MODS=0
      [[ -n "$GR_LREPO" ]] && GR_MODS=$(ls -d "$WORKSPACE/$GR_LREPO"/lessons/mod-* 2>/dev/null | wc -l | tr -d ' ')
      if [[ "${GR_MODS:-0}" -gt 0 ]]; then
        notify_ntfy "✅ $GR_DOM · $ROLE authored" "white_check_mark,books" "default" \
          "$GR_MODS module(s) now in $GR_LREPO"
      elif [[ -z "${AICG_FILL_QUIET:-}" ]]; then
        # Only push the "not filled" notice for direct/scheduled generate runs,
        # not the high-frequency off-peak fill attempts (which cap routinely).
        notify_ntfy "⏸️ $GR_DOM · $ROLE not filled" "hourglass" "default" \
          "generate-role produced no modules (likely the Claude usage cap; retries next cycle)"
      fi
      ;;
    review-role)
      # Per-role nightly freshness review. One role per night, days
      # 14-26, lowest-level-first. Reviews BOTH the learning and
      # solution repos for that role with a 50-artifact cap PER REPO.
      # Replaces the org-wide monthly-review job — same total review
      # cost (~1200 calls / month with the judge enabled) but spread
      # across 13 separate Claude-budget windows instead of one shot.
      # No-op when quality_judge.enabled is false (every artifact
      # gets marked 'skipped').
      [[ -n "$ROLE" ]] || die "review-role requires ROLE"
      # Pull every repo to current main first so freshness review judges
      # the latest shipped artifacts, not a stale local clone.
      run_aicg_org sync
      run_aicg_org review --role "$ROLE" --max-artifacts 50
      ;;
    promote-plan)
      run_aicg_org promote-plan
      local roles
      roles=$("$AICG_BIN" --workspace "$WORKSPACE" org list-roles \
        --manifest "$MANIFEST" --state-dir "$STATE_DIR" 2>/dev/null || true)
      for role in $roles; do
        log "execute-plan for role=$role"
        run_aicg_org execute-plan --role "$role" 2>&1 | tail -3 || true
      done
      run_aicg_org audit
      ;;
    weekly-audit)
      run_aicg_org sync
      # Freshness checks first so their work items are picked up by audit.
      run_aicg_org audit-links || true
      run_aicg_org audit-versions || true
      # Broader structural audits also feed into the work-queue via
      # run_org_audit, but a standalone run here writes their reports
      # and surfaces summary lines in the log.
      run_aicg_org audit-learning || true
      run_aicg_org audit-pairing || true
      run_aicg_org audit-curriculum || true
      run_aicg_org audit-profile || true
      run_aicg_org audit
      ;;
    monthly-review)
      run_aicg_org sync
      run_aicg_org review
      run_aicg_org audit
      ;;
    daily-remediate)
      run_aicg_org daily
      ;;
    fill-next)
      # Continuous initial-fill: pick the FIRST role in this domain whose
      # learning repo has no authored modules yet and run the self-seeding
      # generate-role for just that one. Meant to run DAILY (not day-of-month),
      # so a fresh fleet fills one role per domain per day — within the shared
      # session cap — instead of stalling between the day-1-8 per-role timers.
      # Self-terminating: once every role has modules, this is a no-op.
      run_aicg_org sync
      FILL_PICK=$(PYTHONPATH="$RUNNER_DIR/src" python3 -c "
import json, os, glob, sys
d=json.load(open('$MANIFEST'))
ws='$WORKSPACE'
for r in sorted(d['roles'], key=lambda x: x.get('level',0)):
    lr=os.path.join(ws, r['learning_repo'])
    if not glob.glob(os.path.join(lr,'lessons','mod-*')) and not glob.glob(os.path.join(lr,'modules','mod-*')):
        print(r['id']); break" 2>/dev/null || true)
      if [[ -z "$FILL_PICK" ]]; then
        log "fill-next: every role has modules; nothing to fill"
      else
        log "fill-next: filling next unfilled role -> $FILL_PICK"
        # AICG_LOCK_HELD: this fill-next already holds the org-job lock; tell the
        # nested generate-role (and its seed-role) not to re-acquire it (that
        # deadlocked the fill timers) — env propagates to grandchildren.
        # AICG_FILL_QUIET: fill runs several times/night, so a capped no-op is
        # expected — suppress the per-attempt ⏸️ push (✅ on success still fires).
        AICG_LOCK_HELD=1 AICG_FILL_QUIET=1 bash "$0" generate-role "$FILL_PICK" \
          --workspace "$WORKSPACE" --manifest "$MANIFEST" --state-dir "$STATE_DIR"
      fi
      ;;
    backfill-assessments)
      # Daily quiz/lab backfill: pick the lowest-level role in this domain with
      # an authored module whose quizzes/ or labs/ are still bootstrap
      # placeholders, and author ONE module's assessments from its chapters.
      # The agent may only touch that module's quizzes/ and labs/; anything else
      # is reverted inside aicg (see assessments.py). Only those dirs are staged.
      run_aicg_org sync
      BA_ROLE=$(run_aicg_org backfill-assessments --list 2>/dev/null \
        | sed -n 's/^\([a-z0-9-][a-z0-9-]*\): [0-9][0-9]* module(s)$/\1/p' | head -1)
      if [[ -z "$BA_ROLE" ]]; then
        log "backfill-assessments: no assessment gaps; nothing to do"
      else
        log "backfill-assessments: role -> $BA_ROLE"
        BA_LREPO=$(python3 -c "
import json
d=json.load(open('$MANIFEST'))
print(next((r['learning_repo'] for r in d['roles'] if r['id']=='$BA_ROLE'),''))" 2>/dev/null || true)
        # Fail closed: commit ONLY when aicg exited 0 and its report lists
        # exactly one filled module with a safe id (the agent itself works on an
        # isolated copy with no .git). Before pushing, verify the new commit is
        # the only one on head_before and touches only that module's
        # quizzes/ and labs/.
        if run_aicg_org backfill-assessments --role "$BA_ROLE" --max-modules 1 \
            && [[ -n "$BA_LREPO" && -d "$WORKSPACE/$BA_LREPO/.git" ]]; then
          BA_MOD=$(python3 -c "
import json, re
r=json.load(open('$WORKSPACE/$BA_LREPO/.aicg/assessment-backfill-report.json'))
f=r.get('modules_filled') or []
print(f[0] if len(f)==1 and re.fullmatch(r'mod-[a-z0-9][a-z0-9-]*', f[0]) else '')" 2>/dev/null || true)
          BA_HEAD=$(python3 -c "
import json, re
h=json.load(open('$WORKSPACE/$BA_LREPO/.aicg/assessment-backfill-report.json')).get('head_before','')
print(h if re.fullmatch(r'[0-9a-f]{40}', h or '') else '')" 2>/dev/null || true)
          if [[ -z "$BA_MOD" || -z "$BA_HEAD" ]]; then
            log "backfill-assessments: nothing authored for $BA_ROLE (capped, rejected, or unchanged)"
          else
            ( cd "$WORKSPACE/$BA_LREPO"
              git add -- "lessons/$BA_MOD/quizzes" "lessons/$BA_MOD/labs" 2>/dev/null || true
              if ! git diff --cached --quiet 2>/dev/null; then
                # Hooks off: the agent ran in this checkout, so never execute
                # anything it might have left in .git/hooks.
                git -c core.hooksPath=/dev/null -c core.fsmonitor=false \
                  -c user.email="aicg@veriswarm.ai" -c user.name="AICG Runner" \
                  commit -q -m "content: author $BA_MOD quizzes/labs (aicg backfill-assessments)" \
                  && [[ "$(git rev-parse HEAD~1)" == "$BA_HEAD" ]] \
                  && [[ "$(git rev-list --count "$BA_HEAD..HEAD")" == "1" ]] \
                  && [[ -z "$(git diff --name-only "$BA_HEAD" HEAD \
                         | grep -vE "^lessons/$BA_MOD/(quizzes|labs)/")" ]] \
                  && git -c core.hooksPath=/dev/null push origin HEAD \
                  && log "pushed assessments for $BA_ROLE: $BA_MOD" \
                  && notify_ntfy "📝 $(basename "$MANIFEST" .yaml) · $BA_ROLE assessments" \
                       "memo" "default" "Quizzes/labs authored for $BA_MOD"
              fi ) || log "assessment commit/push failed for $BA_ROLE"
          fi
        else
          log "backfill-assessments failed for $BA_ROLE; nothing committed"
        fi
      fi
      ;;
    resolve-markers)
      # Daily needs-research resolution: pick the role repo with the most
      # markers and have the research agent (web-enabled) cite, soften, or
      # leave one module's batch of markers. The agent works on an isolated
      # copy; marker_resolution.judge_attempt keeps only small, in-scope edits.
      run_aicg_org sync
      RM_REPO=$(run_aicg_org resolve-markers --list 2>/dev/null \
        | sed -n 's/^\([a-z0-9][a-z0-9-]*\): [0-9][0-9]* marker(s)$/\1/p' | head -1)
      if [[ -z "$RM_REPO" ]]; then
        log "resolve-markers: no markers; nothing to do"
      elif run_aicg_org resolve-markers --repo "$RM_REPO" --max-units 1 \
          && [[ -d "$WORKSPACE/$RM_REPO/.git" ]]; then
        # Fail closed: commit only the exact files the report lists, each a
        # module markdown path, on top of the recorded starting commit.
        RM_LIST=$(python3 -c "
import json, re
r=json.load(open('$WORKSPACE/$RM_REPO/.aicg/marker-resolution-report.json'))
ok=re.compile(r'(lessons|modules)/mod-[a-z0-9][a-z0-9-]*/[A-Za-z0-9._/-]+\.md')
files=r.get('files_changed') or []
head=r.get('head_before') or ''
if files and all(ok.fullmatch(f) for f in files) and re.fullmatch(r'[0-9a-f]{40}', head):
    print(head); print(r['markers_resolved']); print('\n'.join(files))" 2>/dev/null || true)
        RM_HEAD=$(printf '%s\n' "$RM_LIST" | sed -n 1p)
        RM_N=$(printf '%s\n' "$RM_LIST" | sed -n 2p)
        if [[ -z "$RM_HEAD" ]]; then
          log "resolve-markers: nothing accepted for $RM_REPO (rejected, unchanged, or capped)"
        else
          ( cd "$WORKSPACE/$RM_REPO"
            mapfile -t RM_FILES < <(printf '%s\n' "$RM_LIST" | sed -n '3,$p')
            git add -- "${RM_FILES[@]}"
            if ! git diff --cached --quiet 2>/dev/null; then
              RM_OUTSIDE=$(git diff --cached --name-only | grep -vxF -f <(printf '%s\n' "${RM_FILES[@]}") || true)
              [[ -z "$RM_OUTSIDE" ]] || { log "resolve-markers: unexpected staged paths; aborting"; exit 1; }
              git -c core.hooksPath=/dev/null -c core.fsmonitor=false \
                -c user.email="aicg@veriswarm.ai" -c user.name="AICG Runner" \
                commit -q -m "content: resolve $RM_N needs-research marker(s) (aicg resolve-markers)" \
                && [[ "$(git rev-parse HEAD~1)" == "$RM_HEAD" ]] \
                && git -c core.hooksPath=/dev/null push -q origin HEAD \
                && log "resolve-markers: pushed $RM_N resolved marker(s) in $RM_REPO" \
                && notify_ntfy "🔎 $(basename "$MANIFEST" .yaml) · $RM_REPO" \
                     "mag" "default" "Resolved $RM_N needs-research marker(s)"
            fi ) || log "resolve-markers: commit/push failed for $RM_REPO"
        fi
      else
        log "resolve-markers failed for $RM_REPO; nothing committed"
      fi
      ;;
    daily-pipeline-tick)
      # Observe-mode tick: run every P2-P5 phase's decision on real data and
      # report what it WOULD do — writes nothing (the design's staged first
      # form). Promote a phase to act-mode by flipping its pipeline.phases flag.
      run_aicg_org sync
      run_aicg_org pipeline-tick
      ;;
    daily-issues)
      run_aicg_org issues --apply
      ;;
    daily-steward)
      run_aicg_org steward --apply
      # Also sweep Dependabot PRs each morning: queue auto-merge,
      # post @dependabot rebase on stale ones, escalate after 3 rebase
      # loops without progress.
      run_aicg_org dependabot --apply || true
      # Re-attempt the maintainer rebrand for any repos that were
      # skipped (dirty tree etc.) on a previous run. Idempotent —
      # already-branded repos are no-ops.
      run_aicg_org rebrand --apply || true
      ;;
    steward)
      # Sibling domains: daily-remediate merges each PR it opens right away,
      # so a PR whose CI was still running is left open. Nothing retried it
      # (daily-steward covers ai-infra only), and PRs sat for days.
      run_aicg_org steward --apply
      ;;
    daily-discussions)
      run_aicg_org discussions
      ;;
    completeness)
      # Weekly: recompute COMPLETENESS_MATRIX.md in every role repo, every
      # org's .github, and the parent org's .github, then commit and push the
      # ones whose numbers changed (date-only changes aren't written). Covers
      # all domains; ignores --manifest. Runs on the runner because the
      # curriculum plans (the denominator) live here.
      PARENT_DIR="$WORKSPACE/ai-curriculum-project-dotgithub"
      if [[ ! -d "$PARENT_DIR/.git" ]]; then
        git clone -q https://github.com/ai-curriculum-project/.github.git "$PARENT_DIR" \
          || die "could not clone the parent org's .github repo"
      fi
      git -C "$PARENT_DIR" pull -q --ff-only || log "parent .github pull failed; continuing"
      # list-domains prints a table; the domain names are the ✓ rows' first field.
      for dom in $("$AICG_BIN" org list-domains 2>/dev/null | awk '/✓/ {print $1}'); do
        "$AICG_BIN" org sync --workspace "$WORKSPACE" --domain "$dom" >/dev/null 2>&1 \
          || log "sync failed for $dom; matrices may use stale content"
      done
      "$AICG_BIN" org completeness --workspace "$WORKSPACE" --all-domains --write \
        --family "$PARENT_DIR/COMPLETENESS_MATRIX.md" | grep -v "^  L" || true
      CHANGED=0
      for repo_dir in "$WORKSPACE"/*/; do
        repo_dir="${repo_dir%/}"
        [[ -d "$repo_dir/.git" && -f "$repo_dir/COMPLETENESS_MATRIX.md" ]] || continue
        if git -C "$repo_dir" status --porcelain -- COMPLETENESS_MATRIX.md | grep -q .; then
          git -C "$repo_dir" add COMPLETENESS_MATRIX.md
          git -C "$repo_dir" commit -q -m "docs: update completeness matrix (aicg completeness)"
          if git -C "$repo_dir" push -q origin HEAD; then
            CHANGED=$((CHANGED + 1))
          else
            log "push failed: $(basename "$repo_dir")"
          fi
        fi
      done
      log "completeness: updated $CHANGED repos"
      ;;
    discover-roles)
      # Monthly, per domain: search postings for job titles no role in any
      # domain covers, filter hard, and open "New role proposal" issues in the
      # parent org. A maintainer approves with the `approved` label.
      run_aicg_org sync
      OUT=$("$AICG_BIN" org discover-roles --workspace "$WORKSPACE" --manifest "$MANIFEST" \
        --state-dir "$STATE_DIR" --apply 2>&1) || log "discover-roles exited non-zero"
      log "$OUT"
      ;;
    approvals)
      # Daily, all domains: act on proposal issues labeled `approved` (merge +
      # promote + author content; create + author new roles), close the PRs of
      # declined proposals, and ping once for every new proposal awaiting a decision.
      OUT=$("$AICG_BIN" org approvals --workspace "$WORKSPACE" --apply 2>&1) || log "approvals exited non-zero"
      log "$OUT"
      while IFS= read -r line; do
        notify_ntfy "🗳️ Proposal awaiting your approval" "ballot_box_with_check" "default" "${line#NEW PROPOSAL: }"
      done < <(printf '%s\n' "$OUT" | grep '^NEW PROPOSAL: ' || true)
      while IFS= read -r line; do
        notify_ntfy "AICG approval: $line" "gear" "default" "$line"
      done < <(printf '%s\n' "$OUT" | grep -E '^issue #[0-9]+: (applied|failed)' || true)
      ;;
    publish)
      # Weekly, and right after an approved new role: regenerate every page that
      # derives from config and push what changed. No Claude calls.
      #   1. org profile pages (<org>-dotgithub/profile/README.md)
      #   2. Module 00 in every learning repo (+ CURRICULUM.md "Start here")
      #   3. the umbrella site and each org's <org>.github.io site
      SITE_DIR="$WORKSPACE/ai-curriculum-project.github.io"
      if [[ ! -d "$SITE_DIR/.git" ]]; then
        git clone -q https://github.com/ai-curriculum-project/ai-curriculum-project.github.io.git "$SITE_DIR" \
          || die "could not clone the site repo"
      fi
      git -C "$SITE_DIR" pull -q --ff-only || log "site repo pull failed; continuing"
      for dom in $("$AICG_BIN" org list-domains 2>/dev/null | awk '/✓/ {print $1}'); do
        "$AICG_BIN" org sync --workspace "$WORKSPACE" --domain "$dom" >/dev/null 2>&1 \
          || log "sync failed for $dom"
      done
      # Repos with local changes before we render are left alone, so nothing
      # unrelated gets committed alongside the generated pages.
      DIRTY_BEFORE=" "
      for repo_dir in "$WORKSPACE"/*/; do
        repo_dir="${repo_dir%/}"
        [[ -d "$repo_dir/.git" ]] || continue
        if git -C "$repo_dir" status --porcelain -- . ':(exclude).aicg' | grep -q .; then
          DIRTY_BEFORE+="$(basename "$repo_dir") "
        fi
      done
      "$AICG_BIN" org publish-profiles --workspace "$WORKSPACE" | tail -n 20 || log "publish-profiles failed"
      "$AICG_BIN" org render-orientation --workspace "$WORKSPACE" --all-domains >/dev/null \
        || log "render-orientation failed"
      PUBLISHED=0
      for repo_dir in "$WORKSPACE"/*/; do
        repo_dir="${repo_dir%/}"; name="$(basename "$repo_dir")"
        [[ -d "$repo_dir/.git" ]] || continue
        [[ "$DIRTY_BEFORE" == *" $name "* ]] && continue
        paths=()
        for p in profile/README.md CURRICULUM.md lessons/00-your-relationship-to-the-model \
                 modules/00-your-relationship-to-the-model; do
          [[ -e "$repo_dir/$p" ]] && paths+=("$p")
        done
        [[ ${#paths[@]} -gt 0 ]] || continue
        git -C "$repo_dir" add -- "${paths[@]}"
        git -C "$repo_dir" diff --cached --quiet && continue
        git -C "$repo_dir" commit -q -m "docs: refresh generated pages (aicg publish)"
        if git -C "$repo_dir" push -q origin HEAD; then
          PUBLISHED=$((PUBLISHED + 1))
        else
          log "push failed: $name"
        fi
      done
      log "publish: updated $PUBLISHED repos (profiles + Module 00)"
      if ( cd "$SITE_DIR" && python3 scripts/export-data.py && python3 build.py ) >/dev/null; then
        git -C "$SITE_DIR" add -- index.html data/curricula.json
        if ! git -C "$SITE_DIR" diff --cached --quiet; then
          # The site repo's main is protected (PRs only, admins included), so the
          # refresh goes through a PR that the runner merges.
          SITE_BRANCH="aicg/publish/$(date +%Y-%m-%d-%H%M)"
          SITE_REPO="ai-curriculum-project/ai-curriculum-project.github.io"
          git -C "$SITE_DIR" checkout -q -b "$SITE_BRANCH"
          git -C "$SITE_DIR" commit -q -m "data: refresh from the curriculum manifests (aicg publish)"
          if git -C "$SITE_DIR" push -q -u origin "$SITE_BRANCH" \
             && gh pr create --repo "$SITE_REPO" --head "$SITE_BRANCH" \
                  --title "data: refresh from the curriculum manifests" \
                  --body "Weekly refresh by the runner's publish job (role lists, module counts)." >/dev/null \
             && gh pr merge "$SITE_BRANCH" --repo "$SITE_REPO" --merge --delete-branch >/dev/null; then
            log "umbrella site refreshed"
          else
            log "umbrella site refresh PR failed; branch $SITE_BRANCH left for review"
          fi
          git -C "$SITE_DIR" checkout -q main
          git -C "$SITE_DIR" pull -q --ff-only || true
        fi
        bash "$SITE_DIR/scripts/publish-org-sites.sh" | grep -v "unchanged" || true
      else
        log "site build failed; sites left as they were"
      fi
      ;;
    fleet-digest)
      # Project-wide daily observability: one ntfy push summarizing fill
      # progress across all domains (N/total roles authored, mode, BAR).
      # Reads all domain manifests + the workspace; ignores --manifest.
      DIGEST=$("$AICG_BIN" --workspace "$WORKSPACE" fleet digest \
        --date "$(date +%Y-%m-%d)" 2>/dev/null || echo "fleet digest failed")
      # Append CLI-token age so a soon-to-lapse token is visible before it
      # stalls the fleet (the subscription token gets invalidated periodically).
      AUTH_FILE="$HOME/.config/aicg/claude-auth.env"
      if [[ -f "$AUTH_FILE" ]]; then
        TOK_AGE=$(( ( $(date +%s) - $(stat -c %Y "$AUTH_FILE" 2>/dev/null || echo 0) ) / 86400 ))
        # Use an `if` (exempt from set -e) rather than `(( ... )) && printf`
        # inline: the arithmetic command returns exit 1 when the comparison is
        # false, which poisons the assignment's status and — under set -e —
        # crashed the whole digest silently whenever the token was <10d old.
        TOK_WARN=""
        if (( TOK_AGE >= 10 )); then TOK_WARN=' — 🔑 refresh soon (setup-token)'; fi
        DIGEST="$DIGEST"$'\n'"token: ${TOK_AGE}d old${TOK_WARN}"
      fi
      log "$DIGEST"
      notify_ntfy "📊 AICG fleet digest" "bar_chart" "default" "$DIGEST"
      ;;
    *)
      die "Unknown job: $JOB"
      ;;
  esac
  log "Completed job: $JOB"

  # Out-of-band heartbeat (C-B2): ping an external dead-man's-switch on success
  # so a monitor independent of this host + token alerts if the pipeline goes
  # silent — the failure mode that bit us before (token expiry, silent stall).
  if [[ -n "${AICG_HEARTBEAT_URL:-}" ]]; then
    curl -fsS -m 10 "$AICG_HEARTBEAT_URL" >/dev/null 2>&1 || log "heartbeat ping failed"
  fi
}

parse_args "$@"
main
