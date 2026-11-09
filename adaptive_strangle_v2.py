#!/usr/bin/env python3
"""
adaptive_strangle_v4.py  --  Nifty intraday short strangle bot (Zero-Touch Edition)

V4.2 Upgrades
---------------
* Universal Skip Logging: Added central `abort_and_log` function. The bot now writes a 
  "SKIPPED" row to the Google Sheet for ALL abort scenarios (Weekends, Holidays, Late Starts, 
  Missing Contracts, Margin Failures) and generates a lock file to prevent duplicate logging.
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

STRATEGY_VERSION = "V4.2_ZERO_TOUCH"

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
                    pnl = ((leg.entry_px - px) if leg.side == "SELL" else (px - leg.entry_px)) * leg.qty
                    self.realized += pnl
                    self.save_state()
                    tg(f"ℹ️ {leg.symbol} is already flat at the broker - no order sent.")
                    return pnl, status
                if (actual < 0) != (expected < 0):
                    msg = (f"{leg.symbol}: broker net qty {actual} has the OPPOSITE direction to the bot's record ({expected}). No order sent.")
                    tg(f"🚨 {msg} CHECK MANUALLY.")
                    raise OrderError(msg)
                if abs(actual) != abs(expected):
                    tg(f"⚠️ {leg.symbol}: broker net qty {actual} differs from bot record {expected}. Closing the broker quantity ({abs(actual)}).")
                    qty_to_close, status = abs(actual), "partial"

        px = self.execute(leg.symbol, leg.token, close_side, qty_to_close, ref_ltp, force_market)
        self.ledger.record(close_side, px, qty_to_close)
        leg.exit_px, leg.active = px, False
        pnl = ((leg.entry_px - px) if leg.side == "SELL" else (px - leg.entry_px)) * qty_to_close
        self.realized += pnl
        self.save_state()
        return pnl, status

    def verify_flat(self) -> bool:
        if PAPER_MODE:
            return True
        for rnd in range(1, FLAT_VERIFY_ROUNDS + 1):
            time.sleep(2)
            self.cancel_pending_orders(f"(post-exit check, round {rnd})")
            net = self.get_net_positions()
            leftover = None if net is None else {s: q for s, q in net.items() if q != 0}
            pend = self.open_orders()
            if leftover == {} and pend == []:
                tg("✅ VERIFIED FLAT: no open positions or pending orders on the bot's symbols.")
                return True
            if leftover is None or pend is None:
                print(f"verify_flat round {rnd}: could not read positions/orders")
                continue
            if leftover:
                tg(f"⚠️ Round {rnd}: still open after exit: {leftover}. Closing again.")
                self.emergency_square_off(f"post-exit leftovers {leftover}")
        net = self.get_net_positions()
        pend = self.open_orders()
        leftover = None if net is None else {s: q for s, q in net.items() if q != 0}
        tg(f"🚨🚨 NOT VERIFIED FLAT after {FLAT_VERIFY_ROUNDS} rounds.\nPositions: {leftover if leftover is not None else 'UNREADABLE'}\nPending orders: {len(pend) if pend is not None else 'UNREADABLE'}\nOPEN ANGEL ONE AND CLOSE / CANCEL MANUALLY NOW.")
        return False

    def active(self, role=None) -> List[Leg]:
        return [l for l in self.legs if l.active and (role is None or l.role == role)]

    def emergency_square_off(self, reason=""):
        if PAPER_MODE:
            tg(f"🛑 [paper] emergency square-off triggered: {reason}")
            return
        tg(f"🛑 EMERGENCY SQUARE-OFF: {reason}")
        self.cancel_pending_orders("before emergency square-off")
        positions = None
        for i in range(POSITION_READ_TRIES):
            try:
                res = self.smart.position()
                if res and res.get("status"):
                    positions = res.get("data") or []
                    break
            except Exception as e:
                print(f"position() error (try {i + 1}): {e}")
            time.sleep(0.7 * (i + 1))
        if positions is None:
            tg("🚨 Could not read positions. CLOSE POSITIONS MANUALLY IN ANGEL ONE NOW.")
            return
        todo = []
        for p in positions:
            if p.get("exchange") != "NFO" or p.get("tradingsymbol") not in self.traded_symbols:
                continue
            if str(p.get("producttype") or "INTRADAY").upper() != "INTRADAY":
                continue
            net = int(float(p.get("netqty", 0) or 0))
            if net != 0:
                todo.append((0 if net < 0 else 1, p["tradingsymbol"], str(p["symboltoken"]), net))
        todo.sort()
        failed = []
        for _, sym, tok, net in todo:
            side = "BUY" if net < 0 else "SELL"
            try:
                oid = self._place(sym, tok, side, abs(net), "MARKET", 0)
                info = self._wait_terminal(oid, MARKET_WAIT_SECS)
                if self._status(info) != "complete":
                    failed.append(sym)
            except Exception as e:
                print(f"Emergency close failed for {sym}: {e}")
                failed.append(sym)
        if failed:
            tg(f"🚨 STILL OPEN after emergency exit: {', '.join(failed)}. CLOSE MANUALLY NOW.")
        else:
            tg(f"✅ Emergency square-off complete ({len(todo)} positions closed).")

    def save_state(self):
        if not self.lock_path:
            return
        try:
            with open(self.lock_path, "w") as f:
                json.dump({"paper": PAPER_MODE, "expiry": str(self.expiry), "realized": self.realized,
                           "legs": [l.__dict__ for l in self.legs]}, f, indent=1)
        except Exception as e:
            print(f"State save failed: {e}")

    def log_sheet(self, row):
        headers = ["Date", "Timestamp", "Strategy_Version", "Session_Status", "Nifty_Spot_950", "Gap_Pct",
                   "ORB_915_950_Pts", "Initial_CE_Symbol", "Initial_PE_Symbol", "Initial_Combined_Premium",
                   "Final_CE_Symbol", "Final_PE_Symbol", "Active_Qty", "Rolled_Status", "Exit_Reason",
                   "Gross_PnL", "Charges", "Net_PnL"]
        try:
            if not os.path.exists(SERVICE_ACCOUNT_FILE):
                raise FileNotFoundError(SERVICE_ACCOUNT_FILE)
            ws = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2).sheet1
            if not ws.row_values(1):
                ws.append_row(headers)
            ws.append_row(row)
        except Exception as e:
            print(f"Sheet logging failed: {e}")
            tg(f"⚠️ SHEET LOGGING FAILED ({e}). Please log this trade manually.")

    def version_tag(self):
        return f"{STRATEGY_VERSION} {'PAPER' if PAPER_MODE else 'LIVE'}"

    def try_roll(self, opt_type, spot):
        shorts = self.active("SHORT_" + opt_type)
        if not shorts or self.rolls[opt_type] >= MAX_ROLLS_PER_SIDE:
            return
        short = shorts[0]
        if opt_type == "CE":
            triggered = spot >= short.strike - SPOT_ROLL_BUFFER
            new_strike = short.strike + ROLL_DISTANCE_PTS
        else:
            triggered = spot <= short.strike + SPOT_ROLL_BUFFER
            new_strike = short.strike - ROLL_DISTANCE_PTS
        if not triggered:
            return
        
        contract = self.find_contract(new_strike, opt_type)
        if not contract:
            self.rolls[opt_type] = MAX_ROLLS_PER_SIDE
            tg(f"⚠️ {opt_type} roll skipped: strike {new_strike} not listed. Position unchanged.")
            return
            
        old_sym = short.symbol
        leg_pnl, close_status = self.close_leg(short, self.last_q.get(short.token))
        self.rolls[opt_type] += 1
        
        if close_status == "already_flat":
            self.rolls[opt_type] = MAX_ROLLS_PER_SIDE
            tg(f"⚠️ {opt_type} roll cancelled: {old_sym} was already closed at the broker. No new leg opened.")
            return
            
        new_ltp = self.get_ltp_retry("NFO", contract["token"])
        new_leg = self.open_leg("SHORT_" + opt_type, opt_type, new_strike, contract, "SELL", new_ltp)
        tg(f"🔄 {opt_type} {old_sym} rolled to {new_leg.symbol} @ ₹{new_leg.entry_px:.2f}\nBooked leg P&L: ₹{leg_pnl:,.2f}")

    def run(self):
        today = now_ist().date()
        self.lock_path = f"run_lock_{today}_{'paper' if PAPER_MODE else 'live'}.json"

        # Check lock first to avoid spamming the sheet if triggered multiple times
        if os.path.exists(self.lock_path):
            tg(f"⚠️ Bot already ran or skipped today ({self.lock_path} exists). Not re-entering.")
            return

        def abort_and_log(reason, spot=0.0, gap=0.0, orb=0.0):
            # Create the lock file so we don't log this again if triggered manually later today
            open(self.lock_path, "w").close()
            today_str = str(today)
            tg(f"⚠️ STRATEGY SKIPPED ({today_str})\nReason: {reason}")
            self.log_sheet([today_str, now_ist().strftime("%H:%M:%S"), self.version_tag(), "SKIPPED",
                            round(spot, 2), round(gap, 2), round(orb, 2), "N/A", "N/A", 0.0,
                            "N/A", "N/A", 0, "No", reason, 0.0, 0.0, 0.0])

        if today.weekday() >= 5:
            abort_and_log("Weekend - Non-trading day")
            return

        try:
            import pandas_market_calendars as mcal
            nse = mcal.get_calendar('BSE') 
            schedule = nse.schedule(start_date=str(today), end_date=str(today))
            if schedule.empty:
                abort_and_log(f"Auto-Holiday detected: Exchange is officially closed.")
                return
        except ImportError:
            print("⚠️ pandas_market_calendars not installed. Cannot auto-verify holidays. Proceeding...")
        except Exception as e:
            print(f"Auto-holiday API check failed: {e}")

        entry_time = at_ist(today, ENTRY_HOUR, ENTRY_MIN)
        exit_time = at_ist(today, EXIT_HOUR, EXIT_MIN)
        grace_limit = entry_time + datetime.timedelta(minutes=ENTRY_GRACE_MIN)

        print("--------------------------------------------------")
        print(f"➜ Configured Entry Time : {entry_time.strftime('%H:%M:%S %Z')}")
        print(f"➜ Configured Exit Time  : {exit_time.strftime('%H:%M:%S %Z')}")
        print(f"➜ Configured Grace Limit: {grace_limit.strftime('%H:%M:%S %Z')} (Grace: {ENTRY_GRACE_MIN} mins)")
        print(f"➜ Current System Time   : {now_ist().strftime('%H:%M:%S %Z')}")
        print("--------------------------------------------------")

        if now_ist() > grace_limit:
            abort_and_log(f"Started too late (cutoff was {grace_limit:%H:%M:%S} IST).")
            return

        self.login()
        self.load_master()
        print(f"Mode: {'PAPER' if PAPER_MODE else 'LIVE'} | Expiry: {self.expiry} ({EXPIRY_MODE})")
        print("Waiting for 9:50 AM IST ...")
        wait_until(entry_time)

        ok, spot, gap_pct, orb, msg = self.check_filters()
        if not ok:
            abort_and_log(msg, spot, gap_pct, orb)
            return

        atm = int(round(spot / 50.0) * 50)
        atm_ce, atm_pe = self.find_contract(atm, "CE"), self.find_contract(atm, "PE")
        
        print(f"➜ Strike Math   -> Spot: {spot:.2f} | Selected ATM: {atm}")
        
        if not atm_ce or not atm_pe:
            abort_and_log(f"ATM {atm} contracts not found for expiry {self.expiry}.", spot, gap_pct, orb)
            return
            
        q = self.get_ltps({"NFO": [atm_ce["token"], atm_pe["token"]]})
        if atm_ce["token"] not in q or atm_pe["token"] not in q:
            abort_and_log("Could not read ATM straddle prices from feed.", spot, gap_pct, orb)
            return
            
        offset = (q[atm_ce["token"]] + q[atm_pe["token"]]) * DELTA_WIDTH_MULTIPLIER
        ce_strike = int(round((spot + offset) / 50.0) * 50)
        pe_strike = int(round((spot - offset) / 50.0) * 50)
        
        print(f"➜ Strike Math   -> ATM CE LTP: {q[atm_ce['token']]} | ATM PE LTP: {q[atm_pe['token']]}")
        print(f"➜ Strike Math   -> Multiplier Offset: {offset:.2f}")
        print(f"➜ Target Strikes-> CE: {ce_strike} | PE: {pe_strike}")
        
        ce_c, pe_c = self.find_contract(ce_strike, "CE"), self.find_contract(pe_strike, "PE")
        
        if not ce_c or not pe_c:
            abort_and_log(f"Required target strikes not listed (CE {ce_strike}, PE {pe_strike}).", spot, gap_pct, orb)
            return

        lot = ce_c["lotsize"]
        self.qty = NUM_LOTS * lot

        margin_ok, margin_msg = self.check_margin()
        if not margin_ok:
            abort_and_log(f"Insufficient margin: {margin_msg}", spot, gap_pct, orb)
            return

        # Deploy Strangle
        open(self.lock_path, "w").close()
        ltps = self.get_ltps({"NFO": [c["token"] for c in (ce_c, pe_c) if c]})
        
        short_ce = self.open_leg("SHORT_CE", "CE", ce_strike, ce_c, "SELL", ltps.get(ce_c["token"]))
        short_pe = self.open_leg("SHORT_PE", "PE", pe_strike, pe_c, "SELL", ltps.get(pe_c["token"]))

        initial_premium = short_ce.entry_px + short_pe.entry_px
        max_loss = initial_premium * MAX_PREMIUM_LOSS_PCT * self.qty
        initial_ce_sym, initial_pe_sym = short_ce.symbol, short_pe.symbol
        
        print("--------------------------------------------------")
        print(f"➜ Execution     -> Total Lots: {NUM_LOTS} (Qty {self.qty})")
        print(f"➜ CE Roll Trigger-> Spot >= {short_ce.strike - SPOT_ROLL_BUFFER:.2f}")
        print(f"➜ PE Roll Trigger-> Spot <= {short_pe.strike + SPOT_ROLL_BUFFER:.2f}")
        print("--------------------------------------------------")

        today_str = str(today)
        tg(f"🟢 STRANGLE DEPLOYED ({NUM_LOTS} lots | {self.qty} qty | {now_ist():%H:%M})\nExpiry: {self.expiry} | Spot: {spot:.2f} | Gap {gap_pct:+.2f}% | ORB {orb:.0f} pts\nCE {short_ce.symbol} @ ₹{short_ce.entry_px:.2f} | PE {short_pe.symbol} @ ₹{short_pe.entry_px:.2f}\nCombined premium ₹{initial_premium:.2f}\nHard SL: -₹{max_loss:,.0f} ({int(MAX_PREMIUM_LOSS_PCT * 100)}% of premium)")
        self.log_sheet([today_str, now_ist().strftime("%H:%M:%S"), self.version_tag(), "ENTERED",
                        round(spot, 2), round(gap_pct, 2), round(orb, 2), initial_ce_sym, initial_pe_sym,
                        round(initial_premium, 2), initial_ce_sym, initial_pe_sym, self.qty, "No",
                        "OPEN", 0.0, 0.0, 0.0])

        exit_reason, force_market = "N/A", False
        last_ok, feed_alerted = time.time(), False
        last_slot = now_ist().hour * 2 + now_ist().minute // 30

        while True:
            now = now_ist()
            if now >= exit_time:
                exit_reason = "Time Exit (3:10 PM)"
                break

            active_legs = self.active()
            need = [NIFTY_SPOT_TOKEN] + [l.token for l in active_legs]
            q = self.get_ltps({"NSE": [NIFTY_SPOT_TOKEN], "NFO": [l.token for l in active_legs]})
            
            if any(t not in q for t in need):
                stale = time.time() - last_ok
                if stale >= DATA_FAIL_EXIT_SECS:
                    exit_reason, force_market = f"Data feed lost {int(stale)}s - forced exit", True
                    break
                if stale >= DATA_FAIL_ALERT_SECS and not feed_alerted:
                    tg(f"⚠️ No market data for {int(stale)}s. Forced exit at {DATA_FAIL_EXIT_SECS}s.")
                    feed_alerted = True
                time.sleep(1)
                continue
                
            last_ok, feed_alerted = time.time(), False
            self.last_q = q
            spot_now = q[NIFTY_SPOT_TOKEN]

            open_pnl = sum(((l.entry_px - q[l.token]) if l.side == "SELL" else (q[l.token] - l.entry_px)) * l.qty
                           for l in active_legs)
            total_pnl = self.realized + open_pnl

            slot = now.hour * 2 + now.minute // 30
            if slot != last_slot and now.minute % 30 == 0:
                last_slot = slot
                tg(f"⏱️ {now:%I:%M %p}: P&L ₹{total_pnl:,.0f} | Spot {spot_now:.2f}")

            if total_pnl <= -max_loss:
                exit_reason = f"Hard SL ({int(MAX_PREMIUM_LOSS_PCT * 100)}% premium loss)"
                break

            try:
                self.try_roll("CE", spot_now)
                self.try_roll("PE", spot_now)
            except OrderError as e:
                exit_reason, force_market = f"Roll order failed: {e}", True
                break

            time.sleep(POLL_SECS)

        self.cancel_pending_orders("at exit time")
        failures = []
        for leg in sorted(self.active(), key=lambda l: 0 if l.side == "SELL" else 1):
            try:
                self.close_leg(leg, self.last_q.get(leg.token), force_market)
            except Exception as e:
                failures.append(leg.symbol)
                print(f"Exit failed for {leg.symbol}: {e}")
                
        if failures:
            self.emergency_square_off(f"normal exit failed for {', '.join(failures)}")
            
        flat_ok = self.verify_flat()
        if not flat_ok:
            exit_reason += " | NOT VERIFIED FLAT - CHECK BROKER"

        gross = self.realized
        charges = self.ledger.charges()
        net = gross - charges
        rolled = "No" if not any(self.rolls.values()) else "+".join(k for k, v in self.rolls.items() if v) + " rolled"

        def last_sym(role, default):
            m = [l.symbol for l in self.legs if l.role == role]
            return m[-1] if m else default

        tg(f"🏁 BOT CLOSED | {exit_reason}\nGross ₹{gross:+,.2f} | Charges ₹{charges:,.2f} | Net ₹{net:+,.2f}")
        self.log_sheet([today_str, now_ist().strftime("%H:%M:%S"), self.version_tag(), "TRADED",
                        round(spot, 2), round(gap_pct, 2), round(orb, 2), initial_ce_sym, initial_pe_sym,
                        round(initial_premium, 2), last_sym("SHORT_CE", "N/A"), last_sym("SHORT_PE", "N/A"),
                        self.qty, rolled, exit_reason, round(gross, 2), round(charges, 2), round(net, 2)])
        try:
            self.smart.terminateSession(CLIENT_CODE)
        except Exception:
            pass

if __name__ == "__main__":
    bot = Bot()
    try:
        bot.run()
    except Exception as e:
        print(f"CRITICAL FAILURE:\n{traceback.format_exc()}")
        tg(f"🚨 FATAL BOT CRASH 🚨\n{e}\nAttempting emergency square-off...")
        try:
            if bot.smart and bot.traded_symbols:
                bot.emergency_square_off(f"crash: {e}")
                bot.verify_flat()
        except Exception as e2:
            tg(f"🚨 Emergency square-off itself failed: {e2}. CLOSE POSITIONS MANUALLY NOW.")
        sys.exit(1)
