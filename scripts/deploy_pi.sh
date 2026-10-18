#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Optional machine-specific defaults; never copied to the deployment target.
if [[ -f "$PROJECT_ROOT/.local/deploy.env" ]]; then
    source "$PROJECT_ROOT/.local/deploy.env"
fi
PI_HOST="${BOOKGYM_PI_HOST:-pi@example-pi.local}"
REMOTE_DIR="${BOOKGYM_PI_DIR:-/home/pi/bookgym}"
SSH_PORT="${BOOKGYM_SSH_PORT:-}"
SSH_HOST_KEY_ALIAS="${BOOKGYM_SSH_HOST_KEY_ALIAS:-}"
INITIAL=false

SSH_CMD=(ssh)
SCP_CMD=(scp)
if [[ -n "$SSH_PORT" ]]; then
    SSH_CMD+=("-p" "$SSH_PORT")
    SCP_CMD+=("-P" "$SSH_PORT")
fi
if [[ -n "$SSH_HOST_KEY_ALIAS" ]]; then
    SSH_CMD+=("-o" "HostKeyAlias=$SSH_HOST_KEY_ALIAS")
    SCP_CMD+=("-o" "HostKeyAlias=$SSH_HOST_KEY_ALIAS")
fi

usage() {
    cat <<'EOF'
Usage: scripts/deploy_pi.sh [--initial] [user@host]

  --initial   Copy the current .env, a consistent SQLite backup, and browser auth state.
              The Pi scheduler remains disabled so the Mac remains the only booking worker.

Examples:
  scripts/deploy_pi.sh --initial pi@example-pi.local
  scripts/deploy_pi.sh pi@example-pi.local
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --initial)
            INITIAL=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            PI_HOST="$1"
            shift
            ;;
    esac
done

if [[ "$INITIAL" == true ]]; then
    for required_file in .env bookgym.db; do
        if [[ ! -f "$PROJECT_ROOT/$required_file" ]]; then
            echo "Missing $PROJECT_ROOT/$required_file" >&2
            exit 1
        fi
    done
fi

echo "Uploading BookGym code to $PI_HOST:$REMOTE_DIR"
"${SSH_CMD[@]}" "$PI_HOST" "mkdir -p '$REMOTE_DIR'"

tar \
    --exclude='./.git' \
    --exclude='./.local' \
    --exclude='./.venv' \
    --exclude='./.env' \
    --exclude='./bookgym.db' \
    --exclude='./bookgym.db-shm' \
    --exclude='./bookgym.db-wal' \
    --exclude='./artifacts' \
    --exclude='./.preview' \
    --exclude='./playwright/.auth' \
    --exclude='./.idea' \
    --exclude='./.DS_Store' \
    --exclude='*/__pycache__' \
    -czf - \
    -C "$PROJECT_ROOT" . \
    | "${SSH_CMD[@]}" "$PI_HOST" "tar -xzf - -C '$REMOTE_DIR'"

if [[ "$INITIAL" == true ]]; then
    DB_BACKUP="$(mktemp /tmp/bookgym-pi-db.XXXXXX)"
    trap 'rm -f "$DB_BACKUP"' EXIT

    echo "Copying configuration and a consistent database snapshot."
    sqlite3 "$PROJECT_ROOT/bookgym.db" ".backup '$DB_BACKUP'"
    "${SCP_CMD[@]}" -p "$PROJECT_ROOT/.env" "$PI_HOST:$REMOTE_DIR/.env"
    "${SCP_CMD[@]}" -p "$DB_BACKUP" "$PI_HOST:$REMOTE_DIR/bookgym.db"
    "${SSH_CMD[@]}" "$PI_HOST" "chmod 600 '$REMOTE_DIR/.env' && mkdir -p '$REMOTE_DIR/playwright/.auth'"

    if [[ -f "$PROJECT_ROOT/playwright/.auth/storage_state.json" ]]; then
        "${SCP_CMD[@]}" -p \
            "$PROJECT_ROOT/playwright/.auth/storage_state.json" \
            "$PI_HOST:$REMOTE_DIR/playwright/.auth/storage_state.json"
        "${SSH_CMD[@]}" "$PI_HOST" "chmod 600 '$REMOTE_DIR/playwright/.auth/storage_state.json'"
    fi
fi

SCHEDULER_ACTION="preserve"
if [[ "$INITIAL" == true ]]; then
    SCHEDULER_ACTION="disable"
fi

echo "Installing dependencies and services. Your Pi sudo password may be requested."
"${SSH_CMD[@]}" -t "$PI_HOST" "sudo bash '$REMOTE_DIR/scripts/bootstrap_pi.sh' '$SCHEDULER_ACTION'"

echo
if [[ "$INITIAL" == true ]]; then
    echo "Deployment finished. The Pi scheduler remains disabled pending cutover."
else
    echo "Deployment finished. The Pi scheduler's existing active/inactive state was preserved."
fi
echo "Open http://${PI_HOST#*@}:8000 to verify the dashboard."
