#!/usr/bin/env bash
# Start RefundClaimers on port 8015 (override with PORT=xxxx ./run.sh).
# If a previous RefundClaimers server still holds the port, it is stopped first.
# Any other program using the port is reported, never killed.
set -euo pipefail

PORT="${PORT:-8015}"
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Virtual environment missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

listener_pids() {
  if command -v lsof >/dev/null; then
    lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
  else
    ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true
  fi
}

is_ledger() {  # true if the PID is this app's uvicorn (worker or --reload parent)
  tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q "backend.main:app"
}

pids="$(listener_pids)"
if [[ -n "$pids" ]]; then
  stop=()
  for pid in $pids; do
    # With --reload the socket is also held by a spawned worker whose command
    # line doesn't name the app, so check the parent (the reloader) too.
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if is_ledger "$pid" || { [[ -n "$parent" ]] && is_ledger "$parent"; }; then
      stop+=("$pid")
      [[ -n "$parent" ]] && is_ledger "$parent" && stop+=("$parent")
    else
      echo "Port $PORT is used by another program:" >&2
      ps -o pid=,args= -p "$pid" >&2
      echo "Stop it, or run on a different port: PORT=8020 ./run.sh" >&2
      exit 1
    fi
  done
  mapfile -t stop < <(printf '%s\n' "${stop[@]}" | sort -u)
  echo "Stopping previous RefundClaimers server (PID ${stop[*]})..."
  kill "${stop[@]}" 2>/dev/null || true
  for _ in $(seq 1 50); do
    [[ -z "$(listener_pids)" ]] && break
    sleep 0.1
  done
  if [[ -n "$(listener_pids)" ]]; then
    kill -9 "${stop[@]}" 2>/dev/null || true
    sleep 0.5
  fi
fi

echo "RefundClaimers: http://127.0.0.1:$PORT"
exec .venv/bin/uvicorn backend.main:app --reload --port "$PORT"
