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

# Send a Telegram message using the pquant bot credentials (read-only access
# to the pquant env file; that file is never modified). Truncates to the
# Telegram 4096-char limit. Returns nonzero on delivery failure.
notify() {
  local msg="$1"
  local token chat
  token=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$PQUANT_ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
  chat=$(grep -m1 '^TELEGRAM_CHAT_ID=' "$PQUANT_ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
  if [[ -z "$token" || -z "$chat" ]]; then
    echo "notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not found in $PQUANT_ENV_FILE" >&2
    return 1
  fi
  curl -sS --max-time 20 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -d chat_id="${chat}" --data-urlencode text="${msg:0:4000}" >/dev/null
}

# notify that must never kill the caller
notify_soft() {
  notify "$1" || echo "notify failed: $1" >&2
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}
