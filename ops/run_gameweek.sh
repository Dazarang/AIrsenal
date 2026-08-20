#!/usr/bin/env bash
#
# Orchestrates one gameweek run: pull, chip research, AIrsenal pipeline,
# apply transfers + lineup, verify server-side, Telegram report.
#
# Usage: run_gameweek.sh <gameweek> <deadline_epoch> [--dry-run]
#   --dry-run: full chain but no POSTs to the FPL API, no run markers,
#              no chip decision recorded, Telegram messages prefixed [DRY RUN].
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GW="${1:?usage: run_gameweek.sh <gameweek> <deadline_epoch> [--dry-run]}"
DEADLINE="${2:?deadline epoch required}"
DRY_RUN=0
[[ "${3:-}" == "--dry-run" ]] && DRY_RUN=1

S="$RUN_DIR/gw${GW}"
TS=$(date +%Y%m%dT%H%M%S)
if (( DRY_RUN )); then
  LOG="$LOG_DIR/gw${GW}-dryrun-${TS}.log"
  PREFIX="[DRY RUN] "
else
  LOG="$LOG_DIR/gw${GW}-${TS}.log"
  PREFIX=""
fi
TMP=$(mktemp -d)
exec >>"$LOG" 2>&1

CURRENT_STEP="init"
on_exit() {
  local code=$?
  rm -rf "$TMP"
  if (( code != 0 )); then
    notify_soft "${PREFIX}AIrsenal GW${GW} FAILED at step: ${CURRENT_STEP} (exit ${code}). Log: $LOG
$(tail -15 "$LOG" 2>/dev/null)"
  fi
}
trap on_exit EXIT

# single active run; a second invocation exits immediately (the scheduler
# probes this lock before launching, so this only triggers on a manual run).
# Children inherit the lock fd on purpose: the run counts as in progress
# until its last process has ended
exec 9>"$RUN_DIR/airsenal.lock"
if ! flock -n 9; then
  log "another run holds the lock, exiting"
  notify_soft "${PREFIX}AIrsenal GW${GW}: launch blocked - another run holds the lock. Check the Pi if this repeats."
  exit 0
fi
# record of this run (pid, start, deadline); unconditional incl. dry runs
echo "$$ $(date +%s) $DEADLINE" > "$S.started"

mins_left() { echo $(( (DEADLINE - $(date +%s)) / 60 )); }
log "=== GW${GW} run starting (dry_run=${DRY_RUN}, $(mins_left)m to deadline) ==="

# --- step 0: update code (non-fatal) -----------------------------------------
CURRENT_STEP="git pull / uv sync"
cd "$APP_DIR"
if ! git pull --ff-only --autostash origin main; then
  notify_soft "${PREFIX}AIrsenal GW${GW}: git pull failed, running with previous version."
fi
if ! "$HOME/.local/bin/uv" sync --frozen; then
  notify_soft "${PREFIX}AIrsenal GW${GW}: uv sync failed, running with existing venv."
fi

# --- step 1: pre-flights ------------------------------------------------------
CURRENT_STEP="pre-flight checks"
: "${FPL_TEAM_ID:?FPL_TEAM_ID not set}"
: "${FPL_LOGIN:?FPL_LOGIN not set}"
: "${FPL_PASSWORD:?FPL_PASSWORD not set}"

"$PY" -c "
from sqlalchemy import select
from airsenal.framework.schema import Player
from airsenal.framework.utils import session
import sys
sys.exit(0 if session.scalars(select(Player)).first() else 1)
" || { log "FATAL: database is empty - run airsenal_setup_initial_db"; exit 1; }

# the login flow is transiently flaky; FPLDataFetcher.login retries internally
log "checking FPL login..."
"$PY" -c "
from airsenal.framework.data_fetcher import FPLDataFetcher
f = FPLDataFetcher()
f.login()
raise SystemExit(0 if f.logged_in else 1)
" || { log "FATAL: FPL login failed - credentials wrong or FPL changed their login flow"; exit 1; }

# --- step 2: database backup --------------------------------------------------
CURRENT_STEP="database backup"
DB_FILE="${AIRSENAL_DB_FILE:-$AIRSENAL_HOME/data.db}"
if [[ -f "$DB_FILE" ]]; then
  # python stdlib backup (no sqlite3 CLI on stock Pi OS); never fatal - a
  # backup is a nicety, not a prerequisite for the transfer run
  "$PY" -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
" "$DB_FILE" "$BACKUP_DIR/data-gw${GW}-${TS}.db" \
    || notify_soft "${PREFIX}AIrsenal GW${GW}: DB backup failed, continuing."
  ls -1t "$BACKUP_DIR"/data-*.db 2>/dev/null | tail -n +9 | xargs -r rm -f || true
fi

# --- step 3: update database --------------------------------------------------
CURRENT_STEP="airsenal_update_db"
"$VENV_BIN/airsenal_update_db" --fpl_team_id "$FPL_TEAM_ID" | tee "$TMP/update_db.out"
if grep -q "Database is empty" "$TMP/update_db.out"; then
  log "FATAL: update_db reported empty database"
  exit 1
fi

# --- step 4: gameweek sanity --------------------------------------------------
CURRENT_STEP="gameweek assertion"
"$PY" "$OPS_LIB_DIR/helpers/gw_context.py" assert-gw --gw "$GW"

# --- step 5: predictions (before chip research so the chip decision can use
# --- AIrsenal's own predicted points) -----------------------------------------
CURRENT_STEP="airsenal_run_prediction"
if (( $(mins_left) < 60 )); then
  notify_soft "${PREFIX}AIrsenal GW${GW}: only $(mins_left)m to deadline before predictions - run may not finish. Consider making transfers manually."
fi
"$VENV_BIN/airsenal_run_prediction" --weeks_ahead "${WEEKS_AHEAD:-3}"

# --- step 6: chip research ----------------------------------------------------
CURRENT_STEP="chip research"
if [[ -f "$S.applied" || -f "$S.posted" ]]; then
  # transfers already POSTed on a previous attempt: reuse that decision,
  # never research a second chip for the same gameweek
  CHIP=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" chip-for-gw --gw "$GW" || echo "none")
  log "reusing recorded chip decision: $CHIP (transfers already POSTed)"
else
  RESEARCH_ARGS=("$GW" "$DEADLINE")
  (( DRY_RUN )) && RESEARCH_ARGS+=(--dry-run)
  CHIP=$("$OPS_LIB_DIR/chip_research.sh" "${RESEARCH_ARGS[@]}" || echo "none")
  CHIP=${CHIP##*$'\n'}  # last line only
fi
log "chip decision: $CHIP"

# --- step 7: optimization -----------------------------------------------------
CURRENT_STEP="airsenal_run_optimization"
PRE_TS=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" snapshot)
CHIP_ARGS=()
case "$CHIP" in
  wildcard)       CHIP_ARGS=(--wildcard_week "$GW") ;;
  free_hit)       CHIP_ARGS=(--free_hit_week "$GW") ;;
  triple_captain) CHIP_ARGS=(--triple_captain_week "$GW") ;;
  bench_boost)    CHIP_ARGS=(--bench_boost_week "$GW") ;;
esac
"$VENV_BIN/airsenal_run_optimization" \
  --weeks_ahead "${WEEKS_AHEAD:-3}" \
  --num_thread "${NUM_THREAD:-3}" \
  --max_hit "${MAX_HIT:-8}" \
  --fpl_team_id "$FPL_TEAM_ID" \
  "${CHIP_ARGS[@]}" | tee "$TMP/optimization.out"
PRED_PTS=$(grep -m1 "Score with Strategy:" "$TMP/optimization.out" | grep -o "[0-9.]*pts" || true)

# --- step 8: inspect suggestions ---------------------------------------------
CURRENT_STEP="suggestion check"
"$PY" "$OPS_LIB_DIR/helpers/post_summary.py" check \
  --gw "$GW" --team-id "$FPL_TEAM_ID" --since "$PRE_TS" > "$TMP/check.json"
read -r NEW_ROWS N_TRANSFERS N_OUT PRE_BANK < <(python3 -c "
import json
d = json.load(open('$TMP/check.json'))
print(d['new_rows'], d['n_transfers'], d['n_out'], d['bank'] or '')
")
log "new_rows=$NEW_ROWS n_transfers=$N_TRANSFERS n_out=$N_OUT bank=$PRE_BANK"
if (( N_TRANSFERS == 0 )) && [[ "$CHIP" == "wildcard" || "$CHIP" == "free_hit" ]]; then
  # a transfer chip with nothing to transfer is pointless: nothing would be
  # POSTed, so it must not be reported or reminded as "to activate" either
  log "$CHIP decided but no transfers suggested - not playing it"
  (( DRY_RUN )) || "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" clear --gw "$GW" || true
  CHIP="none"
fi

# --- step 9: apply ------------------------------------------------------------
CURRENT_STEP="apply transfers/lineup"
if (( $(mins_left) < 3 )); then
  log "FATAL: less than 3 minutes to deadline, refusing to POST"
  exit 1
fi
# pre-apply squad snapshot: the summary diffs this against the post-apply
# squad to show the real moves on full-rebuild weeks
"$PY" "$OPS_LIB_DIR/helpers/post_summary.py" picks-elements --team-id "$FPL_TEAM_ID" \
  > "$TMP/pre_picks.json" 2>/dev/null || echo "[]" > "$TMP/pre_picks.json"
APPLY_MODE="transfers"
if [[ "$NEW_ROWS" != "True" ]]; then
  APPLY_MODE="lineup-only (stale suggestions guard)"
elif (( N_TRANSFERS == 0 )); then
  APPLY_MODE="lineup-only (no transfers suggested)"
elif (( GW > 1 && N_OUT == 0 )); then
  # IN-only rows are a from-scratch squad, legitimate only in GW1 (wildcard /
  # free hit strategies write OUT rows too): the optimizer lost track of the
  # team (API failure) and took its new-team path, whose rows carry no chip -
  # POSTing them would be ~a dozen transfers of points hits
  APPLY_MODE="lineup-only (unexpected full-squad rebuild)"
  notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: the optimizer produced a from-scratch squad (it probably failed to read the current team from the FPL API). Doing lineup only - check the log and make transfers manually if needed."
fi
if [[ -f "$S.applied" ]]; then
  APPLY_MODE="lineup-only (transfers already applied)"
elif [[ -f "$S.posted" ]]; then
  # a previous POST attempt died before we saw its outcome: never re-POST
  APPLY_MODE="lineup-only (previous POST attempt unverified)"
  notify_soft "${PREFIX}AIrsenal GW${GW}: a previous transfer POST was interrupted before confirmation - doing lineup only. CHECK YOUR TRANSFERS on the FPL site."
fi
log "apply mode: $APPLY_MODE"

LINEUP_FAILED=0
if (( DRY_RUN )); then
  log "dry run: skipping POSTs. Would do: $APPLY_MODE"
elif [[ "$APPLY_MODE" == "transfers" ]]; then
  # .posted is written BEFORE the POST: if the process dies mid-POST, the
  # retry does lineup-only + warning instead of a double POST. It is removed
  # again below only when the server shows that nothing was applied.
  touch "$S.posted"
  set +e
  PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal_make_transfers" --fpl_team_id "$FPL_TEAM_ID" --confirm 2>&1 | tee "$TMP/apply.out"
  apply_status=${PIPESTATUS[0]}
  set -e
  if grep -qE "Transfers made!|No transfers needed" "$TMP/apply.out"; then
    # POST confirmed client-side (or nothing to POST): set the never-re-POST
    # marker immediately, before any fallible verification
    touch "$S.applied"
  else
    # no client-side confirmation: ask the server (logged-in my-team view)
    # whether the suggested transfers are there before deciding about a retry
    VERIFY=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" verify \
      --gw "$GW" --team-id "$FPL_TEAM_ID" --chip none || echo "unknown unknown")
    case "${VERIFY%% *}" in
      ok)
        log "no confirmation seen but the suggested transfers are on the server - treating as applied"
        touch "$S.applied"
        ;;
      mismatch)
        # nothing was applied: let the next attempt retry the transfers, but
        # still set a lineup on the current squad (independent and free)
        rm -f "$S.posted"
        log "make_transfers failed and nothing was applied (the next attempt will retry); setting the lineup anyway"
        PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal_set_lineup" --fpl_team_id "$FPL_TEAM_ID" --confirm | tee -a "$TMP/apply.out" || true
        log "FATAL: transfers not applied"
        exit 1
        ;;
      *)
        # server state unknown: the payload is printed (unbuffered) right
        # before the POST - if it never appeared, no POST was attempted
        if ! grep -q "'confirmed': False" "$TMP/apply.out"; then
          rm -f "$S.posted"
          log "FATAL: make_transfers failed before any POST (the next attempt will retry)"
        else
          log "FATAL: make_transfers ended without confirmation and the server state could not be checked - no re-POST"
        fi
        exit 1
        ;;
    esac
  fi
  if (( apply_status != 0 )); then
    # the transfer step is done; only the lineup part failed - retry it alone,
    # and if that fails too still report, but leave .done unset so the next
    # attempt does lineup-only (.applied blocks a second POST)
    log "transfer step done but the lineup step failed; retrying lineup"
    PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal_set_lineup" --fpl_team_id "$FPL_TEAM_ID" --confirm | tee -a "$TMP/apply.out" || {
      notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: transfers applied but setting the lineup failed twice - will retry on the next attempt; set your starting 11 manually if it does not come through."
      LINEUP_FAILED=1
    }
  fi
else
  PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal_set_lineup" --fpl_team_id "$FPL_TEAM_ID" --confirm | tee "$TMP/apply.out"
fi

# --- step 10: server-side verification (warning-only; .applied already set) ---
# the logged-in my-team view is the only source that shows the upcoming
# gameweek's team and chip before the deadline
CURRENT_STEP="server-side verification"
TRANSFERS_OK=""; CHIP_STATUS=""
if (( ! DRY_RUN )); then
  VERIFY=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" verify \
    --gw "$GW" --team-id "$FPL_TEAM_ID" --chip "$CHIP" || echo "unknown unknown")
  read -r TRANSFERS_OK CHIP_STATUS <<<"$VERIFY"
  log "verification: transfers=$TRANSFERS_OK chip=$CHIP_STATUS"
  if [[ "$APPLY_MODE" == "transfers" && "$TRANSFERS_OK" != "ok" ]]; then
    notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: transfers POSTed but the team on the server does not match the suggestions (verification: ${TRANSFERS_OK}). CHECK YOUR TEAM on the FPL site now ($(mins_left)m to deadline)."
  fi
fi

# --- step 11: report ----------------------------------------------------------
CURRENT_STEP="report"
DL_LOCAL=$(TZ=Europe/Stockholm date -d "@$DEADLINE" '+%a %H:%M')
TG_ARGS=(--gw "$GW" --team-id "$FPL_TEAM_ID" --mode "($APPLY_MODE)" --chip "$CHIP")
(( DRY_RUN )) && TG_ARGS+=(--dry-run)
[[ -n "$PRED_PTS" ]] && TG_ARGS+=(--pred "$PRED_PTS")
[[ -n "$PRE_BANK" ]] && TG_ARGS+=(--bank-before "$PRE_BANK")
[[ -s "$TMP/pre_picks.json" ]] && TG_ARGS+=(--pre-picks "$TMP/pre_picks.json")
# chip stock visibility: "(N unused, M GWs left in half)" on every summary
SLACK_INFO=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" slack --gw "$GW" 2>/dev/null || echo "")
CHIPS_LEFT=""; GWS_LEFT=""; SLACK=""
if [[ -n "$SLACK_INFO" ]]; then
  read -r CHIPS_LEFT GWS_LEFT SLACK <<<"$SLACK_INFO"
  (( CHIPS_LEFT > 0 )) && TG_ARGS+=(--chip-note "(${CHIPS_LEFT} unused, ${GWS_LEFT} GWs left in half)")
fi
if MSG=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" telegram "${TG_ARGS[@]}"); then
  notify_html_soft "${PREFIX}${MSG}"
else
  SUMMARY=$(python3 -c "import json;print(json.load(open('$TMP/check.json'))['text'])" 2>/dev/null || true)
  notify_soft "${PREFIX}AIrsenal GW${GW} done ($APPLY_MODE)
$SUMMARY"
fi

if [[ "$CHIP" != "none" ]]; then
  case "$CHIP_STATUS" in
    confirmed)
      notify_soft "${PREFIX}AIrsenal GW${GW}: ${CHIP} is ACTIVE on your team (verified). Nothing to do."
      "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" confirm --gw "$GW"
      ;;
    unknown)
      notify_soft "${PREFIX}AIrsenal GW${GW}: ${CHIP} was decided but its status could not be verified (FPL API error). Make sure it is active on fantasy.premierleague.com before ${DL_LOCAL} ($(mins_left)m left)."
      ;;
    *)
      if [[ "$CHIP" == "wildcard" || "$CHIP" == "free_hit" ]]; then
        notify_soft "${PREFIX}ACTION REQUIRED - AIrsenal GW${GW}: ${CHIP} was requested via the API but NOT confirmed on your team. Activate it manually on fantasy.premierleague.com before ${DL_LOCAL} ($(mins_left)m left) or the transfers will cost a points hit."
      else
        REASONING=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" reasoning --gw "$GW" || true)
        LINEUP_NOTE="Transfers and lineup are already applied."
        (( LINEUP_FAILED )) && LINEUP_NOTE="Transfers are applied; the lineup is NOT set yet (will be retried)."
        notify_soft "${PREFIX}ACTION REQUIRED - AIrsenal GW${GW}: activate ${CHIP^^} on fantasy.premierleague.com before ${DL_LOCAL} ($(mins_left)m left).${REASONING:+ $REASONING} ${LINEUP_NOTE}"
      fi
      ;;
  esac
fi

# anti-hoarding backstop: chips about to expire and research still holding
if [[ "$CHIP" == "none" && -n "$SLACK" ]] && (( CHIPS_LEFT > 0 && SLACK <= 2 )); then
  notify_soft "${PREFIX}AIrsenal GW${GW} CHIP WARNING: ${CHIPS_LEFT} chip(s) unused with only ${GWS_LEFT} gameweek(s) left in this half, and research chose none again. Chips expire worthless - consider playing one manually on fantasy.premierleague.com."
fi

(( DRY_RUN || LINEUP_FAILED )) || touch "$S.done"
log "=== GW${GW} run complete ==="
