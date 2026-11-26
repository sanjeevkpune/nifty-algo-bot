#!/bin/bash
# setup_dashboard.sh  --  install / refresh the Strategy Desk dashboard on the VM. Idempotent: safe on every boot.
# Called (as root) from etf_dukan/setup_etf_dukan.sh, which startup.sh already runs on every boot or sync:
#     bash <trading-bot>/dashboard/setup_dashboard.sh <user> <trading-bot dir>
#
# What it does:
#   1. Creates /etc/trade-config/dashboard.env once, with a random password (never in the repo).
#   2. Installs and (re)starts the systemd service "strategy-dashboard" (python3 dashboard/server.py, port 8080).
#   3. Sends the address (and, the first time, the password) to your Telegram chat.
# Port 8080 is opened by the "Dashboard Network Setup" GitHub workflow (run once).
set -u

USER_NAME="${1:-kumar_sanjeev_pune}"
TARGET_DIR="${2:-/home/${USER_NAME}/trading-bot}"
DASH_DIR="${TARGET_DIR}/dashboard"
ENV_FILE="/etc/trade-config/dashboard.env"
STATE_DIR="/var/lib/strategy-dashboard"
PORT=8080

echo "=== Strategy Desk dashboard: setup ==="
[ -f "${DASH_DIR}/server.py" ] || { echo "Dashboard code not found; skipping."; exit 0; }
mkdir -p /etc/trade-config "${STATE_DIR}"

NEW_PASSWORD=""
if [ ! -s "${ENV_FILE}" ] || ! grep -q '^DASH_PASSWORD=.\+' "${ENV_FILE}"; then
    NEW_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')
    cat <<ENVEOF > "${ENV_FILE}"
# Written once by dashboard/setup_dashboard.sh. Change the password here and run: sudo systemctl restart strategy-dashboard
DASH_USER=admin
DASH_PASSWORD=${NEW_PASSWORD}
DASH_PORT=${PORT}
ENVEOF
fi
chown root:"${USER_NAME}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

cat <<UNIT > /etc/systemd/system/strategy-dashboard.service
[Unit]
Description=Strategy Desk dashboard (port ${PORT})
After=network-online.target
Wants=network-online.target

[Service]
User=${USER_NAME}
WorkingDirectory=${DASH_DIR}
ExecStart=/usr/bin/python3 ${DASH_DIR}/server.py
Restart=always
RestartSec=5
Nice=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable strategy-dashboard >/dev/null 2>&1
systemctl restart strategy-dashboard
sleep 2
systemctl is-active --quiet strategy-dashboard && echo "Service running." || echo "Warning: service did not start (journalctl -u strategy-dashboard)."

# Tell the user where it is: on the first install, and whenever the address changes.
IP=$(curl -sf -H "Metadata-Flavor: Google" \
     "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" || true)
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' /etc/trade-config/config.env 2>/dev/null | cut -d= -f2-)
TG_CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' /etc/trade-config/config.env 2>/dev/null | cut -d= -f2-)
LAST_IP=$(cat "${STATE_DIR}/last_ip" 2>/dev/null || true)
if [ -n "${IP}" ] && { [ -n "${NEW_PASSWORD}" ] || [ "${IP}" != "${LAST_IP}" ]; }; then
    MSG="📊 Strategy Desk dashboard: http://${IP}:${PORT}  (user: admin)"
    [ -n "${NEW_PASSWORD}" ] && MSG="${MSG}
Password: ${NEW_PASSWORD}
Save it; it is only sent this once. It is stored on the VM in ${ENV_FILE}."
    if [ -n "${TG_TOKEN}" ] && [ -n "${TG_CHAT}" ]; then
        curl -sf -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
             --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=${MSG}" >/dev/null \
            && echo "${IP}" > "${STATE_DIR}/last_ip" && echo "Address sent to Telegram."
    else
        echo "Telegram not configured; dashboard is at http://${IP}:${PORT}"
    fi
fi
echo "Strategy Desk ready on port ${PORT}."
