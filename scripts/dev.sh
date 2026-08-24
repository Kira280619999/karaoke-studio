#!/usr/bin/env bash
set -euo pipefail

KARAOKE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KARAOKE_ROOT"
KARAOKE_API_PORT="${KARAOKE_STUDIO_PORT:-8000}"

if command -v lsof >/dev/null 2>&1; then
  while lsof -nP -iTCP:"$KARAOKE_API_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
    KARAOKE_API_PORT=$((KARAOKE_API_PORT + 1))
  done
fi

for KARAOKE_COMMAND in uv pnpm ffmpeg ffprobe; do
  if ! command -v "$KARAOKE_COMMAND" >/dev/null 2>&1; then
    echo "Thiếu dependency bắt buộc: $KARAOKE_COMMAND" >&2
    exit 1
  fi
done

if [[ ! -x .venv/bin/uvicorn ]]; then
  uv sync
fi
if [[ ! -d node_modules ]]; then
  pnpm install
fi

KARAOKE_API_PID=""
KARAOKE_WEB_PID=""
cleanup() {
  if [[ -n "$KARAOKE_WEB_PID" ]]; then kill "$KARAOKE_WEB_PID" 2>/dev/null || true; fi
  if [[ -n "$KARAOKE_API_PID" ]]; then kill "$KARAOKE_API_PID" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

KARAOKE_STUDIO_PORT="$KARAOKE_API_PORT" .venv/bin/uvicorn karaoke_studio.api:app --app-dir backend --host 127.0.0.1 --port "$KARAOKE_API_PORT" &
KARAOKE_API_PID=$!
NEXT_PUBLIC_KARAOKE_API="http://127.0.0.1:$KARAOKE_API_PORT" pnpm run dev --host 127.0.0.1 &
KARAOKE_WEB_PID=$!

echo "Karaoke Studio: http://127.0.0.1:3000"
echo "Local API:      http://127.0.0.1:$KARAOKE_API_PORT"

while kill -0 "$KARAOKE_API_PID" 2>/dev/null && kill -0 "$KARAOKE_WEB_PID" 2>/dev/null; do
  sleep 1
done
exit 1
