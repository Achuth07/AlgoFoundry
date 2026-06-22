#!/usr/bin/env bash
# Start the AlgoFoundry bridge + GUI.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present
if [ -f .env ]; then set -a; . ./.env; set +a; fi

HOST="${ALGOFOUNDRY_HOST:-127.0.0.1}"
PORT="${ALGOFOUNDRY_PORT:-8000}"

exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
