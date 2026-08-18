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
[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] || fail "CLAUDE_CODE_OAUTH_TOKEN not set"

"$PY" "$OPS_LIB_DIR/helpers/gw_context.py" context \
  --gw "$GW" --deadline-epoch "$DEADLINE" > "$TMPD/ctx.json" \
  || fail "context generation failed"

{
  cat "$OPS_LIB_DIR/chip_prompt.md"
  printf '\n\n## Context data (JSON)\n\n'
  cat "$TMPD/ctx.json"
} > "$TMPD/prompt.txt"

echo "chip research: running claude (opus @ xhigh)..." >&2
CLAUDE_OK=0
for attempt in 1 2; do
  # prompt via stdin, not argv (context JSON can grow; argv has ARG_MAX limits)
  if timeout -k 30 720 "$CLAUDE_BIN" -p \
      --model opus --effort xhigh \
      --output-format json \
      --dangerously-skip-permissions \
      --max-turns 30 \
      < "$TMPD/prompt.txt" > "$TMPD/claude.json" 2>"$TMPD/claude.err"; then
    CLAUDE_OK=1
    break
  fi
  echo "chip research: claude attempt $attempt failed:" >&2
  cat "$TMPD/claude.err" >&2
  (( attempt == 1 )) && sleep 60
done
(( CLAUDE_OK )) || fail "claude run failed twice or timed out"

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

CHIP=$("$PY" "$OPS_LIB_DIR/helpers/gw_context.py" validate \
  --context "$TMPD/ctx.json" < "$TMPD/decision.json") || fail "decision validation failed"

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
