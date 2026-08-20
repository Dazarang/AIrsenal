#!/usr/bin/env bash
#
# Shared helpers for AIrsenal Pi automation. Source this from every ops script.
# Expects the ops env file at $AIRSENAL_OPS_ENV (default ~/airsenal-data/env).

OPS_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(cd "$OPS_LIB_DIR/.." && pwd)

OPS_ENV="${AIRSENAL_OPS_ENV:-$HOME/airsenal-data/env}"
if [[ -f "$OPS_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OPS_ENV"
  set +a
fi

DATA_ROOT="${AIRSENAL_DATA_ROOT:-$HOME/airsenal-data}"
RUN_DIR="$DATA_ROOT/run"
LOG_DIR="$DATA_ROOT/logs/cron"
STATE_DIR="$DATA_ROOT/state"
BACKUP_DIR="$DATA_ROOT/backups"
CHIPS_STATE="$STATE_DIR/chips_state.json"
VENV_BIN="$APP_DIR/.venv/bin"
PY="$VENV_BIN/python"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$STATE_DIR" "$BACKUP_DIR"

# Send a Telegram message via the dedicated AIrsenal bot (@fplquant_bot).
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (the single recipient) come from the
# ops env file sourced above. Truncates to the Telegram 4096-char limit.
# Returns nonzero on delivery failure (incl. HTTP errors such as HTML parse
# rejections, via curl -f).
_send_telegram() {
  local msg="$1" mode="${2:-}"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set (expected in $OPS_ENV)" >&2
    return 1
  fi
  local args=(-d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode text="${msg:0:4000}")
  [[ -n "$mode" ]] && args+=(-d parse_mode="$mode")
  curl -sSf --max-time 20 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    "${args[@]}" >/dev/null
}

notify() { _send_telegram "$1"; }

# notify that must never kill the caller
notify_soft() {
  notify "$1" || echo "notify failed: $1" >&2
}

# HTML-formatted notify; falls back to tag-stripped plain text on rejection
notify_html_soft() {
  if ! _send_telegram "$1" HTML; then
    notify_soft "$(sed 's/<[^>]*>//g' <<<"$1")"
  fi
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}
