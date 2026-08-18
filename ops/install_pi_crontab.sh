#!/usr/bin/env bash
#
# Install the AIrsenal scheduler cron line. APPEND-ONLY: existing crontab
# lines (pquant etc.) are never modified or removed.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
DATA_ROOT="${AIRSENAL_DATA_ROOT:-$HOME/airsenal-data}"

MARKER="# AIrsenal automation"
LINE="*/15 * * * * cd \"$APP_DIR\" && ./ops/check_deadline.sh >> \"$DATA_ROOT/logs/cron/scheduler.log\" 2>&1"

current=$(crontab -l 2>/dev/null || true)
if grep -qF "$MARKER" <<<"$current"; then
  echo "AIrsenal cron line already installed:"
  grep -A1 -F "$MARKER" <<<"$current"
  exit 0
fi

printf '%s\n\n%s\n%s\n' "$current" "$MARKER" "$LINE" | crontab -
echo "installed:"
echo "$LINE"
