import os
import sys
import time
import datetime
import logging
import pyotp
import pandas as pd
import gspread
from tradingapi_a.mconnect import MConnect
from logzero import logger

# Configuration from Environment Variables
API_KEY = os.environ.get("MSTOCK_API_KEY")
CLIENT_CODE = os.environ.get("MSTOCK_CLIENT_CODE")
PIN = os.environ.get("MSTOCK_PIN")
TOTP_SECRET = os.environ.get("MSTOCK_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_ID_V3 = os.environ.get("GSHEET_ID_V3")
SERVICE_ACCOUNT_FILE = "service_account.json"

LOT_SIZE = 5 * 65  # Nifty lot size is 25 * 5 lots = 325 quantity

def send_telegram_alert(message):
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[mStock Bot] {message}", "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

def init_mstock_session():
    try:
        mconnect = MConnect()
        login_res = mconnect.login(CLIENT_CODE, PIN)
        logger.info(f"mStock Login Response: {login_res}")
        
        # Generate session using API key and generated TOTP
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        gen_res = mconnect.generate_session(API_KEY, totp_code, "")
        logger.info("mStock Session Initialized Successfully!")
        return mconnect
    except Exception as e:
        logger.error(f"mStock Authentication Failed: {e}")
        send_telegram_alert(f"❌ mStock Auth Failed: {e}")
        sys.exit(1)

def run_mstock_strategy():
    logger.info("Starting mStock Adaptive Strangle Strategy (5 Lots)...")
    send_telegram_alert("🚀 mStock Strangle Strategy Started for the Day.")
    
    mconnect = init_mstock_session()
    
    # Example safety delay matching your 10:00 AM launch schedule
    time.sleep(5)
    
    # Place your strategy execution logic here using mconnect methods:
    # order_resp = mconnect.place_order(...)
    
    send_telegram_alert("✅ mStock Strategy Execution Block Completed.")

if __name__ == "__main__":
    run_mstock_strategy()
