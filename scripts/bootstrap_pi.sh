#!/usr/bin/env bash
set -euo pipefail

TARGET_USER="${BOOKGYM_PI_USER:-pi}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
INSTALL_DIR="${BOOKGYM_PI_DIR:-$TARGET_HOME/bookgym}"
VENV_DIR="$INSTALL_DIR/.venv"
SCHEDULER_ACTION="${1:-preserve}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

if [[ -z "$TARGET_HOME" || ! -d "$TARGET_HOME" ]]; then
    echo "Could not resolve home directory for user $TARGET_USER." >&2
    exit 1
fi

if [[ ! -f "$INSTALL_DIR/requirements.txt" ]]; then
    echo "BookGym files are missing from $INSTALL_DIR." >&2
    exit 1
fi

if [[ "$SCHEDULER_ACTION" != "preserve" && "$SCHEDULER_ACTION" != "enable" && "$SCHEDULER_ACTION" != "disable" ]]; then
    echo "Scheduler action must be preserve, enable, or disable." >&2
    exit 1
fi

echo "Configuring BookGym on $(hostname) for $TARGET_USER."

timedatectl set-timezone America/Los_Angeles
hostnamectl set-hostname bookgym-pi

if grep -qE '^127\.0\.1\.1[[:space:]]+' /etc/hosts; then
    sed -i -E 's/^127\.0\.1\.1[[:space:]]+.*/127.0.1.1\tbookgym-pi/' /etc/hosts
else
    printf '127.0.1.1\tbookgym-pi\n' >> /etc/hosts
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    sqlite3

install -d -o "$TARGET_USER" -g "$TARGET_USER" "$INSTALL_DIR" "$INSTALL_DIR/artifacts"
chown -R "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    sudo -u "$TARGET_USER" -H python3 -m venv "$VENV_DIR"
fi

sudo -u "$TARGET_USER" -H "$VENV_DIR/bin/python" -m pip install --upgrade pip
sudo -u "$TARGET_USER" -H "$VENV_DIR/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

"$VENV_DIR/bin/playwright" install-deps chromium
sudo -u "$TARGET_USER" -H "$VENV_DIR/bin/playwright" install chromium

sudo -u "$TARGET_USER" -H sh -c "cd '$INSTALL_DIR' && '$VENV_DIR/bin/python' -m unittest discover -s tests"

cat > /etc/systemd/system/bookgym-api.service <<EOF
[Unit]
Description=BookGym API and dashboard
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
Environment=HOME=$TARGET_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/bookgym-scheduler.service <<EOF
[Unit]
Description=BookGym scheduler and booking worker
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
Environment=HOME=$TARGET_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/scripts/run_scheduler.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now bookgym-api.service

case "$SCHEDULER_ACTION" in
    enable)
        systemctl enable --now bookgym-scheduler.service
        ;;
    disable)
        systemctl disable --now bookgym-scheduler.service || true
        ;;
    preserve)
        if systemctl is-active --quiet bookgym-scheduler.service; then
            systemctl restart bookgym-scheduler.service
        fi
        ;;
esac

echo
echo "BookGym Pi bootstrap complete."
echo "Dashboard origin: http://127.0.0.1:8000 (available through the configured HTTPS tunnel)"
echo "API service: $(systemctl is-active bookgym-api.service)"
echo "Scheduler service: $(systemctl is-active bookgym-scheduler.service || true)"
