#!/bin/bash
# ==========================================
# GCP VM AUTOMATED STARTUP & PROVISIONING SCRIPT
# ==========================================

REPO_URL="https://github.com/sanjeevkpune/nifty-algo-bot.git"
TARGET_DIR="/home/sanjeev/trading-bot"

# 1. Set System Timezone to IST (Kolkata)
echo "Setting system timezone to Asia/Kolkata..."
timedatectl set-timezone Asia/Kolkata

# 2. Update system & install dependencies
apt-get update -y
apt-get install -y git python3-pip cron nano curl ufw

# 3. Setup working directory and pull code from GitHub
mkdir -p $TARGET_DIR
cd $TARGET_DIR

if [ -d ".git" ]; then
    git pull
else
    git clone $REPO_URL .
fi

# 4. Install Python dependencies
pip3 install -r requirements.txt --break-system-packages

# 5. Read environment variables passed via GCP metadata script-values and save to a secure file
mkdir -p /etc/trade-config
cat << EOF > /etc/trade-config/config.env
API_KEY="${API_KEY}"
CLIENT_CODE="${CLIENT_CODE}"
PIN="${PIN}"
TOTP_SECRET="${TOTP_SECRET}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID}"
GSHEET_ID_V2="${GSHEET_ID_V2}"
EOF
chmod 600 /etc/trade-config/config.env

# 6. Configure Automated Crontab for Staggered Execution (Loading env vars automatically)
cat << 'EOF' > /tmp/crontab.txt
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
38 9 * * 1-5 export $(xargs -a /etc/trade-config/config.env) && cd /home/sanjeev/trading-bot && /usr/bin/python3 strangle_algo.py >> /home/sanjeev/trading-bot/algo_trade.log 2>&1
39 9 * * 1-5 export $(xargs -a /etc/trade-config/config.env) && cd /home/sanjeev/trading-bot && /usr/bin/python3 adaptive_strangle_v2.py >> /home/sanjeev/trading-bot/algo_trade_v2.log 2>&1
EOF
crontab /tmp/crontab.txt
systemctl enable --now cron

echo "Startup script completed successfully at $(date)"
