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

# airsenal prints through Rich, which wraps at 80 columns when not on a
# terminal: keep each line whole (and uncoloured) for the greps below
export COLUMNS=200 NO_COLOR=1

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
if ! "$HOME/.local/bin/uv" sync --frozen --no-dev; then
  notify_soft "${PREFIX}AIrsenal GW${GW}: uv sync failed, running with existing venv."
fi

# --- step 1: pre-flights ------------------------------------------------------
CURRENT_STEP="pre-flight checks"
: "${FPL_TEAM_ID:?FPL_TEAM_ID not set}"
: "${FPL_LOGIN:?FPL_LOGIN not set}"
: "${FPL_PASSWORD:?FPL_PASSWORD not set}"

# exit 3 = empty DB; any other failure (import error, a pre-v2 schema without
# transaction.counts_as_transfer, a locked file) has its own message
DB_CHECK=0
"$PY" -c "
from sqlalchemy import select
from airsenal.db.models import Player, Transaction
from airsenal.db.session import get_session
import sys
s = get_session()
s.scalars(select(Transaction)).first()
sys.exit(0 if s.scalars(select(Player)).first() else 3)
" || DB_CHECK=$?
case $DB_CHECK in
  0) ;;
  3) log "FATAL: database is empty - run airsenal db create"; exit 1 ;;
  *) log "FATAL: database check failed (error above) - a pre-v2 DB needs airsenal db create --clean"; exit 1 ;;
esac

# the login flow is transiently flaky; FPLAuth.login retries internally
log "checking FPL login..."
"$PY" -c "
from airsenal.remote.fpl_api import FPLDataFetcher
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
CURRENT_STEP="airsenal db update"
"$VENV_BIN/airsenal" db update --fpl-team-id "$FPL_TEAM_ID" | tee "$TMP/update_db.out"
if grep -q "Database is empty" "$TMP/update_db.out"; then
  log "FATAL: update_db reported empty database"
  exit 1
fi

# --- step 4: gameweek sanity --------------------------------------------------
CURRENT_STEP="gameweek assertion"
"$PY" "$OPS_LIB_DIR/helpers/gw_context.py" assert-gw --gw "$GW"

# --- step 5: predictions (before chip research so the chip decision can use
# --- AIrsenal's own predicted points) -----------------------------------------
CURRENT_STEP="airsenal predict"
if (( $(mins_left) < 60 )); then
  notify_soft "${PREFIX}AIrsenal GW${GW}: only $(mins_left)m to deadline before predictions - run may not finish. Consider making transfers manually."
fi
"$VENV_BIN/airsenal" predict --n-gameweeks "${WEEKS_AHEAD:-3}"
# the weekly review grades this run's predictions; a later dry run or manual
# predict writes a newer tag for the same gameweeks
if (( ! DRY_RUN )); then
  "$PY" -c "
import sys
from airsenal.db.queries.tags import get_latest_prediction_tag
open(sys.argv[1], 'w').write(get_latest_prediction_tag())
" "$S.tag" || log "could not record the prediction tag (the review will look it up)"
fi

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
CURRENT_STEP="airsenal optimize transfers"
PRE_TS=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" snapshot)
CHIP_ARGS=()
case "$CHIP" in
  wildcard)       CHIP_ARGS=(--wildcard-gameweek "$GW") ;;
  free_hit)       CHIP_ARGS=(--free-hit-gameweek "$GW") ;;
  triple_captain) CHIP_ARGS=(--triple-captain-gameweek "$GW") ;;
  bench_boost)    CHIP_ARGS=(--bench-boost-gameweek "$GW") ;;
esac
"$VENV_BIN/airsenal" optimize transfers \
  --n-gameweeks "${WEEKS_AHEAD:-3}" \
  --num-thread "${NUM_THREAD:-3}" \
  --max-hit "${MAX_HIT:-8}" \
  --fpl-team-id "$FPL_TEAM_ID" \
  "${CHIP_ARGS[@]}" | tee "$TMP/optimization.out"
PRED_PTS=$(grep -m1 "Optimised Score:" "$TMP/optimization.out" | grep -o "[0-9.]*pts" || true)

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
  # the helper (= `airsenal apply transfers --yes`) writes .posted immediately
  # before the transfer POST and .applied as soon as it succeeded (or when
  # nothing needed POSTing): if the process dies mid-POST, the retry does
  # lineup-only + warning instead of a double POST, and no .posted means no
  # POST was ever sent. .posted is removed again below only when the server
  # shows that nothing was applied.
  apply_status=0
  PYTHONUNBUFFERED=1 "$PY" "$OPS_LIB_DIR/helpers/post_transfers.py" --team-id "$FPL_TEAM_ID" \
    --posted "$S.posted" --applied "$S.applied" || apply_status=$?
  if [[ ! -f "$S.applied" ]]; then
    # no client-side confirmation: ask the server (logged-in my-team view)
    # whether the suggested transfers are there before deciding about a retry
    VERIFY=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" verify \
      --gw "$GW" --team-id "$FPL_TEAM_ID" --chip none || echo "unknown unknown unknown")
    case "${VERIFY%% *}" in
      ok)
        log "no confirmation seen but the suggested transfers are on the server - treating as applied"
        touch "$S.applied"
        ;;
      mismatch)
        # nothing was applied: let the next attempt retry the transfers, but
        # still set a lineup on the current squad (independent and free)
        rm -f "$S.posted"
        log "applying transfers failed and nothing was applied (the next attempt will retry); setting the lineup anyway"
        PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal" apply lineup --fpl-team-id "$FPL_TEAM_ID" --yes || true
        log "FATAL: transfers not applied"
        exit 1
        ;;
      *)
        if [[ ! -f "$S.posted" ]]; then
          log "FATAL: applying transfers failed before any POST (the next attempt will retry)"
        else
          log "FATAL: the transfer POST ended without confirmation and the server state could not be checked - no re-POST"
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
    PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal" apply lineup --fpl-team-id "$FPL_TEAM_ID" --yes || {
      notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: transfers applied but setting the lineup failed twice - will retry on the next attempt; set your starting 11 manually if it does not come through."
      LINEUP_FAILED=1
    }
  fi
else
  PYTHONUNBUFFERED=1 "$VENV_BIN/airsenal" apply lineup --fpl-team-id "$FPL_TEAM_ID" --yes
fi

# --- step 10: server-side verification (warning-only; .applied already set) ---
# the logged-in my-team view is the only source that shows the upcoming
# gameweek's team and chip before the deadline
CURRENT_STEP="server-side verification"
TRANSFERS_OK=""; CHIP_STATUS=""; LINEUP_STATUS=""
if (( ! DRY_RUN )); then
  VERIFY=$("$PY" "$OPS_LIB_DIR/helpers/post_summary.py" verify \
    --gw "$GW" --team-id "$FPL_TEAM_ID" --chip "$CHIP" || echo "unknown unknown unknown")
  read -r TRANSFERS_OK CHIP_STATUS LINEUP_STATUS <<<"$VERIFY"
  log "verification: transfers=$TRANSFERS_OK chip=$CHIP_STATUS lineup=$LINEUP_STATUS"
  if [[ "$LINEUP_STATUS" == "mismatch" ]] && (( ! LINEUP_FAILED )); then
    notify_soft "${PREFIX}AIrsenal GW${GW} WARNING: the starting 11 or captain on the server is not the one AIrsenal set. CHECK YOUR LINEUP on the FPL site ($(mins_left)m to deadline)."
  fi
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
