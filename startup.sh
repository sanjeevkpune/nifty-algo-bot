#!/bin/bash
# ==========================================
# GCP VM AUTOMATED STARTUP & PROVISIONING SCRIPT
# ==========================================

REPO_URL="https://github.com/sanjeevkpune/nifty-algo-bot.git"
TARGET_DIR="/home/sanjeev/trading-bot"
STATIC_IP_NAME="algo-vm-ip"
ZONE="us-central1-c"  # Update to your VM zone

# 1. Update system & install dependencies
apt-get update -y
apt-get install -y git python3-pip cron nano curl ufw

# 2. Setup working directory
mkdir -p $TARGET_DIR
cd $TARGET_DIR

# 3. Clone or pull latest code from GitHub
if [ -d ".git" ]; then
    git pull
else
    git clone $REPO_URL .
fi

# 4. Install Python dependencies
pip3 install -r requirements.txt --break-system-packages

# 5. Inject Secrets from Metadata / Environment variables passed during creation
cat << EOF > service_account.json
{
  "type": "service_account",
  "project_id": "$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)",
  "private_key_id": "...",
  ... # Alternatively, inject via GitHub Actions secrets file creation
}
EOF

# 6. Configure Automated Crontab for Staggered Execution
cat << EOF > /tmp/crontab.txt
# 38 9 * * 1-5 cd $TARGET_DIR && /usr/bin/python3 strangle_algo.py >> $TARGET_DIR/algo_trade.log 2>&1
39 9 * * 1-5 cd $TARGET_DIR && /usr/bin/python3 adaptive_strangle_v2.py >> $TARGET_DIR/algo_trade_v2.log 2>&1
EOF
crontab /tmp/crontab.txt
systemctl enable --now cron

echo "Startup script completed successfully at $(date)"
