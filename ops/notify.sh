#!/usr/bin/env bash
#
# Manual Telegram send: ./ops/notify.sh "message"
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
notify "${1:?usage: notify.sh \"message\"}"
