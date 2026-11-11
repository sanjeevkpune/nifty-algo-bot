#!/bin/bash
set -e
export HOME=/root
USER_NAME="kumar_sanjeev_pune"
TARGET_DIR="/home/${USER_NAME}/trading-bot"

timedatectl set-timezone Asia/Kolkata

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends git python3-pip python3-dev build-essential cron nano curl ca-certificates

set +e  
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$USER_NAME"
fi
set -e  

chown -R ${USER_NAME}:${USER_NAME} $TARGET_DIR || true

echo "Installing Multi-Broker Dependencies..."
pip3 install --break-system-packages dhanhq pandas pandas_market_calendars gspread requests pyotp SmartApi-python

echo "Fetching ALL secrets from GCP Metadata..."
META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HEADER="Metadata-Flavor: Google"

# Angel One Secrets
API_KEY=$(curl -sf -H "$HEADER" "$META_URL/api-key")
CLIENT_CODE=$(curl -sf -H "$HEADER" "$META_URL/client-code")
PIN=$(curl -sf -H "$HEADER" "$META_URL/pin")
TOTP_SECRET=$(curl -sf -H "$HEADER" "$META_URL/totp-secret")

# Dhan Secrets
DHAN_CLIENT_ID=$(curl -sf -H "$HEADER" "$META_URL/dhan-client-id")
DHAN_ACCESS_TOKEN=$(curl -sf -H "$HEADER" "$META_URL/dhan-access-token")

# Shared Secrets
TELEGRAM_BOT_TOKEN=$(curl -sf -H "$HEADER" "$META_URL/telegram-token")
TELEGRAM_CHAT_ID=$(curl -sf -H "$HEADER" "$META_URL/telegram-chat")
GSHEET_ID_V2=$(curl -sf -H "$HEADER" "$META_URL/gsheet-v2")
GCP_SA_B64=$(curl -sf -H "$HEADER" "$META_URL/gcp-sa-b64")

if [ -n "$GCP_SA_B64" ]; then
    echo "$GCP_SA_B64" | base64 -d > "${TARGET_DIR}/service_account.json"
    chmod 600 "${TARGET_DIR}/service_account.json"
fi

mkdir -p /etc/trade-config
cat <<EOF > /etc/trade-config/config.env
# Angel One
API_KEY=${API_KEY}
CLIENT_CODE=${CLIENT_CODE}
PIN=${PIN}
TOTP_SECRET=${TOTP_SECRET}

# Dhan
DHAN_CLIENT_ID=${DHAN_CLIENT_ID}
DHAN_ACCESS_TOKEN=${DHAN_ACCESS_TOKEN}

# Shared
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
GSHEET_ID_V2=${GSHEET_ID_V2}

# --- GLOBAL BOT CONFIGURATION VARIABLES ---
PAPER_MODE=1

# --- INDEPENDENT LOT SIZES ---
DHAN_NUM_LOTS=2
ANGEL_NUM_LOTS=5

# --- STRATEGY PARAMETERS ---
PAPER_SLIPPAGE_PTS=0.5
ENTRY_GRACE_MIN=5
DELTA_WIDTH_MULTIPLIER=1.25
MAX_OPENING_RANGE_PTS=140.0
MAX_GAP_PERCENT=0.60
MAX_PREMIUM_LOSS_PCT=0.40
EOF
chmod 644 /etc/trade-config/config.env

echo "Configuring Dual Crontab..."
cat <<'EOF' > /tmp/crontab.txt
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# 1. Dhan Lemonn Dual-Session (Starts at 9:14 AM)
14 9 * * 1-5 set -a && source /etc/trade-config/config.env && set +a && cd /home/kumar_sanjeev_pune/trading-bot && /usr/bin/python3 lemonn_dual_session_dhan.py >> /home/kumar_sanjeev_pune/trading-bot/lemonn_dhan_log.log 2>&1

# 2. Angel One Adaptive Strangle V4.2 (Starts at 9:39 AM)
39 9 * * 1-5 set -a && source /etc/trade-config/config.env && set +a && cd /home/kumar_sanjeev_pune/trading-bot && /usr/bin/python3 adaptive_strangle_v4.py >> /home/kumar_sanjeev_pune/trading-bot/algo_trade_v4.log 2>&1
EOF

crontab -u $USER_NAME /tmp/crontab.txt
rm -f /tmp/crontab.txt
systemctl enable cron
systemctl restart cron

echo "Startup script completed successfully at $(date)"
