#!/usr/bin/env bash
set -euo pipefail

TARGET_USER="${BOOKGYM_PI_USER:-${SUDO_USER:-pi}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
INSTALL_DIR="${BOOKGYM_PI_DIR:-$TARGET_HOME/bookgym}"
STAGED_DB="$INSTALL_DIR/bookgym.db.cutover"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

if [[ ! -f "$STAGED_DB" ]]; then
    echo "Missing staged database $STAGED_DB" >&2
    exit 1
fi

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    echo "BookGym virtual environment is missing." >&2
    exit 1
fi

if [[ "$(sqlite3 "$STAGED_DB" 'PRAGMA integrity_check;')" != "ok" ]]; then
    echo "The staged SQLite database failed its integrity check." >&2
    exit 1
fi

rollback() {
    echo "Pi activation failed; keeping the Pi scheduler disabled." >&2
    systemctl disable --now bookgym-scheduler.service >/dev/null 2>&1 || true
    systemctl start bookgym-api.service >/dev/null 2>&1 || true
}
trap rollback ERR

systemctl stop bookgym-scheduler.service bookgym-api.service
install -o "$TARGET_USER" -g "$TARGET_USER" -m 600 "$STAGED_DB" "$INSTALL_DIR/bookgym.db"
rm -f "$STAGED_DB"

sudo -u "$TARGET_USER" -H sh -c \
    "cd '$INSTALL_DIR' && '$INSTALL_DIR/.venv/bin/python' -c 'from app.db import init_db; init_db()'"

systemctl enable --now bookgym-api.service
systemctl enable --now bookgym-scheduler.service

for _attempt in {1..30}; do
    if curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
        break
    fi
    sleep 1
done

curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null
systemctl is-active --quiet bookgym-api.service
systemctl is-active --quiet bookgym-scheduler.service

trap - ERR

echo "Pi scheduler activation complete."
echo "API service: $(systemctl is-active bookgym-api.service)"
echo "Scheduler service: $(systemctl is-active bookgym-scheduler.service)"
curl --silent --show-error http://127.0.0.1:8000/scheduler-status
echo
