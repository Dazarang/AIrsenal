#!/usr/bin/env bash
#
# One-time (idempotent) Pi provisioning. Run ON the Pi from the repo root:
#   ./ops/install_pi.sh
# Prerequisites done manually first: repo cloned via the github-airsenal SSH
# alias, uv installed. See ops/README.md.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
DATA_ROOT="${AIRSENAL_DATA_ROOT:-$HOME/airsenal-data}"

chmod +x "$SCRIPT_DIR"/*.sh

echo "== directories =="
mkdir -p "$DATA_ROOT"/{logs/cron,run,state,backups,home}

echo "== env file =="
if [[ ! -f "$DATA_ROOT/env" ]]; then
  cp "$SCRIPT_DIR/env.example" "$DATA_ROOT/env"
  chmod 600 "$DATA_ROOT/env"
  echo "created $DATA_ROOT/env - FILL IN FPL_LOGIN, FPL_PASSWORD, CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
else
  echo "$DATA_ROOT/env already exists"
fi

echo "== python env (uv sync --frozen) =="
cd "$APP_DIR"
"$HOME/.local/bin/uv" sync --frozen

echo "== claude code cli =="
if [[ ! -x "$HOME/.local/bin/claude" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash
else
  echo "claude already installed: $("$HOME/.local/bin/claude" --version)"
fi

echo "== database =="
if [[ ! -f "$DATA_ROOT/home/data.db" ]]; then
  echo "database missing - after filling in the env file, run:"
  echo "  set -a; source $DATA_ROOT/env; set +a"
  echo "  .venv/bin/airsenal_setup_initial_db && .venv/bin/airsenal_update_db"
else
  echo "database present"
fi

echo
echo "Next steps: fill $DATA_ROOT/env, initialise the DB (above), run a dry"
echo "run (ops/README.md), then ./ops/install_pi_crontab.sh"
