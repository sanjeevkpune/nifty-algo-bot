#!/bin/bash

# Base directories
PROJECT_DIR="/home/kumar_sanjeev_pune/trading-bot"
LOGS_DIR="$PROJECT_DIR/logs"

# 1. Delete date-named subdirectories inside logs/ older than 2 days (e.g., YYYY-MM-DD format)
if [ -d "$LOGS_DIR" ]; then
    find "$LOGS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +2 -exec rm -rf {} +
fi

# 2. Delete run_lock_* files older than 2 days from the project root
find "$PROJECT_DIR" -maxdepth 1 -name "run_lock_*" -type f -mtime +2 -exec rm -f {} +
