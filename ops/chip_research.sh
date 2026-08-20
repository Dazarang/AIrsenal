#!/usr/bin/env bash
#
# Decides whether to play a chip this gameweek using a headless Claude run
# with web search. Prints the final decision as the LAST line on stdout:
# wildcard | free_hit | triple_captain | bench_boost | none
# All logging goes to stderr. Research failures never abort the pipeline -
# the safe default is "none".
#
# Usage: chip_research.sh <gameweek> <deadline_epoch> [--dry-run]
#   --dry-run: no decision recorded, Telegram messages prefixed [DRY RUN]
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GW="${1:?gameweek required}"
DEADLINE="${2:?deadline epoch required}"
DRY_RUN=0
[[ "${3:-}" == "--dry-run" ]] && DRY_RUN=1
PREFIX=""
(( DRY_RUN )) && PREFIX="[DRY RUN] "
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# the gameweek's decision: "record" replaces any earlier unconfirmed one,
# "clear" drops it - so a re-run that ends without a chip (including every
# failure path) cannot leave a stale chip behind for the reminder / retry paths
record() {
  (( DRY_RUN )) && return 0
  "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" record \
    --gw "$GW" --season "$SEASON" --chip "$1" \
    --confidence "${2:-0}" --reasoning="${3:-}"
}
clear_decision() {
  (( DRY_RUN )) && return 0
  "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" clear --gw "$GW"
}

fail() {
  echo "chip research: $1" >&2
  clear_decision || echo "chip research: could not clear the recorded decision" >&2
  notify_soft "${PREFIX}AIrsenal GW${GW}: chip research skipped - $1. Continuing without a chip." >&2
  echo "none"
  exit 0
}

[[ -x "$CLAUDE_BIN" ]] || fail "claude binary not found at $CLAUDE_BIN"
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && ! -f "$HOME/.claude/.credentials.json" ]]; then
  fail "no claude credentials (CLAUDE_CODE_OAUTH_TOKEN unset and no stored login)"
fi

"$PY" "$OPS_LIB_DIR/helpers/gw_context.py" context \
  --gw "$GW" --deadline-epoch "$DEADLINE" > "$TMPD/ctx.json" \
  || fail "context generation failed"

read -r SEASON SEASON_NAME CHIPS_LEFT GWS_LEFT SLACK ACTIVE < <(python3 -c "
import json
d = json.load(open('$TMPD/ctx.json'))
a = d['chips_available']
g = d.get('gameweeks_left_in_half') or 99
print(d['season'], d['season_name'], len(a), g, g - len(a) if a else 99, d.get('active_chip') or '-')
") || fail "context JSON unreadable"

if [[ "$ACTIVE" != "-" ]]; then
  # a chip is already active on the FPL site (activated by hand, or by an
  # earlier attempt): optimize for it instead of researching another
  echo "chip research: $ACTIVE is already active on the team - no research needed" >&2
  record "$ACTIVE" 1 "already active on the FPL site" || fail "recording the active chip failed"
  echo "$ACTIVE"
  exit 0
fi
if (( CHIPS_LEFT == 0 )); then
  echo "chip research: no chip available this gameweek - nothing to research" >&2
  clear_decision || fail "clearing the decision failed"
  echo "none"
  exit 0
fi

{
  # explicit time/gameweek anchor FIRST - the model must never have to guess
  printf '# You are deciding for: GAMEWEEK %s of the %s Premier League season\n' "$GW" "$SEASON_NAME"
  printf 'Today is %s. The transfer deadline for this gameweek is %s.\n' \
    "$(date -u '+%A %d %B %Y, %H:%M UTC')" \
    "$(date -u -d "@$DEADLINE" '+%A %d %B %Y, %H:%M UTC')"
  printf 'All research and every conclusion must target exactly this gameweek and season - include "%s" and "gameweek %s" in your web searches.\n\n' "$SEASON_NAME" "$GW"
  if (( SLACK <= 2 )); then
    printf 'ESCALATION: you still hold %s chip(s) with only %s gameweek(s) left in this half (slack %s). Chips not played before the half ends are forfeited - worth exactly zero. Holding is now the RISKY choice: map each remaining chip to the best remaining week, and if this week is it, or no clearly better week remains, play.\n\n' "$CHIPS_LEFT" "$GWS_LEFT" "$SLACK"
  fi
  cat "$OPS_LIB_DIR/chip_prompt.md"
  printf '\n## Context data (JSON)\n\n'
  cat "$TMPD/ctx.json"
} > "$TMPD/prompt.txt"

# persist research artifacts for auditability (prompt, envelope, decision)
CHIP_LOG_DIR="$LOG_DIR/chip"
mkdir -p "$CHIP_LOG_DIR"
RESEARCH_TS=$(date +%Y%m%dT%H%M%S)
cp "$TMPD/prompt.txt" "$CHIP_LOG_DIR/gw${GW}-${RESEARCH_TS}.prompt.txt"

# runaway stop, not schedule pressure: up to 40 min per attempt when the
# window allows, shrinking so that research never eats into the ~30 min the
# rest of the pipeline needs (a retry attempt starts with less of the window)
BUDGET=$(( (DEADLINE - $(date +%s) - 30 * 60) / 2 ))
(( BUDGET < 300 )) && fail "not enough time left before the deadline for research"
(( BUDGET > 2400 )) && BUDGET=2400

echo "chip research: running claude (opus @ max, up to ${BUDGET}s per attempt)..." >&2
CLAUDE_OK=0
for attempt in 1 2; do
  # prompt via stdin, not argv (context JSON can grow; argv has ARG_MAX limits)
  # research never needs these secrets - keep them out of the headless env
  if timeout -k 60 "$BUDGET" env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID -u FPL_PASSWORD -u FPL_LOGIN "$CLAUDE_BIN" -p \
      --model opus --effort max \
      --output-format json \
      --dangerously-skip-permissions \
      --max-turns 100 \
      < "$TMPD/prompt.txt" > "$TMPD/claude.json" 2>"$TMPD/claude.err"; then
    CLAUDE_OK=1
    break
  fi
  echo "chip research: claude attempt $attempt failed:" >&2
  cat "$TMPD/claude.err" >&2
  (( attempt == 1 )) && sleep 60
done
(( CLAUDE_OK )) || fail "claude run failed twice or timed out"
cp "$TMPD/claude.json" "$CHIP_LOG_DIR/gw${GW}-${RESEARCH_TS}.claude.json"

# how much live research actually happened? (0 searches = training-data-only)
# searches run by tool subagents are only counted under modelUsage, per model;
# usage.server_tool_use covers the main model alone
SEARCHES=$(python3 -c "
import json
d = json.load(open('$TMPD/claude.json'))
by_model = sum(int(m.get('webSearchRequests') or 0) for m in (d.get('modelUsage') or {}).values())
u = d.get('usage', {}).get('server_tool_use', {})
direct = int(u.get('web_search_requests') or 0) + int(u.get('web_fetch_requests') or 0)
print(max(by_model, direct))
" 2>/dev/null || echo 0)
echo "chip research: $SEARCHES web search/fetch requests" >&2

# claude's JSON envelope -> result text -> first JSON object inside it
python3 - "$TMPD/claude.json" > "$TMPD/decision.json" <<'EOF' || fail "could not parse claude output"
import json, sys

with open(sys.argv[1]) as f:
    result = json.load(f).get("result", "")
dec = json.JSONDecoder()
for i, ch in enumerate(result):
    if ch == "{":
        try:
            obj, _ = dec.raw_decode(result[i:])
        except ValueError:
            continue
        if "play_chip" in obj:
            json.dump(obj, sys.stdout)
            sys.exit(0)
raise SystemExit(1)
EOF

cp "$TMPD/decision.json" "$CHIP_LOG_DIR/gw${GW}-${RESEARCH_TS}.decision.json"

CHIP=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" validate \
  --context "$TMPD/ctx.json" < "$TMPD/decision.json") || fail "decision validation failed"

# a decision to PLAY a chip that did almost no live research is not trustworthy
if [[ "$CHIP" != "none" ]] && (( SEARCHES < 2 )); then
  notify_soft "${PREFIX}AIrsenal GW${GW}: chip research suggested ${CHIP} but performed only ${SEARCHES} web lookups (training data risk) - rejecting the decision, no chip will be played." >&2
  echo "chip research: rejected $CHIP (${SEARCHES} lookups)" >&2
  CHIP="none"
fi

if [[ "$CHIP" == "none" ]]; then
  clear_decision || fail "clearing the decision failed"
else
  read -r CONF REASONING < <(python3 -c "
import json
d = json.load(open('$TMPD/decision.json'))
print(d.get('confidence', 0), str(d.get('reasoning', '')).replace('\n', ' '))
") || { CONF=0; REASONING=""; }
  record "$CHIP" "$CONF" "$REASONING" \
    || fail "recording the decision failed (a different chip is already confirmed?)"
  echo "chip research: recorded decision $CHIP (confidence $CONF)" >&2
fi

echo "$CHIP"
