#!/bin/bash
# setup_etf_dukan.sh  --  set up / refresh the ETF ki Dukan bot on the VM. Idempotent: safe on every boot.
# Called by startup.sh (as root) after the repo is cloned and the main crontab is written:
#     bash /home/<user>/trading-bot/etf_dukan/setup_etf_dukan.sh <user> <trading-bot dir>
#
# Settings come from GCP instance metadata, written by .github/workflows/vm-deploy.yml from repo
# Variables (not secret) and Secrets. Every one has a safe default, so nothing has to be set by hand:
#   ekd-paper-mode   vars.EKD_PAPER_MODE    default 1 (paper). Only the exact value 0 turns on live orders.
#   ekd-start-date   vars.EKD_START_DATE    default auto (the first day this script creates the records)
#   ekd-capital      vars.EKD_CAPITAL       default 4000000
#   ekd-gsheet-id    vars.EKD_GSHEET_ID     default: the ETF ki Dukan tracker sheet
#   ekd-api-key      secrets.EKD_API_KEY    optional separate SmartAPI app key; else the shared API_KEY is used
# AngelOne login, Telegram and the Google service account are the ones startup.sh already writes
# (/etc/trade-config/config.env and <trading-bot>/service_account.json).
set -u

USER_NAME="${1:-kumar_sanjeev_pune}"
TARGET_DIR="${2:-/home/${USER_NAME}/trading-bot}"
BOT_DIR="${TARGET_DIR}/etf_dukan"
DEFAULT_SHEET="1ZEWLugBHiPSsX5letLn_u7zU3dj06u9brASqJM1uo_k"
META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"

meta() { curl -sf -H "Metadata-Flavor: Google" "${META_URL}/$1" 2>/dev/null || true; }

echo "=== ETF ki Dukan bot: setup ==="
if [ ! -f "${BOT_DIR}/etf_dukan_bot.py" ]; then
    echo "ETF bot code not found in ${BOT_DIR}; skipping."
    exit 0
fi

PAPER=$(meta ekd-paper-mode);  [ "${PAPER}" = "0" ] || PAPER=1
START=$(meta ekd-start-date);  [ -n "${START}" ] || START=auto
CAPITAL=$(meta ekd-capital);   [ -n "${CAPITAL}" ] || CAPITAL=4000000
SHEET=$(meta ekd-gsheet-id);   [ -n "${SHEET}" ] || SHEET="${DEFAULT_SHEET}"
EKD_KEY=$(meta ekd-api-key)

echo "1. Python packages (already installed by startup.sh; this only fills gaps)..."
pip3 install --break-system-packages -q smartapi-python pyotp requests gspread google-auth logzero websocket-client pycryptodome \
    || echo "Warning: pip install returned non-zero (packages may already be present)."

echo "2. Folders..."
mkdir -p "${BOT_DIR}/state/backups" "${BOT_DIR}/locks" "${BOT_DIR}/logs" "${BOT_DIR}/cache"

echo "3. Writing /etc/trade-config/etf_dukan.env (paper mode = ${PAPER})..."
mkdir -p /etc/trade-config
cat <<ENVEOF > /etc/trade-config/etf_dukan.env
# Written by etf_dukan/setup_etf_dukan.sh on every boot. Edit the repo Variables, not this file.
EKD_PAPER_MODE=${PAPER}
EKD_HOME=${BOT_DIR}
EKD_SERVICE_ACCOUNT_FILE=${TARGET_DIR}/service_account.json
EKD_GSHEET_ID=${SHEET}
EKD_SHEET_MODE=tracker
EKD_SIGNAL_POLICY=bot
EKD_BROKER_CASH_FLOOR=500000
ENVEOF
if [ -n "${EKD_KEY}" ]; then echo "EKD_API_KEY=${EKD_KEY}" >> /etc/trade-config/etf_dukan.env; fi
chown root:"${USER_NAME}" /etc/trade-config/etf_dukan.env
chmod 640 /etc/trade-config/etf_dukan.env

chown -R "${USER_NAME}:${USER_NAME}" "${BOT_DIR}"

echo "4. Records: restore from the sheet if this VM has none, else create them (never overwrites)..."
MODE_NAME=paper; [ "${PAPER}" = "0" ] && MODE_NAME=live
STATE_FILE="${BOT_DIR}/state/${MODE_NAME}_state.json"
if [ ! -f "${STATE_FILE}" ]; then
    sudo -u "${USER_NAME}" -H bash -c "cd '${BOT_DIR}' && /usr/bin/python3 etf_dukan_bot.py restore-state --if-missing" \
        || echo "Warning: restore-state failed."
fi
sudo -u "${USER_NAME}" -H bash -c "cd '${BOT_DIR}' && /usr/bin/python3 etf_dukan_bot.py init --if-missing --capital '${CAPITAL}' --start '${START}'" \
    || echo "Warning: init failed."

echo "5. Crontab line (2:45 pm IST, Mon-Fri; VM clock is Asia/Kolkata)..."
CRON_LINE="45 14 * * 1-5 cd ${BOT_DIR} && /usr/bin/flock -n /tmp/etf_dukan.lock /usr/bin/python3 etf_dukan_bot.py run >> ${BOT_DIR}/logs/cron.log 2>&1 # EKD-ETF-DUKAN"
( crontab -u "${USER_NAME}" -l 2>/dev/null | grep -v "# EKD-ETF-DUKAN" ; echo "${CRON_LINE}" ) | crontab -u "${USER_NAME}" -

echo "ETF ki Dukan bot ready (mode ${MODE_NAME}, sheet ${SHEET})."

# VM-wide extras: 3:45 pm daily summary cron line, journald size limit. Its failure never affects the bots.
if [ -f "${TARGET_DIR}/setup_vm_extras.sh" ]; then
    bash "${TARGET_DIR}/setup_vm_extras.sh" "${USER_NAME}" "${TARGET_DIR}" \
        || echo "Warning: VM extras setup failed; the bots are unaffected."
fi

# Strategy Desk dashboard (separate service, port 8080). Its failure never affects the bots.
if [ -f "${TARGET_DIR}/dashboard/setup_dashboard.sh" ]; then
    bash "${TARGET_DIR}/dashboard/setup_dashboard.sh" "${USER_NAME}" "${TARGET_DIR}" \
        || echo "Warning: dashboard setup failed; the bots are unaffected."
fi
