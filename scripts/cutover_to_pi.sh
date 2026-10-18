#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Optional machine-specific defaults; never copied to the deployment target.
if [[ -f "$PROJECT_ROOT/.local/deploy.env" ]]; then
    source "$PROJECT_ROOT/.local/deploy.env"
fi
PI_HOST="${BOOKGYM_PI_HOST:-pi@example-pi.local}"
REMOTE_DIR="${BOOKGYM_PI_DIR:-/home/pi/bookgym}"
DOMAIN="gui/$(id -u)"
MAC_SCHEDULER_LABEL="com.bookgym.scheduler"
MAC_SCHEDULER_STOPPED=false
PI_ACTIVATED=false
DB_BACKUP=""

if [[ $# -gt 1 ]]; then
    echo "Usage: scripts/cutover_to_pi.sh [user@host]" >&2
    exit 1
fi
if [[ $# -eq 1 ]]; then
    PI_HOST="$1"
fi

cleanup() {
    if [[ -n "$DB_BACKUP" ]]; then
        rm -f "$DB_BACKUP"
    fi
}

restore_mac_scheduler() {
    if [[ "$MAC_SCHEDULER_STOPPED" == true && "$PI_ACTIVATED" == false ]]; then
        echo "Restoring the Mac scheduler because cutover did not complete." >&2
        "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/install_launchd_scheduler.py" || true
    fi
}

on_error() {
    trap - ERR
    restore_mac_scheduler
    exit 1
}

trap cleanup EXIT
trap on_error ERR

if ! launchctl print "$DOMAIN/$MAC_SCHEDULER_LABEL" >/dev/null 2>&1; then
    echo "The Mac scheduler is not running; refusing an ambiguous cutover." >&2
    exit 1
fi

echo "Uploading the Pi activation helper."
scp -p \
    "$PROJECT_ROOT/scripts/activate_pi_scheduler.sh" \
    "$PI_HOST:$REMOTE_DIR/scripts/activate_pi_scheduler.sh"

echo "Stopping the Mac booking scheduler."
"$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/uninstall_launchd_scheduler.py"
MAC_SCHEDULER_STOPPED=true

DB_BACKUP="$(mktemp /tmp/bookgym-cutover-db.XXXXXX)"
sqlite3 "$PROJECT_ROOT/bookgym.db" ".backup '$DB_BACKUP'"

echo "Uploading the final BookGym database snapshot to $PI_HOST."
scp -p "$DB_BACKUP" "$PI_HOST:$REMOTE_DIR/bookgym.db.cutover"

echo "Activating the Pi scheduler. Your Pi sudo password may be requested."
ssh -t "$PI_HOST" "sudo bash '$REMOTE_DIR/scripts/activate_pi_scheduler.sh'"
PI_ACTIVATED=true

trap - ERR

echo
echo "Cutover complete. The Pi is now the only active BookGym scheduler."
echo "Dashboard: http://${PI_HOST#*@}:8000"
echo "The Mac scheduler remains disabled as the rollback copy."
