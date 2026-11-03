#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly RUNNER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKSPACE="$(cd "$RUNNER_DIR/.." && pwd)"
MANIFEST="$RUNNER_DIR/config/aicg-org.yaml"
STATE_DIR="$RUNNER_DIR/.aicg/org"
AICG_BIN="${AICG_BIN:-$RUNNER_DIR/.venv/bin/aicg}"
SCHEDULER="systemd"
DRY_RUN=0

usage() {
  cat <<EOF
Usage: $SCRIPT_NAME [OPTIONS]

Options:
  --scheduler MODE   systemd or cron. Default: systemd.
  --workspace PATH   Curriculum workspace. Default: parent of runner repo.
  --manifest PATH    Org manifest. Default: config/aicg-org.yaml.
  --state-dir PATH   Org state directory. Default: .aicg/org.
  --aicg-bin PATH    aicg executable used to read the role list. Default: .venv/bin/aicg.
  --dry-run          Print files/entries without installing.
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
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --scheduler)
        SCHEDULER="${2:-}"
        shift 2
        ;;
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
      --dry-run)
        DRY_RUN=1
        shift
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

job_command() {
  local job="$1"
  printf '%q %q --workspace %q --manifest %q --state-dir %q' \
    "$RUNNER_DIR/scripts/aicg-org-job.sh" "$job" "$WORKSPACE" "$MANIFEST" "$STATE_DIR"
}

# Source of truth for the per-role timer list is the manifest itself —
# NOT a hand-maintained array. This is what keeps the research/review
# timers in lockstep when a role is added or moved to a sibling-org
# domain config (e.g. the agentic + governance split). Roles come back
# lowest-level-first (manifest order), which is the cadence we want:
# foundational tracks land their proposals before dependent tracks.
# Prefer the authoritative loader (handles JSON + YAML manifests); fall
# back to a direct parse so --dry-run works on a dev box with no venv.
manifest_roles() {
  local out=""
  if [[ -x "$AICG_BIN" ]]; then
    # Capture so a stale/broken venv shim (wrong shebang) falls through
    # to the direct parse instead of yielding an empty role list.
    out="$("$AICG_BIN" org list-roles --manifest "$MANIFEST" --state-dir "$STATE_DIR" 2>/dev/null || true)"
  fi
  if [[ -n "$out" ]]; then
    printf '%s\n' "$out"
    return
  fi
  python3 - "$MANIFEST" <<'PY'
import sys, json
path = sys.argv[1]
try:
    data = json.load(open(path))
except Exception:
    import yaml
    data = yaml.safe_load(open(path))
for role in data.get("roles", []):
    print(role["id"])
PY
}

# Read the role list once into an array used by every install path.
load_roles() {
  ROLES=()
  local role
  while IFS= read -r role; do
    [[ -n "$role" ]] && ROLES+=("$role")
  done < <(manifest_roles)
  [[ "${#ROLES[@]}" -gt 0 ]] || die "no roles found in manifest: $MANIFEST"
}

# research-role / review-role take a positional ROLE that must follow
# the job name, before option flags. Build the command in that order.
job_command_research_role() {
  local role="$1"
  printf '%q %q %q --workspace %q --manifest %q --state-dir %q' \
    "$RUNNER_DIR/scripts/aicg-org-job.sh" "research-role" "$role" \
    "$WORKSPACE" "$MANIFEST" "$STATE_DIR"
}

job_command_review_role() {
  local role="$1"
  printf '%q %q %q --workspace %q --manifest %q --state-dir %q' \
    "$RUNNER_DIR/scripts/aicg-org-job.sh" "review-role" "$role" \
    "$WORKSPACE" "$MANIFEST" "$STATE_DIR"
}

install_cron() {
  local marker_begin="# BEGIN AICG ORG JOBS"
  local marker_end="# END AICG ORG JOBS"
  # Per-role slots are derived from the manifest (see load_roles), so the
  # list can never drift from the org config. Research at 00:15 on day N
  # (role index, 1-based); review at 12:30 on day 14+N (each role reviewed
  # 14 days after its research). Research (00:15) and review (12:30) are
  # 12h apart so the late-research / early-review overlap never races for
  # the org-job lock. With the 11 ai-infra roles: research days 1-11,
  # review days 15-25.
  local per_role_lines=""
  local i role day
  for i in "${!ROLES[@]}"; do
    role="${ROLES[$i]}"
    day=$((i + 1))
    # minute 15, not 0 — daily-remediate fires at minute 0 every hour
    # and holds the org-job lock. Mirrors the systemd OnCalendar fix.
    per_role_lines+=$'\n'"15 0 $day * * $(job_command_research_role "$role")"
  done
  for i in "${!ROLES[@]}"; do
    role="${ROLES[$i]}"
    day=$((i + 15))
    # 12:30 — review runs at noon, 12h from the 00:15 research slot, so
    # the late-research / early-review overlap never collides on the lock.
    per_role_lines+=$'\n'"30 12 $day * * $(job_command_review_role "$role")"
  done

  local block
  block="$marker_begin
5 2 1 * * $(job_command monthly-release)
5 3 * * 0 $(job_command weekly-audit)
0 * * * * $(job_command daily-remediate)
20 4 * * * $(job_command daily-issues)
40 4 * * * $(job_command daily-steward)
5 5 * * * $(job_command daily-discussions)
35 5 * * * $(job_command resolve-markers)
50 3 * * * $(job_command fill-next)
5 7 * * 0 $(job_command completeness)${per_role_lines}
$marker_end"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s\n' "$block"
    return
  fi

  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | sed "/$marker_begin/,/$marker_end/d" >"$tmp" || true
  printf '\n%s\n' "$block" >>"$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  log "Installed cron jobs."
}

write_systemd_pair() {
  local unit_dir="$1"
  local name="$2"
  local job="$3"
  local calendar="$4"
  local service="$unit_dir/aicg-$name.service"
  local timer="$unit_dir/aicg-$name.timer"

  # Drop-in marker file lets operators keep a local override in
  # aicg-$name.timer.d/override.conf — we never touch *.d/ subdirs.
  # Anything written here gets the install-script default; existing
  # drop-ins take effect after `daemon-reload`.
  :

  local log_dir="$HOME/.cache/aicg"
  local service_content="[Unit]
Description=AICG org job: $job

[Service]
Type=oneshot
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=$HOME
ExecStartPre=/bin/mkdir -p $log_dir
ExecStart=$(job_command "$job")
StandardOutput=append:$log_dir/$job.log
StandardError=append:$log_dir/$job.log
"
  local timer_content="[Unit]
Description=AICG org timer: $job

[Timer]
OnCalendar=$calendar
Persistent=true

[Install]
WantedBy=timers.target
"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s\n%s\n' "----- $service -----" "$service_content"
    printf '%s\n%s\n' "----- $timer -----" "$timer_content"
    return
  fi

  printf '%s' "$service_content" >"$service"
  printf '%s' "$timer_content" >"$timer"
}

install_per_role_research() {
  local unit_dir="$1"
  local log_dir="$HOME/.cache/aicg"
  local service_path="$unit_dir/aicg-research-role@.service"
  local template_path="$RUNNER_DIR/scripts/cron/aicg-research-role@.service.template"

  [[ -f "$template_path" ]] || die "missing template: $template_path"

  # Render the templated service file. systemd substitutes %i at
  # invocation time; we substitute the install-time variables now.
  local service_content
  service_content="$(
    sed \
      -e "s#{PATH}#$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin#g" \
      -e "s#{HOME}#$HOME#g" \
      -e "s#{LOG_DIR}#$log_dir#g" \
      -e "s#{RUNNER_DIR}#$RUNNER_DIR#g" \
      -e "s#{WORKSPACE}#$WORKSPACE#g" \
      -e "s#{MANIFEST}#$MANIFEST#g" \
      -e "s#{STATE_DIR}#$STATE_DIR#g" \
      "$template_path"
  )"

  # Research day-of-month slot (00:15) = role index in manifest order
  # (lowest-level-first), so foundational tracks land their proposals
  # before dependent tracks. The role list comes from load_roles (the
  # manifest), NOT a hand-maintained array — moving a role to a sibling
  # org's domain config drops it here automatically, and stale instances
  # are torn down by teardown_stale_role_timers.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s\n%s\n' "----- $service_path -----" "$service_content"
  else
    printf '%s' "$service_content" >"$service_path"
  fi

  local i day role timer_path timer_content
  for i in "${!ROLES[@]}"; do
    role="${ROLES[$i]}"
    printf -v day '%02d' "$((i + 1))"
    timer_path="$unit_dir/aicg-research-role@${role}.timer"
    # 00:15, NOT 00:00 — daily-remediate fires hourly at *:00:00 and
    # has been running for weeks, so it wins the org-job lock by a
    # few hundred ms at the top of every hour. Both 2026-06-05 and
    # 2026-06-06 research-role runs bailed in 32 seconds with
    # "Another AICG org job is running; exiting." Same fix pattern
    # as daily-discussions @ 05:05. 15 min is comfortably past
    # remediate's typical 30-60s runtime.
    timer_content="[Unit]
Description=AICG per-role research timer: ${role}

[Timer]
OnCalendar=*-*-${day} 00:15:00
Persistent=true
Unit=aicg-research-role@${role}.service

[Install]
WantedBy=timers.target
"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s\n%s\n' "----- $timer_path -----" "$timer_content"
    else
      printf '%s' "$timer_content" >"$timer_path"
    fi
  done

  # Return the role list so the caller can enable each timer.
  PER_ROLE_SLUGS=("${ROLES[@]}")
}

install_per_role_review() {
  local unit_dir="$1"
  local log_dir="$HOME/.cache/aicg"
  local service_path="$unit_dir/aicg-review-role@.service"
  local template_path="$RUNNER_DIR/scripts/cron/aicg-review-role@.service.template"

  [[ -f "$template_path" ]] || die "missing template: $template_path"

  local service_content
  service_content="$(
    sed \
      -e "s#{PATH}#$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin#g" \
      -e "s#{HOME}#$HOME#g" \
      -e "s#{LOG_DIR}#$log_dir#g" \
      -e "s#{RUNNER_DIR}#$RUNNER_DIR#g" \
      -e "s#{WORKSPACE}#$WORKSPACE#g" \
      -e "s#{MANIFEST}#$MANIFEST#g" \
      -e "s#{STATE_DIR}#$STATE_DIR#g" \
      "$template_path"
  )"

  # Review day-of-month slot (12:30) = 14 + role index, so each role is
  # reviewed 14 days after its 00:15 research. Review runs at noon, NOT
  # 00:30, so the late-research / early-review overlap is 12h apart and
  # never races for the org-job lock. Role list comes from load_roles
  # (the manifest) — kept in lockstep with research automatically. With
  # the 11 ai-infra roles: review days 15-25.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s\n%s\n' "----- $service_path -----" "$service_content"
  else
    printf '%s' "$service_content" >"$service_path"
  fi

  local i day role timer_path timer_content
  for i in "${!ROLES[@]}"; do
    role="${ROLES[$i]}"
    printf -v day '%02d' "$((i + 15))"
    timer_path="$unit_dir/aicg-review-role@${role}.timer"
    # 00:30 — see the lock-race comment on aicg-research-role above.
    # Review starts later than research so they don't compete for the
    # lock on the day-13→14 boundary (research junior-engineer day 14
    # at 00:15, review junior-engineer day 14 at 00:30 — no overlap
    # because no role fires on both timers the same night).
    timer_content="[Unit]
Description=AICG per-role freshness review timer: ${role}

[Timer]
OnCalendar=*-*-${day} 12:30:00
Persistent=true
Unit=aicg-review-role@${role}.service

[Install]
WantedBy=timers.target
"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s\n%s\n' "----- $timer_path -----" "$timer_content"
    else
      printf '%s' "$timer_content" >"$timer_path"
    fi
  done

  PER_ROLE_REVIEW_SLUGS=("${ROLES[@]}")
}

# Disable + remove per-role timer instances whose role is no longer in
# the manifest. Without this, roles moved to a sibling-org domain config
# (e.g. the agentic + governance split) would leave orphaned timers that
# fire and fail against the trimmed manifest with "role not found". Takes
# the instance kind (research|review) and the set of current role slugs.
teardown_stale_role_timers() {
  local unit_dir="$1"
  local kind="$2"
  shift 2
  local -a current=("$@")
  local timer base role keep slug
  for timer in "$unit_dir"/aicg-"$kind"-role@*.timer; do
    [[ -e "$timer" ]] || continue
    base="$(basename "$timer")"               # aicg-research-role@security.timer
    role="${base#aicg-${kind}-role@}"          # security.timer
    role="${role%.timer}"                       # security
    keep=0
    for slug in "${current[@]}"; do
      [[ "$slug" == "$role" ]] && { keep=1; break; }
    done
    [[ "$keep" -eq 1 ]] && continue
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "would remove stale ${kind} timer: $base"
      continue
    fi
    log "removing stale ${kind} timer: $base (role not in manifest)"
    systemctl --user disable --now "$base" 2>/dev/null || true
    rm -f "$timer"
  done
}

install_systemd() {
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$unit_dir"
  fi

  # Every job below that would fire at minute 0 is offset to :05 — daily-remediate
  # fires at *:00:00 and holds the org-job lock, so a :00 job races it and
  # usually bails with "Another AICG org job is running" (fleet-digest lost
  # 5 of 7 days in late Oct 2026; monthly-release and weekly-audit also lost).
  write_systemd_pair "$unit_dir" "monthly-release" "monthly-release" "*-*-01 02:05:00"
  # Per-role nightly research replaces the org-wide monthly-research job.
  # The legacy aicg-monthly-research.{service,timer} files are still
  # written for compatibility (so existing operators see the rename in
  # the dry-run), but install_per_role_research is the new path and the
  # enable list below disables monthly-research.timer in favor of the
  # per-role instances.
  write_systemd_pair "$unit_dir" "monthly-research" "monthly-research" "*-03,06,09,12-01 05:30:00"
  # Quarterly LLM freshness review (1st of Mar/Jun/Sep/Dec at 06:00).
  write_systemd_pair "$unit_dir" "monthly-review" "monthly-review" "*-03,06,09,12-01 06:05:00"
  write_systemd_pair "$unit_dir" "weekly-audit" "weekly-audit" "Sun 03:05:00"
  write_systemd_pair "$unit_dir" "daily-remediate" "daily-remediate" "*-*-* *:00:00"
  write_systemd_pair "$unit_dir" "daily-issues" "daily-issues" "*-*-* 04:20:00"
  write_systemd_pair "$unit_dir" "daily-steward" "daily-steward" "*-*-* 04:40:00"
  # Discussions at 05:05, not 05:00 — the hourly remediate timer also
  # fires at 05:00 and they raced for the org-job lock. Remediate won
  # consistently and discussions bailed with "Another AICG org job is
  # running" every day. 5 min is comfortably past remediate's typical
  # runtime (~1-100s).
  write_systemd_pair "$unit_dir" "daily-discussions" "daily-discussions" "*-*-* 05:05:00"
  # Project-wide daily fleet digest -> ntfy (read-only; covers all domains).
  write_systemd_pair "$unit_dir" "fleet-digest" "fleet-digest" "*-*-* 09:05:00"
  # needs-research resolution for the ai-infra org (05:35, off minute 0).
  write_systemd_pair "$unit_dir" "resolve-markers" "resolve-markers" "*-*-* 05:35:00"
  # Daily initial-fill: author the next ai-infra role with no modules yet
  # (self-seeding generate-role). No-op once every role has content, so a
  # role added via bootstrap-role gets authored without a manual kickoff.
  # 03:50, not 03:05: weekly-audit runs Sun 03:05 and would race it for the lock.
  write_systemd_pair "$unit_dir" "fill-next" "fill-next" "*-*-* 03:50:00"
  # Weekly completeness matrices for every repo, org, and the parent org
  # (after every domain's Sun 03-06 audits). Commits only changed numbers.
  write_systemd_pair "$unit_dir" "completeness" "completeness" "Sun 07:05:00"

  install_per_role_research "$unit_dir"
  install_per_role_review "$unit_dir"

  # Remove any per-role timers for roles that have left the manifest
  # (moved to a sibling-org domain config) so they stop firing/failing.
  teardown_stale_role_timers "$unit_dir" "research" "${PER_ROLE_SLUGS[@]}"
  teardown_stale_role_timers "$unit_dir" "review" "${PER_ROLE_REVIEW_SLUGS[@]}"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    systemctl --user daemon-reload
    # Disable the legacy org-wide research/review timers in favor of
    # per-role instances. Tolerate failure (they may already be inactive).
    systemctl --user disable --now aicg-monthly-research.timer 2>/dev/null || true
    systemctl --user disable --now aicg-monthly-review.timer 2>/dev/null || true
    systemctl --user enable --now \
      aicg-monthly-release.timer \
      aicg-weekly-audit.timer \
      aicg-daily-remediate.timer \
      aicg-daily-issues.timer \
      aicg-daily-steward.timer \
      aicg-daily-discussions.timer \
      aicg-fleet-digest.timer \
      aicg-fill-next.timer \
      aicg-completeness.timer
    local slug
    for slug in "${PER_ROLE_SLUGS[@]}"; do
      systemctl --user enable --now "aicg-research-role@${slug}.timer"
    done
    for slug in "${PER_ROLE_REVIEW_SLUGS[@]}"; do
      systemctl --user enable --now "aicg-review-role@${slug}.timer"
    done
    log "Installed systemd user timers (${#PER_ROLE_SLUGS[@]} per-role research + ${#PER_ROLE_REVIEW_SLUGS[@]} per-role review)."
  fi
}

main() {
  [[ -f "$MANIFEST" ]] || die "manifest not found: $MANIFEST"
  [[ -x "$RUNNER_DIR/scripts/aicg-org-job.sh" ]] || die "job wrapper is not executable"

  # Populate ROLES from the manifest — drives every per-role timer slot.
  load_roles
  log "Loaded ${#ROLES[@]} roles from $(basename "$MANIFEST"): ${ROLES[*]}"

  case "$SCHEDULER" in
    cron)
      install_cron
      ;;
    systemd)
      install_systemd
      ;;
    *)
      die "Unsupported scheduler: $SCHEDULER"
      ;;
  esac
}

parse_args "$@"
main
