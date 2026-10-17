#!/usr/bin/env bash
#
# Install the AIrsenal cron lines. APPEND-ONLY: existing crontab lines
# (pquant etc.) are never modified or removed, and a line whose marker is
# already present is skipped.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
DATA_ROOT="${AIRSENAL_DATA_ROOT:-$HOME/airsenal-data}"
LOG="$DATA_ROOT/logs/cron"

install_line() {
  local marker="$1" line="$2" current
  current=$(crontab -l 2>/dev/null || true)
  if grep -qF "$marker" <<<"$current"; then
    echo "already installed:"
    grep -A1 -F "$marker" <<<"$current"
    return 0
  fi
  printf '%s\n\n%s\n%s\n' "$current" "$marker" "$line" | crontab -
  echo "installed:"
  echo "$line"
}

install_line "# AIrsenal automation" \
  "*/15 * * * * cd \"$APP_DIR\" && ./ops/check_deadline.sh >> \"$LOG/scheduler.log\" 2>&1"
# Monday 04:00: gameweek deadlines fall later in the week, so an update never lands mid-run
install_line "# AIrsenal claude cli update" \
  "0 4 * * 1 \"$HOME/.local/bin/claude\" update >> \"$LOG/claude-update.log\" 2>&1"
