#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREVIEW_DIR="${BOOKGYM_PREVIEW_DIR:-$PROJECT_ROOT/.preview}"
SOURCE_DB="${BOOKGYM_PREVIEW_SOURCE_DB:-$PROJECT_ROOT/bookgym.db}"
PREVIEW_DB="$PREVIEW_DIR/bookgym-preview.db"
NEXT_DB="$PREVIEW_DIR/bookgym-preview.next.db"
PORT="${BOOKGYM_PREVIEW_PORT:-8001}"

if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    echo "BookGym's virtual environment is missing at $PROJECT_ROOT/.venv." >&2
    exit 1
fi

if [[ ! -f "$SOURCE_DB" ]]; then
    echo "Preview source database is missing at $SOURCE_DB." >&2
    exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is already in use. Stop the existing preview or choose BOOKGYM_PREVIEW_PORT." >&2
    exit 1
fi

mkdir -p "$PREVIEW_DIR"
rm -f "$NEXT_DB" "$NEXT_DB-shm" "$NEXT_DB-wal"
sqlite3 "$SOURCE_DB" ".backup '$NEXT_DB'"
mv "$NEXT_DB" "$PREVIEW_DB"
chmod 600 "$PREVIEW_DB"

WIFI_IP=""
for interface in en0 en1; do
    WIFI_IP="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
    if [[ -n "$WIFI_IP" ]]; then
        break
    fi
done

export BOOKGYM_ENV="preview"
export BOOKGYM_PREVIEW_MODE="true"
export BOOKGYM_DATABASE_URL="sqlite:///$PREVIEW_DB"
export BOOKGYM_HEADLESS="true"
export BOOKGYM_APP_AUTH_ENABLED="false"
export BOOKGYM_APP_SECURE_COOKIE="false"
export BOOKGYM_APP_ORIGIN=""
export BOOKGYM_APP_ALLOWED_HOSTS="*"
export BOOKGYM_SMTP_HOST=""

echo
echo "BookGym local preview"
echo "  Laptop: http://127.0.0.1:$PORT"
if [[ -n "$WIFI_IP" ]]; then
    echo "  Phone:  http://$WIFI_IP:$PORT"
else
    echo "  Phone:  use this Mac's Wi-Fi IP address with port $PORT"
fi
echo
echo "Safety: private database copy, no scheduler, live gym reads enabled, real bookings blocked."
echo "Stop the preview with Control-C. A fresh database copy is made on every start."
echo

cd "$PROJECT_ROOT"
exec "$PROJECT_ROOT/.venv/bin/python" -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload \
    --reload-dir "$PROJECT_ROOT/app"
