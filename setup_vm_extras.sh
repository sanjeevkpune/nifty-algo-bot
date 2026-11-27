#!/bin/bash
# setup_vm_extras.sh  --  VM-wide extras that are not tied to one bot. Idempotent: safe on every boot.
# Called (as root) from the end of etf_dukan/setup_etf_dukan.sh, which startup.sh runs on every boot,
# so changes here apply on the next boot from the repo copy, without touching startup.sh:
#     bash /home/<user>/trading-bot/setup_vm_extras.sh <user> <trading-bot dir>
#
#   1. Crontab line: daily_summary.py at 15:45 IST Mon-Fri (tagged "# VM-DAILY-SUMMARY", replaced each boot)
#   2. journald capped at 200 MB / 30 days (system logs were unbounded on the 20 GB disk)
#   3. apt package cache emptied (the boot's apt-get install leaves .deb files behind)
set -u

USER_NAME="${1:-kumar_sanjeev_pune}"
TARGET_DIR="${2:-/home/${USER_NAME}/trading-bot}"

echo "=== VM extras ==="

echo "1. Daily summary cron line (3:45 pm IST, Mon-Fri)..."
CRON_LINE="45 15 * * 1-5 cd ${TARGET_DIR} && /usr/bin/python3 daily_summary.py >> ${TARGET_DIR}/daily_summary.log 2>&1 # VM-DAILY-SUMMARY"
( crontab -u "${USER_NAME}" -l 2>/dev/null | grep -v "# VM-DAILY-SUMMARY" ; echo "${CRON_LINE}" ) | crontab -u "${USER_NAME}" -

echo "2. journald size limit..."
mkdir -p /etc/systemd/journald.conf.d
cat <<'EOF' > /etc/systemd/journald.conf.d/90-trading-bot.conf
# Written by trading-bot/setup_vm_extras.sh
[Journal]
SystemMaxUse=200M
MaxRetentionSec=30day
EOF
systemctl restart systemd-journald 2>/dev/null || true

echo "3. apt cache..."
apt-get clean 2>/dev/null || true

df -h / | tail -1 | awk '{print "Disk: " $3 " used of " $2 " (" $5 ")"}'
echo "VM extras done."
