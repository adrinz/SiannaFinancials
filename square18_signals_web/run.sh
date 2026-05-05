#!/usr/bin/env bash
# Launch the Sianna Financials web app.
#
# Usage:   ./run.sh           # dev (auto-reload) on http://127.0.0.1:8000
#          ./run.sh prod      # no reload
#          PORT=9000 ./run.sh # custom port
set -euo pipefail

cd "$(dirname "$0")"

: "${PORT:=8000}"
: "${HOST:=127.0.0.1}"

MODE="${1:-dev}"
EXTRA=()
if [[ "$MODE" == "dev" ]]; then
  EXTRA+=("--reload")
fi

# Prefer an already-active venv; otherwise use system python3.
PY="${PYTHON:-python3}"

echo ">> Sianna Financials web app"
echo ">> mode=$MODE  host=$HOST  port=$PORT"
if [[ -f .env ]]; then
  echo ">> loading .env (API keys / local overrides)"
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi
echo ">> open http://$HOST:$PORT/  (Ctrl+C to stop)"
echo

exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" ${EXTRA[@]+"${EXTRA[@]}"}
