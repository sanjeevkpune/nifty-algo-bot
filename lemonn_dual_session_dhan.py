#!/usr/bin/env python3
"""
lemonn_dual_session_dhan.py -- Nifty & Sensex Dual-Session Options Selling Bot
Integrated via DhanHQ API. Standalone execution with Auto-TOTP Login.
"""
import sys
import os
import time
import datetime
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import pyotp
import gspread
from dhanhq import dhanhq

CONFIG_ENV_PATH = "/etc/trade-config/config.env"
if os.path.exists(CONFIG_ENV_PATH):
    with open(CONFIG_ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

IST = ZoneInfo("Asia/Kolkata")
def now_ist(): return datetime.datetime.now(IST)
def at_ist(date, hour, minute): return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=IST)

DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
DHAN_PIN = os.environ.get("DHAN_PIN")
DHAN_TOTP_SECRET = os.environ.get("DHAN_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_ID_V2 = os.environ.get("GSHEET_ID_V2")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")

# ==============================================================
# DHAN-SPECIFIC 4-WEEK PAPER TRADING OVERRIDE
# ==============================================================
PAPER_MODE_END_DATE = datetime.date(2026, 12, 10)
if now_ist().date() <= PAPER_MODE_END_DATE:
    PAPER_MODE = True
else:
    PAPER_MODE = os.environ.get("PAPER_MODE", "0").strip() in ("1", "true")
# ==============================================================

NUM_LOTS = int(os.environ.get("DHAN_NUM_LOTS", "2"))
PAPER_SLIPPAGE_PTS = 0.5
ORDER_POLL_SECS = 1.0

def tg(message: str):
    tag = "[PAPER] " if PAPER_MODE else ""
    print(f"{tag}{message}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{tag}{message}"}, timeout=5)
        except: pass

def wait_until(target):
    while True:
        rem = (target - now_ist()).total_seconds()
        if rem <= 0: return
        time.sleep(min(rem, 1.0))

@dataclass
class Leg:
    session: str
    opt_type: str
    strike: int
    symbol: str
    token: str
    side: str
    qty: int
    entry_px: float
    sl_px: float
    exch_seg: str
    active: bool = True
    exit_px: float = 0.0
    exit_reason: str = ""

class LemonnDhanBot:
    def __init__(self):
        self.dhan = None
        self.options_df = None
        self.expiry = None
        self.legs: List[Leg] = []
        self.traded_symbols = set()
        self.realized_pnl = 0.0
        
        wd = now_ist().weekday()
        if wd in [0, 1, 4]: 
            self.idx_name, self.exch_seg, self.dhan_exch_seg, self.spot_exch, self.spot_token = "NIFTY", "NFO", "NSE_FNO", "NSE", "13"
            self.strike_step, self.m_entry, self.m_exit, self.a_entry, self.a_exit = 50, (9, 16), (14, 15), (14, 16), (15, 28)
        elif wd in [2, 3]: 
            self.idx_name, self.exch_seg, self.dhan_exch_seg, self.spot_exch, self.spot_token = "SENSEX", "BFO", "BSE_FNO", "BSE", "1"
            self.strike_step, self.m_entry, self.m_exit, self.a_entry, self.a_exit = 100, (9, 16), (13, 30), (13, 35), (15, 28)
        else: self.idx_name = "NONE"

    def login(self):
        if not DHAN_CLIENT_ID or not DHAN_PIN or not DHAN_TOTP_SECRET: 
            raise RuntimeError("Missing Dhan API credentials (DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET).")
        
        # 1. Generate live TOTP
        try:
            totp_code = pyotp.TOTP(DHAN_TOTP_SECRET).now()
        except Exception as e:
            raise RuntimeError(f"Failed to generate TOTP code: {e}")

        # 2. Fetch fresh 24-hour Access Token
        url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={DHAN_CLIENT_ID}&pin={DHAN_PIN}&totp={totp_code}"
        try:
            res = requests.post(url, timeout=15)
            data = res.json()
            if "accessToken" in data:
                fresh_token = data["accessToken"]
                self.dhan = dhanhq(client_id=DHAN_CLIENT_ID, access_token=fresh_token)
                print("➜ DhanHQ API dynamically authenticated via TOTP.")
                return
            else:
                raise RuntimeError(f"Token generation failed: {data}")
        except Exception as e:
            raise RuntimeError(f"Dhan API auto-login failed: {e}")

    def load_master(self):
        df = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master.csv", low_memory=False)
        df = df[(df["SEM_CUSTOM_SYMBOL"] == self.idx_name) & (df["SEM_EXM_EXCH_ID"] == self.exch_seg)].copy()
        df["strike"] = pd.to_numeric(df["SEM_STRIKE_PRICE"], errors="coerce")
        df["expirydate"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        df["lotsize"] = pd.to_numeric(df["SEM_LOT_REGULAR"], errors="coerce")
        
        today = now_ist().date()
        self.options_df = df[df["expirydate"] >= today].copy()
        expiries = sorted(self.options_df["expirydate"].dropna().unique())
        if not expiries: raise RuntimeError(f"No {self.idx_name} options found.")
        self.expiry = expiries[0]

    def get_dhan_ltp(self, exch, token):
        for _ in range(3):
            try:
                res = self.dhan.market_quote(exchange_segment=exch, security_id=token)
                if res and res.get('status') == 'success' and 'data' in res: 
                    return float(res['data'].get('LTP', 0))
            except: pass
            time.sleep(0.5)
        return 0.0

    def get_spot(self): 
        dhan_spot_exch = self.dhan.NSE if self.spot_exch == "NSE" else self.dhan.BSE
        return self.get_dhan_ltp(dhan_spot_exch, self.spot_token)

    def find_contract(self, strike, opt_type):
        m = self.options_df[(self.options_df["expirydate"] == self.expiry) & 
                            (self.options_df["strike"] == float(strike)) & 
                            (self.options_df["SEM_OPTION_TYPE"] == opt_type)]
        if m.empty: return None
        return {"symbol": str(m.iloc[0]["SEM_TRADING_SYMBOL"]), "token": str(m.iloc[0]["SEM_SMST_SECURITY_ID"]), "lotsize": int(m.iloc[0]["lotsize"])}

    def execute_market(self, symbol, token, side, qty, ref_ltp=0.0):
        if PAPER_MODE:
            px = ref_ltp or self.get_dhan_ltp(self.dhan_exch_seg, token)
            self.traded_symbols.add(symbol)
            return round(px + PAPER_SLIPPAGE_PTS if side == "BUY" else max(0.05, px - PAPER_SLIPPAGE_PTS), 2)
            
        dhan_side = self.dhan.SELL if side == "SELL" else self.dhan.BUY
        try:
            res = self.dhan.place_order(security_id=str(token), exchange_segment=self.dhan_exch_seg, transaction_type=dhan_side, quantity=int(qty), order_type=self.dhan.MARKET, product_type=self.dhan.INTRA, price=0)
            if res.get('status') == 'success':
                oid = res['data'].get('orderId')
                for _ in range(15):
                    time.sleep(ORDER_POLL_SECS)
                    ob = self.dhan.get_order_list()
                    if ob and ob.get('status') == 'success':
                        for o in ob.get('data', []):
                            if str(o.get('orderId')) == str(oid) and o.get('orderStatus') == 'TRADED':
                                return float(o.get('averagePrice') or ref_ltp)
        except Exception as e: print(f"Order failed for {symbol}: {e}")
        return ref_ltp

    def deploy_session(self, session_name, ce_strike, pe_strike):
        ce, pe = self.find_contract(ce_strike, "CE"), self.find_contract(pe_strike, "PE")
        if not ce or not pe: return tg(f"⚠️ {session_name} Skipped: Strikes not found.")
        qty = NUM_LOTS * ce["lotsize"]
        
        ce_px = self.execute_market(ce["symbol"], ce["token"], "SELL", qty, self.get_dhan_ltp(self.dhan_exch_seg, ce["token"]))
        pe_px = self.execute_market(pe["symbol"], pe["token"], "SELL", qty, self.get_dhan_ltp(self.dhan_exch_seg, pe["token"]))
        
        ce_sl, pe_sl = round(ce_px * 1.50, 1), round(pe_px * 1.50, 1)
        self.legs.extend([Leg(session_name, "CE", ce_strike, ce["symbol"], ce["token"], "SELL", qty, ce_px, ce_sl, self.dhan_exch_seg), Leg(session_name, "PE", pe_strike, pe["symbol"], pe["token"], "SELL", qty, pe_px, pe_sl, self.dhan_exch_seg)])
        tg(f"🟢 {session_name} DEPLOYED ({self.idx_name})\nCE {ce['symbol']} @ ₹{ce_px:.2f} (SL: ₹{ce_sl:.2f})\nPE {pe['symbol']} @ ₹{pe_px:.2f} (SL: ₹{pe_sl:.2f})")

    def close_leg(self, leg, ref_ltp, reason):
        px = self.execute_market(leg.symbol, leg.token, "BUY", leg.qty, ref_ltp)
        leg.exit_px, leg.active, leg.exit_reason = px, False, reason
        pnl = (leg.entry_px - px) * leg.qty
        self.realized_pnl += pnl
        tg(f"🔴 CLOSED {leg.symbol} @ ₹{px:.2f} | Reason: {reason} | P&L: ₹{pnl:,.2f}")

    def monitor_until(self, exit_time):
        while now_ist() < exit_time:
            active_legs = [l for l in self.legs if l.active]
            if active_legs:
                for leg in active_legs:
                    current_px = self.get_dhan_ltp(leg.exch_seg, leg.token)
                    if current_px and current_px >= leg.sl_px: self.close_leg(leg, current_px, "50%_SL_HIT")
            time.sleep(1.5)
        for leg in [l for l in self.legs if l.active]: self.close_leg(leg, self.get_dhan_ltp(leg.exch_seg, leg.token), "TIME_EXIT")

    def log_sheet(self, date_str):
        if not GSHEET_ID_V2 or not os.path.exists(SERVICE_ACCOUNT_FILE): return
        try: ws = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2).worksheet("Lemonn_Log")
        except:
            try:
                sh = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2)
                ws = sh.add_worksheet(title="Lemonn_Log", rows="1000", cols="12")
                ws.append_row(["Date", "Index", "Session", "CE_Symbol", "CE_Entry", "CE_Exit", "CE_Reason", "PE_Symbol", "PE_Entry", "PE_Exit", "PE_Reason", "Session_PnL"])
            except: return
            
        for session in ["MORNING_OTM2", "AFTERNOON_ATM"]:
            s_legs = [l for l in self.legs if l.session == session]
            if len(s_legs) == 2:
                ce, pe = s_legs[0], s_legs[1]
                pnl = ((ce.entry_px - ce.exit_px) * ce.qty) + ((pe.entry_px - pe.exit_px) * pe.qty)
                ws.append_row([date_str, self.idx_name, session, ce.symbol, ce.entry_px, ce.exit_px, ce.exit_reason, pe.symbol, pe.entry_px, pe.exit_px, pe.exit_reason, round(pnl, 2)])

    def run(self):
        if self.idx_name == "NONE": return tg("Weekend - Nothing to trade.")
        today = now_ist().date()
        try:
            import pandas_market_calendars as mcal
            if mcal.get_calendar('BSE').schedule(start_date=str(today), end_date=str(today)).empty: return tg("📅 Exchange Holiday. Sleeping.")
        except: pass
        
        self.login()
        self.load_master()
        tg(f"🚀 LEMONN DUAL-SESSION (DhanHQ | {self.idx_name} | {NUM_LOTS} Lots)")

        # 1. MORNING OTM 2 SESSION
        wait_until(at_ist(today, *self.m_entry))
        spot = self.get_spot()
        if spot > 0:
            atm = round(spot / self.strike_step) * self.strike_step
            self.deploy_session("MORNING_OTM2", atm + (2 * self.strike_step), atm - (2 * self.strike_step))
            self.monitor_until(at_ist(today, *self.m_exit))
        else:
            tg("⚠️ Unable to fetch Spot Price for Morning Session.")

        # 2. AFTERNOON ATM SESSION
        wait_until(at_ist(today, *self.a_entry))
        spot = self.get_spot()
        if spot > 0:
            atm = round(spot / self.strike_step) * self.strike_step
            self.deploy_session("AFTERNOON_ATM", atm, atm)
            self.monitor_until(at_ist(today, *self.a_exit))
        else:
            tg("⚠️ Unable to fetch Spot Price for Afternoon Session.")

        self.log_sheet(str(today))
        tg(f"🏁 DUAL-SESSION COMPLETE | Total Gross PnL: ₹{self.realized_pnl:,.2f}")

if __name__ == "__main__":
    try: LemonnDhanBot().run()
    except Exception as e: tg(f"🚨 FATAL BOT CRASH 🚨\n{e}")
