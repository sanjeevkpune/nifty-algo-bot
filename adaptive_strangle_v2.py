import sys
import os
import time
import datetime
import urllib.request
import json
import pandas as pd
import pyotp
import requests
import gspread
import traceback
from SmartApi import SmartConnect

# ==========================================
# 1. DYNAMIC CONFIGURATION (FROM GITHUB SECRETS / ENV)
# ==========================================
API_KEY = os.environ.get("API_KEY")
CLIENT_CODE = os.environ.get("CLIENT_CODE")
PIN = os.environ.get("PIN")
TOTP_SECRET = os.environ.get("TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

GSHEET_ID_V2 = os.environ.get("GSHEET_ID_V2")
SERVICE_ACCOUNT_FILE = "service_account.json"

# Validate that critical env variables exist
missing_env = [var for var, val in {
    "API_KEY": API_KEY, "CLIENT_CODE": CLIENT_CODE, "PIN": PIN, 
    "TOTP_SECRET": TOTP_SECRET, "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, 
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID, "GSHEET_ID_V2": GSHEET_ID_V2
}.items() if not val]

if missing_env:
    print(f"🚨 CRITICAL ERROR: Missing required environment variables/secrets: {', '.join(missing_env)}")
    sys.exit(1)

# ==========================================
# 2. FINAL OPTIMIZED PARAMETERS (V2.1 Uncapped)
# ==========================================
NIFTY_LOT_SIZE = 65
TRANCHE_1_QTY = NIFTY_LOT_SIZE * 5       # Fixed 5 Lots (325 qty) at 9:50 AM

DELTA_WIDTH_MULTIPLIER = 1.25            # Pushed to 1.25x for wider, safer strikes
MAX_OPENING_RANGE_PTS = 140.0            # Relaxed to 140 pts to maximize traded days & ROI
MAX_GAP_PERCENT = 0.60                   # Kept at 0.60% to filter catastrophic overnight gaps

MAX_PREMIUM_LOSS_PCT = 0.40              # Hard Stop-Loss: 40% expansion of combined initial premium

SPOT_ROLL_BUFFER = 40.0                  # Roll tested leg if Spot comes within 40 pts
ROLL_DISTANCE_PTS = 100                  # Distance (pts) to roll tested leg further OTM

NIFTY_SPOT_TOKEN = "99926000"

# ==========================================
# 3. ORDER & TURNOVER LEDGER
# ==========================================
class OrderLedger:
    def __init__(self):
        self.sell_turnover = 0.0
        self.buy_turnover = 0.0
        self.executed_orders = 0

    def record_trade(self, side, price, qty):
        turnover = float(price) * int(qty)
        if side.upper() == "SELL":
            self.sell_turnover += turnover
        elif side.upper() == "BUY":
            self.buy_turnover += turnover
        self.executed_orders += 1

    def calculate_charges(self):
        total_turnover = self.sell_turnover + self.buy_turnover
        brokerage = self.executed_orders * 20.00
        stt = self.sell_turnover * 0.0015
        exchange_charges = total_turnover * 0.000355299
        sebi_fee = total_turnover * 0.000001
        gst = (brokerage + exchange_charges + sebi_fee) * 0.18
        stamp_duty = self.buy_turnover * 0.00003
        return brokerage + stt + exchange_charges + sebi_fee + gst + stamp_duty

ledger = OrderLedger()

# ==========================================
# 4. CORE HELPER FUNCTIONS
# ==========================================
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5)
    except Exception as e:
        print(f"Telegram alert error: {e}")

def init_smartapi_session():
    smart_client = SmartConnect(api_key=API_KEY)
    for attempt in range(1, 4):
        try:
            totp_code = pyotp.TOTP(TOTP_SECRET).now()
            res = smart_client.generateSession(CLIENT_CODE, PIN, totp_code)
            if res and res.get("status"):
                print("SmartAPI Session authenticated successfully.")
                return smart_client
            print(f"Session attempt {attempt} rejected: {res}")
        except Exception as e:
            print(f"Session error on attempt {attempt}: {e}")
        time.sleep(3)
    send_telegram_alert("🚨 CRITICAL ERROR: Angel One authentication failed after 3 attempts.")
    sys.exit(1)

def wait_for_market_time(target_hour, target_minute):
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))

def get_nifty_options_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    data = json.loads(urllib.request.urlopen(url, timeout=15).read())
    df = pd.DataFrame(data)
    nifty_opt = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX')].copy()
    nifty_opt['strike'] = pd.to_numeric(nifty_opt['strike'], errors='coerce') / 100.0
    nifty_opt['expirydate'] = pd.to_datetime(nifty_opt['expiry'], format='%d%b%Y', errors='coerce').dt.date
    today = datetime.date.today()
    return nifty_opt[nifty_opt['expirydate'] >= today].copy()

def get_next_expiry(df):
    unique_expiries = sorted(df['expirydate'].dropna().unique())
    return unique_expiries[1] if len(unique_expiries) > 1 else unique_expiries[0]

def get_contract_by_expiry_and_strike(df, expiry_date, strike, opt_type):
    contracts = df[(df['expirydate'] == expiry_date) & (df['strike'] == float(strike)) & (df['symbol'].str.endswith(opt_type))]
    if not contracts.empty:
        return str(contracts.iloc[0]['symbol']), str(contracts.iloc[0]['token'])
    return None, None

def get_ltp(smart_client, exchange, symbol, token, retries=3):
    for attempt in range(retries):
        try:
            res = smart_client.ltpData(exchange, symbol, str(token))
            if res and res.get("status") and res.get("data"):
                ltp = float(res["data"].get("ltp", 0.0))
                if ltp > 0: return ltp
        except Exception:
            pass
        time.sleep(0.4 * (attempt + 1))
    return None

def get_nifty_spot_ohlc(smart_client):
    for tok in [NIFTY_SPOT_TOKEN, "26000"]:
        try:
            res = smart_client.getMarketData("FULL", {"NSE": [tok]})
            if res and res.get("status") and res.get("data") and res["data"].get("fetched"):
                q = res["data"]["fetched"][0]
                return {"ltp": float(q.get("ltp", 0)), "open": float(q.get("open", 0)), "high": float(q.get("high", 0)), "low": float(q.get("low", 0)), "close": float(q.get("close", 0))}
        except Exception:
            pass
    return None

def check_market_filters(smart_client):
    ohlc = get_nifty_spot_ohlc(smart_client)
    if not ohlc or ohlc["ltp"] <= 0:
        return False, 0.0, 0.0, 0.0, "Unable to fetch Nifty Spot OHLC data at 9:50 AM."

    spot = ohlc["ltp"]
    prev_close = ohlc["close"] if ohlc["close"] > 0 else spot
    day_high = ohlc["high"] if ohlc["high"] > 0 else spot
    day_low = ohlc["low"] if ohlc["low"] > 0 else spot

    gap_pct = ((spot - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
    opening_range = day_high - day_low

    if abs(gap_pct) > MAX_GAP_PERCENT:
        return False, spot, gap_pct, opening_range, f"Overnight Gap ({gap_pct:+.2f}%) exceeds {MAX_GAP_PERCENT}% limit."
    if opening_range > MAX_OPENING_RANGE_PTS:
        return False, spot, gap_pct, opening_range, f"9:15-9:50 Range ({opening_range:.2f} pts) exceeds {MAX_OPENING_RANGE_PTS} pt limit."
    return True, spot, gap_pct, opening_range, "PASSED"

def place_market_order(smart_client, symbol, token, side, qty):
    if not symbol or not token or qty <= 0: return None
    order_params = {
        "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": str(token),
        "transactiontype": side.upper(), "exchange": "NFO", "ordertype": "MARKET",
        "producttype": "INTRADAY", "duration": "DAY", "quantity": int(qty)
    }
    for _ in range(3):
        try:
            res = smart_client.placeOrder(order_params)
            if isinstance(res, str) and res.strip(): return res.strip()
            elif isinstance(res, dict) and res.get("status"):
                data = res.get("data")
                return data if isinstance(data, str) else str(data.get("orderid"))
        except Exception:
            pass
        time.sleep(0.5)
    return None

def get_order_book_dict(smart_client):
    order_map = {}
    try:
        ob = smart_client.orderBook()
        if ob and ob.get("status") and ob.get("data"):
            for order in ob["data"]:
                order_map[str(order.get("orderid", ""))] = (float(order.get("averageprice", 0)), str(order.get("orderstatus", "")).lower())
    except Exception:
        pass
    return order_map

def get_filled_price(smart_client, order_id, exchange, symbol, token, fallback_ltp=None):
    if order_id:
        ob = get_order_book_dict(smart_client)
        if str(order_id) in ob:
            avg_px, status = ob[str(order_id)]
            # Patch 2: Aggressive Ghost Position Protection
            if status == "rejected":
                send_telegram_alert(f"❌ ORDER REJECTED BY BROKER: {symbol}. Check Angel One App for margin/errors.")
                raise Exception(f"Order {order_id} rejected. Halting bot to prevent ghost positions.")
            if avg_px > 0: 
                return avg_px
                
    # Fallback to live LTP if order pending or API slow
    live_px = get_ltp(smart_client, exchange, symbol, token)
    return live_px if live_px and live_px > 0 else (fallback_ltp if fallback_ltp else 0.0)

def log_to_google_sheet(row_values):
    headers = [
        "Date", "Timestamp", "Strategy_Version", "Session_Status",
        "Nifty_Spot_950", "Gap_Pct", "ORB_915_950_Pts",
        "Initial_CE_Symbol", "Initial_PE_Symbol", "Initial_Combined_Premium",
        "Final_CE_Symbol", "Final_PE_Symbol", "Active_Qty", "Rolled_Status",
        "Exit_Reason", "Gross_PnL", "Charges", "Net_PnL"
    ]
    try:
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sh = gc.open_by_key(GSHEET_ID_V2)
        worksheet = sh.sheet1
        if not worksheet.row_values(1): worksheet.append_row(headers)
        worksheet.append_row(row_values)
    except Exception as e:
        print(f"Failed to log to Google Sheet: {e}")
        send_telegram_alert("⚠️ SHEET LOGGING FAILED\nTrade finished, but Google API timed out. Please log manually.")

# ==========================================
# 5. MAIN EXECUTION PIPELINE (9:50 AM ENTRY)
# ==========================================
def main():
    smart = init_smartapi_session()

    options_df = get_nifty_options_master()
    next_expiry_date = get_next_expiry(options_df)
    expiry_str = next_expiry_date.strftime("%d-%b-%Y")

    print("Waiting until 9:50 AM for entry execution...")
    wait_for_market_time(9, 50)

    is_safe, entry_spot, gap_pct, orb_pts, filter_msg = check_market_filters(smart)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    now_time_str = datetime.datetime.now().strftime("%H:%M:%S")

    if not is_safe:
        skip_alert = f"⚠️ STRATEGY SKIPPED ({today_str})\nReason: {filter_msg}"
        print(skip_alert); send_telegram_alert(skip_alert)
        log_to_google_sheet([today_str, now_time_str, "V2.1 Uncapped", "SKIPPED", round(entry_spot, 2), round(gap_pct, 2), round(orb_pts, 2), "N/A", "N/A", 0.0, "N/A", "N/A", 0, "No", filter_msg, 0.0, 0.0, 0.0])
        sys.exit(0)

    atm_strike = round(entry_spot / 50.0) * 50
    atm_ce_sym, atm_ce_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, atm_strike, "CE")
    atm_pe_sym, atm_pe_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, atm_strike, "PE")

    combined_atm = get_ltp(smart, "NFO", atm_ce_sym, atm_ce_tok) + get_ltp(smart, "NFO", atm_pe_sym, atm_pe_tok)
    strike_offset = combined_atm * DELTA_WIDTH_MULTIPLIER

    ce_strike = int(round((entry_spot + strike_offset) / 50.0) * 50)
    pe_strike = int(round((entry_spot - strike_offset) / 50.0) * 50)

    ce_sym, ce_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, ce_strike, "CE")
    pe_sym, pe_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, pe_strike, "PE")

    ce_avg = get_filled_price(smart, place_market_order(smart, ce_sym, ce_tok, "SELL", TRANCHE_1_QTY), "NFO", ce_sym, ce_tok, get_ltp(smart, "NFO", ce_sym, ce_tok))
    time.sleep(1)
    pe_avg = get_filled_price(smart, place_market_order(smart, pe_sym, pe_tok, "SELL", TRANCHE_1_QTY), "NFO", pe_sym, pe_tok, get_ltp(smart, "NFO", pe_sym, pe_tok))

    ledger.record_trade("SELL", ce_avg, TRANCHE_1_QTY)
    ledger.record_trade("SELL", pe_avg, TRANCHE_1_QTY)

    initial_ce_sym, initial_pe_sym = ce_sym, pe_sym
    initial_combined_premium = ce_avg + pe_avg
    active_qty = TRANCHE_1_QTY
    rolled_status = "No"
    realized_roll_pnl = 0.0
    exit_reason = "N/A"

    # Calculate exact dynamic SL based on 40% of collected premium
    max_monetary_loss = (initial_combined_premium * MAX_PREMIUM_LOSS_PCT) * active_qty

    send_telegram_alert(
        f"🟢 STRANGLE DEPLOYED (5 Lots | {active_qty} Qty | 9:50 AM)\n"
        f"Expiry: {expiry_str} | Spot: {entry_spot:.2f}\n"
        f"CE: {ce_sym} @ ₹{ce_avg:.2f} | PE: {pe_sym} @ ₹{pe_avg:.2f}\n"
        f"Combined Premium: ₹{initial_combined_premium:.2f}\n"
        f"Dynamic SL: -₹{max_monetary_loss:,.0f} (40% Exp)"
    )

    last_alert_minute = datetime.datetime.now().minute

    while True:
        now = datetime.datetime.now()
        live_spot = get_ltp(smart, "NSE", "NIFTY", NIFTY_SPOT_TOKEN)
        live_ce = get_ltp(smart, "NFO", ce_sym, ce_tok)
        live_pe = get_ltp(smart, "NFO", pe_sym, pe_tok)

        if not live_spot or not live_ce or not live_pe:
            time.sleep(2); continue

        open_pnl = ((ce_avg - live_ce) + (pe_avg - live_pe)) * active_qty
        total_running_pnl = realized_roll_pnl + open_pnl

        if now.minute % 30 == 0 and now.minute != last_alert_minute:
            last_alert_minute = now.minute
            send_telegram_alert(f"⏱️ UPDATE ({now.strftime('%I:%M %p')}): P&L ₹{total_running_pnl:,.2f} | Spot: {live_spot:.2f}")

        # 1. Check Hard Stop Loss (40% Premium Expansion)
        if total_running_pnl <= -max_monetary_loss:
            exit_reason = f"Hard SL Hit (40% Premium Loss)"
            break

        # 2. Check Spot Roll Boundaries
        if rolled_status == "No":
            if live_spot >= (ce_strike - SPOT_ROLL_BUFFER):
                new_ce_strike = ce_strike + ROLL_DISTANCE_PTS
                new_ce_sym, new_ce_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, new_ce_strike, "CE")
                if not new_ce_sym: break
                
                # Buy back losing CE
                exit_ce_px = get_filled_price(smart, place_market_order(smart, ce_sym, ce_tok, "BUY", active_qty), "NFO", ce_sym, ce_tok, live_ce)
                ledger.record_trade("BUY", exit_ce_px, active_qty)
                realized_roll_pnl += (ce_avg - exit_ce_px) * active_qty
                
                leg_loss = (ce_avg - exit_ce_px) * active_qty
                old_ce_sym = ce_sym

                # Sell new OTM CE
                ce_avg = get_filled_price(smart, place_market_order(smart, new_ce_sym, new_ce_tok, "SELL", active_qty), "NFO", new_ce_sym, new_ce_tok, get_ltp(smart, "NFO", new_ce_sym, new_ce_tok))
                ledger.record_trade("SELL", ce_avg, active_qty)
                ce_strike, ce_sym, ce_tok = new_ce_strike, new_ce_sym, new_ce_tok
                rolled_status = "Yes (Call Rolled Up)"
                send_telegram_alert(f"🔄 CALL {old_ce_sym} ROLLED UP to {ce_sym} @ ₹{ce_avg:.2f}\nBooked Leg Loss: ₹{leg_loss:,.2f}")

            elif live_spot <= (pe_strike + SPOT_ROLL_BUFFER):
                new_pe_strike = pe_strike - ROLL_DISTANCE_PTS
                new_pe_sym, new_pe_tok = get_contract_by_expiry_and_strike(options_df, next_expiry_date, new_pe_strike, "PE")
                if not new_pe_sym: break

                # Buy back losing PE
                exit_pe_px = get_filled_price(smart, place_market_order(smart, pe_sym, pe_tok, "BUY", active_qty), "NFO", pe_sym, pe_tok, live_pe)
                ledger.record_trade("BUY", exit_pe_px, active_qty)
                realized_roll_pnl += (pe_avg - exit_pe_px) * active_qty
                
                leg_loss = (pe_avg - exit_pe_px) * active_qty
                old_pe_sym = pe_sym

                # Sell new OTM PE
                pe_avg = get_filled_price(smart, place_market_order(smart, new_pe_sym, new_pe_tok, "SELL", active_qty), "NFO", new_pe_sym, new_pe_tok, get_ltp(smart, "NFO", new_pe_sym, new_pe_tok))
                ledger.record_trade("SELL", pe_avg, active_qty)
                pe_strike, pe_sym, pe_tok = new_pe_strike, new_pe_sym, new_pe_tok
                rolled_status = "Yes (Put Rolled Down)"
                send_telegram_alert(f"🔄 PUT {old_pe_sym} ROLLED DOWN to {pe_sym} @ ₹{pe_avg:.2f}\nBooked Leg Loss: ₹{leg_loss:,.2f}")

        # 3. Check Time Exit (Hold till 3:10 PM for Uncapped Theta)
        if now.hour > 15 or (now.hour == 15 and now.minute >= 10):
            exit_reason = "Time Exit (3:10 PM)" if rolled_status == "No" else "Rolled & Time Exit (3:10 PM)"
            break

        time.sleep(2)

    # ==========================================
    # 6. SQUARE-OFF & LOGGING
    # ==========================================
    final_ce_px = get_filled_price(smart, place_market_order(smart, ce_sym, ce_tok, "BUY", active_qty), "NFO", ce_sym, ce_tok, get_ltp(smart, "NFO", ce_sym, ce_tok))
    final_pe_px = get_filled_price(smart, place_market_order(smart, pe_sym, pe_tok, "BUY", active_qty), "NFO", ce_sym, ce_tok, get_ltp(smart, "NFO", pe_sym, pe_tok))

    ledger.record_trade("BUY", final_ce_px, active_qty)
    ledger.record_trade("BUY", final_pe_px, active_qty)

    gross_pnl = realized_roll_pnl + (((ce_avg - final_ce_px) + (pe_avg - final_pe_px)) * active_qty)
    total_charges = ledger.calculate_charges()
    net_pnl = gross_pnl - total_charges

    send_telegram_alert(f"🏁 BOT CLOSED | Exit: {exit_reason} | Net P&L: ₹{net_pnl:+,.2f}")

    log_to_google_sheet([
        today_str, datetime.datetime.now().strftime("%H:%M:%S"), "V2.1 Uncapped", "TRADED",
        round(entry_spot, 2), round(gap_pct, 2), round(orb_pts, 2),
        initial_ce_sym, initial_pe_sym, round(initial_combined_premium, 2),
        ce_sym, pe_sym, active_qty, rolled_status, exit_reason,
        round(gross_pnl, 2), round(total_charges, 2), round(net_pnl, 2)
    ])

# Patch 1: Global Catch-All with Telegram SOS
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error_msg = traceback.format_exc()
        send_telegram_alert(f"🚨 FATAL BOT CRASH 🚨\nManual intervention required immediately!\n\nError: {str(e)}")
        print(f"CRITICAL FAILURE:\n{error_msg}")
        sys.exit(1)
