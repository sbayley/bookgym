#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${BOOKGYM_PI_HOST:-pi@example-pi.local}"
REMOTE_DIR="${BOOKGYM_PI_DIR:-/home/pi/bookgym}"
SSH_PORT="${BOOKGYM_SSH_PORT:-}"
SSH_HOST_KEY_ALIAS="${BOOKGYM_SSH_HOST_KEY_ALIAS:-}"
MEMBER_EMAIL=""
ADMIN_EMAIL=""

SSH_ARGS=()
if [[ -n "$SSH_PORT" ]]; then
    SSH_ARGS+=("-p" "$SSH_PORT")
fi
if [[ -n "$SSH_HOST_KEY_ALIAS" ]]; then
    SSH_ARGS+=("-o" "HostKeyAlias=$SSH_HOST_KEY_ALIAS")
fi

usage() {
    cat <<'EOF'
Usage: scripts/enable_pi_app_auth.sh [options] [user@host]

Options:
  --member-email EMAIL   Invited BookGym member email (required)
  --admin-email EMAIL    Invited administrator email (required)
  -h, --help             Show this help

Example:
  scripts/enable_pi_app_auth.sh \
    --member-email member@example.com \
    --admin-email admin@example.com \
    pi@example-pi.local
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --member-email)
            MEMBER_EMAIL="${2:-}"
            shift 2
            ;;
        --admin-email)
            ADMIN_EMAIL="${2:-}"
            shift 2
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

if [[ -z "$MEMBER_EMAIL" || -z "$ADMIN_EMAIL" ]]; then
    usage >&2
    exit 2
fi

printf -v remote_dir_q '%q' "$REMOTE_DIR"
printf -v member_email_q '%q' "$MEMBER_EMAIL"
printf -v admin_email_q '%q' "$ADMIN_EMAIL"

echo "Configuring BookGym identities on $PI_HOST."
ssh "${SSH_ARGS[@]}" "$PI_HOST" \
    "cd $remote_dir_q && ./.venv/bin/python scripts/configure_app_auth.py --member-name Member --member-email $member_email_q --admin-name Administrator --admin-email $admin_email_q"

echo "Restarting the BookGym API and scheduler. Your Pi sudo password may be requested."
ssh "${SSH_ARGS[@]}" -t "$PI_HOST" "sudo systemctl restart bookgym-api.service bookgym-scheduler.service"

echo "Verifying services, login protection, and the scheduler."
ssh "${SSH_ARGS[@]}" "$PI_HOST" \
    "sleep 2 && systemctl is-active bookgym-api.service && systemctl is-active bookgym-scheduler.service && curl -sS --retry 10 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/health && printf '\n' && curl -sS -o /dev/null -w 'Calendar: %{http_code} %{redirect_url}\n' http://127.0.0.1:8000/calendar"

echo
echo "BookGym login is enabled."
echo "Open http://${PI_HOST#*@}:8000 and sign in with an invited email address."
