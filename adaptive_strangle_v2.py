#!/usr/bin/env python3
"""
adaptive_strangle_v4.py  --  Nifty intraday short strangle bot (Zero-Touch Edition)

V4.1 Upgrades
---------------
* Enhanced Observability: Detailed console prints for intermediate mathematical 
  variables (OHLC data, strike offsets, and roll thresholds) for manual debugging.
"""
import sys
import os
import time
import json
import datetime
import traceback
import urllib.request
from dataclasses import dataclass
from typing import Optional, Dict, List
from zoneinfo import ZoneInfo

# ==========================================
# 0. AUTO-LOAD CONFIG FOR MANUAL RUNS
# ==========================================
CONFIG_ENV_PATH = "/etc/trade-config/config.env"
if os.path.exists(CONFIG_ENV_PATH):
    with open(CONFIG_ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

import pandas as pd
import pyotp
import requests
import gspread
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")

def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)

def at_ist(date: datetime.date, hour: int, minute: int) -> datetime.datetime:
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=IST)

# ==========================================
# 1. CONFIGURATION
# ==========================================
def env_bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y")

def env_float(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default

def env_int(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default

API_KEY = os.environ.get("API_KEY")
CLIENT_CODE = os.environ.get("CLIENT_CODE")
PIN = os.environ.get("PIN")
TOTP_SECRET = os.environ.get("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GSHEET_ID_V2 = os.environ.get("GSHEET_ID_V2")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")

missing_env = [k for k, v in {
    "API_KEY": API_KEY, "CLIENT_CODE": CLIENT_CODE, "PIN": PIN, "TOTP_SECRET": TOTP_SECRET,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "GSHEET_ID_V2": GSHEET_ID_V2,
}.items() if not v]
if missing_env:
    print(f"CRITICAL: missing environment variables: {', '.join(missing_env)}")
    sys.exit(1)

STRATEGY_VERSION = "V4.1_ZERO_TOUCH"

# --- Mode -------------------------------------------------------------
PAPER_MODE = env_bool("PAPER_MODE", True)          
PAPER_SLIPPAGE_PTS = env_float("PAPER_SLIPPAGE_PTS", 0.5)

# --- Strategy rules ---------------------------------------------------
NUM_LOTS = env_int("NUM_LOTS", 3)
FALLBACK_LOT_SIZE = env_int("FALLBACK_LOT_SIZE", 65)
ENTRY_HOUR, ENTRY_MIN = 9, 50
ENTRY_GRACE_MIN = env_int("ENTRY_GRACE_MIN", 5)
EXIT_HOUR, EXIT_MIN = 15, 10

DELTA_WIDTH_MULTIPLIER = env_float("DELTA_WIDTH_MULTIPLIER", 1.25)
MAX_OPENING_RANGE_PTS = env_float("MAX_OPENING_RANGE_PTS", 140.0)
MAX_GAP_PERCENT = env_float("MAX_GAP_PERCENT", 0.60)
MAX_PREMIUM_LOSS_PCT = env_float("MAX_PREMIUM_LOSS_PCT", 0.40)
SPOT_ROLL_BUFFER = env_float("SPOT_ROLL_BUFFER", 40.0)
ROLL_DISTANCE_PTS = env_int("ROLL_DISTANCE_PTS", 100)
MAX_ROLLS_PER_SIDE = env_int("MAX_ROLLS_PER_SIDE", 1)

EXPIRY_MODE = os.environ.get("EXPIRY_MODE", "NEXT").upper()

# --- Execution --------------------------------------------------------
ORDER_MODE = "MARKET"
MARKET_WAIT_SECS = env_int("MARKET_WAIT_SECS", 15)
ORDER_POLL_SECS = 1.0

# --- Monitoring -------------------------------------------------------
POLL_SECS = env_float("POLL_SECS", 1.5)
DATA_FAIL_ALERT_SECS = env_int("DATA_FAIL_ALERT_SECS", 15)
DATA_FAIL_EXIT_SECS = env_int("DATA_FAIL_EXIT_SECS", 60)

# --- Safety -----------------------------------------------------------
MIN_REQUIRED_MARGIN = env_float("MIN_REQUIRED_MARGIN", 0.0)

# --- Charges ---
BROKERAGE_PER_ORDER = env_float("BROKERAGE_PER_ORDER", 20.0)
STT_SELL_PCT = env_float("STT_SELL_PCT", 0.0015)
EXCHANGE_PCT = env_float("EXCHANGE_PCT", 0.000355299)
SEBI_PCT = 0.000001
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003

# --- Exit verification --------------------------------------------------
EXIT_BLIND_IF_POSITIONS_UNAVAILABLE = env_bool("EXIT_BLIND_IF_POSITIONS_UNAVAILABLE", False)
POSITION_READ_TRIES = env_int("POSITION_READ_TRIES", 4)
FLAT_VERIFY_ROUNDS = env_int("FLAT_VERIFY_ROUNDS", 3)

NIFTY_SPOT_TOKEN = "99926000"
TERMINAL_STATES = {"complete", "rejected", "cancelled", "canceled"}

class OrderError(Exception):
    pass

# ==========================================
# 2. HELPERS
# ==========================================
def tg(message: str):
    tag = "[PAPER] " if PAPER_MODE else ""
    print(f"{tag}{message}")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{tag}{message}"}, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def wait_until(target: datetime.datetime):
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))

class OrderLedger:
    def __init__(self):
        self.sell_turnover = 0.0
        self.buy_turnover = 0.0
        self.orders = 0

    def record(self, side, price, qty):
        t = float(price) * int(qty)
        if side.upper() == "SELL":
            self.sell_turnover += t
        else:
            self.buy_turnover += t
        self.orders += 1

    def charges(self):
        total = self.sell_turnover + self.buy_turnover
        brokerage = self.orders * BROKERAGE_PER_ORDER
        stt = self.sell_turnover * STT_SELL_PCT
        exch = total * EXCHANGE_PCT
        sebi = total * SEBI_PCT
        gst = (brokerage + exch + sebi) * GST_PCT
        stamp = self.buy_turnover * STAMP_BUY_PCT
        return brokerage + stt + exch + sebi + gst + stamp

@dataclass
class Leg:
    role: str
    opt_type: str
    strike: int
    symbol: str
    token: str
    side: str
    qty: int
    entry_px: float
    active: bool = True
    exit_px: float = 0.0

# ==========================================
# 3. BOT
# ==========================================
class Bot:
    def __init__(self):
        self.smart: Optional[SmartConnect] = None
        self.options_df: Optional[pd.DataFrame] = None
        self.expiry: Optional[datetime.date] = None
        self.legs: List[Leg] = []
        self.traded_symbols = set()
        self.realized = 0.0
        self.ledger = OrderLedger()
        self.rolls = {"CE": 0, "PE": 0}
        self.qty = 0
        self.last_q: Dict[str, float] = {}
        self.lock_path = ""

    def login(self):
        self.smart = SmartConnect(api_key=API_KEY)
        for attempt in range(1, 4):
            try:
                res = self.smart.generateSession(CLIENT_CODE, PIN, pyotp.TOTP(TOTP_SECRET).now())
                if res and res.get("status"):
                    print("➜ SmartAPI session authenticated.")
                    return
                print(f"Session attempt {attempt} rejected: {res}")
            except Exception as e:
                print(f"Session error attempt {attempt}: {e}")
            time.sleep(3)
        raise RuntimeError("Angel One authentication failed after 3 attempts.")

    def load_master(self):
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        last_err = None
        for _ in range(3):
            try:
                data = json.loads(urllib.request.urlopen(url, timeout=30).read())
                break
            except Exception as e:
                last_err = e
                time.sleep(3)
        else:
            raise RuntimeError(f"Could not download scrip master: {last_err}")
        df = pd.DataFrame(data)
        df = df[(df["name"] == "NIFTY") & (df["instrumenttype"] == "OPTIDX")].copy()
        df["strike"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
        df["expirydate"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce").dt.date
        df["lotsize"] = pd.to_numeric(df["lotsize"], errors="coerce")
        today = now_ist().date()
        self.options_df = df[df["expirydate"] >= today].copy()
        expiries = sorted(self.options_df["expirydate"].dropna().unique())
        if not expiries:
            raise RuntimeError("No Nifty option expiries found in scrip master.")
        idx = 1 if (EXPIRY_MODE == "NEXT" and len(expiries) > 1) else 0
        self.expiry = expiries[idx]

    def find_contract(self, strike, opt_type) -> Optional[Dict]:
        df = self.options_df
        m = df[(df["expirydate"] == self.expiry) & (df["strike"] == float(strike)) &
               (df["symbol"].str.endswith(opt_type))]
        if m.empty:
            return None
        row = m.iloc[0]
        lot = int(row["lotsize"]) if pd.notna(row["lotsize"]) and row["lotsize"] > 0 else FALLBACK_LOT_SIZE
        return {"symbol": str(row["symbol"]), "token": str(row["token"]), "lotsize": lot}

    def get_ltps(self, request: Dict[str, List[str]]) -> Dict[str, float]:
        try:
            res = self.smart.getMarketData("LTP", request)
            if res and res.get("status") and res.get("data"):
                out = {}
                for q in res["data"].get("fetched", []):
                    p = float(q.get("ltp", 0) or 0)
                    if p > 0:
                        out[str(q.get("symbolToken"))] = p
                return out
        except Exception as e:
            print(f"LTP fetch error: {e}")
        return {}

    def get_ltp_retry(self, exch, token, tries=3) -> Optional[float]:
        for i in range(tries):
            v = self.get_ltps({exch: [str(token)]}).get(str(token))
            if v:
                return v
            time.sleep(0.5 * (i + 1))
        return None

    def spot_ohlc(self) -> Optional[Dict]:
        for tok in (NIFTY_SPOT_TOKEN, "26000"):
            try:
                res = self.smart.getMarketData("FULL", {"NSE": [tok]})
                if res and res.get("status") and res.get("data") and res["data"].get("fetched"):
                    q = res["data"]["fetched"][0]
                    pc = float(q.get("previousClose", 0) or q.get("pc", 0) or q.get("close", 0) or 0)
                    return {"ltp": float(q.get("ltp", 0) or 0), "open": float(q.get("open", 0) or 0),
                            "high": float(q.get("high", 0) or 0), "low": float(q.get("low", 0) or 0),
                            "close": pc}
            except Exception as e:
                print(f"OHLC error ({tok}): {e}")
        return None

    def check_filters(self):
        o = self.spot_ohlc()
        if not o or o["ltp"] <= 0 or o["open"] <= 0 or o["close"] <= 0:
            return False, 0.0, 0.0, 0.0, "No valid Nifty OHLC feed available. Market closed or feed down."
        
        gap_pct = (o["open"] - o["close"]) / o["close"] * 100.0     
        orb = o["high"] - o["low"]
        
        print(f"➜ API OHLC Feed -> Open: {o['open']}, High: {o['high']}, Low: {o['low']}, PrevClose: {o['close']}")
        print(f"➜ Filter Math   -> Gap Calc: {gap_pct:+.2f}% | ORB Calc: {orb:.1f} pts")
        
        if abs(gap_pct) > MAX_GAP_PERCENT:
            return False, o["ltp"], gap_pct, orb, f"Opening gap {gap_pct:+.2f}% exceeds {MAX_GAP_PERCENT}%."
        if orb > MAX_OPENING_RANGE_PTS:
            return False, o["ltp"], gap_pct, orb, f"9:15-9:50 range {orb:.1f} pts exceeds {MAX_OPENING_RANGE_PTS} pts."
        return True, o["ltp"], gap_pct, orb, "PASSED"

    def check_margin(self):
        if PAPER_MODE or MIN_REQUIRED_MARGIN <= 0:
            return True, "skipped"
        try:
            res = self.smart.rmsLimit()
            data = res.get("data") or {}
            avail = float(data.get("net") or data.get("availablecash") or 0)
            print(f"➜ Margin Check  -> Available: ₹{avail:,.0f} | Required: ₹{MIN_REQUIRED_MARGIN:,.0f}")
            return avail >= MIN_REQUIRED_MARGIN, f"available ₹{avail:,.0f} vs required ₹{MIN_REQUIRED_MARGIN:,.0f}"
        except Exception as e:
            return False, f"margin check failed: {e}"

    @staticmethod
    def _status(info) -> str:
        return str((info or {}).get("orderstatus") or (info or {}).get("status") or "").lower()

    def _order_info(self, order_id) -> Optional[Dict]:
        try:
            ob = self.smart.orderBook()
            if ob and ob.get("status") and ob.get("data"):
                for o in ob["data"]:
                    if str(o.get("orderid", "")) == str(order_id):
                        return o
        except Exception as e:
            print(f"orderBook error: {e}")
        return None

    def _wait_terminal(self, order_id, timeout) -> Optional[Dict]:
        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            time.sleep(ORDER_POLL_SECS)
            info = self._order_info(order_id) or info
            if self._status(info) in TERMINAL_STATES:
                return info
        return info

    def _place(self, symbol, token, side, qty, ordertype, price=0.0) -> str:
        params = {
            "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": str(token),
            "transactiontype": side.upper(), "exchange": "NFO", "ordertype": ordertype,
            "producttype": "INTRADAY", "duration": "DAY", "price": str(price),
            "squareoff": "0", "stoploss": "0", "quantity": int(qty),
        }
        self.traded_symbols.add(symbol)
        for attempt in range(2):
            try:
                res = self.smart.placeOrder(params)
            except Exception as e:
                raise OrderError(f"placeOrder exception for {symbol}: {e}")
            if isinstance(res, str) and res.strip():
                return res.strip()
            if isinstance(res, dict):
                if res.get("status") and res.get("data"):
                    d = res["data"]
                    return d if isinstance(d, str) else str(d.get("orderid"))
                print(f"Order not accepted (attempt {attempt + 1}): {res}")
                time.sleep(0.5)
                continue
            raise OrderError(f"Ambiguous placeOrder response for {symbol}: {res!r}")
        raise OrderError(f"Broker did not accept {side} {symbol}")

    def execute(self, symbol, token, side, qty, ref_ltp=None, force_market=False) -> float:
        side = side.upper()
        if PAPER_MODE:
            px = ref_ltp or self.get_ltp_retry("NFO", token)
            if not px:
                raise OrderError(f"[paper] no price for {symbol}")
            fill = px + PAPER_SLIPPAGE_PTS if side == "BUY" else max(0.05, px - PAPER_SLIPPAGE_PTS)
            self.traded_symbols.add(symbol)
            print(f"[paper] {side} {qty} {symbol} @ {fill:.2f}")
            return round(fill, 2)

        oid2 = self._place(symbol, token, side, qty, "MARKET", 0)
        info2 = self._wait_terminal(oid2, MARKET_WAIT_SECS)
        st2 = self._status(info2)
        
        if st2 == "rejected":
            tg(f"❌ ORDER REJECTED: {side} {symbol} - {(info2 or {}).get('text', '')}")
            raise OrderError(f"{symbol} market order rejected")
        if st2 != "complete":
            raise OrderError(f"{symbol} market order not confirmed (status={st2 or 'unknown'})")
            
        avg2 = float(info2.get("averageprice") or 0)
        if avg2 <= 0:
            avg2 = ref_ltp or 0.0
            
        return avg2

    def open_leg(self, role, opt_type, strike, contract, side, ref_ltp) -> Leg:
        px = self.execute(contract["symbol"], contract["token"], side, self.qty, ref_ltp)
        self.ledger.record(side, px, self.qty)
        leg = Leg(role, opt_type, int(strike), contract["symbol"], contract["token"], side, self.qty, px)
        self.legs.append(leg)
        self.save_state()
        return leg

    @staticmethod
    def _is_terminal(status: str) -> bool:
        return status in TERMINAL_STATES

    def get_net_positions(self, tries=None) -> Optional[Dict[str, int]]:
        tries = tries or POSITION_READ_TRIES
        for i in range(tries):
            try:
                res = self.smart.position()
                if res and res.get("status"):
                    out: Dict[str, int] = {}
                    for p in (res.get("data") or []):
                        sym = p.get("tradingsymbol")
                        if p.get("exchange") != "NFO" or sym not in self.traded_symbols:
                            continue
                        if str(p.get("producttype") or "INTRADAY").upper() != "INTRADAY":
                            continue
                        out[sym] = out.get(sym, 0) + int(float(p.get("netqty", 0) or 0))
                    return out
                print(f"position() not ok (try {i + 1}): {res}")
            except Exception as e:
                print(f"position() error (try {i + 1}): {e}")
            time.sleep(0.7 * (i + 1))
        return None

    def open_orders(self) -> Optional[List[Dict]]:
        try:
            ob = self.smart.orderBook()
            if not ob or not ob.get("status"):
                return None
            return [o for o in (ob.get("data") or [])
                    if o.get("tradingsymbol") in self.traded_symbols
                    and not self._is_terminal(self._status(o))]
        except Exception as e:
            print(f"orderBook error: {e}")
            return None

    def cancel_pending_orders(self, reason="") -> int:
        if PAPER_MODE:
            return 0
        pend = self.open_orders()
        if pend is None:
            tg("⚠️ Could not read order book to cancel pending orders. Check Angel One for open orders.")
            return 0
        cancelled = 0
        for o in pend:
            try:
                self.smart.cancelOrder(o.get("orderid"), o.get("variety") or "NORMAL")
                cancelled += 1
                print(f"Cancelled pending order {o.get('orderid')} {o.get('tradingsymbol')} ({self._status(o)}) {reason}")
            except Exception as e:
                print(f"Cancel failed for {o.get('orderid')}: {e}")
        if cancelled:
            tg(f"🧹 Cancelled {cancelled} pending order(s) {reason}".strip())
        return cancelled

    def close_leg(self, leg: Leg, ref_ltp, force_market=False):
        expected = -leg.qty if leg.side == "SELL" else leg.qty
        close_side = "BUY" if leg.side == "SELL" else "SELL"
        qty_to_close = leg.qty
        status = "closed"

        if not PAPER_MODE:
            self.cancel_pending_orders(f"before exiting {leg.symbol}")
            net = self.get_net_positions()
            if net is None:
                msg = f"Cannot read broker positions - NOT sending exit for {leg.symbol}"
                tg(f"🚨 {msg}. CHECK AND CLOSE MANUALLY IF STILL OPEN.")
                if not EXIT_BLIND_IF_POSITIONS_UNAVAILABLE:
                    raise OrderError(msg)
                tg("⚠️ EXIT_BLIND_IF_POSITIONS_UNAVAILABLE=1: trading on the bot's own records.")
            else:
                actual = net.get(leg.symbol, 0)
                if actual == 0:
                    leg.active, status = False, "already_flat"
                    px = ref_ltp or leg.entry_px
                    leg.exit_px = px
                    pnl = ((leg.entry_
