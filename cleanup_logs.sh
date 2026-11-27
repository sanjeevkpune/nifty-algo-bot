#!/bin/bash
# cleanup_logs.sh  --  daily disk housekeeping for ~/trading-bot (cron as the bot user, 09:00 IST, as startup.sh sets it;
# no bot is mid-trade then). Deletes only old runtime files; never today's state, the Dhan token cache,
# the ETF bot's records (state/*_state.json) or the trade CSVs.
# System-wide limits (journald size, apt cache) are set by setup_vm_extras.sh as root on every boot.
#
# Ages (days) can be changed in config.env: CLEAN_SMARTAPI_DAYS, CLEAN_STATE_DAYS, CLEAN_LOCK_DAYS.

[ -f /etc/trade-config/config.env ] && { set -a; source /etc/trade-config/config.env 2>/dev/null; set +a; }

PROJECT_DIR="${CLEAN_PROJECT_DIR:-/home/kumar_sanjeev_pune/trading-bot}"
LOGS_DIR="$PROJECT_DIR/logs"
SMARTAPI_DAYS="${CLEAN_SMARTAPI_DAYS:-3}"   # SmartAPI's logs/<date>/ folders
STATE_DAYS="${CLEAN_STATE_DAYS:-45}"        # Dhan bots' daily state/lemonn_*_<yyyymmdd>.json
LOCK_DAYS="${CLEAN_LOCK_DAYS:-10}"          # adaptive strangle run_lock_<date>_*.json
OUT="$PROJECT_DIR/cleanup.log"

log() { echo "$(date '+%F %T') $*" >> "$OUT"; }
before=$(df -P / | awk 'NR==2 {print $5}')

# 1. SmartAPI date folders (logs/2026-11-20/app.log ...)
if [ -d "$LOGS_DIR" ]; then
    find "$LOGS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$SMARTAPI_DAYS" -exec rm -rf {} +
fi

# 2. Adaptive strangle daily lock files (the daily summary only needs today's)
find "$PROJECT_DIR" -maxdepth 1 -name "run_lock_*.json" -type f -mtime +"$LOCK_DAYS" -delete

# 3. Dhan bots' per-day state files and leftovers from interrupted saves (dhan_token.json is kept)
if [ -d "$PROJECT_DIR/state" ]; then
    find "$PROJECT_DIR/state" -maxdepth 1 -type f -name "lemonn_*_2*.json" -mtime +"$STATE_DAYS" -delete
    find "$PROJECT_DIR/state" -maxdepth 1 -type f -name "*.tmp" -mtime +1 -delete
fi

# 4. Rotated logs that logrotate left behind (it keeps 8 / 60 days; this is a safety net)
find "$PROJECT_DIR" "$PROJECT_DIR/etf_dukan/logs" "$PROJECT_DIR/dashboard" -maxdepth 1 -type f \
     \( -name "*.log.[0-9]*" -o -name "*.log-*" \) -mtime +60 -delete 2>/dev/null

# 5. ETF ki Dukan bot: old daily locks, price caches and state backups (the state file itself is never touched)
EKD_DIR="$PROJECT_DIR/etf_dukan"
if [ -d "$EKD_DIR" ]; then
    find "$EKD_DIR/locks" -maxdepth 1 -name "run_*.json" -type f -mtime +10 -delete 2>/dev/null
    find "$EKD_DIR/cache" -maxdepth 1 -name "closes_*.json" -type f -mtime +3 -delete 2>/dev/null
    find "$EKD_DIR/state/backups" -maxdepth 1 -name "*.json" -type f -mtime +30 -delete 2>/dev/null
fi

# 6. Python caches
find "$PROJECT_DIR" -type d -name "__pycache__" -prune -mtime +7 -exec rm -rf {} + 2>/dev/null

# 7. Keep this script's own log short
[ -f "$OUT" ] && [ "$(wc -l < "$OUT")" -gt 500 ] && tail -n 200 "$OUT" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"

after=$(df -P / | awk 'NR==2 {print $5}')
log "cleanup done: disk ${before} -> ${after} used"

# 8. Disk alert: one Telegram message if the disk is still above DISK_WARN_PCT (default 80%)
pct=${after%\%}
if [ -n "$pct" ] && [ "$pct" -ge "${DISK_WARN_PCT:-80}" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    top=$(du -sh "$PROJECT_DIR"/* "$PROJECT_DIR"/etf_dukan/* 2>/dev/null | sort -rh | head -5 | tr '\t' ' ' | tr '\n' ';')
    curl -s -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=⚠️ VM disk ${after} full after the daily cleanup. Biggest: ${top}" >/dev/null
fi
exit 0
