#!/usr/bin/env bash
# Starts the whole Kora demo with one command.
#
#   ./demo.sh            clean start: archives old bookings, calls and callbacks
#   ./demo.sh --keep     keep the data from the last demo
#   ./demo.sh --share    also create a temporary public link (needs cloudflared)
#
# The agent runs in LiveKit's "dev" mode on purpose: "start" (production) mode
# refuses new calls when the computer's CPU is above 70%, which can happen on a
# laptop that is also screen-sharing a video call.
#
# Press Ctrl-C to stop everything.

set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
DB="${BOOKINGS_DB:-data/bookings.db}"
LOGS="data/logs"
KEEP=0; SHARE=0; MODE="dev"
for arg in "$@"; do
  case "$arg" in
    --keep)  KEEP=1 ;;
    --share) SHARE=1 ;;
    *) echo "Unknown option: $arg (use --keep or --share)"; exit 1 ;;
  esac
done

say()  { printf "\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\033[31m%s\033[0m\n" "$*"; exit 1; }

# --- Checks before starting -------------------------------------------------
[ -f .env.local ] || fail "Missing .env.local (copy .env.example and add your keys)."
command -v uv >/dev/null || fail "uv is not installed."
grep -Eq '^ADMIN_PASSWORD=.+' .env.local || say "Note: ADMIN_PASSWORD is empty, so the admin page is switched off."
if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
  fail "Port $PORT is already in use. Stop the other server (or run: PORT=8001 ./demo.sh)."
fi
if [ "$SHARE" = 1 ] && ! command -v cloudflared >/dev/null; then
  fail "--share needs cloudflared. Install it with: brew install cloudflared"
fi
curl -s --max-time 5 https://api.anthropic.com >/dev/null || fail "No internet connection. The agent needs it."

mkdir -p "$LOGS" data/old

# --- Fresh data for this demo -----------------------------------------------
if [ "$KEEP" = 0 ] && [ -f "$DB" ]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  for f in "$DB" "$DB-wal" "$DB-shm"; do
    [ -f "$f" ] && mv "$f" "data/old/$(basename "$f").$stamp"
  done
  say "Archived the previous demo's data to data/old/ (bookings, calls, callbacks)."
fi

# --- Start everything ---------------------------------------------------------
PIDS=()
STOPPED=0
cleanup() {
  [ "$STOPPED" = 1 ] && return
  STOPPED=1
  echo
  say "Stopping the demo..."
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    pkill -TERM -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  say "Stopped. Logs are in $LOGS/."
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# Keep the Mac awake while the demo runs.
caffeinate -dims -w $$ & PIDS+=($!)

say "Starting the agent..."
uv run agent.py "$MODE" >"$LOGS/agent.log" 2>&1 & PIDS+=($!)
AGENT_PID=$!

say "Starting the web server on port $PORT..."
uv run uvicorn server:app --port "$PORT" >"$LOGS/server.log" 2>&1 & PIDS+=($!)
SERVER_PID=$!

# Wait for both to be ready (up to 60 seconds).
ready_server=0; ready_agent=0
for _ in $(seq 1 60); do
  kill -0 "$AGENT_PID" 2>/dev/null || { tail -20 "$LOGS/agent.log"; fail "The agent stopped. Its last log lines are above."; }
  kill -0 "$SERVER_PID" 2>/dev/null || { tail -20 "$LOGS/server.log"; fail "The web server stopped. Its last log lines are above."; }
  [ "$ready_server" = 0 ] && curl -s -o /dev/null "http://localhost:$PORT/" && ready_server=1
  [ "$ready_agent" = 0 ] && grep -qi "registered worker" "$LOGS/agent.log" && ready_agent=1
  [ "$ready_server" = 1 ] && [ "$ready_agent" = 1 ] && break
  sleep 1
done
[ "$ready_server" = 1 ] || fail "The web server did not start. See $LOGS/server.log."
if [ "$ready_agent" = 1 ]; then
  say "Agent connected to LiveKit."
else
  say "Warning: the agent has not confirmed it is connected yet. Check $LOGS/agent.log before calling."
fi

open "http://localhost:$PORT/admin"
open "http://localhost:$PORT/"

# --- Optional public link -----------------------------------------------------
if [ "$SHARE" = 1 ]; then
  say "Creating a temporary public link..."
  cloudflared tunnel --url "http://localhost:$PORT" >"$LOGS/tunnel.log" 2>&1 & PIDS+=($!)
  url=""
  for _ in $(seq 1 30); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1)"
    [ -n "$url" ] && break
    sleep 1
  done
  if [ -n "$url" ]; then
    say "Public link (works until you press Ctrl-C): $url"
    printf "%s" "$url" | pbcopy && say "Copied to your clipboard. Paste it in the meeting chat."
  else
    say "Could not create the public link. See $LOGS/tunnel.log. The local demo still works."
  fi
fi

echo
say "Demo ready:"
echo "  Demo clinic:  http://localhost:$PORT/"
echo "  Staff page:   http://localhost:$PORT/admin  (user: admin)"
echo "  Logs:         $LOGS/"
say "Press Ctrl-C to stop everything."

# Stay here until one of the services stops or Ctrl-C.
while kill -0 "$AGENT_PID" 2>/dev/null && kill -0 "$SERVER_PID" 2>/dev/null; do sleep 2; done
say "One of the services stopped unexpectedly. Check $LOGS/."
