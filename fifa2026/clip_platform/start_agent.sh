#!/usr/bin/env bash
# Start the @fifa → @ScrollandSoull auto promo agent in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export PYTHONUNBUFFERED=1
LOG="clip_platform/data/agent.log"
mkdir -p clip_platform/data
if pgrep -f "clip_platform/auto_promo_agent.py --loop" >/dev/null 2>&1; then
  echo "Auto promo agent already running — not starting a second copy"
  pgrep -fl "clip_platform/auto_promo_agent.py"
  exit 0
fi
echo "Starting auto promo agent … log: $LOG"
nohup python clip_platform/auto_promo_agent.py --loop >> "$LOG" 2>&1 &
echo "PID $! — tail -f $LOG"
