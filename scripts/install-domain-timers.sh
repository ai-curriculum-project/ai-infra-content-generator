#!/usr/bin/env bash
#
# Install a namespaced systemd --user timer set for ONE sibling domain, so the
# autonomous pipeline researches + authors that domain's roles without
# colliding with ai-infra's timers (install-schedules.sh) or the other
# domains.
#
# Each domain gets:
#   - aicg-<domain>-research-role@<role>.timer  (seed curriculum plan; day=role index)
#   - aicg-<domain>-review-role@<role>.timer    (freshness review; day=14+index)
#   - aicg-<domain>-generate-role@<role>.timer  (author content from the plan; day=index)
#   - aicg-<domain>-daily.timer                 (daily authoring tick from the queue)
#   - aicg-<domain>-fill.timer                  (daily: fill the next unfilled role)
#   - aicg-<domain>-backfill-assessments.timer  (daily: quizzes/labs for one module)
#   - aicg-<domain>-resolve-markers.timer       (daily: needs-research markers, one module batch)
#   - aicg-<domain>-steward.timer               (twice daily: merge open PRs whose CI passed)
# all pointing at the domain's manifest + a per-domain state dir, and offset by
# --hour-offset so 4 domains don't hit the shared Claude subscription at once.
#
# Roles come from the manifest (via aicg org list-roles), so the set stays in
# lockstep with config. Stale per-role timers (role left the manifest) are
# removed. Run per domain:
#   install-domain-timers.sh --domain ml-engineering --hour-offset 2
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE="$(cd "$RUNNER_DIR/.." && pwd)"
AICG_BIN="${AICG_BIN:-$RUNNER_DIR/.venv/bin/aicg}"
DOMAIN=""
HOUR_OFFSET=0
DRY_RUN=0

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; }
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2;;
    --hour-offset) HOUR_OFFSET="${2:-0}"; shift 2;;
    --workspace) WORKSPACE="${2:-}"; shift 2;;
    --aicg-bin) AICG_BIN="${2:-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

[[ -n "$DOMAIN" ]] || die "--domain is required"
MANIFEST="$RUNNER_DIR/config/domains/$DOMAIN.yaml"
[[ -f "$MANIFEST" ]] || die "manifest not found: $MANIFEST"
STATE_DIR="$RUNNER_DIR/.aicg/$DOMAIN"
LOG_DIR="$HOME/.cache/aicg"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Roles from the manifest (authoritative ordering), with a parse fallback.
manifest_roles() {
  local out=""
  if [[ -x "$AICG_BIN" ]]; then
    out="$("$AICG_BIN" org list-roles --manifest "$MANIFEST" --state-dir "$STATE_DIR" 2>/dev/null || true)"
  fi
  if [[ -n "$out" ]]; then printf '%s\n' "$out"; return; fi
  python3 - "$MANIFEST" <<'PY'
import sys, json
d=json.load(open(sys.argv[1]))
for r in d.get("roles", []): print(r["id"])
PY
}

ROLES=()
while IFS= read -r _role; do
  [[ -n "$_role" ]] && ROLES+=("$_role")
done < <(manifest_roles)
[[ "${#ROLES[@]}" -gt 0 ]] || die "no roles in manifest"
log "Domain '$DOMAIN': ${#ROLES[@]} roles, hour-offset=$HOUR_OFFSET, state=$STATE_DIR"

# minute 15 keeps clear of any top-of-hour jobs; offset shifts the whole domain.
# All content/judge timers run OFF-PEAK (Anthropic eases subscription limits
# overnight, and the session quota resets at midnight local). Each job type gets
# a distinct off-peak hour (MST/MDT); the three domains are separated by a
# per-domain minute (FMIN) so they never hit the shared subscription on the same
# second. Any rare same-domain overlap bails gracefully on the org-job lock.
FMIN=$(printf '%02d' $(( (HOUR_OFFSET * 9 + 5) % 60 )))   # offset 2/4/6 -> :23/:41/:59

job_cmd() {  # job role
  printf '%q %q %q --workspace %q --manifest %q --state-dir %q' \
    "$RUNNER_DIR/scripts/aicg-org-job.sh" "$1" "$2" "$WORKSPACE" "$MANIFEST" "$STATE_DIR"
}
job_cmd_noarg() {  # job
  printf '%q %q --workspace %q --manifest %q --state-dir %q' \
    "$RUNNER_DIR/scripts/aicg-org-job.sh" "$1" "$WORKSPACE" "$MANIFEST" "$STATE_DIR"
}

write_unit() {  # name exec-cmd calendar
  local name="$1" cmd="$2" cal="$3"
  local svc="$UNIT_DIR/aicg-$name.service" tmr="$UNIT_DIR/aicg-$name.timer"
  local svc_body="[Unit]
Description=AICG $DOMAIN: $name
[Service]
Type=oneshot
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=$HOME
ExecStartPre=/bin/mkdir -p $LOG_DIR
ExecStart=$cmd
StandardOutput=append:$LOG_DIR/$name.log
StandardError=append:$LOG_DIR/$name.log
"
  local tmr_body="[Unit]
Description=AICG $DOMAIN timer: $name
[Timer]
OnCalendar=$cal
Persistent=true
[Install]
WantedBy=timers.target
"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf -- '----- %s -----\n%s\n----- %s -----\n%s\n' "$svc" "$svc_body" "$tmr" "$tmr_body"
    return
  fi
  printf '%s' "$svc_body" >"$svc"
  printf '%s' "$tmr_body" >"$tmr"
  systemctl --user enable --now "aicg-$name.timer" >/dev/null 2>&1 || true
}

[[ "$DRY_RUN" -eq 1 ]] || mkdir -p "$UNIT_DIR" "$STATE_DIR"

# Per-role research -> generate -> review, day = role index (research+generate)
# / 14+index (review). All off-peak: research 01:FMIN, generate 04:FMIN (3h
# later, same day, so the plan is seeded), review 23:FMIN.
i=0
for role in "${ROLES[@]}"; do
  i=$((i + 1))
  rday=$(printf '%02d' "$i")
  vday=$(printf '%02d' "$((i + 14))")
  write_unit "$DOMAIN-research-role@$role" "$(job_cmd research-role "$role")" "*-*-$rday 01:$FMIN:00"
  write_unit "$DOMAIN-generate-role@$role" "$(job_cmd generate-role "$role")" "*-*-$rday 04:$FMIN:00"
  write_unit "$DOMAIN-review-role@$role"   "$(job_cmd review-role "$role")"   "*-*-$vday 23:$FMIN:00"
done

# Authoring ticks for the domain (queue drain): four a day, each takes the
# top queued item. Was once a day, which left a 500-item backlog (skeleton
# modules + stub solutions, surfaced by the depth audit) needing over a year.
# Four keeps the subscription footprint modest (see the fill note below).
write_unit "$DOMAIN-daily" "$(job_cmd_noarg daily-remediate)" "*-*-* 00,07,19,22:$FMIN:00"

# Weekly queue rebuild: re-audit every repo so new gaps (including skeletons
# and stubs from the depth checks) get queued. Without it a sibling domain's
# queue is built once and never refreshed. Sun 06:FMIN, its own hour.
write_unit "$DOMAIN-weekly-audit" "$(job_cmd_noarg weekly-audit)" "Sun 06:$FMIN:00"

# Monthly role discovery: titles no role covers become proposal issues. Day 28
# at 01:FMIN, clear of per-role research (days 1-N at 01:FMIN).
write_unit "$DOMAIN-discover-roles" "$(job_cmd_noarg discover-roles)" "*-*-28 01:$FMIN:00"

# Off-peak fill: author the next unfilled role (self-seeding), ONCE per night.
# Deliberately throttled to a single deep-off-peak run per domain (03:FMIN) to
# keep the runner's footprint low on the subscription token (heavy automation
# patterns trigger token invalidation). ~3 roles/night across the fleet fills
# the remaining roles in a few nights, then it's a quiet daily no-op. Capped or
# auth-failed runs no-op quietly (⏸️ suppressed via AICG_FILL_QUIET; a 🔑 re-auth
# alert fires separately on "Not logged in").
write_unit "$DOMAIN-fill" "$(job_cmd_noarg fill-next)" "*-*-* 03:$FMIN:00"

# Off-peak quiz/lab backfill: author assessments for ONE module per domain per
# night (02:FMIN — its own hour, clear of research 01, fill 03, generate 04).
# Self-terminating: once no module has placeholder quizzes/labs it no-ops.
write_unit "$DOMAIN-backfill-assessments" "$(job_cmd_noarg backfill-assessments)" "*-*-* 02:$FMIN:00"

# Off-peak needs-research resolution: one module batch per domain per night
# (05:FMIN, its own hour). Uses the web-enabled research agent.
write_unit "$DOMAIN-resolve-markers" "$(job_cmd_noarg resolve-markers)" "*-*-* 05:$FMIN:00"

# PR steward: merge open PRs whose CI has passed. daily-remediate merges
# inline, but a PR whose CI is still running is left open, and without this
# nothing retries it. No Claude calls, so it can run in the daytime. 09 and
# 17 sit clear of the authoring ticks (00/07/19/22) so it rarely meets the lock.
write_unit "$DOMAIN-steward" "$(job_cmd_noarg steward)" "*-*-* 09,17:$FMIN:00"

if [[ "$DRY_RUN" -eq 0 ]]; then
  systemctl --user daemon-reload
  # Tear down stale per-role timers for roles no longer in the manifest.
  current=" ${ROLES[*]} "
  for t in "$UNIT_DIR"/aicg-"$DOMAIN"-research-role@*.timer \
           "$UNIT_DIR"/aicg-"$DOMAIN"-generate-role@*.timer \
           "$UNIT_DIR"/aicg-"$DOMAIN"-review-role@*.timer; do
    [[ -e "$t" ]] || continue
    base="$(basename "$t")"; role="${base##*@}"; role="${role%.timer}"
    [[ "$current" == *" $role "* ]] && continue
    log "removing stale timer: $base"
    systemctl --user disable --now "$base" 2>/dev/null || true
    rm -f "$t"
  done
  log "Installed $DOMAIN timers (${#ROLES[@]} research + ${#ROLES[@]} generate + ${#ROLES[@]} review + 1 daily (x4/day) + 1 weekly-audit + 1 discover-roles + 1 fill + 1 backfill + 1 markers + 1 steward (x2/day))."
fi
