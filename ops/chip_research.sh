#!/usr/bin/env bash
#
# Decides whether to play a chip this gameweek using a headless Claude run
# with web search. Prints the final decision as the LAST line on stdout:
# wildcard | free_hit | triple_captain | bench_boost | none
# All logging goes to stderr. Research failures never abort the pipeline -
# the safe default is "none".
#
# Usage: chip_research.sh <gameweek> <deadline_epoch>
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GW="${1:?gameweek required}"
DEADLINE="${2:?deadline epoch required}"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

fail() {
  echo "chip research: $1" >&2
  notify_soft "AIrsenal GW${GW}: chip research skipped - $1. Continuing without a chip." >&2
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

SEASON_NAME=$(python3 -c "import json;print(json.load(open('$TMPD/ctx.json'))['season_name'])")
{
  # explicit time/gameweek anchor FIRST - the model must never have to guess
  printf '# You are deciding for: GAMEWEEK %s of the %s Premier League season\n' "$GW" "$SEASON_NAME"
  printf 'Today is %s. The transfer deadline for this gameweek is %s.\n' \
    "$(date -u '+%A %d %B %Y, %H:%M UTC')" \
    "$(date -u -d "@$DEADLINE" '+%A %d %B %Y, %H:%M UTC')"
  printf 'All research and every conclusion must target exactly this gameweek and season - include "%s" and "gameweek %s" in your web searches.\n\n' "$SEASON_NAME" "$GW"
  cat "$OPS_LIB_DIR/chip_prompt.md"
  printf '\n## Context data (JSON)\n\n'
  cat "$TMPD/ctx.json"
} > "$TMPD/prompt.txt"

# persist research artifacts for auditability (prompt, envelope, decision)
CHIP_LOG_DIR="$LOG_DIR/chip"
mkdir -p "$CHIP_LOG_DIR"
RESEARCH_TS=$(date +%Y%m%dT%H%M%S)
cp "$TMPD/prompt.txt" "$CHIP_LOG_DIR/gw${GW}-${RESEARCH_TS}.prompt.txt"

echo "chip research: running claude (opus @ max)..." >&2
CLAUDE_OK=0
for attempt in 1 2; do
  # prompt via stdin, not argv (context JSON can grow; argv has ARG_MAX limits)
  # generous limits: the deadline window is 3h and the rest of the pipeline
  # needs ~10 min, so research is never the bottleneck - these are runaway
  # stops, not schedule pressure
  if timeout -k 60 2400 "$CLAUDE_BIN" -p \
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
SEARCHES=$(python3 -c "
import json
u = json.load(open('$TMPD/claude.json')).get('usage', {}).get('server_tool_use', {})
print(int(u.get('web_search_requests', 0) or 0) + int(u.get('web_fetch_requests', 0) or 0))
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
  notify_soft "AIrsenal GW${GW}: chip research suggested ${CHIP} but performed only ${SEARCHES} web lookups (training data risk) - rejecting the decision, no chip will be played." >&2
  echo "chip research: rejected $CHIP (${SEARCHES} lookups)" >&2
  CHIP="none"
fi

if [[ "$CHIP" != "none" ]]; then
  SEASON=$(python3 -c "import json;print(json.load(open('$TMPD/ctx.json'))['season'])")
  REASONING=$(python3 -c "import json;print(json.load(open('$TMPD/decision.json')).get('reasoning',''))")
  CONF=$(python3 -c "import json;print(json.load(open('$TMPD/decision.json')).get('confidence',0))")
  "$PY" "$OPS_LIB_DIR/helpers/gw_context.py" record \
    --gw "$GW" --season "$SEASON" --chip "$CHIP" \
    --confidence "$CONF" --reasoning "$REASONING" \
    || fail "recording the decision failed (a confirmed chip already exists?)"
  echo "chip research: recorded decision $CHIP (confidence $CONF)" >&2
fi

echo "$CHIP"
