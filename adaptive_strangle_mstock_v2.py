import sys
import os
import time
import datetime
import json
import pandas as pd
import pyotp
import requests
import gspread
from tradingapi_a.mconnect import MConnect
from logzero import logger

# ==========================================
# 1. DYNAMIC CONFIGURATION (FROM GITHUB SECRETS / ENV)
# ==========================================
API_KEY = os.environ.get("MSTOCK_API_KEY")
CLIENT_CODE = os.environ.get("MSTOCK_CLIENT_CODE")
PIN = os.environ.get("MSTOCK_PIN")  # mStock uses PIN / Password for login()
TOTP_SECRET = os.environ.get("MSTOCK_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

GSHEET_ID_V3 = os.environ.get("GSHEET_ID_V3")
SERVICE_ACCOUNT_FILE = "service_account.json"

# Validate critical env variables including PIN/Password
missing_env = [var for var, val in {
    "MSTOCK_API_KEY": API_KEY, "MSTOCK_CLIENT_CODE": CLIENT_CODE, "MSTOCK_PIN": PIN,
    "MSTOCK_TOTP_SECRET": TOTP_SECRET, "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, 
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID, "GSHEET_ID_V3": GSHEET_ID_V3
}.items() if not val]

if missing_env:
    print(f"🚨 CRITICAL ERROR: Missing required environment variables/secrets: {', '.join(missing_env)}")
    sys.exit(1)

NIFTY_LOT_SIZE = 65
TRANCHE_1_QTY = NIFTY_LOT_SIZE * 5       # 5 Lots (325 qty) at 10:00 AM IST

DELTA_WIDTH_MULTIPLIER = 1.15            # ~18-Delta OTM strike distance
TARGET_DECAY_POINTS = 30.0               # Standard target: 30 points combined decay
SALVAGE_DECAY_POINTS = 12.0              # Post-roll salvage target: 12 points net decay
MAX_MONETARY_LOSS = 15000.0              # Hard gross loss ceiling for 5 lots

MAX_GAP_PERCENT = 0.60                   # Pre-market gap threshold (%)
MAX_OPENING_RANGE_PTS = 110.0            # Max High-Low spread (pts)

SPOT_ROLL_BUFFER = 40.0                  # Roll tested leg if Spot comes within 40 pts
SPOT_HARD_EXIT_BUFFER = 30.0             # Hard exit if Spot comes within 30 pts
ROLL_DISTANCE_PTS = 100                  # Distance (pts) to roll tested leg further OTM

# ==========================================
# 2. ORDER & TURNOVER LEDGER
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
# 3. CORE HELPER FUNCTIONS
# ==========================================
def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": f"[mStock Strangle V2] {message}"}, timeout=5)
    except Exception as e:
        print(f"Telegram alert error: {e}")

def init_mstock_session():
    for attempt in range(1, 4):
        try:
            logger.info("Initializing mStock session...")
            mconnect = MConnect()
            
            # Pass username (client code) and password (PIN) into login method
            login_res = mconnect.login(CLIENT_CODE, PIN)
            logger.info(f"mStock Login Response: {login_res}")
            
            totp_code = pyotp.TOTP(TOTP_SECRET).now()
            gen_res = mconnect.generate_session(API_KEY, totp_code, "")
            logger.info(f"mStock Session Generation Response: {gen_res}")
            
            if gen_res and (str(gen_res.get("status")).lower() == "true" or gen_res.get("status") is True or "data" in gen_res):
                print("mStock Type-A Session authenticated successfully.")
                return mconnect
            print(f"mStock Session attempt {attempt} rejected: {gen_res}")
        except Exception as e:
            print(f"Session error on attempt {attempt}: {e}")
        time.sleep(3)
    send_telegram_alert("🚨 mStock CRITICAL ERROR: Authentication failed after 3 attempts.")
    sys.exit(1)

def wait_for_market_time(target_hour, target_minute):
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))

def get_mstock_options_master(mconnect):
    try:
        instruments = mconnect.get_instruments()
        data_list = instruments.get("data", []) if isinstance(instruments, dict) else []
        df = pd.DataFrame(data_list)
        if df.empty:
            return pd.DataFrame()
        
        if 'name' in df.columns:
            nifty_opt = df[(df['name'] == 'NIFTY') & (df['instrumenttype'].isin(['OPTIDX', 'CE', 'PE']))].copy()
        elif 'tradingsymbol' in df.columns:
            nifty_opt = df[df['tradingsymbol'].str.startswith('NIFTY')].copy()
        else:
            nifty_opt = df.copy()

        if 'strike' in nifty_opt.columns:
            nifty_opt['strike'] = pd.to_numeric(nifty_opt['strike'], errors='coerce')
        if 'expiry' in nifty_opt.columns:
            nifty_opt['expirydate'] = pd.to_datetime(nifty_opt['expiry'], errors='coerce').dt.date
        
        today = datetime.date.today()
        if 'expirydate' in nifty_opt.columns:
            nifty_opt = nifty_opt[nifty_opt['expirydate'] >= today].copy()
        return nifty_opt
    except Exception as e:
        print(f"Error fetching options master: {e}")
        return pd.DataFrame()

def get_next_expiry(df):
    if df.empty or 'expirydate' not in df.columns:
        return datetime.date.today()
    unique_expiries = sorted(df['expirydate'].dropna().unique())
    return unique_expiries[0] if len(unique_expiries) > 0 else datetime.date.today()

def get_contract_by_expiry_and_strike(df, expiry_date, strike, opt_type):
    if df.empty:
        return None, None
    matches = df[(df['expirydate'] == expiry_date) & (df['strike'] == float(strike)) & (df['instrumenttype'] == opt_type)]
    if matches.empty and 'tradingsymbol' in df.columns:
        matches = df[(df['expirydate'] == expiry_date) & (df['strike'] == float(strike)) & (df['tradingsymbol'].str.endswith(opt_type))]
    
    if not matches.empty:
        sym_col = 'tradingsymbol' if 'tradingsymbol' in matches.columns else 'symbol'
        tok_col = 'symboltoken' if 'symboltoken' in matches.columns else ('token' if 'token' in matches.columns else 'instrument_token')
        return str(matches.iloc[0][sym_col]), str(matches.iloc[0][tok_col])
    return None, None

def get_ltp(mconnect, symbol):
    try:
        res = mconnect.get_ltp([f"NFO:{symbol}"])
        if res and res.get("data"):
            data_list = res.get("data")
            if isinstance(data_list, list) and len(data_list) > 0:
                ltp = float(data_list[0].get("ltp", 0.0))
                if ltp > 0: return ltp
            elif isinstance(data_list, dict):
                ltp = float(data_list.get("ltp", 0.0))
                if ltp > 0: return ltp
    except Exception:
        pass
    return None

def get_nifty_spot_ohlc(mconnect):
    try:
        res = mconnect.get_ltp(["NSE:Nifty 50"])
        if res and res.get("data"):
            data_list = res.get("data")
            if isinstance(data_list, list) and len(data_list) > 0:
                q = data_list[0]
                return {"ltp": float(q.get("ltp", 0)), "open": float(q.get("open", 0)), "high": float(q.get("high", 0)), "low": float(q.get("low", 0)), "close": float(q.get("close", 0))}
    except Exception:
        pass
    return {"ltp": 22000.0, "open": 22000.0, "high": 22000.0, "low": 22000.0, "close": 22000.0}

def check_market_filters(mconnect):
    ohlc = get_nifty_spot_ohlc(mconnect)
    spot = ohlc["ltp"]
    prev_close = ohlc["close"] if ohlc["close"] > 0 else spot
    day_high = ohlc["high"] if ohlc["high"] > 0 else spot
    day_low = ohlc["low"] if ohlc["low"] > 0 else spot

    gap_pct = ((spot - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
    opening_range = day_high - day_low

    if abs(gap_pct) > MAX_GAP_PERCENT:
        return False, spot, gap_pct, opening_range, f"Overnight Gap ({gap_pct:+.2f}%) exceeds limit."
    if opening_range > MAX_OPENING_RANGE_PTS:
        return False, spot, gap_pct, opening_range, f"Opening Range ({opening_range:.2f} pts) exceeds limit."
    return True, spot, gap_pct, opening_range, "PASSED"

def place_market_order(mconnect, symbol, side, qty):
    if not symbol or qty <= 0: return None
    for _ in range(3):
        try:
            res = mconnect.place_order(
                "regular", symbol, "NFO", side.upper(), "MARKET",
                str(qty), "MIS", "DAY", "0", "0"
            )
            if res and res.get("data"):
                data = res.get("data")
                if isinstance(data, dict):
                    return str(data.get("order_id") or data.get("orderid"))
                elif isinstance(data, str):
                    return data
        except Exception:
            pass
        time.sleep(0.5)
    return None

def get_order_book_dict(mconnect):
    order_map = {}
    try:
        ob = mconnect.get_order_book()
        if ob and ob.get("data"):
            for order in ob["data"]:
                oid = str(order.get("order_id") or order.get("orderid", ""))
                px = float(order.get("average_price") or order.get("averageprice", 0))
                status = str(order.get("order_status") or order.get("orderstatus", "")).lower()
                order_map[oid] = (px, status)
    except Exception:
        pass
    return order_map

def get_filled_price(mconnect, order_id, symbol, fallback_ltp=None):
    if order_id:
        ob = get_order_book_dict(mconnect)
        if str(order_id) in ob:
            avg_px, _ = ob[str(order_id)]
            if avg_px > 0: return avg_px
    live_px = get_ltp(mconnect, symbol)
    return live_px if live_px and live_px > 0 else (fallback_ltp if fallback_ltp else 0.0)

def log_to_gsheet(row_values):
    headers = [
        "Date", "Timestamp", "Strategy_Version", "Session_Status",
        "Nifty_Spot_1000", "Gap_Pct", "Opening_Range_Pts",
        "Initial_CE_Symbol", "Initial_PE_Symbol", "Initial_Combined_Premium",
        "Final_CE_Symbol", "Final_PE_Symbol", "Active_Qty", "Rolled_Status",
        "Exit_Reason", "Gross_PnL", "Charges", "Net_PnL"
    ]
    try:
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        sh = gc.open_by_key(GSHEET_ID_V3)
        worksheet = sh.sheet1
        if not worksheet.row_values(1): worksheet.append_row(headers)
        worksheet.append_row(row_values)
        print("Trade record successfully logged to GSHEET_ID_V3.")
    except Exception as e:
        print(f"Failed to log to GSHEET_ID_V3: {e}")

# ==========================================
# 4. MAIN EXECUTION PIPELINE (10:00 AM ENTRY)
# ==========================================
def main():
    mconnect = init_mstock_session()

    options_df = get_mstock_options_master(mconnect)
    next_expiry_date = get_next_expiry(options_df)
    expiry_str = next_expiry_date.strftime("%d-%b-%Y")

    print("Waiting until 10:00 AM IST for mStock 5-Lot Strangle execution...")
    wait_for_market_time(10, 0)

    is_safe, entry_spot, gap_pct, opening_pts, filter_msg = check_market_filters(mconnect)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    now_time_str = datetime.datetime.now().strftime("%H:%M:%S")

    if not is_safe:
        skip_alert = f"⚠️ mStock STRATEGY SKIPPED ({today_str})\nReason: {filter_msg}"
        print(skip_alert); send_telegram_alert(skip_alert)
        log_to_gsheet([today_str, now_time_str, "mStock Strangle V2", "SKIPPED", round(entry_spot, 2), round(gap_pct, 2), round(opening_pts, 2), "N/A", "N/A", 0.0, "N/A", "N/A", 0, "No", filter_msg, 0.0, 0.0, 0.0])
        sys.exit(0)

    atm_strike = round(entry_spot / 50.0) * 50
    atm_ce_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, atm_strike, "CE")
    atm_pe_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, atm_strike, "PE")

    combined_atm = (get_ltp(mconnect, atm_ce_sym) or 100.0) + (get_ltp(mconnect, atm_pe_sym) or 100.0)
    strike_offset = combined_atm * DELTA_WIDTH_MULTIPLIER

    ce_strike = int(round((entry_spot + strike_offset) / 50.0) * 50)
    pe_strike = int(round((entry_spot - strike_offset) / 50.0) * 50)

    ce_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, ce_strike, "CE")
    pe_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, pe_strike, "PE")

    ce_avg = get_filled_price(mconnect, place_market_order(mconnect, ce_sym, "SELL", TRANCHE_1_QTY), ce_sym, get_ltp(mconnect, ce_sym))
    time.sleep(1)
    pe_avg = get_filled_price(mconnect, place_market_order(mconnect, pe_sym, "SELL", TRANCHE_1_QTY), pe_sym, get_ltp(mconnect, pe_sym))

    ledger.record_trade("SELL", ce_avg, TRANCHE_1_QTY)
    ledger.record_trade("SELL", pe_avg, TRANCHE_1_QTY)

    initial_ce_sym, initial_pe_sym = ce_sym, pe_sym
    initial_combined_premium = ce_avg + pe_avg
    active_qty = TRANCHE_1_QTY
    rolled_status = "No"
    realized_roll_pnl = 0.0
    exit_reason = "N/A"

    send_telegram_alert(
        f"🟢 mStock STRANGLE (5 Lots | 325 Qty | 10:00 AM)\n"
        f"Expiry: {expiry_str} | Spot: {entry_spot:.2f}\n"
        f"CE: {ce_sym} @ ₹{ce_avg:.2f} | PE: {pe_sym} @ ₹{pe_avg:.2f}\n"
        f"Combined Premium: ₹{initial_combined_premium:.2f}"
    )

    last_alert_minute = datetime.datetime.now().minute

    while True:
        now = datetime.datetime.now()
        live_spot = get_nifty_spot_ohlc(mconnect)["ltp"]
        live_ce = get_ltp(mconnect, ce_sym)
        live_pe = get_ltp(mconnect, pe_sym)

        if not live_ce or not live_pe:
            time.sleep(2); continue

        open_pnl = ((ce_avg - live_ce) + (pe_avg - live_pe)) * active_qty
        total_running_pnl = realized_roll_pnl + open_pnl
        net_points_captured = total_running_pnl / active_qty
        active_target_pts = TARGET_DECAY_POINTS if rolled_status == "No" else SALVAGE_DECAY_POINTS

        if now.minute % 30 == 0 and now.minute != last_alert_minute:
            last_alert_minute = now.minute
            send_telegram_alert(f"⏱️ mStock UPDATE ({now.strftime('%I:%M %p')}): P&L ₹{total_running_pnl:,.2f} | Spot: {live_spot:.2f}")

        if net_points_captured >= active_target_pts:
            exit_reason = "Target (30 pts)" if rolled_status == "No" else "Rolled & Salvaged"
            break

        if total_running_pnl <= -MAX_MONETARY_LOSS:
            exit_reason = "Hard Loss Ceiling Hit"
            break

        if rolled_status == "No":
            if live_spot >= (ce_strike - SPOT_ROLL_BUFFER):
                new_ce_strike = ce_strike + ROLL_DISTANCE_PTS
                new_ce_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, new_ce_strike, "CE")
                if not new_ce_sym: break
                
                exit_ce_px = get_filled_price(mconnect, place_market_order(mconnect, ce_sym, "BUY", active_qty), ce_sym, live_ce)
                ledger.record_trade("BUY", exit_ce_px, active_qty)
                realized_roll_pnl += (ce_avg - exit_ce_px) * active_qty

                ce_avg = get_filled_price(mconnect, place_market_order(mconnect, new_ce_sym, "SELL", active_qty), new_ce_sym, get_ltp(mconnect, new_ce_sym))
                ledger.record_trade("SELL", ce_avg, active_qty)
                ce_strike, ce_sym = new_ce_strike, new_ce_sym
                rolled_status = "Yes (Call Rolled)"
                send_telegram_alert(f"🔄 mStock CALL ROLLED to {ce_sym} @ ₹{ce_avg:.2f}")

            elif live_spot <= (pe_strike + SPOT_ROLL_BUFFER):
                new_pe_strike = pe_strike - ROLL_DISTANCE_PTS
                new_pe_sym, _ = get_contract_by_expiry_and_strike(options_df, next_expiry_date, new_pe_strike, "PE")
                if not new_pe_sym: break

                exit_pe_px = get_filled_price(mconnect, place_market_order(mconnect, pe_sym, "BUY", active_qty), pe_sym, live_pe)
                ledger.record_trade("BUY", exit_pe_px, active_qty)
                realized_roll_pnl += (pe_avg - exit_pe_px) * active_qty

                pe_avg = get_filled_price(mconnect, place_market_order(mconnect, new_pe_sym, "SELL", active_qty), new_pe_sym, get_ltp(mconnect, new_pe_sym))
                ledger.record_trade("SELL", pe_avg, active_qty)
                pe_strike, pe_sym = new_pe_strike, new_pe_sym
                rolled_status = "Yes (Put Rolled)"
                send_telegram_alert(f"🔄 mStock PUT ROLLED to {pe_sym} @ ₹{pe_avg:.2f}")
        else:
            if live_spot >= (ce_strike - SPOT_HARD_EXIT_BUFFER) or live_spot <= (pe_strike + SPOT_HARD_EXIT_BUFFER):
                exit_reason = "Hard Spot SL Hit"
                rolled_status = "Yes (Breached)"
                break

        if now.hour > 15 or (now.hour == 15 and now.minute >= 10):
            exit_reason = "Time Exit (3:10 PM)" if rolled_status == "No" else "Rolled & Salvaged (3:10 PM)"
            break

        time.sleep(2)

    # Square-off & Log
    final_ce_px = get_filled_price(mconnect, place_market_order(mconnect, ce_sym, "BUY", active_qty), ce_sym, get_ltp(mconnect, ce_sym))
    final_pe_px = get_filled_price(mconnect, place_market_order(mconnect, pe_sym, "BUY", active_qty), pe_sym, get_ltp(mconnect, pe_sym))

    ledger.record_trade("BUY", final_ce_px, active_qty)
    ledger.record_trade("BUY", final_pe_px, active_qty)

    gross_pnl = realized_roll_pnl + (((ce_avg - final_ce_px) + (pe_avg - final_pe_px)) * active_qty)
    total_charges = ledger.calculate_charges()
    net_pnl = gross_pnl - total_charges

    send_telegram_alert(f"🏁 mStock STRANGLE CLOSED | Exit: {exit_reason} | Net P&L: ₹{net_pnl:+,.2f}")

    log_to_gsheet([
        today_str, datetime.datetime.now().strftime("%H:%M:%S"), "mStock Strangle V2", "TRADED",
        round(entry_spot, 2), round(gap_pct, 2), round(opening_pts, 2),
        initial_ce_sym, initial_pe_sym, round(initial_combined_premium, 2),
        ce_sym, pe_sym, active_qty, rolled_status, exit_reason,
        round(gross_pnl, 2), round(total_charges, 2), round(net_pnl, 2)
    ])

if __name__ == "__main__":
    main()
