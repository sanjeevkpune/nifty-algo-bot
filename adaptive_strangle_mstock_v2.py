import os
import sys
import time
import datetime
import logging
import pyotp
import requests
import pandas as pd
import numpy as np
import gspread
from tradingapi_a.mconnect import MConnect
from logzero import logger

# --- 1. CONFIGURATION & ENVIRONMENT SETUP ---
API_KEY = os.environ.get("MSTOCK_API_KEY")
CLIENT_CODE = os.environ.get("MSTOCK_CLIENT_CODE")
PIN = os.environ.get("MSTOCK_PIN")
TOTP_SECRET = os.environ.get("MSTOCK_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_ID_V2 = os.environ.get("GSHEET_ID_V2")
SERVICE_ACCOUNT_FILE = "service_account.json"

# Quantity configuration: 5 Lots * 65 Nifty Lot Size = 325 quantity
NIFTY_LOT_SIZE = 325
SL_PERCENT = 30.0  # 30% Stop loss on individual leg premium
ROLL_DISTANCE_PTS = 100

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[mStock Strangle V2] {message}", "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

def init_mstock_session():
    try:
        logger.info("Initializing mStock session...")
        mconnect = MConnect()
        login_res = mconnect.login(CLIENT_CODE, PIN)
        logger.info(f"mStock Login Response: {login_res}")
        
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        gen_res = mconnect.generate_session(API_KEY, totp_code, "")
        logger.info("mStock Session Generated Successfully!")
        return mconnect
    except Exception as e:
        logger.error(f"mStock Authentication Failed: {e}")
        send_telegram_alert(f"❌ mStock Auth Failed: {e}")
        sys.exit(1)

def wait_for_market_time(target_hour, target_minute):
    while True:
        now = datetime.datetime.now()
        if now.hour > target_hour or (now.hour == target_hour and now.minute >= target_minute):
            break
        time.sleep(10)

def get_nifty_spot_ohlc(mconnect):
    try:
        # Fetching Nifty 50 Index LTP/OHLC via mStock
        res = mconnect.get_ltp("NSE", ["Nifty 50"])
        # Adjust parsing depending on mStock payload structure
        spot_price = float(res.get("data", {}).get("ltp", 22000.0))
        return {"open": spot_price, "high": spot_price, "low": spot_price, "close": spot_price, "ltp": spot_price}
    except Exception as e:
        logger.error(f"Error fetching Nifty spot: {e}")
        return {"open": 22000.0, "high": 22000.0, "low": 22000.0, "close": 22000.0, "ltp": 22000.0}

def get_mstock_options_master(mconnect):
    try:
        instruments = mconnect.get_instruments("NFO")
        df = pd.DataFrame(instruments.get("data", []))
        return df
    except Exception as e:
        logger.error(f"Failed to fetch mStock options master: {e}")
        return pd.DataFrame()

def get_next_expiry(df):
    if df.empty or 'expiry' not in df.columns:
        return datetime.date.today().strftime("%d-%b-%Y").upper()
    expiries = sorted(df['expiry'].unique())
    today = datetime.date.today()
    for exp in expiries:
        # Parse expiry format appropriately
        exp_date = pd.to_datetime(exp).date()
        if exp_date >= today:
            return exp
    return expiries[0]

def get_contract_by_expiry_and_strike(df, expiry, strike, opt_type):
    filtered = df[(df['expiry'] == expiry) & (df['strike'] == float(strike)) & (df['instrumenttype'] == opt_type)]
    if not filtered.empty:
        return filtered.iloc[0]['tradingsymbol'], filtered.iloc[0]['symboltoken']
    return None, None

def get_ltp(mconnect, exchange, symbol, token):
    try:
        res = mconnect.get_ltp(exchange, [symbol])
        return float(res.get("data", {}).get("ltp", 0.0))
    except Exception as e:
        logger.error(f"Failed to fetch LTP for {symbol}: {e}")
        return 0.0

def place_order(mconnect, symbol, token, transaction_type, price=0.0, order_type="MARKET"):
    try:
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "exchange": "NFO",
            "transaction_type": transaction_type,
            "order_type": order_type,
            "quantity": str(NIFTY_LOT_SIZE),
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(price),
            "trigger_price": "0"
        }
        res = mconnect.place_order(**order_params)
        logger.info(f"Order Placed ({transaction_type} {symbol}): {res}")
        return res
    except Exception as e:
        logger.error(f"Order Placement Error for {symbol}: {e}")
        return None

def log_to_gsheet(data_row):
    try:
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sh = gc.open_by_key(GSHEET_ID_V2)
        sh.sheet1.append_row(data_row)
    except Exception as e:
        logger.error(f"Failed to log to Google Sheets: {e}")

def run_strategy():
    logger.info("Initializing mStock Adaptive Strangle Strategy (10:00 AM IST - 5 Lots)...")
    send_telegram_alert("🤖 mStock Bot Initialized. Waiting for 10:00 AM IST execution window...")

    # Wait until 10:00 AM IST
    wait_for_market_time(10, 0)
    
    mconnect = init_mstock_session()
    
    # 1. Fetch Spot and Filter Check
    spot_data = get_nifty_spot_ohlc(mconnect)
    spot = spot_data["ltp"]
    atm_strike = round(spot / 50.0) * 50
    
    logger.info(f"Market Open Verified. Nifty Spot: {spot} | ATM: {atm_strike}")
    send_telegram_alert(f"📊 mStock Strategy Triggered. Nifty Spot: {spot:.2f} | ATM: {atm_strike}")

    # 2. Select Strikes (Strangle: OTM CE and OTM PE, e.g., ATM + 200 / ATM - 200)
    options_df = get_mstock_options_master(mconnect)
    expiry = get_next_expiry(options_df)
    
    ce_strike = atm_strike + 200
    pe_strike = atm_strike - 200
    
    ce_sym, ce_token = get_contract_by_expiry_and_strike(options_df, expiry, ce_strike, "CE")
    pe_sym, pe_token = get_contract_by_expiry_and_strike(options_df, expiry, pe_strike, "PE")
    
    if not ce_sym or not pe_sym:
        logger.error("Could not find valid option contracts for the specified strikes.")
        send_telegram_alert("❌ Error: Failed to resolve option contracts for mStock strategy.")
        return

    # 3. Sell (Short) Strangling Legs (5 Lots)
    logger.info(f"Shorting CE: {ce_sym} & PE: {pe_sym} with {NIFTY_LOT_SIZE} quantity (5 Lots)")
    ce_order = place_order(mconnect, ce_sym, ce_token, "SELL")
    pe_order = place_order(mconnect, pe_sym, pe_token, "SELL")
    
    ce_entry_px = get_ltp(mconnect, "NFO", ce_sym, ce_token)
    pe_entry_px = get_ltp(mconnect, "NFO", pe_sym, pe_token)
    
    send_telegram_alert(f"📉 *Strangle Position Entered (mStock - 5 Lots)*\nShort CE: {ce_sym} @ ~{ce_entry_px}\nShort PE: {pe_sym} @ ~{pe_entry_px}")

    # 4. Monitoring Loop & Stop Loss / Adjustment Tracking
    ce_sl = ce_entry_px * (1 + SL_PERCENT / 100.0)
    pe_sl = pe_entry_px * (1 + SL_PERCENT / 100.0)
    
    running = True
    while running:
        try:
            now = datetime.datetime.now()
            # Auto-square off at 3:15 PM IST
            if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                logger.info("Square-off time reached. Exiting positions...")
                place_order(mconnect, ce_sym, ce_token, "BUY")
                place_order(mconnect, pe_sym, pe_token, "BUY")
                send_telegram_alert("🔒 Market Square-off time reached. mStock positions closed.")
                break

            current_ce_ltp = get_ltp(mconnect, "NFO", ce_sym, ce_token)
            current_pe_ltp = get_ltp(mconnect, "NFO", pe_sym, pe_token)

            # Check CE Stop Loss
            if current_ce_ltp >= ce_sl:
                logger.warning(f"CE Stop Loss Hit! LTP: {current_ce_ltp} >= SL: {ce_sl}")
                place_order(mconnect, ce_sym, ce_token, "BUY")
                send_telegram_alert(f"⚠️ mStock CE Stop Loss Triggered at {current_ce_ltp}. Exiting leg.")
                # Implement roll or single-leg exit logic here matching v2
                break

            # Check PE Stop Loss
            if current_pe_ltp >= pe_sl:
                logger.warning(f"PE Stop Loss Hit! LTP: {current_pe_ltp} >= SL: {pe_sl}")
                place_order(mconnect, pe_sym, pe_token, "BUY")
                send_telegram_alert(f"⚠️ mStock PE Stop Loss Triggered at {current_pe_ltp}. Exiting leg.")
                break

            time.sleep(10)
        except Exception as e:
            logger.error(f"Error in mStock monitoring loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
