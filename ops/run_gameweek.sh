#!/usr/bin/env bash
#
# Orchestrates one gameweek run: pull, chip research, AIrsenal pipeline,
# apply transfers + lineup, verify server-side, Telegram report.
#
# Usage: run_gameweek.sh <gameweek> <deadline_epoch> [--dry-run]
#   --dry-run: full chain but no POSTs to the FPL API, no run markers,
#              Telegram messages prefixed [DRY RUN].
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

# single active run; a second invocation exits immediately
exec 9>"$RUN_DIR/airsenal.lock"
if ! flock -n 9; then
  log "another run holds the lock, exiting"
  # a blocked launch is not a real try - refund the attempt it consumed
  a=$(cat "$S.attempts" 2>/dev/null || echo 1)
  echo $(( a > 0 ? a - 1 : 0 )) > "$S.attempts"
  if [[ ! -f "$S.lockblocked" ]]; then
    notify_soft "${PREFIX}AIrsenal GW${GW}: launch blocked - another run still holds the lock (possibly a hung earlier run). Will keep retrying; check the Pi if this repeats."
    touch "$S.lockblocked"
  fi
  exit 0
fi
# unconditional (incl. dry runs) so the scheduler's liveness check sees us
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

log "checking FPL login..."
LOGIN_OK=0
for login_try in 1 2 3; do
  if "$PY" -c "
from airsenal.framework.data_fetcher import FPLDataFetcher
f = FPLDataFetcher()
f.login()
raise SystemExit(0 if f.logged_in else 1)
"; then
    LOGIN_OK=1
    break
  fi
  log "login attempt $login_try failed (FPL login flow is transiently flaky), retrying..."
  sleep 20
done
(( LOGIN_OK )) || { log "FATAL: FPL login failed 3x - credentials wrong or FPL changed their login flow"; exit 1; }

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

# --- step 5: chip research ----------------------------------------------------
CURRENT_STEP="chip research"
if [[ -f "$S.applied" || -f "$S.posted" ]]; then
  # transfers already POSTed on a previous attempt: reuse that decision,
  # never research a second chip for the same gameweek
  CHIP=$(python3 "$OPS_LIB_DIR/helpers/gw_context.py" chip-for-gw --gw "$GW" || echo "none")
  log "reusing recorded chip decision: $CHIP (transfers already POSTed)"
else
  CHIP=$("$OPS_LIB_DIR/chip_research.sh" "$GW" "$DEADLINE" || echo "none")
  CHIP=${CHIP##*$'\n'}  # last line only
fi
log "chip decision: $CHIP"

# --- step 6: predictions ------------------------------------------------------
CURRENT_STEP="airsenal_run_prediction"
if (( $(mins_left) < 60 )); then
  notify_soft "${PREFIX}AIrsenal GW${GW}: only $(mins_left)m to deadline before predictions - run may not finish. Consider making transfers manually."
fi
"$VENV_BIN/airsenal_run_prediction" --weeks_ahead "${WEEKS_AHEAD:-3}"

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
NEW_ROWS=$(python3 -c "import json;print(json.load(open('$TMP/check.json'))['new_rows'])")
N_TRANSFERS=$(python3 -c "import json;print(json.load(open('$TMP/check.json'))['n_transfers'])")
SUMMARY=$(python3 -c "import json;print(json.load(open('$TMP/check.json'))['text'])")
log "new_rows=$NEW_ROWS n_transfers=$N_TRANSFERS"

# --- step 9: apply ------------------------------------------------------------
CURRENT_STEP="apply transfers/lineup"
if (( $(mins_left) < 3 )); then
  log "FATAL: less than 3 minutes to deadline, refusing to POST"
  exit 1
fi
PRE_COUNT=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" transfer-count --gw "$GW" --team-id "$FPL_TEAM_ID")
APPLY_MODE="transfers"
[[ "$NEW_ROWS" != "True" ]] && APPLY_MODE="lineup-only (stale suggestions guard)"
(( N_TRANSFERS == 0 )) && [[ "$CHIP" == "none" ]] && APPLY_MODE="lineup-only (no transfers suggested)"
if [[ -f "$S.applied" ]]; then
  APPLY_MODE="lineup-only (transfers already applied)"
elif [[ -f "$S.posted" ]]; then
  # a previous POST attempt died before we saw its outcome: never re-POST
  APPLY_MODE="lineup-only (previous POST attempt unverified)"
  notify_soft "${PREFIX}AIrsenal GW${GW}: a previous transfer POST was interrupted before confirmation - doing lineup only. CHECK YOUR TRANSFERS on the FPL site."
fi
log "apply mode: $APPLY_MODE"

if (( DRY_RUN )); then
  log "dry run: skipping POSTs. Would do: $APPLY_MODE"
elif [[ "$APPLY_MODE" == "transfers" ]]; then
  # .posted is written BEFORE the POST and never removed: if the process dies
  # mid-POST, the retry does lineup-only + warning instead of a double POST
  touch "$S.posted"
  set +e
  "$VENV_BIN/airsenal_make_transfers" --fpl_team_id "$FPL_TEAM_ID" --confirm 2>&1 | tee "$TMP/apply.out"
  apply_status=${PIPESTATUS[0]}
  set -e
  if grep -q "Transfers made!" "$TMP/apply.out"; then
    # POST confirmed client-side: set the never-re-POST marker immediately,
    # before any fallible verification
    touch "$S.applied"
  fi
  if (( apply_status != 0 )); then
    if [[ -f "$S.applied" ]]; then
      # transfers went through; only the lineup part failed - retry it alone
      log "transfers POSTed but the lineup step failed; retrying lineup"
      "$VENV_BIN/airsenal_set_lineup" --fpl_team_id "$FPL_TEAM_ID" --confirm | tee -a "$TMP/apply.out" \
        || notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: transfers applied but setting the lineup failed - set your starting 11 manually on the FPL site."
    else
      log "FATAL: make_transfers failed before the POST (no 'Transfers made!')"
      exit 1
    fi
  elif (( N_TRANSFERS > 0 )) && [[ ! -f "$S.applied" ]]; then
    log "FATAL: expected transfers but 'Transfers made!' not seen"
    exit 1
  fi
else
  "$VENV_BIN/airsenal_set_lineup" --fpl_team_id "$FPL_TEAM_ID" --confirm | tee "$TMP/apply.out"
fi

# --- step 10: server-side verification (warning-only; .applied already set) ---
CURRENT_STEP="server-side verification"
CHIP_STATUS=""
if (( ! DRY_RUN )) && [[ "$APPLY_MODE" == "transfers" && "$N_TRANSFERS" -gt 0 ]]; then
  if ! "$PY" "$OPS_LIB_DIR/helpers/post_summary.py" verify \
      --gw "$GW" --team-id "$FPL_TEAM_ID" --expected "$N_TRANSFERS" \
      --pre-count "$PRE_COUNT" --chip "$CHIP" > "$TMP/verify.json"; then
    notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: transfers POSTed but server-side verification failed. CHECK YOUR TEAM on the FPL site now ($(mins_left)m to deadline)."
  fi
  CHIP_STATUS=$(python3 -c "import json;print(json.load(open('$TMP/verify.json')).get('chip_status',''))" 2>/dev/null || true)
fi

# --- step 11: report ----------------------------------------------------------
CURRENT_STEP="report"
DL_LOCAL=$(TZ=Europe/Stockholm date -d "@$DEADLINE" '+%a %H:%M')
TG_ARGS=(--gw "$GW" --team-id "$FPL_TEAM_ID" --mode "($APPLY_MODE)")
(( DRY_RUN )) && TG_ARGS+=(--dry-run)
[[ -n "$PRED_PTS" ]] && TG_ARGS+=(--pred "$PRED_PTS (${WEEKS_AHEAD:-3}gw)")
if MSG=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" telegram "${TG_ARGS[@]}"); then
  notify_html_soft "${PREFIX}${MSG}"
else
  notify_soft "${PREFIX}AIrsenal GW${GW} done ($APPLY_MODE)
$SUMMARY"
fi

if [[ "$CHIP" == "triple_captain" || "$CHIP" == "bench_boost" ]]; then
  REASONING=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" reasoning --gw "$GW" || true)
  notify_soft "${PREFIX}ACTION REQUIRED - AIrsenal GW${GW}: activate ${CHIP^^} on fantasy.premierleague.com before ${DL_LOCAL} ($(mins_left)m left). ${REASONING} Transfers and lineup are already applied - do NOT change the lineup after activating."
elif [[ "$CHIP" == "wildcard" || "$CHIP" == "free_hit" ]]; then
  if [[ "$CHIP_STATUS" == "confirmed" ]]; then
    notify_soft "${PREFIX}AIrsenal GW${GW}: ${CHIP} PLAYED via the API and verified on your team."
    "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" confirm --gw "$GW"
  else
    notify_soft "${PREFIX}ACTION REQUIRED - AIrsenal GW${GW}: ${CHIP} was requested via the API but NOT confirmed on your team. Activate it manually on fantasy.premierleague.com before ${DL_LOCAL} ($(mins_left)m left) or the transfers will cost a points hit."
  fi
fi

(( DRY_RUN )) || touch "$S.done"
log "=== GW${GW} run complete ==="
