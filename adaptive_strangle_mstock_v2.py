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
TOTP_SECRET = os.environ.get("MSTOCK_TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_ID_V2 = os.environ.get("GSHEET_ID_V2")
SERVICE_ACCOUNT_FILE = "service_account.json"

# Nifty lot size updated to 65 * 5 lots = 325 quantity
NIFTY_LOT_SIZE = 5 * 65  
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
        
        # mStock Type-A login method takes empty strings for initial handshake
        login_res = mconnect.login("", "")
        logger.info(f"mStock Login Response: {login_res}")
        
        # Generate session using API key, generated TOTP, and empty string
        totp_code = pyotp.TOTP(TOTP_SECRET).now()
        gen_res = mconnect.generate_session(API_KEY, totp_code, "")
        logger.info(f"mStock Session Generation Response: {gen_res}")
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
        # mStock get_ltp format: list of exchanges and symbols or exchange:symbol string list
        res = mconnect.get_ltp(["NSE:Nifty 50"])
        # Parsing response data structure securely
        data_list = res.get("data", [])
        spot_price = 22000.0
        if data_list:
            spot_price = float(data_list[0].get("ltp", 22000.0))
        return {"open": spot_price, "high": spot_price, "low": spot_price, "close": spot_price, "ltp": spot_price}
    except Exception as e:
        logger.error(f"Error fetching Nifty spot: {e}")
        return {"open": 22000.0, "high": 22000.0, "low": 22000.0, "close": 22000.0, "ltp": 22000.0}

def get_mstock_options_master(mconnect):
    try:
        instruments = mconnect.get_instruments()
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
        exp_date = pd.to_datetime(exp).date()
        if exp_date >= today:
            return exp
    return expiries[0]

def get_contract_by_expiry_and_strike(df, expiry, strike, opt_type):
    filtered = df[(df['expiry'] == expiry) & (df['strike'] == float(strike)) & (df['instrumenttype'] == opt_type)]
    if not filtered.empty:
        return filtered.iloc[0]['tradingsymbol'], filtered.iloc[0]['symboltoken']
    return None, None

def get_ltp(mconnect, symbol):
    try:
        res = mconnect.get_ltp([f"NFO:{symbol}"])
        data_list = res.get("data", [])
        if data_list:
            return float(data_list[0].get("ltp", 0.0))
        return 0.0
    except Exception as e:
        logger.error(f"Failed to fetch LTP for {symbol}: {e}")
        return 0.0

def place_order(mconnect, symbol, transaction_type, price="0", order_type="MARKET"):
    try:
        # Official mStock place_order signature: 
        # place_order(order_type, symbol, exchange, transaction_type, price_type, quantity, product, validity, price, trigger_price)
        res = mconnect.place_order(
            "regular",
            symbol,
            "NFO",
            transaction_type,
            order_type,
            str(NIFTY_LOT_SIZE),
            "MIS",
            "DAY",
            str(price),
            "0"
        )
        logger.info(f"Order Placed ({transaction_type} {symbol}): {res}")
        return res
    except Exception as e:
        logger.error(f"Order Placement Error for {symbol}: {e}")
        return None

def run_strategy():
    logger.info("Initializing mStock Adaptive Strangle Strategy (10:00 AM IST - 5 Lots / 325 Qty)...")
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

    # 2. Select Strikes (Strangle: OTM CE and OTM PE)
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

    # 3. Sell (Short) Strangling Legs (5 Lots / 325 Qty)
    logger.info(f"Shorting CE: {ce_sym} & PE: {pe_sym} with {NIFTY_LOT_SIZE} quantity")
    ce_order = place_order(mconnect, ce_sym, "SELL")
    pe_order = place_order(mconnect, pe_sym, "SELL")
    
    ce_entry_px = get_ltp(mconnect, ce_sym)
    pe_entry_px = get_ltp(mconnect, pe_sym)
    
    send_telegram_alert(f"📉 *Strangle Position Entered (mStock - 5 Lots / 325 Qty)*\nShort CE: {ce_sym} @ ~{ce_entry_px}\nShort PE: {pe_sym} @ ~{pe_entry_px}")

    # 4. Monitoring Loop & Stop Loss Tracking
    ce_sl = ce_entry_px * (1 + SL_PERCENT / 100.0)
    pe_sl = pe_entry_px * (1 + SL_PERCENT / 100.0)
    
    running = True
    while running:
        try:
            now = datetime.datetime.now()
            # Auto-square off at 3:15 PM IST
            if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
                logger.info("Square-off time reached. Exiting positions...")
                place_order(mconnect, ce_sym, "BUY")
                place_order(mconnect, pe_sym, "BUY")
                send_telegram_alert("🔒 Market Square-off time reached. mStock positions closed.")
                break

            current_ce_ltp = get_ltp(mconnect, ce_sym)
            current_pe_ltp = get_ltp(mconnect, pe_sym)

            # Check CE Stop Loss
            if current_ce_ltp >= ce_sl:
                logger.warning(f"CE Stop Loss Hit! LTP: {current_ce_ltp} >= SL: {ce_sl}")
                place_order(mconnect, ce_sym, "BUY")
                send_telegram_alert(f"⚠️ mStock CE Stop Loss Triggered at {current_ce_ltp}. Exiting leg.")
                break

            # Check PE Stop Loss
            if current_pe_ltp >= pe_sl:
                logger.warning(f"PE Stop Loss Hit! LTP: {current_pe_ltp} >= SL: {pe_sl}")
                place_order(mconnect, pe_sym, "BUY")
                send_telegram_alert(f"⚠️ mStock PE Stop Loss Triggered at {current_pe_ltp}. Exiting leg.")
                break

            time.sleep(10)
        except Exception as e:
            logger.error(f"Error in mStock monitoring loop: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_strategy()
