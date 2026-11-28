#!/usr/bin/env python3
"""
etf_dukan_bot.py  --  ETF ki Dukan (best setup) delivery bot for AngelOne SmartAPI.

What it does on each NSE trading day (one cron start at 2:45 pm IST):
  14:50  login, market-open check, reconcile the bot's records with AngelOne holdings + positions,
         daily closes for the 77-name universe, RSI(14) with the live price, Telegram DRY-RUN PLAN.
  15:00  target sell (max 1/day, 80% of the position, other 20% -> Core lot) + Core 3x exits.
  15:10  ETF slot + stock slot (new buys, Rs 40k, whole shares), else 1 averaging buy.
  after  Telegram summary, Google Sheet rows, reconcile again.
  1 Apr / 1 Oct (first run on or after): refresh the ETF + stock list by 6-month liquidity
         (universe_refresh.py), write it to the sheet's Universe + Watchlist tabs; used from the next day.

Rules live in strategy.py (pure functions, replay-tested against the 5-year backtest).

SAFETY
  * EKD_PAPER_MODE=1 is the default: no order is ever sent; fills are simulated at the live price.
    Paper mode still logs in to read prices. It keeps its own state file, separate from live.
  * EKD_DRY_RUN=1 sends the plan by Telegram and stops (no orders, no state change, even paper).
  * Bot settings use the EKD_ prefix, so a PAPER_MODE=0 meant for the strangle bot in the shared
    config.env can never switch this bot live.
  * Orders: LIMIT at LTP +/- a small buffer, wait, cancel, then MARKET for the remainder
    (only if the price hasn't run more than EKD_MAX_CHASE_PCT away).
  * Idempotent: daily lock file (no double start), every order is written as an intent before it
    is sent and tagged (ordertag) so a crash can be recovered from the order book, and the
    "1 sell / 1 averaging / 1 new per slot a day" limits are counted from the trade journal,
    so a re-run can never repeat a trade.
  * Never sells more than the bot's own quantity, and blocks a symbol whose AngelOne quantity is
    below the bot's records.

Usage
  python3 etf_dukan_bot.py init --capital 4000000 --start 2026-12-01
  python3 etf_dukan_bot.py run              # the cron entry (waits for 15:00 and 15:10)
  python3 etf_dukan_bot.py run --resume     # after a crash, same day
  python3 etf_dukan_bot.py plan             # dry-run plan right now (Telegram + console)
  python3 etf_dukan_bot.py status | reconcile | recover
  python3 etf_dukan_bot.py withdraw 15000 [--date YYYY-MM-DD]
  python3 etf_dukan_bot.py deposit 100000
  python3 etf_dukan_bot.py split SYMBOL RATIO   # e.g. a 1:10 split -> RATIO 10
  python3 etf_dukan_bot.py refresh-universe [--dry-run] [--force]   # the 6-monthly list refresh, by hand
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import shutil
import signal
import sys
import time
import traceback
import urllib.request
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import strategy as S  # noqa: E402
import signals as SG  # noqa: E402
import universe_refresh as UR  # noqa: E402


# ==========================================
# 0. CONFIG FILES (bot file first so its values win; env vars beat both)
# ==========================================
def _load_env_files():
    default = ":".join([os.path.expanduser("~/trading-bot/etf_dukan/etf_dukan.env"),
                        "/etc/trade-config/etf_dukan.env", "/etc/trade-config/config.env"])
    files = os.environ.get("EKD_CONFIG_FILES", default)
    for path in [p for p in files.split(":") if p]:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())


_load_env_files()

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def at_ist(d: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(d, dt.time(h, m), tzinfo=IST)


def env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def env_bool(name, default):
    v = os.environ.get(name)
    return default if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "y")


def env_float(name, default):
    return float(env(name, default))


def env_int(name, default):
    return int(env(name, default))


# --- credentials (EKD_ value first, else the shared strangle-bot value) ---
API_KEY = env("EKD_API_KEY", env("API_KEY"))
CLIENT_CODE = env("EKD_CLIENT_CODE", env("CLIENT_CODE"))
PIN = env("EKD_PIN", env("PIN"))
TOTP_SECRET = env("EKD_TOTP_SECRET", env("TOTP_SECRET"))
TELEGRAM_BOT_TOKEN = env("EKD_TELEGRAM_BOT_TOKEN", env("TELEGRAM_BOT_TOKEN"))
TELEGRAM_CHAT_ID = env("EKD_TELEGRAM_CHAT_ID", env("TELEGRAM_CHAT_ID"))
SERVICE_ACCOUNT_FILE = env("EKD_SERVICE_ACCOUNT_FILE", env("SERVICE_ACCOUNT_FILE", "service_account.json"))
GSHEET_ID = env("EKD_GSHEET_ID")

# --- mode ---
PAPER_MODE = env_bool("EKD_PAPER_MODE", True)
DRY_RUN = env_bool("EKD_DRY_RUN", False)
STRATEGY_VERSION = "EKD_BEST_V1.0"
HOME = env("EKD_HOME", HERE)
MODE = "paper" if PAPER_MODE else "live"
STATE_FILE = os.path.join(HOME, "state", f"{MODE}_state.json")
LOCK_DIR = os.path.join(HOME, "locks")
LOG_DIR = os.path.join(HOME, "logs")
CACHE_DIR = os.path.join(HOME, "cache")
UNIVERSE_FILE = env("EKD_UNIVERSE_FILE", os.path.join(HERE, "universe.csv"))   # the starting list (from the repo)

# --- 6-monthly universe refresh (universe_refresh.py has the rules) ---
UNIVERSE_AUTO = env_bool("EKD_UNIVERSE_AUTO", True)          # 0 = never refresh; the bot keeps universe.csv
UNIVERSE_MONTHS = tuple(int(m) for m in env("EKD_UNIVERSE_MONTHS", "4,10").split(",") if m.strip())
ACTIVE_UNIVERSE_FILE = os.path.join(HOME, "state", "universe_active.csv")   # the refreshed list (git-ignored)
UNIVERSE_META_FILE = os.path.join(HOME, "state", "universe_meta.json")
UNIVERSE_TAB = env("EKD_UNIVERSE_TAB", "Universe")
UNIVERSE_HISTORY_TAB = env("EKD_UNIVERSE_HISTORY_TAB", "Universe History")
UNIVERSE_SYNC_WATCHLIST = env_bool("EKD_UNIVERSE_SYNC_WATCHLIST", True)   # also update the tracker's Watchlist rows
SHEET_SYNC_STAMP = os.path.join(HERE, "universe_synced.txt")   # written by the "Sync list from sheet" workflow
UNIVERSE_LATEST = env("EKD_UNIVERSE_LATEST", "15:50")        # don't start a refresh after this (VM stops ~16:00)
UNIVERSE_FALLBACK_DAYS = env_int("EKD_UNIVERSE_FALLBACK_DAYS", 10)   # NSE lists unreachable this many tries: re-rank current names
NIFTY50_URLS = [u for u in env("EKD_NIFTY50_URLS",
                               "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv,"
                               "https://archives.nseindia.com/content/indices/ind_nifty50list.csv,"
                               "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv").split(",") if u.strip()]
ETF_LIST_URLS = [u for u in env("EKD_ETF_LIST_URLS",
                                "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv,"
                                "https://archives.nseindia.com/content/equities/eq_etfseclist.csv").split(",") if u.strip()]


def universe_params() -> "UR.Params":
    return UR.Params(etf_min_cr=env_float("EKD_UNIV_ETF_MIN_CR", 1.0), min_days=env_int("EKD_UNIV_MIN_DAYS", 60),
                     max_etfs=env_int("EKD_UNIV_MAX_ETFS", 50), max_stocks=env_int("EKD_UNIV_MAX_STOCKS", 30),
                     max_financials=env_int("EKD_UNIV_MAX_FINANCIALS", 4), max_per_sector=env_int("EKD_UNIV_MAX_PER_SECTOR", 3),
                     incumbent_edge=env_float("EKD_UNIV_INCUMBENT_EDGE", 1.25),
                     exclude=[x.strip() for x in env("EKD_UNIV_EXCLUDE", "").split(",") if x.strip()])

# --- timing (IST) ---
PLAN_TIME = env("EKD_PLAN_TIME", "14:50")
SELL_TIME = env("EKD_SELL_TIME", "15:00")
BUY_TIME = env("EKD_BUY_TIME", "15:10")
LATEST_START = env("EKD_LATEST_START", "15:15")   # started later than this: skip the day
SELL_CUTOFF = env("EKD_SELL_CUTOFF", "15:09")     # no new sell order after this
BUY_CUTOFF = env("EKD_BUY_CUTOFF", "15:24")       # no new buy order after this (market closes 15:30)

# --- orders ---
LIMIT_BUFFER_PCT = env_float("EKD_LIMIT_BUFFER_PCT", 0.10)   # buy limit = LTP x (1 + 0.10%), sell = LTP x (1 - 0.10%)
LIMIT_WAIT_SECS = env_int("EKD_LIMIT_WAIT_SECS", 45)
MARKET_FALLBACK = env_bool("EKD_MARKET_FALLBACK", True)
MAX_CHASE_PCT = env_float("EKD_MAX_CHASE_PCT", 1.0)          # skip the market step if price moved > 1% from the plan
MARKET_WAIT_SECS = env_int("EKD_MARKET_WAIT_SECS", 20)
ORDER_POLL_SECS = 1.5
PAPER_SLIPPAGE_PCT = env_float("EKD_PAPER_SLIPPAGE_PCT", 0.05)
API_TIMEOUT_SECS = env_int("EKD_API_TIMEOUT_SECS", 20)
TERMINAL = {"complete", "rejected", "cancelled", "canceled"}

# --- cash / reconciliation ---
BROKER_CASH_FLOOR = env_float("EKD_BROKER_CASH_FLOOR", 500_000)   # never spend AngelOne cash below this (option-bot buffer)
RECON_STRICT = env_bool("EKD_RECON_STRICT", False)               # any shortfall -> skip the whole day
RECON_INCLUDE_COLLATERAL = env_bool("EKD_RECON_INCLUDE_COLLATERAL", False)
HISTORY_DAYS = env_int("EKD_HISTORY_DAYS", 400)                  # calendar days of daily closes for RSI

# --- sheet ---
SHEET_MODE = env("EKD_SHEET_MODE", "tracker").lower()   # off | log | tracker (log tab + writes into tracker tabs)
LOG_TAB = env("EKD_LOG_TAB", "Bot Log")
# --- live signals from the sheet's Signals tab (see signals.py) ---
SIGNAL_POLICY = env("EKD_SIGNAL_POLICY", "bot").lower()        # bot (user choice) | agree | sheet | off
SIGNAL_IF_SHEET_DOWN = env("EKD_SIGNAL_IF_SHEET_DOWN", "skip").lower()   # skip (no orders) | bot (trade the bot's plan)
SIGNAL_RECALC_SECS = env_int("EKD_SIGNAL_RECALC_SECS", 30)     # wait after writing the sells, before reading buy signals


def rules_from_env() -> S.Rules:
    r = S.Rules(
        buy_amount=env_float("EKD_BUY_AMOUNT", 40_000), avg_amount=env_float("EKD_AVG_AMOUNT", 40_000),
        etf_target=env_float("EKD_ETF_TARGET", 0.05), etf_avg=env_float("EKD_ETF_AVG", -0.04),
        stock_target=env_float("EKD_STOCK_TARGET", 0.08), stock_avg=env_float("EKD_STOCK_AVG", -0.08),
        sell_fraction=env_float("EKD_SELL_FRACTION", 0.8), core_multiple=env_float("EKD_CORE_MULTIPLE", 3.0),
        max_positions=env_int("EKD_MAX_POSITIONS", 30), etf_shortlist=env_int("EKD_ETF_SHORTLIST", 8),
        stock_shortlist=env_int("EKD_STOCK_SHORTLIST", 2), rsi_period=env_int("EKD_RSI_PERIOD", 14),
        max_sells_per_day=env_int("EKD_MAX_SELLS_PER_DAY", 1), max_avg_per_day=env_int("EKD_MAX_AVG_PER_DAY", 1),
        withdrawal=env_float("EKD_WITHDRAWAL", 15_000), withdraw_after_months=env_int("EKD_WITHDRAW_AFTER_MONTHS", 6),
        keep_back=env_float("EKD_KEEP_BACK", 15_000), rebuy_gap_days=env_int("EKD_REBUY_GAP_DAYS", 7))
    # EKD_SYMBOL_LEVELS=GOLDBEES:0.05:-0.04,SILVERBEES:0.05:-0.04 (not needed: gold/silver use the ETF levels)
    for item in [x for x in env("EKD_SYMBOL_LEVELS", "").split(",") if x.strip()]:
        sym, tgt, avg = item.split(":")
        r.symbol_levels[sym.strip().upper()] = (float(tgt), float(avg))
    return r


RULES = rules_from_env()
RATES = S.ChargeRates(brokerage_per_order=env_float("EKD_BROKERAGE", 20.0))


class OrderError(Exception):
    pass


class SkipDay(Exception):
    pass


# ==========================================
# 1. HELPERS
# ==========================================
def _ensure_dirs():
    for d in (os.path.dirname(STATE_FILE), LOCK_DIR, LOG_DIR, CACHE_DIR, os.path.join(HOME, "state", "backups")):
        os.makedirs(d, exist_ok=True)


def tg(message: str, loud=None):
    tag = ("[PAPER] " if PAPER_MODE else "[LIVE] ") + ("[DRY RUN] " if DRY_RUN else "") + "🟢 ANGEL ONE · ETF ki Dukan\n"
    print(f"{tag}{message}", flush=True)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:   # ../notify.py: routine messages silent, repeats within 10 min dropped, one retry
        if os.path.dirname(HERE) not in sys.path:
            sys.path.append(os.path.dirname(HERE))
        import notify
        notify.send(tag, message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, loud=loud)
        return
    except ImportError:
        pass
    except Exception as e:
        print(f"Telegram error: {e}")
        return
    try:
        import requests
        for i in range(0, len(message), 3800):     # Telegram limit is 4096 characters
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": tag + message[i:i + 3800]}, timeout=8)
    except Exception as e:
        print(f"Telegram error: {e}")


def wait_until(target: dt.datetime, bot=None):
    while True:
        if bot is not None and bot.stop_requested:
            raise SkipDay("stop signal received while waiting")
        rem = (target - now_ist()).total_seconds()
        if rem <= 0:
            return
        time.sleep(min(rem, 1.0))


def inr(x: float) -> str:
    return f"₹{x:,.0f}"


def load_universe(path=None) -> Dict[str, dict]:
    """The refreshed list (state/universe_active.csv) once a refresh has run, else the repo's universe.csv."""
    if path is None:
        path = ACTIVE_UNIVERSE_FILE if os.path.exists(ACTIVE_UNIVERSE_FILE) else UNIVERSE_FILE
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym = r["symbol"].strip().upper()
            out[sym] = {"type": S.ETF if r["type"].strip().upper() == "ETF" else S.STOCK,
                        "include": r["include"].strip().upper() == "Y", "name": r.get("name", ""),
                        "sector": r.get("sector", ""), "angel_symbol": (r.get("angel_symbol") or f"{sym}-EQ").strip()}
    return out


# ==========================================
# 2. STATE (atomic JSON, daily backup)
# ==========================================
def load_state() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def backup_state():
    if os.path.exists(STATE_FILE):
        dst = os.path.join(HOME, "state", "backups", f"{MODE}_state_{now_ist():%Y-%m-%d_%H%M}.json")
        shutil.copy2(STATE_FILE, dst)


# ==========================================
# 3. BROKER (AngelOne SmartAPI)
# ==========================================
def make_smart():
    """Separate function so tests can swap in a fake SmartConnect."""
    from SmartApi import SmartConnect
    try:
        return SmartConnect(api_key=API_KEY, timeout=API_TIMEOUT_SECS)
    except TypeError:
        return SmartConnect(api_key=API_KEY)


class Broker:
    SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

    def __init__(self, universe: Dict[str, dict]):
        self.u = universe
        self.smart = None
        self.tokens: Dict[str, dict] = {}     # sym -> {"token", "tick", "tradingsymbol"}
        self.last_candle_error = ""

    # --- session ---
    def login(self):
        missing = [k for k, v in {"API_KEY": API_KEY, "CLIENT_CODE": CLIENT_CODE, "PIN": PIN,
                                  "TOTP_SECRET": TOTP_SECRET}.items() if not v]
        if missing:
            raise RuntimeError(f"missing credentials: {', '.join(missing)}")
        import pyotp
        self.smart = make_smart()
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
        raise RuntimeError("AngelOne authentication failed after 3 attempts.")

    def logout(self):
        try:
            if self.smart:
                self.smart.terminateSession(CLIENT_CODE)
        except Exception:
            pass

    # --- instruments ---
    def load_tokens(self):
        cache = os.path.join(CACHE_DIR, "scrip_master_nse_eq.json")
        data = None
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 20 * 3600:
            with open(cache) as f:
                data = json.load(f)
        if data is None:
            try:
                raw = json.loads(urllib.request.urlopen(self.SCRIP_URL, timeout=60).read())
                want = {v["angel_symbol"] for v in self.u.values()}
                data = [r for r in raw if r.get("exch_seg") == "NSE" and r.get("symbol") in want]
                with open(cache, "w") as f:
                    json.dump(data, f)
            except Exception as e:
                if os.path.exists(cache):
                    tg(f"⚠️ Scrip master download failed ({e}); using the cached copy.")
                    with open(cache) as f:
                        data = json.load(f)
                else:
                    raise RuntimeError(f"scrip master unavailable: {e}")
        by_ts = {r["symbol"]: r for r in data}
        missing = []
        for sym, u in self.u.items():
            r = by_ts.get(u["angel_symbol"])
            if not r:
                missing.append(sym)
                continue
            tick = float(r.get("tick_size") or 1) / 100.0      # scrip master gives paise
            self.tokens[sym] = {"token": str(r["token"]), "tick": tick if tick > 0 else 0.01,
                                "tradingsymbol": r["symbol"]}
        if missing:
            inc = [m for m in missing if self.u[m]["include"]]
            if inc:
                tg(f"⚠️ Not found in AngelOne scrip master (skipped): {', '.join(inc)}")

    def sym_of(self, tradingsymbol: str) -> Optional[str]:
        for sym, t in self.tokens.items():
            if t["tradingsymbol"] == tradingsymbol:
                return sym
        base = tradingsymbol.rsplit("-", 1)[0]
        return base if base in self.u else None

    # --- market data ---
    def ltp_many(self, syms: List[str]) -> Dict[str, float]:
        toks = {self.tokens[s]["token"]: s for s in syms if s in self.tokens}
        out: Dict[str, float] = {}
        items = list(toks)
        for i in range(0, len(items), 50):          # 50 tokens per call
            chunk = items[i:i + 50]
            for attempt in range(3):
                try:
                    res = self.smart.getMarketData("LTP", {"NSE": chunk})
                    if res and res.get("status") and res.get("data"):
                        for q in res["data"].get("fetched", []):
                            p = float(q.get("ltp", 0) or 0)
                            if p > 0:
                                out[toks[str(q.get("symbolToken"))]] = p
                        break
                    print(f"LTP not ok (try {attempt + 1}): {res}")
                except Exception as e:
                    print(f"LTP error (try {attempt + 1}): {e}")
                time.sleep(1.2)
            time.sleep(1.1)                           # stay under 1 request/sec for quotes
        return out

    def market_open_today(self, probe="NIFTYBEES") -> Tuple[bool, str]:
        """True if the probe ETF has traded today (catches holidays not in any calendar)."""
        t = self.tokens.get(probe)
        if not t:
            return True, "probe symbol missing; assuming open"
        for attempt in range(3):
            try:
                res = self.smart.getMarketData("FULL", {"NSE": [t["token"]]})
                q = (((res or {}).get("data") or {}).get("fetched") or [None])[0]
                if q:
                    stamp = str(q.get("exchTradeTime") or q.get("exchFeedTime") or "")
                    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
                        try:
                            d = dt.datetime.strptime(stamp, fmt).date()
                            ok = d == now_ist().date()
                            return ok, f"last {probe} trade {stamp}"
                        except ValueError:
                            continue
                    return True, f"could not parse trade time '{stamp}'; assuming open"
            except Exception as e:
                print(f"FULL quote error: {e}")
            time.sleep(1.5)
        return True, "quote unavailable; assuming open"

    CANDLE_TRIES = 5

    def daily_closes(self, sym: str, today: dt.date) -> Optional[List[Tuple[str, float]]]:
        """Daily closes before today, oldest first. None = the API never answered properly
        (rate limit / server error), so the caller retries later instead of treating it as
        "no history". An empty list means AngelOne really returned no candles."""
        t = self.tokens[sym]
        frm = today - dt.timedelta(days=HISTORY_DAYS)
        self.last_candle_error = ""
        for attempt in range(self.CANDLE_TRIES):
            try:
                res = self.smart.getCandleData({"exchange": "NSE", "symboltoken": t["token"], "interval": "ONE_DAY",
                                                "fromdate": f"{frm} 09:15", "todate": f"{today} 15:30"})
                rows = (res or {}).get("data") or []
                if rows:
                    out = []
                    for c in rows:
                        d = str(c[0])[:10]
                        if d < today.isoformat():          # today's bar is replaced by the live price
                            out.append((d, float(c[4])))
                    out.sort()
                    return out
                if res and res.get("status") and res.get("data") is not None:
                    return []                              # a proper answer with no candles
                self.last_candle_error = str((res or {}).get("message") or (res or {}).get("errorcode") or res)[:80]
                print(f"candles {sym} not ok (try {attempt + 1}): {res}")
            except Exception as e:
                self.last_candle_error = str(e)[:80]
                print(f"candles {sym} error (try {attempt + 1}): {e}")
            time.sleep(min(2.0 * 2 ** attempt, 20.0))      # 2, 4, 8, 16 s: lets a rate limit clear
        return None

    # --- account ---
    def scrip_master_nse(self) -> Dict[str, dict]:
        """Every NSE cash-market row of the scrip master, by trading symbol (e.g. NIFTYBEES-EQ). Used by the refresh."""
        raw = json.loads(urllib.request.urlopen(self.SCRIP_URL, timeout=120).read())
        return {r["symbol"]: r for r in raw if r.get("exch_seg") == "NSE" and str(r.get("symbol", "")).endswith("-EQ")}

    def daily_bars(self, token: str, today: dt.date, days: int = 190) -> Optional[List[Tuple[str, float, float]]]:
        """(date, close, volume) for completed days before today, oldest first. None = API never answered."""
        frm = today - dt.timedelta(days=days)
        self.last_candle_error = ""
        for attempt in range(self.CANDLE_TRIES):
            try:
                res = self.smart.getCandleData({"exchange": "NSE", "symboltoken": str(token), "interval": "ONE_DAY",
                                                "fromdate": f"{frm} 09:15", "todate": f"{today} 15:30"})
                rows = (res or {}).get("data")
                if rows:
                    return sorted((str(c[0])[:10], float(c[4]), float(c[5] if len(c) > 5 else 0)) for c in rows
                                  if str(c[0])[:10] < today.isoformat())
                if res and res.get("status") and rows is not None:
                    return []
                self.last_candle_error = str((res or {}).get("message") or res)[:80]
            except Exception as e:
                self.last_candle_error = str(e)[:80]
            time.sleep(min(2.0 * 2 ** attempt, 20.0))
        return None

    def available_cash(self) -> Optional[float]:
        try:
            res = self.smart.rmsLimit()
            d = (res or {}).get("data") or {}
            return float(d.get("availablecash") or d.get("net") or 0)
        except Exception as e:
            print(f"rmsLimit error: {e}")
            return None

    def broker_quantities(self) -> Tuple[Optional[Dict[str, int]], List[str]]:
        """Holdings (settled + T1) plus today's DELIVERY net position, per universe symbol."""
        raw_notes = []
        q: Dict[str, int] = {}
        try:
            h = self.smart.holding()
            if not (h and h.get("status")):
                return None, [f"holding() not ok: {h}"]
            for r in h.get("data") or []:
                if r.get("exchange") not in (None, "NSE"):
                    continue
                sym = self.sym_of(str(r.get("tradingsymbol", "")))
                if not sym:
                    continue
                n = int(float(r.get("quantity") or 0)) + int(float(r.get("t1quantity") or 0))
                if RECON_INCLUDE_COLLATERAL:
                    n += int(float(r.get("collateralquantity") or 0))
                q[sym] = q.get(sym, 0) + n
                raw_notes.append(f"{sym}: qty {r.get('quantity')} t1 {r.get('t1quantity')} "
                                 f"collateral {r.get('collateralquantity')}")
            p = self.smart.position()
            if not (p and p.get("status") is not False):
                return None, [f"position() not ok: {p}"]
            for r in (p.get("data") or []):
                if str(r.get("producttype", "")).upper() != "DELIVERY" or r.get("exchange") != "NSE":
                    continue
                sym = self.sym_of(str(r.get("tradingsymbol", "")))
                if sym:
                    q[sym] = q.get(sym, 0) + int(float(r.get("netqty") or 0))
        except Exception as e:
            return None, [f"holdings/positions error: {e}"]
        return q, raw_notes

    # --- orders ---
    def order_book(self) -> Optional[List[dict]]:
        for attempt in range(3):
            try:
                ob = self.smart.orderBook()
                if ob and ob.get("status"):
                    return ob.get("data") or []
            except Exception as e:
                print(f"orderBook error: {e}")
            time.sleep(1.0)
        return None

    def order_info(self, oid: str) -> Optional[dict]:
        for o in self.order_book() or []:
            if str(o.get("orderid")) == str(oid):
                return o
        return None

    @staticmethod
    def status(o) -> str:
        return str((o or {}).get("orderstatus") or (o or {}).get("status") or "").lower()

    def wait_terminal(self, oid: str, secs: float) -> Optional[dict]:
        end = time.time() + secs
        info = None
        while time.time() < end:
            time.sleep(ORDER_POLL_SECS)
            info = self.order_info(oid) or info
            if self.status(info) in TERMINAL:
                return info
        return info

    def place(self, sym: str, side: str, qty: int, ordertype: str, price: float, tag: str) -> str:
        t = self.tokens[sym]
        params = {"variety": "NORMAL", "tradingsymbol": t["tradingsymbol"], "symboltoken": t["token"],
                  "transactiontype": side, "exchange": "NSE", "ordertype": ordertype, "producttype": "DELIVERY",
                  "duration": "DAY", "price": f"{price:.2f}" if ordertype == "LIMIT" else "0",
                  "squareoff": "0", "stoploss": "0", "quantity": str(int(qty)), "ordertag": tag}
        try:
            res = self.smart.placeOrder(params)
        except Exception as e:
            raise OrderError(f"placeOrder exception {side} {sym}: {e}")
        if isinstance(res, str) and res.strip():
            return res.strip()
        if isinstance(res, dict) and res.get("status") and res.get("data"):
            d = res["data"]
            return d if isinstance(d, str) else str(d.get("orderid"))
        raise OrderError(f"order not accepted {side} {sym}: {res!r}")

    def cancel(self, oid: str):
        try:
            self.smart.cancelOrder(oid, "NORMAL")
        except Exception as e:
            print(f"cancel {oid} error: {e}")

    def tick_round(self, sym: str, price: float, side: str) -> float:
        tick = self.tokens[sym]["tick"]
        n = price / tick
        n = math.floor(n + 1e-9) if side == "BUY" else math.ceil(n - 1e-9)
        return round(n * tick, 2)


# ==========================================
# 4. GOOGLE SHEET (append-only, outbox so nothing is lost when Sheets is down)
# ==========================================
LOG_HEADERS = ["Date", "Time", "Mode", "Event", "Symbol", "Type", "Qty", "Price", "Value", "Charges (est)",
               "Avg cost", "Gain %", "Net P&L", "Kept as Core", "Order tag / IDs", "Note"]


class TrackerMismatch(Exception):
    """The tracker doesn't have the row the bot expected (e.g. a lot entered before the bot). Not retried."""


class SheetLogger:
    def __init__(self):
        self.outbox = os.path.join(HOME, "state", f"{MODE}_sheet_outbox.jsonl")
        self.local = os.path.join(LOG_DIR, f"{MODE}_bot_log.csv")
        self._sh = None

    def _book(self):
        if self._sh is None:
            import gspread
            if not os.path.exists(SERVICE_ACCOUNT_FILE):
                raise FileNotFoundError(SERVICE_ACCOUNT_FILE)
            self._sh = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID)
        return self._sh

    def _ws(self, name, create_headers=None):
        sh = self._book()
        try:
            return sh.worksheet(name)
        except Exception:
            if create_headers is None:
                raise
            ws = sh.add_worksheet(title=name, rows=2000, cols=len(create_headers))
            ws.append_row(create_headers)
            return ws

    def log(self, row: list, tracker: Optional[dict] = None):
        """row: LOG_HEADERS order. tracker: optional {"op": "buy"|"sell"|"core_sell"|"withdraw", ...}."""
        try:
            new = not os.path.exists(self.local)
            with open(self.local, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(LOG_HEADERS)
                w.writerow(row)
        except Exception as e:
            print(f"local log failed: {e}")
        if SHEET_MODE == "off" or not GSHEET_ID:
            return
        with open(self.outbox, "a") as f:
            f.write(json.dumps({"row": row, "tracker": tracker if SHEET_MODE == "tracker" else None}) + "\n")

    def flush(self, state: Optional[dict] = None) -> bool:
        """Send queued rows. Returns True when the outbox is empty."""
        if SHEET_MODE == "off" or not GSHEET_ID or not os.path.exists(self.outbox):
            return True
        with open(self.outbox) as f:
            items = [json.loads(l) for l in f if l.strip()]
        left = []
        err = None
        for i, it in enumerate(items):
            if err:
                left.append(it)
                continue
            try:
                if it.get("row") is not None and not it.get("row_done"):
                    self._ws(LOG_TAB, LOG_HEADERS).append_row(it["row"], value_input_option="USER_ENTERED")
                    it["row_done"] = True
                if it.get("tracker"):
                    try:
                        self._tracker(it["tracker"], state)
                    except TrackerMismatch as tm:
                        tg(f"⚠️ Tracker not updated for {it['tracker'].get('op')} {it['tracker'].get('sym', '')}: {tm}. "
                           f"The Bot Log tab has the trade; please fill the tracker row by hand.")
            except Exception as e:
                err = e
                left.append(it)
        with open(self.outbox + ".tmp", "w") as f:
            for it in left:
                f.write(json.dumps(it) + "\n")
        os.replace(self.outbox + ".tmp", self.outbox)
        if err:
            tg(f"⚠️ Google Sheet update failed ({err}). {len(left)} row(s) queued; they are retried on the next run. "
               f"Local copy: {self.local}")
            return False
        return True

    # --- state backup in a sheet tab, so a rebuilt VM gets its records back ---
    STATE_CHUNK = 45000

    def state_tab(self):
        return f"Bot State ({MODE})"

    def backup_state(self, state: dict):
        if SHEET_MODE == "off" or not GSHEET_ID or not state:
            return
        try:
            txt = json.dumps(state, separators=(",", ":"), default=str)
            chunks = [txt[i:i + self.STATE_CHUNK] for i in range(0, len(txt), self.STATE_CHUNK)]
            sh = self._book()
            try:
                ws = sh.worksheet(self.state_tab())
            except Exception:
                ws = sh.add_worksheet(title=self.state_tab(), rows=max(50, len(chunks) + 5), cols=2)
                try:
                    ws.hide()
                except Exception:
                    pass
            ws.clear()
            ws.update(range_name=f"A1:A{len(chunks)}", values=[[c] for c in chunks], value_input_option="RAW")
            ws.update(range_name="B1", values=[[f"saved {now_ist():%Y-%m-%d %H:%M} IST, {len(txt)} chars, do not edit"]],
                      value_input_option="RAW")
        except Exception as e:
            print(f"state backup to sheet failed: {e}")
            tg(f"⚠️ Could not back up the bot's records to the sheet ({e}). The local file is fine.")

    def restore_state(self) -> Optional[dict]:
        ws = self._book().worksheet(self.state_tab())
        txt = "".join(ws.col_values(1))
        return json.loads(txt) if txt else None

    def read_signals(self) -> dict:
        ws = self._ws("Signals")
        try:
            rows = ws.get("A7:J10", value_render_option="UNFORMATTED_VALUE")
        except TypeError:
            rows = ws.get("A7:J10")
        return SG.parse_signals(rows)

    # --- 6-monthly universe refresh: Universe tab, Universe History tab, Watchlist rows ---
    def write_universe(self, rows: List[dict], summary: dict, params_text: str, next_due: dt.date, source: str):
        sh = self._book()
        table = UR.sheet_table(rows)
        need_rows = len(table) + 20
        try:
            ws = sh.worksheet(UNIVERSE_TAB)
            if ws.row_count < need_rows:
                ws.add_rows(need_rows - ws.row_count)
        except Exception:
            ws = sh.add_worksheet(title=UNIVERSE_TAB, rows=max(300, need_rows), cols=len(UR.SHEET_HEADERS))
        ws.clear()
        head = [["ETF ki Dukan universe: refreshed by the bot every 6 months (1 Apr and 1 Oct). This tab is the refresh "
                 "report; to change the list, edit Type / Include / Symbol on the Watchlist tab, then run the GitHub "
                 "workflow 'ETF Bot: Sync list from sheet'."],
                ["Last refresh", summary["date"], "Next refresh", next_due.isoformat(), "Source", source],
                [params_text],
                [f"{summary['etfs']} ETFs + {summary['stocks']} stocks with Include = Y. New: {', '.join(summary['added']) or 'none'}. "
                 f"Dropped: {', '.join(summary['removed'] + summary['held_dropped']) or 'none'}."],
                [],
                UR.SHEET_HEADERS]
        ws.update(range_name="A1", values=head + table, value_input_option="RAW")
        try:
            hs = sh.worksheet(UNIVERSE_HISTORY_TAB)
        except Exception:
            hs = sh.add_worksheet(title=UNIVERSE_HISTORY_TAB, rows=200, cols=7)
            hs.append_row(["Date", "ETFs", "Stocks", "Added", "Dropped", "Dropped but held (sell/average only)", "Source"])
        hs.append_row([summary["date"], summary["etfs"], summary["stocks"], ", ".join(summary["added"]),
                       ", ".join(summary["removed"]), ", ".join(summary["held_dropped"]), source],
                      value_input_option="RAW")

    def sync_watchlist(self, universe: Dict[str, dict], rows: List[dict], held: set) -> List[str]:
        """Makes the tracker's Watchlist match the new list, so the sheet's own Signals rank the same names.
        Writes only the input cells (B type, C include, D symbol, G name, H sector, I volume, J price; A #).
        A new name goes into an empty row, else into the row of a dropped, unheld name. Rows are never deleted."""
        ws = self._ws("Watchlist")
        first, last = 5, 200
        cur = ws.get(f"A{first}:D{last}")
        cur = [list(r) + [""] * 4 for r in cur] + [["", "", "", ""]] * (last - first + 1 - len(cur))
        row_of = {str(r[3]).strip().upper(): first + i for i, r in enumerate(cur) if str(r[3]).strip()}
        stats = {r["symbol"]: r for r in rows}
        upd, notes = [], []
        free = [first + i for i, r in enumerate(cur) if not str(r[3]).strip()]
        reusable = [row_of[s] for s in row_of if s not in universe and s not in held]
        for s, rn in row_of.items():
            inc = "Y" if (s in universe and universe[s]["include"]) else "N"
            upd.append({"range": f"C{rn}", "values": [[inc]]})
            st = stats.get(s)
            if st and st.get("avg_volume") is not None and st.get("last_close"):
                upd.append({"range": f"I{rn}:J{rn}", "values": [[st["avg_volume"], st["last_close"]]]})
        for s, u in universe.items():
            if s in row_of:
                continue
            slots = free or reusable
            if not slots:
                notes.append(f"Watchlist has no free row for {s} (rows {first}-{last} full)")
                continue
            rn = slots.pop(0)
            st = stats.get(s, {})
            upd.append({"range": f"A{rn}:D{rn}", "values": [[rn - first + 1, u["type"], "Y" if u["include"] else "N", s]]})
            upd.append({"range": f"G{rn}:J{rn}", "values": [[u.get("name", ""), u.get("sector", ""),
                                                            st.get("avg_volume", ""), st.get("last_close", "")]]})
            upd.append({"range": f"M{rn}", "values": [[""]]})        # backup CMP / RSI belonged to the old name
            upd.append({"range": f"P{rn}", "values": [[""]]})
        if upd:
            ws.batch_update(upd, value_input_option="USER_ENTERED")
        return notes

    def log_universe_change(self, changes: List[str]):
        try:
            hs = self._book().worksheet(UNIVERSE_HISTORY_TAB)
        except Exception:
            hs = self._book().add_worksheet(title=UNIVERSE_HISTORY_TAB, rows=200, cols=7)
            hs.append_row(["Date", "ETFs", "Stocks", "Added", "Dropped", "Dropped but held (sell/average only)", "Source"])
        hs.append_row([now_ist().date().isoformat(), "", "", "; ".join(changes)[:45000], "", "",
                       "your sheet edits (Sync list from sheet workflow)"], value_input_option="RAW")

    def read_universe(self) -> Tuple[Dict[str, dict], Optional[str]]:
        ws = self._book().worksheet(UNIVERSE_TAB)
        vals = ws.get_all_values()
        last = vals[1][1] if len(vals) > 1 and len(vals[1]) > 1 else None
        return UR.universe_from_table(vals), last

    @staticmethod
    def _next_row(ws, col: int, first: int) -> int:
        return max(first, len(ws.col_values(col)) + 1)

    def _tracker(self, t: dict, state: Optional[dict]):
        """Writes only the yellow input cells of the tracker, exactly as the Read Me tells a person to."""
        op = t["op"]
        if op == "buy":
            ws = self._ws("Holdings")
            r = self._next_row(ws, 3, 5)
            ws.update(range_name=f"A{r}:F{r}", values=[[t["date"], t["time"], t["sym"], t["kind"], t["price"], t["qty"]]],
                      value_input_option="USER_ENTERED")
            ws.update(range_name=f"H{r}", values=[[f"bot {t['tag']}"]], value_input_option="USER_ENTERED")
        elif op == "sell":
            hs = self._ws("Holdings")
            cs = hs.col_values(3)
            isv = hs.col_values(9)
            for idx, sym in enumerate(cs, start=1):
                if idx >= 5 and sym == t["sym"] and (idx > len(isv) or not isv[idx - 1]):
                    hs.update(range_name=f"I{idx}", values=[[t["date"]]], value_input_option="USER_ENTERED")
            ws = self._ws("Sold")
            r = self._next_row(ws, 3, 5)
            ws.update(range_name=f"A{r}:C{r}", values=[[t["date"], t["time"], t["sym"]]], value_input_option="USER_ENTERED")
            ws.update(range_name=f"I{r}:J{r}", values=[[t["qty"], t["price"]]], value_input_option="USER_ENTERED")
            if state is not None and t.get("core_id"):
                for lot in state["core"]:
                    if lot["id"] == t["core_id"]:
                        lot["sold_row"] = r
                save_state(state)
        elif op == "core_sell":
            ws = self._ws("Core")
            r = t.get("sold_row")
            if not r:
                bs, ms = ws.col_values(2), ws.col_values(13)
                for idx, sym in enumerate(bs, start=1):
                    if idx >= 5 and sym == t["sym"] and (idx > len(ms) or not ms[idx - 1]):
                        r = idx
                        break
            if not r:
                raise TrackerMismatch(f"no unsold Core row for {t['sym']} on the Core tab")
            ws.update(range_name=f"M{r}:N{r}", values=[[t["date"], t["price"]]], value_input_option="USER_ENTERED")
        elif op == "withdraw":
            ws = self._ws("Capital")
            r = self._next_row(ws, 1, 21)
            ws.update(range_name=f"A{r}:D{r}", values=[[t["date"], "Withdrawal", t["amount"], "recorded by bot"]],
                      value_input_option="USER_ENTERED")


# ==========================================
# 4b. UNIVERSE FILES
# ==========================================
def load_universe_meta() -> dict:
    try:
        with open(UNIVERSE_META_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_universe_meta(meta: dict):
    with open(UNIVERSE_META_FILE + ".tmp", "w") as f:
        json.dump(meta, f, indent=1, default=str)
    os.replace(UNIVERSE_META_FILE + ".tmp", UNIVERSE_META_FILE)


def save_active_universe(universe: Dict[str, dict]):
    with open(ACTIVE_UNIVERSE_FILE + ".tmp", "w", newline="") as f:
        f.write(UR.to_csv(universe))
    os.replace(ACTIVE_UNIVERSE_FILE + ".tmp", ACTIVE_UNIVERSE_FILE)


def restore_universe(sheet: "SheetLogger"):
    """A rebuilt server has no state/universe_active.csv: take the refreshed list back from the sheet's Universe tab."""
    if os.path.exists(ACTIVE_UNIVERSE_FILE) or not UNIVERSE_AUTO or SHEET_MODE == "off" or not GSHEET_ID:
        return
    try:
        u, last = sheet.read_universe()
    except Exception as e:
        print(f"no Universe tab to restore from ({e}); using {UNIVERSE_FILE}")
        return
    if len([1 for x in u.values() if x["include"]]) >= 10:
        save_active_universe(u)
        meta = load_universe_meta()
        meta.update(last_refresh=last, restored_from_sheet=now_ist().isoformat())
        save_universe_meta(meta)
        print(f"Universe restored from the sheet's {UNIVERSE_TAB} tab ({len(u)} rows, refreshed {last}).")


def fetch_csv(urls: List[str], parser, min_rows: int) -> Tuple[Optional[list], str]:
    errs = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                                                       "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"})
            txt = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
            rows = parser(txt)
            if len(rows) >= min_rows:
                return rows, url
            errs.append(f"{url}: only {len(rows)} rows")
        except Exception as e:
            errs.append(f"{url}: {str(e)[:60]}")
    return None, "; ".join(errs)


# ==========================================
# 5. BOT
# ==========================================
class Bot:
    def __init__(self):
        _ensure_dirs()
        self.sheet = SheetLogger()
        restore_universe(self.sheet)
        self.universe = load_universe()
        self.broker = Broker(self.universe)
        self.state: Optional[dict] = None
        self.today = now_ist().date()
        self.lock_path = os.path.join(LOCK_DIR, f"run_{self.today}_{MODE}.json")
        self.closes: Dict[str, List[Tuple[str, float]]] = {}
        self.blocked: set = set()
        self.broker_qty: Optional[Dict[str, int]] = None
        self.stop_requested = False
        self.in_order = False
        self.events: List[str] = []
        self.seq = 0

    # --- lock ---
    def _write_lock(self, **kw):
        cur = {}
        try:
            with open(self.lock_path) as f:
                cur = json.load(f)
        except Exception:
            pass
        cur.update(kw, pid=os.getpid(), updated=now_ist().isoformat())
        with open(self.lock_path, "w") as f:
            json.dump(cur, f, indent=1)

    def claim_lock(self, resume: bool) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            self._write_lock(status="started")
            return True
        except FileExistsError:
            pass
        st = {}
        try:
            with open(self.lock_path) as f:
                st = json.load(f)
        except Exception:
            pass
        pid = st.get("pid")
        alive = False
        if pid and int(pid) != os.getpid():
            try:
                os.kill(int(pid), 0)
                alive = True
            except Exception:
                alive = False
        if alive:
            tg(f"⚠️ Bot is already running today (pid {pid}). Not starting a second copy.")
            return False
        if st.get("status") in ("done", "skipped") and not resume:
            print(f"Already ran today ({st.get('status')}). Use --resume to run again (journal limits still apply).")
            return False
        if st.get("status") not in ("done", "skipped") and not resume:
            tg(f"🚨 Today's earlier run stopped part-way (status {st.get('status')}, pid {pid}). "
               f"Recovering any orders it sent. Run `etf_dukan_bot.py run --resume` to finish the day.")
            self.state = load_state()
            if self.state:
                self.broker.login()
                self.broker.load_tokens()
                self.recover()
                self.sheet.flush(self.state)
                self.broker.logout()
            return False
        self._write_lock(status="resumed")
        return True

    # --- recovery of orders sent before a crash ---
    @staticmethod
    def _is_mine(o: dict, tag: str, it: dict) -> bool:
        return str(o.get("ordertag", "")) in (tag, tag + "M") or str(o.get("orderid")) in it.get("order_ids", [])

    def recover(self):
        st = self.state
        pend = dict(st.get("pending") or {})
        if not pend:
            print("recover: nothing pending.")
            return
        if PAPER_MODE:
            for tag, it in pend.items():
                tg(f"⚠️ [paper] order {tag} ({it['action']} {it['sym']}) was in flight at a crash; treated as not filled.")
                st["pending"].pop(tag, None)
            save_state(st)
            return
        ob = self.broker.order_book()
        if ob is None:
            tg("🚨 Could not read the order book to recover pending orders. Check AngelOne manually, then run `recover`.")
            return
        for tag, it in pend.items():
            mine = [o for o in ob if self._is_mine(o, tag, it)]
            for o in mine:
                if self.broker.status(o) not in TERMINAL:
                    self.broker.cancel(str(o.get("orderid")))
            if any(self.broker.status(o) not in TERMINAL for o in mine):
                time.sleep(3)
                ob2 = self.broker.order_book() or ob
                mine = [o for o in ob2 if self._is_mine(o, tag, it)]
            qty = sum(int(float(o.get("filledshares") or 0)) for o in mine)
            val = sum(int(float(o.get("filledshares") or 0)) * float(o.get("averageprice") or 0) for o in mine)
            ids = ",".join(str(o.get("orderid")) for o in mine)
            if qty > 0:
                self._apply_fill(it, qty, val / qty, tag, ids, note="recovered after crash")
                tg(f"♻️ Recovered {it['action']} {it['sym']}: {qty} filled @ ₹{val / qty:,.2f} (orders {ids}).")
            else:
                tg(f"♻️ {it['action']} {it['sym']} ({tag}) had no fill; nothing recorded.")
            st["pending"].pop(tag, None)
            save_state(st)

    # --- data ---
    def _needs_history(self, s: str) -> bool:
        return (self.universe[s]["include"] or s in self.state["positions"]
                or any(l["sym"] == s for l in self.state["core"]))

    def load_history(self, alert: bool = True):
        """Fetch daily closes for every counter that still lacks enough of them.
        Only good results are cached, so a counter that failed (rate limit, API hiccup)
        is fetched again on the next pass or the next call, not left out for the day."""
        cache = os.path.join(CACHE_DIR, f"closes_{self.today}.json")
        if os.path.exists(cache) and not self.closes:
            with open(cache) as f:
                self.closes = {k: [tuple(x) for x in v] for k, v in json.load(f).items()}
        enough = RULES.rsi_period + 1
        errors: Dict[str, str] = {}
        for pass_no in range(3):
            need = [s for s in self.universe if s in self.broker.tokens and self._needs_history(s)
                    and len(self.closes.get(s, [])) < enough]
            if not need:
                break
            if pass_no:
                print(f"history pass {pass_no + 1}: retrying {len(need)} counters")
                time.sleep(15 * pass_no)                  # let the per-minute limit reset
            fails_in_row = 0
            for s in need:
                if fails_in_row >= 5:                     # API looks down: stop hammering, retry next pass
                    errors.setdefault(s, "skipped, API not answering")
                    continue
                got = self.broker.daily_closes(s, self.today)
                if got is None:
                    errors[s] = self.broker.last_candle_error or "no answer"
                    fails_in_row += 1
                else:
                    fails_in_row = 0
                    errors.pop(s, None)
                    if len(got) >= len(self.closes.get(s, [])):
                        self.closes[s] = got
                time.sleep(0.5)                           # candle API limit is 3/sec and 180/min
        good = {k: v for k, v in self.closes.items() if len(v) >= enough}
        with open(cache, "w") as f:
            json.dump(good, f)
        thin = [s for s in self.universe if self.universe[s]["include"] and len(self.closes.get(s, [])) < enough]
        if thin and alert:
            failed = [s for s in thin if s in errors]
            short = [s for s in thin if s not in errors]
            parts = []
            if failed:
                parts.append(f"AngelOne did not return candles after retries: {', '.join(failed)}"
                             f" (last error: {errors[failed[-1]]})")
            if short:
                parts.append("fewer than " + str(enough) + " daily closes (new listing or no trades): "
                             + ", ".join(f"{s} ({len(self.closes.get(s, []))})" for s in short))
            tg("ℹ️ Left out of today's RSI ranking. " + "; ".join(parts)
               + ". They will not be picked for a new buy today.")
        return thin

    def snapshot(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        held = set(self.state["positions"]) | {l["sym"] for l in self.state["core"]}
        syms = [s for s in self.universe if self.universe[s]["include"] or s in held]
        ltp = self.broker.ltp_many(syms)
        rsi = {}
        for s in syms:
            if s in ltp and self.universe[s]["include"]:
                cl = [c for _, c in self.closes.get(s, [])] + [ltp[s]]
                v = S.rsi_wilder(cl, RULES.rsi_period)
                if v is not None:
                    rsi[s] = v
        return ltp, rsi

    def reconcile(self, report_only=False) -> List[str]:
        lines = []
        if PAPER_MODE:
            self.blocked = set()
            return ["Paper mode: AngelOne holdings are not compared (paper trades are not at the broker)."]
        q, raw = self.broker.broker_quantities()
        if q is None:
            msg = "Could not read AngelOne holdings/positions: " + "; ".join(raw)
            if report_only:
                return [msg]
            raise SkipDay(msg)
        self.broker_qty = q
        mine = S.bot_quantities(self.state)
        short, extra = [], []
        for sym, n in sorted(mine.items()):
            b = q.get(sym, 0)
            if b < n:
                short.append(f"{sym}: bot {n}, AngelOne {b}")
            elif b > n:
                extra.append(f"{sym}: bot {n}, AngelOne {b}")
        self.blocked = {s.split(":")[0] for s in short}
        if short:
            lines.append("🚨 AngelOne has FEWER shares than the bot's records (these symbols are blocked today):\n  " + "\n  ".join(short))
        if extra:
            lines.append("ℹ️ AngelOne has more shares than the bot holds (your own shares, left alone):\n  " + "\n  ".join(extra[:15]))
        if not short:
            lines.append(f"✅ Reconciled: AngelOne covers all {len(mine)} bot symbols.")
        if short and RECON_STRICT and not report_only:
            raise SkipDay("reconciliation shortfall and EKD_RECON_STRICT=1:\n" + "\n".join(short))
        return lines

    def sheet_check(self, phase: str, actions: List[dict], ltp, rsi=None, cash_cap=None, report_only=False):
        """Cross-check the bot's plan with the sheet's Signals tab. Returns (actions to trade, lines)."""
        if SIGNAL_POLICY == "off" or SHEET_MODE == "off" or not GSHEET_ID:
            return actions, []
        try:
            if not self.sheet.flush(self.state):
                raise RuntimeError("earlier rows are still queued, so the sheet's signals would be out of date")
            sig = self.sheet.read_signals()
        except Exception as e:
            if SIGNAL_IF_SHEET_DOWN == "bot" or SIGNAL_POLICY == "bot" or report_only:
                return actions, [f"⚠️ Sheet signals unreadable ({e}); using the bot's own plan."]
            return [], [f"🚨 Sheet signals unreadable ({e}). No {('sell' if phase == 'S' else 'buy')} orders sent "
                        f"(EKD_SIGNAL_IF_SHEET_DOWN=skip)."]
        if phase == "S":
            out, lines = SG.cross_check_sells(actions, sig, SIGNAL_POLICY, self.state, ltp, RULES, self.today, self.blocked)
        else:
            out, lines = SG.cross_check_buys(actions, sig, SIGNAL_POLICY, self.state, self.universe, ltp, rsi or {}, RULES,
                                             self.today, RATES, self.blocked, cash_cap)
        if lines:
            what = {"agree": "only matching actions are traded", "sheet": "trading the sheet's signals",
                    "bot": "trading the bot's plan"}.get(SIGNAL_POLICY, SIGNAL_POLICY)
            lines = [f"🔀 Sheet vs bot ({'sell' if phase == 'S' else 'buy'} check, {what}):"] + [f"  {l}" for l in lines]
        else:
            lines = [f"✅ Sheet signals match the bot ({'sell' if phase == 'S' else 'buy'} check)."]
        return out, lines

    def broker_cash_cap(self) -> Optional[float]:
        if PAPER_MODE:
            return None
        c = self.broker.available_cash()
        if c is None:
            raise SkipDay("could not read AngelOne available cash")
        return c - BROKER_CASH_FLOOR

    # --- plan text ---
    def plan_text(self, ltp, rsi, sells, buys, notes, title="DRY-RUN PLAN") -> str:
        sm = S.summary(self.state, ltp, RULES, self.today)
        L = [f"📋 {title} {self.today:%d-%b-%Y} {now_ist():%H:%M}",
             f"Cash {inr(sm['cash'])} | free for buys {inr(sm['free_for_buys'])} (owed withdrawal {inr(sm['owed'])}, kept back {inr(sm['kept_back'])})",
             f"Open {sm['open_positions']}/{RULES.max_positions} | Core lots {sm['core_lots']} | Net worth {inr(sm['net_worth'])}"]
        for kind, k in ((S.ETF, RULES.etf_shortlist), (S.STOCK, RULES.stock_shortlist)):
            sl = S.shortlist(self.universe, rsi, ltp, kind, k)
            L.append(f"{kind} top {k} by RSI: " + ", ".join(f"{s} {rsi[s]:.1f}{'*' if s in self.state['positions'] else ''}" for s in sl))
        L.append("(* = already held)")
        L.append(f"3:00 pm sells:" + ("" if sells else " none"))
        for a in sells:
            L.append(f"  {a['action']} {a['sym']} x{a['qty']} @~₹{a['ref']:,.2f} ({a['why']})" + (f", keep {a['keep']} as Core" if a.get("keep") else ""))
        L.append(f"3:10 pm buys:" + ("" if buys else " none"))
        for a in buys:
            L.append(f"  {a['action']} {a['sym']} x{a['qty']} @~₹{a['ref']:,.2f} = {inr(a['qty'] * a['ref'])} ({a['why']})")
        for n in notes:
            L.append(f"  · {n}")
        if self.blocked:
            L.append(f"Blocked today (broker mismatch): {', '.join(sorted(self.blocked))}")
        if sm["owed"] > 0:
            L.append(f"💸 Withdrawal of {inr(sm['owed'])} is due. Take it from AngelOne yourself, then run: etf_dukan_bot.py withdraw {sm['owed']:.0f}")
        return "\n".join(L)

    def planned_buys_after_sells(self, ltp, rsi, sells):
        """Buys as they would look after the planned sells fill (for the 2:50 pm report only)."""
        sim = json.loads(json.dumps(self.state))
        for a in sells:
            val = a["qty"] * a["ref"]
            ch = S.charges("SELL", a["type"], val, RATES)
            if a["action"] == "SELL":
                S.apply_sell(sim, a["sym"], a["qty"], a["ref"], ch, self.today, a["qty"], a["keep"])
            else:
                S.apply_core_sell(sim, a["core_id"], a["qty"], a["ref"], ch, self.today)
        cap = None if PAPER_MODE else (self.broker.available_cash() or 0) - BROKER_CASH_FLOOR
        return S.plan_buys(sim, self.universe, rsi, ltp, RULES, self.today, RATES, self.blocked, cap)

    # --- execution ---
    def new_tag(self, phase: str) -> str:
        self.seq += 1
        base = f"EKD{self.today:%y%m%d}{phase}"
        used = set(self.state.get("applied_tags", [])) | set(self.state.get("pending", {}))
        n = self.seq
        while f"{base}{n}" in used:
            n += 1
        self.seq = n
        return f"{base}{n}"

    def execute(self, a: dict, tag: str) -> Tuple[int, float, str, str]:
        """Returns (filled_qty, avg_price, order_ids, note)."""
        side = "SELL" if a["action"] in ("SELL", "CORE_SELL") else "BUY"
        sym, qty, ref = a["sym"], int(a["qty"]), float(a["ref"])
        if PAPER_MODE:
            slip = PAPER_SLIPPAGE_PCT / 100.0
            px = ref * (1 + slip) if side == "BUY" else ref * (1 - slip)
            return qty, round(px, 4), "paper", "paper fill at live price ± slippage"
        b = self.broker
        if side == "SELL" and self.broker_qty is not None and self.broker_qty.get(sym, 0) < qty:
            raise OrderError(f"AngelOne shows {self.broker_qty.get(sym, 0)} {sym}, bot wants to sell {qty}")
        fills: List[Tuple[int, float]] = []
        ids: List[str] = []
        notes = []

        def record(oid):
            ids.append(oid)
            self.state["pending"][tag]["order_ids"] = list(ids)
            save_state(self.state)

        def filled_of(info):
            return int(float((info or {}).get("filledshares") or 0)), float((info or {}).get("averageprice") or 0)

        # step 1: LIMIT near LTP
        lim = b.tick_round(sym, ref * (1 + LIMIT_BUFFER_PCT / 100) if side == "BUY" else ref * (1 - LIMIT_BUFFER_PCT / 100), side)
        oid = b.place(sym, side, qty, "LIMIT", lim, tag)
        record(oid)
        info = b.wait_terminal(oid, LIMIT_WAIT_SECS)
        st = b.status(info)
        if st != "complete":
            if st not in TERMINAL:
                b.cancel(oid)
                info = b.wait_terminal(oid, 15) or info
            st = b.status(info)
            if st == "rejected":
                txt = (info or {}).get("text", "")
                tg(f"❌ {side} {sym} LIMIT rejected: {txt}" + ("\nSells need DDPI / eDIS authorisation on AngelOne." if side == "SELL" else ""))
                raise OrderError(f"{sym} limit order rejected: {txt}")
            if st not in TERMINAL:
                raise OrderError(f"{sym} limit order {oid} not cancelled (status {st or 'unknown'}); check AngelOne")
        fq, fp = filled_of(info)
        if fq:
            fills.append((fq, fp))
        rem = qty - fq
        notes.append(f"limit ₹{lim:,.2f}: {fq}/{qty}")
        # step 2: MARKET for what is left, if the price hasn't run away
        if rem > 0 and MARKET_FALLBACK and not self.stop_requested:
            now_px = b.ltp_many([sym]).get(sym)
            if not now_px:
                notes.append("no live price for the market step; stopped")
            elif abs(now_px / ref - 1) * 100 > MAX_CHASE_PCT:
                notes.append(f"price moved {(now_px / ref - 1) * 100:+.2f}% from plan (> {MAX_CHASE_PCT}%); market step skipped")
            else:
                oid2 = b.place(sym, side, rem, "MARKET", 0.0, tag + "M")
                record(oid2)
                info2 = b.wait_terminal(oid2, MARKET_WAIT_SECS)
                if b.status(info2) not in TERMINAL:
                    b.cancel(oid2)
                    info2 = b.wait_terminal(oid2, 15) or info2
                if b.status(info2) == "rejected":
                    notes.append(f"market rejected: {(info2 or {}).get('text', '')}")
                q2, p2 = filled_of(info2)
                if q2:
                    fills.append((q2, p2))
                notes.append(f"market: {q2}/{rem}")
        tq = sum(q for q, _ in fills)
        ap = sum(q * p for q, p in fills) / tq if tq else 0.0
        if 0 < ap and tq and any(p <= 0 for _, p in fills):
            ap = ref
        return tq, ap, ",".join(ids), "; ".join(notes)

    def _apply_fill(self, it: dict, qty: int, price: float, tag: str, ids: str, note: str = ""):
        val = qty * price
        side = "SELL" if it["action"] in ("SELL", "CORE_SELL") else "BUY"
        ch = S.charges(side, it["type"], val, RATES)
        d = dt.date.fromisoformat(it["date"])
        tm = now_ist().strftime("%H:%M:%S")
        if side == "SELL" and self.broker_qty is not None:
            self.broker_qty[it["sym"]] = self.broker_qty.get(it["sym"], 0) - qty
        if it["action"] == "SELL":
            rec = S.apply_sell(self.state, it["sym"], qty, price, ch, d, it["qty"], it.get("keep", 0), tag, ids)
            self.sheet.log([it["date"], tm, MODE, "SELL", it["sym"], it["type"], qty, round(price, 2), round(val, 2),
                            round(ch, 2), round(rec["avg"], 4), round(rec["gain"] * 100, 2), round(rec["net"], 2),
                            rec["kept"], f"{tag} {ids}", note],
                           {"op": "sell", "date": it["date"], "time": tm, "sym": it["sym"], "qty": qty,
                            "price": round(price, 2), "core_id": rec["core_id"]})
            msg = (f"🔴 SOLD {it['sym']} x{qty} @ ₹{price:,.2f} ({rec['gain'] * 100:+.2f}%) net {inr(rec['net'])}"
                   + (f", kept {rec['kept']} as Core" if rec["kept"] else "") + (f", {rec['left_open']} still open (partial fill)" if rec["left_open"] else ""))
        elif it["action"] == "CORE_SELL":
            rec = S.apply_core_sell(self.state, it["core_id"], qty, price, ch, d, tag, ids)
            self.sheet.log([it["date"], tm, MODE, "CORE_SELL (3x)", it["sym"], it["type"], qty, round(price, 2), round(val, 2),
                            round(ch, 2), round(rec["avg"], 4), round(rec["gain"] * 100, 2), round(rec["net"], 2), 0,
                            f"{tag} {ids}", note],
                           {"op": "core_sell", "date": it["date"], "sym": it["sym"], "price": round(price, 2),
                            "sold_row": rec.get("sold_row")})
            msg = f"🏆 CORE 3x SOLD {it['sym']} x{qty} @ ₹{price:,.2f} ({rec['gain'] * 100:+.0f}%) net {inr(rec['net'])}"
        else:
            rec = S.apply_buy(self.state, it["sym"], it["type"], qty, price, ch, d, it["action"], tag, ids, it.get("rsi"))
            self.sheet.log([it["date"], tm, MODE, it["action"], it["sym"], it["type"], qty, round(price, 2), round(val, 2),
                            round(ch, 2), round(rec["avg_after"], 4), "", "", "", f"{tag} {ids}", note],
                           {"op": "buy", "date": it["date"], "time": tm, "sym": it["sym"], "kind": it["action"],
                            "price": round(price, 2), "qty": qty, "tag": tag})
            msg = f"🟢 BOUGHT ({it['action']}) {it['sym']} x{qty} @ ₹{price:,.2f} = {inr(val)}"
        self.state["pending"].pop(tag, None)
        save_state(self.state)
        self.events.append(msg)
        tg(msg + (f"\n{note}" if note else ""))

    def do_actions(self, actions: List[dict], phase: str, cutoff: dt.datetime):
        for a in actions:
            if self.stop_requested:
                tg("🛑 Stop requested: no further orders today.")
                return
            if now_ist() > cutoff:
                tg(f"⏰ Past {cutoff:%H:%M}: {a['action']} {a['sym']} not sent.")
                continue
            tag = self.new_tag(phase)
            it = dict(a, date=self.today.isoformat(), tag=tag, order_ids=[], sent_at=now_ist().isoformat())
            self.state["pending"][tag] = it
            save_state(self.state)
            self.in_order = True
            try:
                q, px, ids, note = self.execute(a, tag)
            except OrderError as e:
                self.in_order = False
                # anything that did fill is still in the order book: recover() picks it up
                self.recover() if not PAPER_MODE else self.state["pending"].pop(tag, None)
                save_state(self.state)
                tg(f"❌ {a['action']} {a['sym']} failed: {e}")
                self.events.append(f"❌ {a['action']} {a['sym']}: {e}")
                continue
            self.in_order = False
            if q > 0:
                self._apply_fill(it, q, px, tag, ids, note)
            else:
                self.state["pending"].pop(tag, None)
                save_state(self.state)
                tg(f"⚠️ {a['action']} {a['sym']}: nothing filled ({note}).")
                self.events.append(f"⚠️ {a['action']} {a['sym']} not filled")

    # --- day ---
    def skip(self, reason: str):
        self._write_lock(status="skipped", reason=reason)
        tg(f"⚠️ ETF BOT SKIPPED ({self.today})\nReason: {reason}")
        self.sheet.log([self.today.isoformat(), now_ist().strftime("%H:%M:%S"), MODE, "SKIPPED", "", "", "", "", "", "",
                        "", "", "", "", "", reason])
        self.sheet.flush(self.state)

    def is_trading_day(self) -> Tuple[bool, str]:
        if self.today.weekday() >= 5:
            return False, "Weekend"
        hol_file = os.path.join(HERE, "nse_holidays.txt")
        if os.path.exists(hol_file):
            with open(hol_file) as f:
                hol = {l.split("#")[0].strip() for l in f if l.split("#")[0].strip()}
            if self.today.isoformat() in hol:
                return False, "NSE holiday (nse_holidays.txt)"
        try:
            import pandas_market_calendars as mcal
            sch = mcal.get_calendar("XNSE").schedule(start_date=str(self.today), end_date=str(self.today))
            if sch.empty:
                return False, "NSE holiday (pandas_market_calendars)"
        except Exception:
            pass
        return True, ""

    def run(self, resume=False):
        ok, why = self.is_trading_day()
        if not ok:
            print(f"Not a trading day: {why}")
            return
        if not self.claim_lock(resume):
            return
        if now_ist() > at_ist(self.today, LATEST_START) and not resume:
            self.state = load_state()
            self.skip(f"started too late ({now_ist():%H:%M}, cutoff {LATEST_START})")
            return
        self.state = load_state()
        if not self.state:
            self._write_lock(status="skipped", reason="no state file")
            tg(f"🚨 No state file {STATE_FILE}. Run `etf_dukan_bot.py init --capital 4000000 --start YYYY-MM-DD` first.")
            return
        backup_state()
        self.apply_sheet_sync()                       # your list from the "Sync list from sheet" workflow, if new
        self.broker.login()
        self.broker.load_tokens()
        try:
            self.recover()
            open_ok, open_msg = self.broker.market_open_today()
            if not open_ok:
                raise SkipDay(f"market closed today ({open_msg})")
            recon = self.reconcile()
            self.load_history(alert=False)
            # ---- 2:50 pm dry-run plan
            wait_until(at_ist(self.today, PLAN_TIME), self)
            self.load_history()                       # re-fetch anything that failed earlier
            ltp, rsi = self.snapshot()
            sells = S.plan_sells(self.state, ltp, RULES, self.today, self.blocked)
            buys, notes = self.planned_buys_after_sells(ltp, rsi, sells)
            _, chk = self.sheet_check("S", sells, ltp, report_only=True)
            tg(self.plan_text(ltp, rsi, sells, buys, notes) + "\n\n" + "\n".join(recon + chk)
               + ("\n(The sheet's buy signals are checked at 3:10 pm, after the sell is written to it.)" if chk else ""))
            if DRY_RUN:
                self._write_lock(status="done", reason="dry run")
                return
            self._write_lock(status="planned")
            # ---- 3:00 pm sells
            wait_until(at_ist(self.today, SELL_TIME), self)
            ltp, _ = self.snapshot()
            sells = S.plan_sells(self.state, ltp, RULES, self.today, self.blocked)
            sells, chk = self.sheet_check("S", sells, ltp)
            if chk and not chk[0].startswith("✅"):
                tg("\n".join(chk))
            self.do_actions(sells, "S", at_ist(self.today, SELL_CUTOFF))
            self._write_lock(status="sold")
            # ---- 3:10 pm buys (RSI and prices refreshed)
            self.sheet.flush(self.state)                # write the sells so the sheet's buy signals see them
            wait_until(max(at_ist(self.today, BUY_TIME), now_ist() + dt.timedelta(seconds=SIGNAL_RECALC_SECS)), self)
            ltp, rsi = self.snapshot()
            cap = self.broker_cash_cap()
            buys, notes = S.plan_buys(self.state, self.universe, rsi, ltp, RULES, self.today, RATES, self.blocked, cap)
            buys, chk = self.sheet_check("B", buys, ltp, rsi, cap)
            if chk and not chk[0].startswith("✅"):
                tg("\n".join(chk))
            for n in notes:
                print(f"note: {n}")
            self.do_actions(buys, "B", at_ist(self.today, BUY_CUTOFF))
            self._write_lock(status="done")
            # ---- summary
            sm = S.summary(self.state, ltp, RULES, self.today)
            txt = (f"🏁 DAY DONE {self.today:%d-%b}\n" + ("\n".join(self.events) if self.events else "No trades today.")
                   + ("\n" + "\n".join(f"· {n}" for n in notes) if notes and not buys else "")
                   + f"\nCash {inr(sm['cash'])} | open {sm['open_positions']}/{RULES.max_positions} | Core {sm['core_lots']}"
                   f" | net worth {inr(sm['net_worth'])} | realised {inr(sm['realised_net'])}")
            tg(txt)
            self.sheet.log([self.today.isoformat(), now_ist().strftime("%H:%M:%S"), MODE, "DAY DONE", "", "", "", "", "", "",
                            "", "", round(sm["realised_net"], 2), "", "",
                            f"cash {sm['cash']:.0f}; open {sm['open_positions']}; core {sm['core_lots']}; net worth {sm['net_worth']:.0f}"])
            if not PAPER_MODE:
                time.sleep(5)
                print("\n".join(self.reconcile(report_only=True)))
            self.sheet.flush(self.state)
            self.maybe_refresh_universe()             # 1 Apr / 1 Oct: new ETF + stock list, used from tomorrow
        except SkipDay as e:
            self.skip(str(e))
        finally:
            self.sheet.flush(self.state)
            self.sheet.backup_state(self.state)
            self.broker.logout()

    # --- your own edits, synced from the sheet by the "ETF Bot: Sync list from sheet" workflow ---
    def apply_sheet_sync(self) -> bool:
        """The workflow commits universe.csv + universe_synced.txt; the VM gets them on boot (or the workflow
        copies them onto a running VM). The first run after that makes universe.csv the bot's list.
        Held counters missing from it stay with Include = N. Returns True if a new sync was applied."""
        if not os.path.exists(SHEET_SYNC_STAMP):
            return False
        with open(SHEET_SYNC_STAMP) as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        stamp = lines[0] if lines else ""
        meta = load_universe_meta()
        if not stamp or meta.get("sheet_sync_applied") == stamp:
            return False
        if meta.get("last_refresh") and stamp[:10] < str(meta["last_refresh"])[:10]:
            meta["sheet_sync_applied"] = stamp          # older than the last 6-monthly refresh: superseded by it
            save_universe_meta(meta)
            return False
        try:
            new = load_universe(UNIVERSE_FILE)
        except Exception as e:
            tg(f"⚠️ Synced list in universe.csv is unreadable ({e}); keeping the current list.")
            return False
        kept = []
        for s in sorted(self.held_symbols()):
            if s not in new and s in self.universe:
                new[s] = dict(self.universe[s], include=False)
                kept.append(s)
        if sum(1 for u in new.values() if u["include"]) < 10:
            tg("⚠️ Synced list has fewer than 10 names with Include = Y; keeping the current list.")
            return False
        save_active_universe(new)
        self.universe = new
        self.broker.u = new
        meta["sheet_sync_applied"] = stamp
        save_universe_meta(meta)
        changes = lines[1:]
        tg(f"📝 Your sheet list (synced {stamp}) is now the bot's list:\n  " + "\n  ".join(changes[:40])
           + (f"\nHeld but not on the sheet, kept with Include = N (sell / average only): {', '.join(kept)}" if kept else ""),
           loud=True)
        try:
            self.sheet.log_universe_change([f"synced {stamp}"] + changes)
        except Exception as e:
            print(f"Universe History not updated: {e}")
        return True

    # --- 6-monthly universe refresh ---
    def held_symbols(self) -> set:
        st = self.state or {}
        return set(st.get("positions", {})) | {l["sym"] for l in st.get("core", [])}

    def maybe_refresh_universe(self):
        """Called after the day's buys. Never raises: a failed refresh is retried on the next trading day."""
        meta = load_universe_meta()
        try:
            if meta.get("sheet_pending"):
                self._write_universe_sheet(meta)
            if UNIVERSE_AUTO and UR.is_due(self.today, meta.get("last_refresh"), UNIVERSE_MONTHS):
                if now_ist() > at_ist(self.today, UNIVERSE_LATEST):
                    print(f"universe refresh due but it is after {UNIVERSE_LATEST}; next run")
                    return
                self.refresh_universe()
        except Exception as e:
            print(f"universe refresh failed:\n{traceback.format_exc()}")
            tg(f"⚠️ 6-monthly ETF/stock list refresh failed ({str(e)[:150]}). The current list stays; "
               f"it is tried again on the next trading day.")

    def _write_universe_sheet(self, meta: dict) -> bool:
        if SHEET_MODE == "off" or not GSHEET_ID:
            return True
        last = os.path.join(HOME, "state", "universe_last_refresh.json")
        try:
            with open(last) as f:
                d = json.load(f)
            self.sheet.write_universe(d["rows"], d["summary"], d["params"], dt.date.fromisoformat(d["next_due"]), d["source"])
            notes = self.sheet.sync_watchlist(load_universe(ACTIVE_UNIVERSE_FILE), d["rows"], set(d["held"])) \
                if UNIVERSE_SYNC_WATCHLIST else []
            if notes:
                tg("⚠️ Watchlist update: " + "; ".join(notes))
            meta["sheet_pending"] = False
            save_universe_meta(meta)
            return True
        except Exception as e:
            meta["sheet_pending"] = True
            save_universe_meta(meta)
            tg(f"⚠️ New ETF/stock list saved on the server but not yet written to the sheet ({str(e)[:120]}). "
               f"Retried on the next run.")
            return False

    def refresh_universe(self, force: bool = False, dry_run: bool = False) -> bool:
        meta = load_universe_meta()
        today = self.today.isoformat()
        n50, src1 = fetch_csv(NIFTY50_URLS, UR.parse_nifty50, 45)
        etl, src2 = fetch_csv(ETF_LIST_URLS, UR.parse_etf_list, 50)
        if (n50 is None or etl is None) and not force:
            fails = sorted(set(meta.get("list_fail_dates", [])) | {today})
            meta["list_fail_dates"] = fails
            save_universe_meta(meta)
            if len(fails) < UNIVERSE_FALLBACK_DAYS:
                tg(f"⚠️ 6-monthly list refresh: could not download the NSE "
                   f"{'NIFTY 50 list' if n50 is None else ''}{' and ' if n50 is None and etl is None else ''}"
                   f"{'ETF list' if etl is None else ''} ({(src1 if n50 is None else src2)[:200]}). "
                   f"Trying again next trading day ({len(fails)}/{UNIVERSE_FALLBACK_DAYS}); after that the current "
                   f"names are re-ranked by liquidity instead.")
                return False
        old = self.universe
        etf_c: Dict[str, dict] = {}
        for r in etl or []:
            o = old.get(r["symbol"], {})
            etf_c[r["symbol"]] = {"symbol": r["symbol"], "underlying": r["underlying"] or r["name"], "name": r["name"],
                                  "sector": o.get("sector") or UR.guess_sector(r["underlying"] or r["name"])}
        for s, o in old.items():
            if o["type"] == S.ETF and s not in etf_c:
                etf_c[s] = {"symbol": s, "underlying": o.get("name", s), "name": o.get("name", s), "sector": o.get("sector", "")}
        if n50 is not None:
            stk_c = [{"symbol": r["symbol"], "name": r["name"], "industry": r["industry"],
                      "sector": (old.get(r["symbol"]) or {}).get("sector") or r["industry"]} for r in n50]
        else:
            stk_c = [{"symbol": s, "name": o.get("name", s), "sector": o.get("sector", ""),
                      "industry": "Financial Services" if UR.is_financial(o.get("sector", "")) else o.get("sector", "")}
                     for s, o in old.items() if o["type"] == S.STOCK]
        source = (f"NSE NIFTY 50 list" if n50 is not None else "current stock names (NSE list unreachable)") + " + " + \
                 (f"NSE ETF list" if etl is not None else "current ETF names (NSE list unreachable)") + \
                 " + AngelOne daily candles"
        sm = self.broker.scrip_master_nse()
        cands = {c["symbol"]: c for c in list(etf_c.values()) + stk_c}
        stats: Dict[str, dict] = {}
        missing, failed, run_fail = [], [], 0
        for i, (sym, c) in enumerate(cands.items()):
            row = sm.get((old.get(sym) or {}).get("angel_symbol") or f"{sym}-EQ")
            if not row:
                missing.append(sym)
                continue
            if run_fail >= 8:                          # API struggling: let the per-minute limit reset
                time.sleep(30)
                run_fail = 0
            bars = self.broker.daily_bars(row["token"], self.today)
            if bars is None:
                failed.append(sym)
                run_fail += 1
            else:
                run_fail = 0
                lq = UR.liquidity(bars)
                if lq:
                    stats[sym] = lq
            time.sleep(0.4)                            # candle API: 3/sec, 180/min
        asked = len(cands) - len(missing)
        if asked == 0 or len(failed) > 0.3 * asked:
            raise RuntimeError(f"AngelOne candles failed for {len(failed)}/{asked} counters")
        p = universe_params()
        new, rows, summary = UR.build(old, self.held_symbols(), list(etf_c.values()), stk_c, stats, p, self.today)
        for s in new:                                  # keep the scrip-master symbol that was actually found
            if not new[s].get("angel_symbol"):
                new[s]["angel_symbol"] = f"{s}-EQ"
        if summary["etfs"] < 10 or summary["stocks"] < 10:
            raise RuntimeError(f"only {summary['etfs']} ETFs / {summary['stocks']} stocks passed; list not changed")
        nd = UR.next_due(self.today, UNIVERSE_MONTHS)
        txt = (f"🔄 {'PREVIEW of the ' if dry_run else ''}6-monthly ETF + stock list refresh {self.today:%d-%b-%Y}\n"
               f"{summary['etfs']} ETFs + {summary['stocks']} stocks (by 6-month median traded value).\n"
               f"New: {', '.join(summary['added']) or 'none'}\n"
               f"Dropped: {', '.join(summary['removed']) or 'none'}\n"
               + (f"Dropped but held (not sold; no new buys, target sell / averaging / Core 3x continue): "
                  f"{', '.join(summary['held_dropped'])}\n" if summary["held_dropped"] else "")
               + (f"Not found at AngelOne: {', '.join(missing[:15])}{' …' if len(missing) > 15 else ''}\n" if missing else "")
               + (f"No candles: {', '.join(failed[:15])}\n" if failed else "")
               + f"Source: {source}. Next refresh {nd:%d-%b-%Y}."
               + ("" if dry_run else f"\nUsed from the next trading day. Full list: sheet tab '{UNIVERSE_TAB}'."))
        for n in summary["notes"]:
            print("universe:", n)
        if dry_run:
            with open(os.path.join(HOME, "state", "universe_preview.csv"), "w") as f:
                f.write(UR.to_csv(new))
            tg(txt + f"\n(Preview only: nothing changed. File state/universe_preview.csv)")
            return True
        save_active_universe(new)
        with open(os.path.join(HOME, "state", "universe_last_refresh.json"), "w") as f:
            json.dump({"rows": rows, "summary": summary, "params": p.text(), "next_due": nd.isoformat(),
                       "source": source, "held": sorted(self.held_symbols())}, f, default=str)
        meta.update(last_refresh=today, source=source, etfs=summary["etfs"], stocks=summary["stocks"],
                    list_fail_dates=[], sheet_pending=True)
        save_universe_meta(meta)
        tg(txt, loud=bool(summary["added"] or summary["removed"] or summary["held_dropped"]))   # list changed: ring
        self._write_universe_sheet(meta)
        return True

    def plan_now(self):
        self.state = load_state()
        if not self.state:
            raise SystemExit(f"No state file {STATE_FILE}; run init first.")
        self.apply_sheet_sync()
        self.broker.login()
        self.broker.load_tokens()
        try:
            recon = self.reconcile(report_only=True)
            self.load_history()
            ltp, rsi = self.snapshot()
            sells = S.plan_sells(self.state, ltp, RULES, self.today, self.blocked)
            buys, notes = self.planned_buys_after_sells(ltp, rsi, sells)
            _, chk = self.sheet_check("S", sells, ltp, report_only=True)
            _, chk2 = self.sheet_check("B", buys, ltp, rsi, report_only=True) if not sells else ([], [])
            tg(self.plan_text(ltp, rsi, sells, buys, notes, "PLAN (no orders)") + "\n\n" + "\n".join(recon + chk + chk2))
        finally:
            self.broker.logout()


# ==========================================
# 6. CLI
# ==========================================
def cmd_status():
    st = load_state()
    if not st:
        raise SystemExit(f"No state file {STATE_FILE}")
    today = now_ist().date()
    sm = S.summary(st, {}, RULES, today)
    print(f"Mode {MODE} | start {st['start_date']} | state {STATE_FILE}")
    for k, v in sm.items():
        print(f"  {k:15s} {v:,.2f}" if isinstance(v, float) else f"  {k:15s} {v}")
    print("Open positions:")
    for s, p in sorted(st["positions"].items()):
        print(f"  {s:12s} {p['type']:5s} qty {p['qty']:6d} avg {S.avg_cost(p):10.2f} last buy {p['last']:10.2f} since {p['first']} buys {p['buys']}")
    print("Core lots:")
    for l in st["core"]:
        print(f"  #{l['id']:<3d} {l['sym']:12s} qty {l['qty']:6d} cost {l['cost_price']:10.2f} 3x at {l['cost_price'] * RULES.core_multiple:10.2f} since {l['since']}")
    if st.get("pending"):
        print(f"PENDING (crash?): {list(st['pending'])} -> run `recover`")


def main():
    ap = argparse.ArgumentParser(description="ETF ki Dukan AngelOne bot")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init")
    p.add_argument("--capital", type=float, required=True)
    p.add_argument("--start", required=True, help="first trading day YYYY-MM-DD, or 'auto' for today (withdrawals start 6 months later)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--if-missing", action="store_true", help="do nothing if the state file already exists (for setup scripts)")
    rs = sp.add_parser("restore-state", help="copy the records back from the sheet's 'Bot State' tab")
    rs.add_argument("--if-missing", action="store_true")
    r = sp.add_parser("run")
    r.add_argument("--resume", action="store_true")
    sp.add_parser("plan")
    sp.add_parser("status")
    sp.add_parser("reconcile")
    sp.add_parser("recover")
    sp.add_parser("flush-sheet")
    ru = sp.add_parser("refresh-universe", help="the 6-monthly ETF + stock list refresh, now")
    ru.add_argument("--dry-run", action="store_true", help="show the new list on Telegram; change nothing")
    ru.add_argument("--force", action="store_true", help="refresh even if not due / NSE lists unreachable")
    for name in ("withdraw", "deposit"):
        w = sp.add_parser(name)
        w.add_argument("amount", type=float)
        w.add_argument("--date")
    s = sp.add_parser("split")
    s.add_argument("symbol")
    s.add_argument("ratio", type=float, help="new shares per old share, e.g. 10 for a 1:10 split")
    a = ap.parse_args()
    _ensure_dirs()

    if a.cmd == "restore-state":
        if os.path.exists(STATE_FILE) and a.if_missing:
            print(f"{STATE_FILE} exists; nothing restored.")
            return
        try:
            st = SheetLogger().restore_state()
        except Exception as e:
            print(f"No state restored from the sheet ({e}).")
            return
        if not st:
            print("The sheet has no saved state.")
            return
        backup_state()
        save_state(st)
        tg(f"♻️ Restored the bot's {MODE} records from the sheet: {len(st['positions'])} open positions, "
           f"{len(st['core'])} Core lots, cash {inr(st['cash'])}.")
        return
    if a.cmd == "init":
        if os.path.exists(STATE_FILE) and a.if_missing:
            print(f"{STATE_FILE} exists; init skipped.")
            return
        if a.start.lower() == "auto":
            a.start = now_ist().date().isoformat()
        if os.path.exists(STATE_FILE) and not a.force:
            raise SystemExit(f"{STATE_FILE} exists. Use --force to overwrite (a backup is kept).")
        backup_state()
        st = S.new_state(a.capital, dt.date.fromisoformat(a.start), MODE)
        save_state(st)
        print(f"Created {STATE_FILE}: capital ₹{a.capital:,.0f}, start {a.start}, "
              f"withdrawals from {S.withdrawal_start(st, RULES)}")
        return
    if a.cmd == "status":
        return cmd_status()
    if a.cmd in ("withdraw", "deposit"):
        st = load_state()
        d = dt.date.fromisoformat(a.date) if a.date else now_ist().date()
        backup_state()
        (S.withdraw if a.cmd == "withdraw" else S.deposit)(st, a.amount, d)
        save_state(st)
        sl = SheetLogger()
        sl.log([d.isoformat(), now_ist().strftime("%H:%M:%S"), MODE, a.cmd.upper(), "", "", "", "", round(a.amount, 2),
                "", "", "", "", "", "", "manual"], {"op": "withdraw", "date": d.isoformat(), "amount": a.amount} if a.cmd == "withdraw" else None)
        sl.flush(st)
        print(f"{a.cmd} ₹{a.amount:,.0f} recorded. Cash now ₹{st['cash']:,.0f}; owed {S.withdrawal_owed(st, RULES, d):,.0f}")
        return
    if a.cmd == "split":
        st = load_state()
        sym = a.symbol.upper()
        backup_state()
        if sym in st["positions"]:
            p = st["positions"][sym]
            p["qty"] = int(round(p["qty"] * a.ratio))
            p["last"] /= a.ratio
        for l in st["core"]:
            if l["sym"] == sym:
                l["qty"] = int(round(l["qty"] * a.ratio))
                l["cost_price"] /= a.ratio
        st["journal"].append({"date": now_ist().date().isoformat(), "kind": "SPLIT", "sym": sym, "ratio": a.ratio})
        save_state(st)
        print(f"{sym} adjusted for split ratio {a.ratio}")
        return
    if a.cmd == "flush-sheet":
        print("sheet outbox empty" if SheetLogger().flush(load_state()) else "some rows still queued")
        return

    bot = Bot()
    _install_signal_handlers(bot)
    if a.cmd == "run":
        try:
            bot.run(resume=a.resume)
        except Exception as e:
            print(f"CRITICAL FAILURE:\n{traceback.format_exc()}")
            tg(f"🚨 ETF BOT CRASH 🚨\n{e}\nNo positions are at risk (delivery only). Pending orders are recovered on the next run "
               f"or with `etf_dukan_bot.py recover`.")
            try:
                bot._write_lock(status="crashed", error=str(e))
                if bot.state and bot.state.get("pending") and bot.broker.smart:
                    bot.recover()
            except Exception as e2:
                tg(f"🚨 Recovery after crash failed: {e2}. Check AngelOne order book manually.")
            sys.exit(1)
    elif a.cmd == "plan":
        bot.plan_now()
    elif a.cmd == "refresh-universe":
        bot.state = load_state()
        meta = load_universe_meta()
        if not (a.force or a.dry_run) and not UR.is_due(bot.today, meta.get("last_refresh"), UNIVERSE_MONTHS):
            print(f"Not due: last refresh {meta.get('last_refresh')}, next {UR.next_due(bot.today, UNIVERSE_MONTHS)}. "
                  f"Use --force or --dry-run.")
            return
        bot.broker.login()
        try:
            bot.refresh_universe(force=a.force, dry_run=a.dry_run)
        finally:
            bot.broker.logout()
    elif a.cmd in ("reconcile", "recover"):
        bot.state = load_state()
        bot.broker.login()
        bot.broker.load_tokens()
        try:
            if a.cmd == "recover":
                bot.recover()
            else:
                q, raw = bot.broker.broker_quantities() if not PAPER_MODE else ({}, [])
                print("\n".join(raw))
                print("\n".join(bot.reconcile(report_only=True)))
        finally:
            bot.broker.logout()


def _install_signal_handlers(bot: Bot):
    def handler(signum, frame):
        bot.stop_requested = True
        print(f"Signal {signum}: finishing the current order step, then stopping.")
        if not bot.in_order:
            raise SystemExit(0)
    for sg in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sg, handler)
        except Exception:
            pass


if __name__ == "__main__":
    main()
