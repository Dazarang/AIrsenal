#!/usr/bin/env bash
#
# Cron entry point (every 15 min). Reads the next FPL deadline live from
# bootstrap-static and:
#   - fires run_gameweek.sh once, RUN_HOURS_BEFORE the deadline (max 2 attempts)
#   - sends a chip reminder REMIND_HOURS_BEFORE the deadline if a chip was
#     decided for this gameweek
#   - sends a post-mortem alert if a deadline passed without a completed run
#   - detects deadline moves (and season rollover reusing gameweek numbers)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BOOTSTRAP_URL="https://fantasy.premierleague.com/api/bootstrap-static/"
MAX_ATTEMPTS=2
now=$(date +%s)

# --- post-mortem for passed deadlines -----------------------------------------
# Runs FIRST and depends only on local marker files: it must fire even when
# the FPL API is down or returning garbage.
for df in "$RUN_DIR"/gw*.deadline; do
  [[ -e "$df" ]] || continue
  g=$(basename "$df" .deadline)
  g=${g#gw}
  d=$(cat "$df")
  if (( d < now )) && [[ ! -f "$RUN_DIR/gw${g}.done" && ! -f "$RUN_DIR/gw${g}.postmortem" ]]; then
    att=$(cat "$RUN_DIR/gw${g}.attempts" 2>/dev/null || echo 0)
    notify_soft "AIrsenal GW${g} POST-MORTEM: deadline passed without a completed run (attempts: ${att}). Check logs in $LOG_DIR and your team on the FPL site."
    touch "$RUN_DIR/gw${g}.postmortem"
  fi
done

# --- fetch + parse the next deadline (all failures non-fatal: retry next tick)
json=$(curl -sSf --max-time 30 "$BOOTSTRAP_URL") || {
  log "bootstrap-static fetch failed; will retry next tick"
  exit 0
}

# "<id> <deadline_iso>" for the next gameweek, or empty (pre/post season)
next=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
ev = [e for e in data.get("events", []) if e.get("is_next")]
if ev:
    print(ev[0]["id"], ev[0]["deadline_time"])
' <<<"$json") || { log "bootstrap-static parse failed; will retry next tick"; exit 0; }
[[ -z "$next" ]] && exit 0

gw=${next%% *}
deadline_iso=${next#* }
deadline_epoch=$(date -u -d "$deadline_iso" +%s) || { log "date parse failed for '$deadline_iso'"; exit 0; }
# round-trip assertion: a mis-parsed timezone here would shift the trigger
if [[ $(date -u -d "@$deadline_epoch" +%Y-%m-%dT%H:%M:%SZ) != "$deadline_iso" ]]; then
  if [[ ! -f "$RUN_DIR/parse_mismatch.notified" ]]; then
    notify_soft "AIrsenal scheduler: deadline format changed ('$deadline_iso' does not round-trip) - scheduling is SUSPENDED until this is fixed."
    touch "$RUN_DIR/parse_mismatch.notified"
  fi
  exit 0
fi

secs_left=$((deadline_epoch - now))
S="$RUN_DIR/gw${gw}"

# --- record deadline + detect moves / season rollover -------------------------
if [[ -f "$S.deadline" ]]; then
  prev_deadline=$(cat "$S.deadline")
  if [[ "$prev_deadline" != "$deadline_epoch" ]]; then
    echo "$deadline_epoch" > "$S.deadline"
    if (( deadline_epoch - prev_deadline > 2592000 )); then
      # jumped > 30 days: a new season is reusing this gameweek number -
      # clear last season's markers or this gameweek never runs again
      rm -f "$S.done" "$S.attempts" "$S.started" "$S.applied" "$S.posted" \
        "$S.postmortem" "$S.reminded" "$S.moved"
      # and at GW1 every recorded chip decision is last season's
      (( gw == 1 )) && echo '{"entries": []}' > "$CHIPS_STATE"
    else
      attempts=$(cat "$S.attempts" 2>/dev/null || echo 0)
      if (( attempts > 0 && deadline_epoch > prev_deadline + 1800 )) && [[ ! -f "$S.moved" ]]; then
        notify_soft "AIrsenal GW${gw}: deadline moved later (now $(date -d "@$deadline_epoch" '+%a %d %b %H:%M')). Transfers already applied; review manually if you want changes."
        touch "$S.moved"
      fi
    fi
  fi
else
  echo "$deadline_epoch" > "$S.deadline"
fi

(( secs_left <= 0 )) && exit 0

# --- chip reminder at T-REMIND_HOURS_BEFORE ---------------------------------
if (( secs_left <= REMIND_HOURS_BEFORE * 3600 )) && [[ ! -f "$S.reminded" && -f "$CHIPS_STATE" ]]; then
  chip=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" chip-for-gw --gw "$gw" || echo "none")
  if [[ -n "$chip" && "$chip" != "none" ]]; then
    mins_left=$((secs_left / 60))
    # ground truth: the logged-in my-team view (the public endpoints only show
    # a gameweek's chip after its deadline)
    api_chip=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" active-chip || echo "unknown")
    if [[ "$api_chip" == "$chip" ]]; then
      notify_soft "AIrsenal GW${gw} chip check: ${chip} is ACTIVE on your team. Nothing to do. (${mins_left}m to deadline)"
    elif [[ "$api_chip" == "unknown" ]]; then
      notify_soft "AIrsenal GW${gw} REMINDER: ${chip} was decided but its status could not be checked (FPL API error). Make sure it is active on fantasy.premierleague.com - ${mins_left}m left before the deadline."
    else
      notify_soft "AIrsenal GW${gw} REMINDER: ${chip} was decided but is NOT active yet. Activate it on fantasy.premierleague.com NOW - ${mins_left}m left before the deadline."
    fi
    touch "$S.reminded"
  fi
fi

# --- main run at T-RUN_HOURS_BEFORE ------------------------------------------
if (( secs_left <= RUN_HOURS_BEFORE * 3600 )); then
  [[ -f "$S.done" ]] && exit 0
  attempts=$(cat "$S.attempts" 2>/dev/null || echo 0)
  (( attempts >= MAX_ATTEMPTS )) && exit 0
  # a run is in progress while run_gameweek.sh (or a child it left behind)
  # holds the lock; unlike a recorded pid this survives reboots and pid reuse.
  # Only a real lock conflict (exit 99) means busy - any other failure of the
  # probe must not silently disable the scheduler
  flock -n -E 99 "$RUN_DIR/airsenal.lock" true && rc=0 || rc=$?
  (( rc == 99 )) && exit 0
  (( rc != 0 )) && log "lock probe failed (exit $rc) - launching anyway"
  echo $((attempts + 1)) > "$S.attempts"
  log "firing run_gameweek for GW${gw} (attempt $((attempts + 1)), ${secs_left}s to deadline)"
  setsid nohup "$OPS_LIB_DIR/run_gameweek.sh" "$gw" "$deadline_epoch" \
    >> "$LOG_DIR/gw${gw}-launch.log" 2>&1 < /dev/null &
fi
