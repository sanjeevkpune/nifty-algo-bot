#!/bin/bash
set -e
#!/bin/bash
set -e

# Fix for fatal: $HOME not set when running as root during GCP boot
export HOME=/root

REPO_URL="https://github.com/sanjeevkpune/nifty-algo-bot.git"
USER_NAME="kumar_sanjeev_pune"
TARGET_DIR="/home/${USER_NAME}/trading-bot"

echo "1. Setting system timezone to Asia/Kolkata..."
timedatectl set-timezone Asia/Kolkata

echo "2. Allocating 2GB Swap File for memory stability..."
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "3. Installing system packages (Ubuntu 24.04 / Python 3.12)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends git python3-pip python3-dev build-essential cron nano curl ca-certificates

echo "4. Setting up user & cloning repository..."
id -u $USER_NAME &>/dev/null || useradd -m -s /bin/bash $USER_NAME
mkdir -p $TARGET_DIR
git config --global --add safe.directory $TARGET_DIR
cd $TARGET_DIR

if [ -d ".git" ]; then
    git fetch --all
    git reset --hard origin/main
else
    git clone $REPO_URL .
fi

echo "5. Installing Python dependencies..."
pip3 install --break-system-packages -r requirements.txt || pip3 install --break-system-packages --ignore-installed -r requirements.txt

echo "6. Fetching secrets from GCP Metadata..."
META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HEADER="Metadata-Flavor: Google"

# Angel One Secrets
API_KEY=$(curl -sf -H "$HEADER" "$META_URL/api-key")
CLIENT_CODE=$(curl -sf -H "$HEADER" "$META_URL/client-code")
PIN=$(curl -sf -H "$HEADER" "$META_URL/pin")
TOTP_SECRET=$(curl -sf -H "$HEADER" "$META_URL/totp-secret")

# Shared Secrets
TELEGRAM_BOT_TOKEN=$(curl -sf -H "$HEADER" "$META_URL/telegram-token")
TELEGRAM_CHAT_ID=$(curl -sf -H "$HEADER" "$META_URL/telegram-chat")
GSHEET_ID_V2=$(curl -sf -H "$HEADER" "$META_URL/gsheet-v2")
GCP_SA_B64=$(curl -sf -H "$HEADER" "$META_URL/gcp-sa-b64")

# Decode Google Service Account JSON into the trading directory
if [ -n "$GCP_SA_B64" ]; then
    echo "$GCP_SA_B64" | base64 -d > "${TARGET_DIR}/service_account.json"
    chmod 600 "${TARGET_DIR}/service_account.json"
fi

mkdir -p /etc/trade-config
cat <<EOF > /etc/trade-config/config.env
API_KEY=${API_KEY}
CLIENT_CODE=${CLIENT_CODE}
PIN=${PIN}
TOTP_SECRET=${TOTP_SECRET}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
GSHEET_ID_V2=${GSHEET_ID_V2}

# --- COMPLETE BOT CONFIGURATION VARIABLES ---
PAPER_MODE=0
PAPER_SLIPPAGE_PTS=0.5
NUM_LOTS=5
FALLBACK_LOT_SIZE=65
ENTRY_GRACE_MIN=5
DELTA_WIDTH_MULTIPLIER=1.25
MAX_OPENING_RANGE_PTS=140.0
MAX_GAP_PERCENT=0.60
MAX_PREMIUM_LOSS_PCT=0.40
SPOT_ROLL_BUFFER=40.0
ROLL_DISTANCE_PTS=100
MAX_ROLLS_PER_SIDE=1
EXPIRY_MODE=NEXT
ORDER_MODE=MARKET
MARKET_WAIT_SECS=15
POLL_SECS=1.5
DATA_FAIL_ALERT_SECS=15
DATA_FAIL_EXIT_SECS=60
MIN_REQUIRED_MARGIN=0.0
MARKET_HOLIDAYS=
BROKERAGE_PER_ORDER=20.0
STT_SELL_PCT=0.0015
EXCHANGE_PCT=0.000355299
EXIT_BLIND_IF_POSITIONS_UNAVAILABLE=0
POSITION_READ_TRIES=4
FLAT_VERIFY_ROUNDS=3
EOF
chmod 644 /etc/trade-config/config.env

chown -R ${USER_NAME}:${USER_NAME} $TARGET_DIR

echo "7. Configuring Crontab for Angel One (09:39)..."
cat <<'EOF' > /tmp/crontab.txt
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Angel One Strategy (9:39 AM IST)
39 9 * * 1-5 set -a && source /etc/trade-config/config.env && set +a && cd /home/kumar_sanjeev_pune/trading-bot && /usr/bin/python3 adaptive_strangle_v2.py >> /home/kumar_sanjeev_pune/trading-bot/algo_trade_v2.log 2>&1
EOF

crontab -u $USER_NAME /tmp/crontab.txt
rm -f /tmp/crontab.txt
systemctl enable cron
systemctl restart cron

echo "Startup script completed successfully at $(date)"
# Fix for fatal: $HOME not set when running as root during GCP boot
export HOME=/root

REPO_URL="https://github.com/sanjeevkpune/nifty-algo-bot.git"
USER_NAME="kumar_sanjeev_pune"
TARGET_DIR="/home/${USER_NAME}/trading-bot"

echo "1. Setting system timezone to Asia/Kolkata..."
timedatectl set-timezone Asia/Kolkata

echo "2. Allocating 2GB Swap File for memory stability..."
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "3. Installing system packages (Ubuntu 24.04 / Python 3.12)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends git python3-pip python3-dev build-essential cron nano curl ca-certificates

echo "4. Setting up user & cloning repository..."
id -u $USER_NAME &>/dev/null \vert{}\vert{} useradd -m -s /bin/bash$USER_NAME
mkdir -p $TARGET_DIR
git config --global --add safe.directory $TARGET_DIR
cd $TARGET_DIR

if [ -d ".git" ]; then
    git fetch --all
    git reset --hard origin/main
else
    git clone $REPO_URL .
fi

echo "5. Installing Python dependencies..."
pip3 install --break-system-packages -r requirements.txt || pip3 install --break-system-packages --ignore-installed -r requirements.txt

echo "6. Fetching secrets from GCP Metadata..."
META_URL="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
HEADER="Metadata-Flavor: Google"

# Angel One Secrets
API_KEY=$(curl -sf -H "$HEADER" "$META_URL/api-key")
CLIENT_CODE=$(curl -sf -H "$HEADER" "$META_URL/client-code")
PIN=$(curl -sf -H "$HEADER" "$META_URL/pin")
TOTP_SECRET=$(curl -sf -H "$HEADER" "$META_URL/totp-secret")

# Shared Secrets
TELEGRAM_BOT_TOKEN=$(curl -sf -H "$HEADER" "$META_URL/telegram-token")
TELEGRAM_CHAT_ID=$(curl -sf -H "$HEADER" "$META_URL/telegram-chat")
GSHEET_ID_V2=$(curl -sf -H "$HEADER" "$META_URL/gsheet-v2")
GCP_SA_B64=$(curl -sf -H "$HEADER" "$META_URL/gcp-sa-b64")

# Decode Google Service Account JSON into the trading directory
if [ -n "$GCP_SA_B64" ]; then
    echo "$GCP_SA_B64" \vert{} base64 -d > "${TARGET_DIR}/service_account.json"
    chmod 600 "${TARGET_DIR}/service_account.json"
fi

mkdir -p /etc/trade-config
cat <<EOF > /etc/trade-config/config.env
API_KEY=${API_KEY}
CLIENT_CODE=${CLIENT_CODE}
PIN=${PIN}
TOTP_SECRET=${TOTP_SECRET}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
GSHEET_ID_V2=${GSHEET_ID_V2}

# --- COMPLETE BOT CONFIGURATION VARIABLES ---
PAPER_MODE=0
PAPER_SLIPPAGE_PTS=0.5
NUM_LOTS=5
FALLBACK_LOT_SIZE=65
ENTRY_GRACE_MIN=5
DELTA_WIDTH_MULTIPLIER=1.25
MAX_OPENING_RANGE_PTS=140.0
MAX_GAP_PERCENT=0.60
MAX_PREMIUM_LOSS_PCT=0.40
SPOT_ROLL_BUFFER=40.0
ROLL_DISTANCE_PTS=100
MAX_ROLLS_PER_SIDE=1
EXPIRY_MODE=NEXT
ORDER_MODE=MARKET
MARKET_WAIT_SECS=15
POLL_SECS=1.5
DATA_FAIL_ALERT_SECS=15
DATA_FAIL_EXIT_SECS=60
MIN_REQUIRED_MARGIN=0.0
MARKET_HOLIDAYS=
BROKERAGE_PER_ORDER=20.0
STT_SELL_PCT=0.0015
EXCHANGE_PCT=0.000355299
EXIT_BLIND_IF_POSITIONS_UNAVAILABLE=0
POSITION_READ_TRIES=4
FLAT_VERIFY_ROUNDS=3
EOF
chmod 644 /etc/trade-config/config.env

chown -R ${USER_NAME}:${USER_NAME}$TARGET_DIR

echo "7. Configuring Crontab for Angel One (09:39)..."
cat <<'EOF' > /tmp/crontab.txt
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Angel One Strategy (9:39 AM IST)
39 9 * * 1-5 set -a && source /etc/trade-config/config.env && set +a && cd /home/kumar_sanjeev_pune/trading-bot && /usr/bin/python3 adaptive_strangle_v2.py >> /home/kumar_sanjeev_pune/trading-bot/algo_trade_v2.log 2>&1
EOF

crontab -u $USER_NAME /tmp/crontab.txt
rm -f /tmp/crontab.txt
systemctl enable cron
systemctl restart cron

echo "Startup script completed successfully at $(date)"
