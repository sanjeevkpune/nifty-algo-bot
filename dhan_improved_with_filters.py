#!/usr/bin/env python3
"""
lemonn_dual_session_final_dhan_v3.py  (v3.0)
NIFTY + SENSEX dual-session options selling bot for DhanHQ v2.

Mirrors Tradetron strategy "Lemonn Dual Session NIFTY+SENSEX Final"
(template 999489342, backtest BID 999403058261120), the bot's original split:

  * Mon / Tue / Fri -> NIFTY  (NSE_FNO, strike step 50,  ORB limit 140 pts)
    Wed / Thu       -> SENSEX (BSE_FNO, strike step 100, ORB limit 450 pts)
    Nearest weekly expiry, 5 lots MIS (lot size read from Dhan's scrip master).
  * Day skipped if the index opens more than 0.50% away from the previous close.
  * MORNING 09:50-10:00: only if the 09:15-09:50 range is within the ORB limit.
    Premium-matched OTM1-OTM4 short strangle (default OTM2).
    50% stop per leg; once one leg is stopped, the survivor's stop moves to
    entry + 10%. Time exit 14:15 (NIFTY) / 13:30 (SENSEX).
  * AFTERNOON 14:16-14:30 (NIFTY) / 13:35-13:45 (SENSEX): skipped if both
    morning legs were stopped or the morning realised P&L is <= -12,000.
    Synthetic-future ATM short straddle (spot + CE_atm - PE_atm), same stops.
    Time exit 15:28.

What changed from your v2.1 file (the trading rules above are unchanged):
  1. Fill-or-flag order execution. v2.1 returned the reference price even when
     an order was rejected or never filled, so the bot could "think" it held a
     position it did not have. v3 polls the real order status, steps the limit
     price twice, then converts to MARKET, and returns the actual average fill
     and filled quantity, or None.
  2. Leg-risk unwind. If the CE sells but the PE fails, the CE is bought back
     immediately, so a single naked leg is never left open.
  3. Exchange-side stop-loss orders. Every short leg gets a resting
     STOP_LOSS (SL-limit) BUY order at the exchange, so the stop works even if
     the VM, network or script dies. The monitor loop watches those orders,
     chases a stop that gaps through its limit, and modifies the survivor's
     stop to cost + 10%. Paper mode keeps the software stop.
  4. Crash-safe state. Legs, phase and realised P&L are saved to a JSON file
     after every change. On restart the bot reloads the file, reconciles it
     with Dhan's positions and resumes monitoring instead of forgetting
     the open trades.
  5. Entry windows enforced (as in the backtest), so a late restart never
     opens a fresh trade at a random time.
  6. Scrip-master filter tightened: only the index's own options near the
     current spot are used, so SENSEX50 / NIFTYNXT50 contracts can never be
     picked by mistake, and the expiry chosen is the index's own nearest one.
  7. Catastrophe guard (DAILY_HARD_STOP, default -45,000 total MTM): closes
     everything and stops for the day. It is set beyond the worst backtested
     day (about -31k), so it should not change normal results; it exists for
     broker or data failures.
  8. Holiday check uses the right exchange calendar for the day's index,
     time exits cancel the resting stop first and handle the race where it
     fills during cancellation.
  9. Optional protection wings (HEDGE_ENABLED=1, NIFTY +/-500, SENSEX +/-1500).
     OFF by default: its Tradetron backtest (BID 999403718261120) made less
     than half the profit with a deeper drawdown, so it is not recommended.
"""
import sys
import os
import json
import time
import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import pyotp
from dhanhq import DhanContext, dhanhq

try:
    import gspread
except Exception:  # sheet logging is optional
    gspread = None

# ==============================================================
# CONFIG
# ==============================================================
CONFIG_ENV_PATH = "/etc/trade-config/config.env"
if os.path.exists(CONFIG_ENV_PATH):
    with open(CONFIG_ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


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
STATE_DIR = os.environ.get("LEMONN_STATE_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))

# Paper unless DHAN_PAPER_MODE is exactly 0. Set it with the "Bot settings (paper/live, lots)" workflow.
# (A missing value means paper, so a broken config.env can never switch this bot live.)
PAPER_MODE = os.environ.get("DHAN_PAPER_MODE", "1").strip() != "0"

NUM_LOTS = int(os.environ.get("DHAN_NUM_LOTS", "5"))
PAPER_SLIPPAGE_PTS = 0.25

# --- Strategy rules (match the Tradetron v4 backtest) ---
MAX_GAP_PCT = 0.50
SL_MULT = 1.50                 # 50% stop on each short leg
SL_COST_BUFFER = 1.10          # survivor stop -> entry + 10%
MORNING_MAX_LOSS_CB = -12000.0

# Per-index profile, chosen by weekday (Mon/Tue/Fri NIFTY, Wed/Thu SENSEX)
PROFILES = {
    "NIFTY": dict(exch_id="NSE", fno_seg="NSE_FNO", spot_token="13", step=50, orb=140.0,
                  m_exit=(14, 15), a_entry=(14, 16), a_last=(14, 30), hedge=500, calendar="NSE"),
    "SENSEX": dict(exch_id="BSE", fno_seg="BSE_FNO", spot_token="51", step=100, orb=450.0,
                   m_exit=(13, 30), a_entry=(13, 35), a_last=(13, 45), hedge=1500, calendar="BSE"),
}
_WD = now_ist().weekday()
INDEX = "SENSEX" if _WD in (2, 3) else "NIFTY"
# Testing only: LEMONN_FORCE_INDEX=NIFTY|SENSEX overrides the weekday choice
if os.environ.get("LEMONN_FORCE_INDEX", "").upper() in PROFILES:
    INDEX = os.environ["LEMONN_FORCE_INDEX"].upper()
P = PROFILES[INDEX]
STRIKE_STEP = P["step"]
MAX_ORB_PTS = P["orb"]
M_ENTRY, M_ENTRY_LAST, M_EXIT = (9, 50), (10, 0), P["m_exit"]
A_ENTRY, A_ENTRY_LAST, A_EXIT = P["a_entry"], P["a_last"], (15, 28)

# --- Risk / execution extras (do not change the rules) ---
HEDGE_ENABLED = env_bool("HEDGE_ENABLED", "0")
HEDGE_WIDTH_PTS = int(os.environ.get(f"HEDGE_WIDTH_PTS_{INDEX}", str(P["hedge"])))
DAILY_HARD_STOP = float(os.environ.get("DAILY_HARD_STOP", "-45000"))
SL_LIMIT_BUFFER_PCT = float(os.environ.get("SL_LIMIT_BUFFER_PCT", "0.05"))  # SL-limit = trigger + 5%
USE_EXCHANGE_SL = env_bool("USE_EXCHANGE_SL", "1")
MIN_FREE_MARGIN = float(os.environ.get("MIN_FREE_MARGIN", "0"))  # skip a session if free funds are below this (0 = off)

SPOT_EXCH, SPOT_TOKEN = "IDX_I", P["spot_token"]
FNO_SEG = P["fno_seg"]

OPEN_STATUSES = ("PENDING", "TRANSIT", "PART_TRADED", "TRIGGER_PENDING")
DEAD_STATUSES = ("REJECTED", "CANCELLED", "EXPIRED")


BOT_TAG = f"🔷 DHAN v3 · Dual-Session ({INDEX})"
OTHER_BOT_STATE_PREFIXES = ["lemonn_v4_"]  # the other Dhan bot(s) on this account


def tick(px: float) -> float:
    return round(max(0.05, px) * 20) / 20.0


def tg(message: str):
    tag = ("[PAPER] " if PAPER_MODE else "[LIVE] ") + BOT_TAG + "\n"
    print(f"{now_ist():%H:%M:%S} {tag}{message}", flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:   # notify.py: routine messages silent, repeats within 10 min dropped, one retry
            import notify
            notify.send(tag, message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, timeout=5, retry=False)
            return
        except ImportError:
            pass
        except Exception:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{tag}{message}"},
                timeout=5,
            )
        except Exception:
            pass


def wait_until(target):
    while True:
        rem = (target - now_ist()).total_seconds()
        if rem <= 0:
            return
        time.sleep(min(rem, 1.0))


@dataclass
class Leg:
    session: str
    opt_type: str
    strike: int
    symbol: str
    token: str
    side: str            # "SELL" = short premium leg, "BUY" = protection wing
    qty: int
    entry_px: float
    sl_px: float = 0.0   # only for short legs
    is_hedge: bool = False
    active: bool = True
    sl_moved_to_cost: bool = False
    sl_order_id: str = ""
    sl_limit_px: float = 0.0
    exit_px: float = 0.0
    exit_reason: str = ""
    sheet_logged: bool = False   # row already written to the Lemonn_Dual_v3 tab

    def pnl(self, px: float) -> float:
        return (self.entry_px - px) * self.qty if self.side == "SELL" else (px - self.entry_px) * self.qty


class LemonnDualSessionBot:
    def __init__(self):
        self.dhan = None
        self.access_token = None
        self.options_df = None
        self.expiry = None
        self.lot_size = 0
        self.today = now_ist().date()
        self.legs: List[Leg] = []
        self.realized_pnl = 0.0
        self.phase = "init"     # init -> morning_open -> morning_done -> afternoon_open -> done
        self.halted = False
        self._last_feed_call = 0.0
        self._ws = None              # cached Lemonn_Dual_v3 worksheet
        self._sheet_retry_at = 0.0   # after a failed write, wait before trying again
        self.state_path = os.path.join(STATE_DIR, f"lemonn_dual_{self.today:%Y%m%d}.json")

    # ----------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------
    def save_state(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({
                    "date": str(self.today), "phase": self.phase, "halted": self.halted,
                    "realized_pnl": self.realized_pnl, "legs": [asdict(l) for l in self.legs],
                }, fh, indent=1)
            os.replace(tmp, self.state_path)
        except Exception as e:
            print(f"⚠️ State save failed: {e}")

    def load_state(self) -> bool:
        if not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path) as fh:
                st = json.load(fh)
            self.phase = st.get("phase", "init")
            self.halted = st.get("halted", False)
            self.realized_pnl = float(st.get("realized_pnl", 0.0))
            self.legs = [Leg(**d) for d in st.get("legs", [])]
            # A finished day saved by the older code (no sheet_logged flag) was already
            # written at day end - don't write it again.
            if self.phase == "done":
                for d, l in zip(st.get("legs", []), self.legs):
                    if "sheet_logged" not in d:
                        l.sheet_logged = True
            msg = (f"♻️ Resumed state: phase={self.phase}, active legs={sum(l.active for l in self.legs)}, "
                   f"realised ₹{self.realized_pnl:,.0f}")
            if self.phase in ("init", "done"):
                print(msg)   # nothing to resume - no Telegram (the v4 watchdog checks every 10 min)
            else:
                tg(msg)
            return True
        except Exception as e:
            tg(f"⚠️ Could not read state file ({e}). Starting fresh - CHECK BROKER POSITIONS MANUALLY.")
            return False

    # ----------------------------------------------------------
    # Login / master
    # ----------------------------------------------------------
    def _token_cache_path(self) -> str:
        return os.path.join(STATE_DIR, "dhan_token.json")

    def _try_token(self, token: str) -> bool:
        try:
            self.access_token = token
            self.dhan = dhanhq(DhanContext(DHAN_CLIENT_ID, token))
            fl = self.dhan.get_fund_limits()
            return isinstance(fl, dict) and fl.get("status") == "success" and "data" in fl
        except Exception:
            return False

    def login(self):
        if not DHAN_CLIENT_ID or not DHAN_PIN or not DHAN_TOTP_SECRET:
            raise RuntimeError("Missing Dhan API credentials in /etc/trade-config/config.env")
        # 1) Reuse today's token if we already generated one (Dhan tokens last ~24h,
        #    and generating a new one on every restart is slow and rate-limited).
        try:
            with open(self._token_cache_path()) as fh:
                c = json.load(fh)
            if c.get("date") == str(self.today) and self._try_token(c.get("token", "")):
                print("✅ Reused today's Dhan access token.")
                return
        except Exception:
            pass
        print(f"🔑 Authenticating Dhan HQ API for {DHAN_CLIENT_ID}...")
        last_code = None
        for attempt in range(1, 6):
            totp = pyotp.TOTP(DHAN_TOTP_SECRET)
            code = totp.now()
            if code == last_code:  # never resend a TOTP that was already tried
                time.sleep(30.5 - (time.time() % 30))
                code = totp.now()
            last_code = code
            url = (f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={DHAN_CLIENT_ID}"
                   f"&pin={DHAN_PIN}&totp={code}")
            try:
                response = requests.post(url, headers={"Accept": "application/json"}, timeout=30)
                body = {}
                try:
                    body = response.json()
                except Exception:
                    pass
                if response.status_code == 200 and body.get("accessToken"):
                    if self._try_token(body["accessToken"]):
                        try:
                            os.makedirs(STATE_DIR, exist_ok=True)
                            path = self._token_cache_path()
                            with open(path, "w") as fh:
                                json.dump({"date": str(self.today), "token": body["accessToken"]}, fh)
                            os.chmod(path, 0o600)
                        except Exception as e:
                            print(f"⚠️ Could not cache token: {e}")
                        print("✅ Dhan SDK Initialized Successfully.")
                        return
                # Show Dhan's reason (never the token itself)
                reason = {k: v for k, v in body.items() if "token" not in k.lower()} if body else response.text[:200]
                print(f"⚠️ Attempt {attempt} rejected - HTTP {response.status_code}: {reason}")
            except Exception as e:
                print(f"⚠️ Attempt {attempt} error: {e}")
            if attempt < 5:
                # wait for the next TOTP window instead of a fixed 10 s
                time.sleep(max(5.0, 30.5 - (time.time() % 30)))
        raise RuntimeError("Dhan authentication failed after 5 attempts.")

    def load_master(self):
        print("📥 Fetching latest Dhan master CSV...")
        df = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master.csv", low_memory=False)
        if "SEM_EXM_EXCH_ID" in df.columns:
            df = df[df["SEM_EXM_EXCH_ID"].astype(str).str.upper().eq(P["exch_id"])]
        if "SEM_INSTRUMENT_NAME" in df.columns:
            df = df[df["SEM_INSTRUMENT_NAME"].astype(str).str.upper().eq("OPTIDX")]
        # "NIFTY-Nov2026-24000-CE" style symbols; excludes NIFTYNXT50 / SENSEX50 etc.
        df = df[df["SEM_TRADING_SYMBOL"].str.startswith(INDEX + "-", na=False)].copy()
        df = df[df["SEM_TRADING_SYMBOL"].str.contains(r"(?:CE|PE)$", regex=True, na=False)].copy()
        if df.empty:
            raise RuntimeError(f"Scrip master contains no {INDEX} CE/PE options.")
        df["strike"] = pd.to_numeric(df["SEM_STRIKE_PRICE"], errors="coerce")
        # Sanity: keep strikes within 15% of the current/previous spot
        ref = self.get_spot() or float(self.spot_ohlc().get("close", 0) or 0)
        if ref > 0:
            df = df[(df["strike"] > ref * 0.85) & (df["strike"] < ref * 1.15)]
        df["expirydate"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        lot_col = next((c for c in ["SEM_LOT_UNITS", "SEM_LOT_SIZE", "SEM_LOT_REGULAR", "LOT_SIZE"] if c in df.columns), None)
        df["lotsize"] = pd.to_numeric(df[lot_col], errors="coerce") if lot_col else 0
        valid = df[df["expirydate"] >= self.today].copy()
        if valid.empty:
            raise RuntimeError(f"No {INDEX} expiry on/after {self.today} in the scrip master.")
        self.options_df = valid
        self.expiry = sorted(valid["expirydate"].dropna().unique())[0]
        lots = valid[valid["expirydate"] == self.expiry]["lotsize"].dropna()
        self.lot_size = int(lots.mode().iloc[0]) if not lots.empty else 0
        if self.lot_size <= 0:
            raise RuntimeError(f"Could not read {INDEX} lot size from the scrip master.")
        print(f"✅ Master loaded. Expiry {self.expiry}, lot size {self.lot_size}")

    def find_contract(self, strike: int, opt_type: str) -> Optional[Dict]:
        m = self.options_df[
            (self.options_df["expirydate"] == self.expiry)
            & (self.options_df["strike"] == float(strike))
            & (self.options_df["SEM_OPTION_TYPE"] == opt_type)
        ]
        if m.empty:
            return None
        return {"symbol": str(m.iloc[0]["SEM_TRADING_SYMBOL"]),
                "token": str(m.iloc[0]["SEM_SMST_SECURITY_ID"]),
                "strike": int(strike)}

    # ----------------------------------------------------------
    # Market data (batched, throttled to < 1 req/sec)
    # ----------------------------------------------------------
    def _feed(self, endpoint: str, payload: dict) -> dict:
        for _ in range(3):
            wait = 1.15 - (time.time() - self._last_feed_call)
            if wait > 0:
                time.sleep(wait)
            self._last_feed_call = time.time()
            try:
                r = requests.post(
                    f"https://api.dhan.co/v2/marketfeed/{endpoint}",
                    headers={"access-token": self.access_token, "client-id": DHAN_CLIENT_ID,
                             "Content-Type": "application/json", "Accept": "application/json"},
                    json=payload, timeout=10)
                if r.status_code == 200 and r.json().get("status") == "success":
                    return r.json().get("data", {})
                print(f"⚠️ marketfeed/{endpoint} HTTP {r.status_code}")
            except Exception as e:
                print(f"⚠️ marketfeed/{endpoint} error: {e}")
        return {}

    def ltp_batch(self, exch: str, tokens: List[str]) -> Dict[str, float]:
        if not tokens:
            return {}
        data = self._feed("ltp", {exch: sorted({int(t) for t in tokens})}).get(exch, {})
        return {str(k): float(v["last_price"]) for k, v in data.items()
                if isinstance(v, dict) and v.get("last_price") is not None}

    def get_spot(self) -> float:
        return self.ltp_batch(SPOT_EXCH, [SPOT_TOKEN]).get(SPOT_TOKEN, 0.0)

    def spot_ohlc(self) -> dict:
        d = self._feed("ohlc", {SPOT_EXCH: [int(SPOT_TOKEN)]})
        return d.get(SPOT_EXCH, {}).get(SPOT_TOKEN, {}).get("ohlc", {}) or {}

    # ----------------------------------------------------------
    # Filters
    # ----------------------------------------------------------
    def gap_too_big(self) -> bool:
        ohlc = self.spot_ohlc()
        o, pc = float(ohlc.get("open", 0) or 0), float(ohlc.get("close", 0) or 0)
        if o > 0 and pc > 0:
            gap = abs(o - pc) / pc * 100.0
            if gap > MAX_GAP_PCT:
                tg(f"🛑 Gap filter: {INDEX} opened {gap:.2f}% from previous close (> {MAX_GAP_PCT}%). No trades today.")
                return True
            print(f"✅ Gap filter passed: {gap:.2f}%")   # log only: the deploy / skip message follows
            return False
        tg("⚠️ Gap check could not read OHLC - proceeding (same as v2.1).")
        return False

    def orb_too_wide(self) -> bool:
        orb_end = at_ist(self.today, *M_ENTRY)
        ohlc = self.spot_ohlc()
        hi = float(ohlc.get("high", 0) or 0)
        lo = float(ohlc.get("low", 0) or 0) or float("inf")
        if now_ist() < orb_end:
            print(f"🔍 Watching {INDEX} 09:15-09:50 range (limit {MAX_ORB_PTS:.0f} pts)...")
        while now_ist() < orb_end:
            s = self.get_spot()
            if s > 0:
                hi, lo = max(hi, s), min(lo, s)
                if hi - lo > MAX_ORB_PTS:
                    tg(f"🛑 ORB filter: range {hi - lo:.1f} pts > {MAX_ORB_PTS:.0f}. Skipping morning session.")
                    return True
            time.sleep(10)
        if hi > 0 and lo < float("inf"):
            if hi - lo > MAX_ORB_PTS:
                tg(f"🛑 ORB filter: range {hi - lo:.1f} pts > {MAX_ORB_PTS:.0f}. Skipping morning session.")
                return True
            print(f"✅ Opening range OK: {hi - lo:.1f} pts")
        return False

    # ----------------------------------------------------------
    # Strike selection (unchanged logic)
    # ----------------------------------------------------------
    def select_strikes(self, spot: float, session: str) -> Tuple[int, int]:
        atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        if session == "MORNING_OTM2":
            ce_c = {atm + i * STRIKE_STEP: self.find_contract(atm + i * STRIKE_STEP, "CE") for i in range(1, 5)}
            pe_c = {atm - i * STRIKE_STEP: self.find_contract(atm - i * STRIKE_STEP, "PE") for i in range(1, 5)}
            d_ce, d_pe = atm + 2 * STRIKE_STEP, atm - 2 * STRIKE_STEP
            if not ce_c.get(d_ce) or not pe_c.get(d_pe):
                return d_ce, d_pe
            toks = [c["token"] for c in list(ce_c.values()) + list(pe_c.values()) if c]
            ltp = self.ltp_batch(FNO_SEG, toks)
            target = (ltp.get(ce_c[d_ce]["token"], 0) + ltp.get(pe_c[d_pe]["token"], 0)) / 2.0
            if target <= 0:
                return d_ce, d_pe
            best, best_score = (d_ce, d_pe), float("inf")
            for cs, cc in ce_c.items():
                for ps, pc in pe_c.items():
                    if not cc or not pc:
                        continue
                    c_px, p_px = ltp.get(cc["token"], 0.0), ltp.get(pc["token"], 0.0)
                    if c_px > 5.0 and p_px > 5.0:
                        score = abs(c_px - p_px) + 0.5 * abs((c_px + p_px) / 2.0 - target)
                        if score < best_score:
                            best, best_score = (cs, ps), score
            return best
        ce, pe = self.find_contract(atm, "CE"), self.find_contract(atm, "PE")
        if ce and pe:
            ltp = self.ltp_batch(FNO_SEG, [ce["token"], pe["token"]])
            c_px, p_px = ltp.get(ce["token"], 0.0), ltp.get(pe["token"], 0.0)
            if c_px > 0 and p_px > 0:
                k = int(round((spot + c_px - p_px) / STRIKE_STEP) * STRIKE_STEP)
                return k, k
        return atm, atm

    # ----------------------------------------------------------
    # Orders
    # ----------------------------------------------------------
    @staticmethod
    def _data(res):
        if not isinstance(res, dict) or res.get("status") != "success":
            return None
        d = res.get("data")
        if isinstance(d, list):
            return d[0] if d else None
        return d

    def order_status(self, oid: str) -> dict:
        try:
            d = self._data(self.dhan.get_order_by_id(oid)) or {}
        except Exception as e:
            print(f"⚠️ get_order_by_id({oid}) error: {e}")
            d = {}
        return {
            "status": str(d.get("orderStatus", "")).upper(),
            "avg": float(d.get("averageTradedPrice") or d.get("averagePrice") or 0) or 0.0,
            "filled": int(d.get("filledQty") or 0),
            "price": float(d.get("price") or 0),
        }

    def _modify(self, oid: str, order_type: str, qty: int, price: float = 0.0, trigger: float = 0.0) -> bool:
        try:
            res = self.dhan.modify_order(order_id=oid, order_type=order_type, leg_name="ENTRY_LEG",
                                         quantity=int(qty), price=price, trigger_price=trigger,
                                         disclosed_quantity=0, validity="DAY")
            return isinstance(res, dict) and res.get("status") == "success"
        except Exception as e:
            print(f"⚠️ modify_order({oid}) error: {e}")
            return False

    def _cancel(self, oid: str):
        try:
            self.dhan.cancel_order(oid)
        except Exception as e:
            print(f"⚠️ cancel_order({oid}) error: {e}")

    def execute_order(self, symbol: str, token: str, side: str, qty: int, ref_px: float) -> Optional[Tuple[float, int]]:
        """Returns (average fill price, filled qty) or None if nothing filled."""
        if ref_px <= 0:
            ref_px = self.ltp_batch(FNO_SEG, [token]).get(str(token), 0.0)
        if PAPER_MODE:
            if ref_px <= 0:
                return None
            px = ref_px + PAPER_SLIPPAGE_PTS if side == "BUY" else max(0.05, ref_px - PAPER_SLIPPAGE_PTS)
            return round(px, 2), qty

        sgn = 1 if side == "BUY" else -1
        try:
            res = self.dhan.place_order(
                security_id=str(token), exchange_segment=FNO_SEG,
                transaction_type=self.dhan.BUY if side == "BUY" else self.dhan.SELL,
                quantity=int(qty), order_type=self.dhan.LIMIT, product_type=self.dhan.INTRA,
                price=tick(ref_px + 0.25 if side == "BUY" else ref_px - 0.15))
        except Exception as e:
            tg(f"⚠️ place_order failed for {symbol} {side}: {e}")
            return None
        d = self._data(res)
        if not d or not d.get("orderId"):
            tg(f"⚠️ Order rejected for {symbol} {side}: {res}")
            return None
        oid = str(d["orderId"])

        st = {}
        for step in range(12):
            time.sleep(1.0)
            st = self.order_status(oid)
            if st["status"] == "TRADED":
                return (st["avg"] or ref_px), (st["filled"] or qty)
            if st["status"] in DEAD_STATUSES:
                break
            if step == 2:     # step the limit 1% through the reference price
                self._modify(oid, self.dhan.LIMIT, qty, tick(ref_px * (1 + sgn * 0.01)))
            elif step == 5:   # then guarantee the fill
                self._modify(oid, self.dhan.MARKET, qty)
        if st.get("status") in OPEN_STATUSES:
            self._cancel(oid)
            time.sleep(1.0)
            st = self.order_status(oid)
        if st.get("filled", 0) > 0:
            tg(f"⚠️ {symbol} {side}: only {st['filled']}/{qty} filled ({st.get('status')}).")
            return (st["avg"] or ref_px), st["filled"]
        tg(f"❌ {symbol} {side} NOT filled (status {st.get('status')}).")
        return None

    def place_exchange_sl(self, leg: Leg) -> None:
        """Resting SL-limit BUY order for a short leg."""
        if PAPER_MODE or not USE_EXCHANGE_SL:
            return
        trig = tick(leg.sl_px)
        lim = tick(trig * (1 + SL_LIMIT_BUFFER_PCT))
        try:
            res = self.dhan.place_order(
                security_id=str(leg.token), exchange_segment=FNO_SEG, transaction_type=self.dhan.BUY,
                quantity=int(leg.qty), order_type=getattr(self.dhan, "SL", "STOP_LOSS"),
                product_type=self.dhan.INTRA, price=lim, trigger_price=trig)
            d = self._data(res)
            if d and d.get("orderId"):
                leg.sl_order_id, leg.sl_limit_px = str(d["orderId"]), lim
                return
            tg(f"⚠️ Exchange SL rejected for {leg.symbol}: {res}. Software stop will be used.")
        except Exception as e:
            tg(f"⚠️ Exchange SL error for {leg.symbol}: {e}. Software stop will be used.")

    def move_exchange_sl(self, leg: Leg):
        if not leg.sl_order_id:
            return
        trig = tick(leg.sl_px)
        lim = tick(trig * (1 + SL_LIMIT_BUFFER_PCT))
        if self._modify(leg.sl_order_id, getattr(self.dhan, "SL", "STOP_LOSS"), leg.qty, lim, trig):
            leg.sl_limit_px = lim
        else:
            tg(f"⚠️ Could not move exchange SL for {leg.symbol}; software stop at ₹{leg.sl_px:.2f} still active.")

    # ----------------------------------------------------------
    # Sessions
    # ----------------------------------------------------------
    def close_leg(self, leg: Leg, ref_px: float, reason: str) -> bool:
        if not leg.active:
            return True
        # Cancel the resting stop first; if it filled meanwhile, book that fill instead.
        if leg.sl_order_id and not PAPER_MODE:
            self._cancel(leg.sl_order_id)
            time.sleep(0.8)
            st = self.order_status(leg.sl_order_id)
            if st["status"] == "TRADED":
                self._book_exit(leg, st["avg"] or leg.sl_px, "SL_HIT_EXCHANGE")
                return True
            leg.sl_order_id = ""
        side = "BUY" if leg.side == "SELL" else "SELL"
        fill = self.execute_order(leg.symbol, leg.token, side, leg.qty, ref_px)
        if not fill:
            tg(f"🚨 FAILED TO CLOSE {leg.symbol} ({reason}). Close it manually in Dhan now!")
            return False
        self._book_exit(leg, fill[0], reason)
        return True

    def _book_exit(self, leg: Leg, px: float, reason: str):
        leg.exit_px, leg.active, leg.exit_reason = px, False, reason
        pnl = leg.pnl(px)
        self.realized_pnl += pnl
        self.save_state()
        tg(f"🔴 CLOSED {leg.symbol} @ ₹{px:.2f} | {reason} | P&L ₹{pnl:,.0f} | Day ₹{self.realized_pnl:,.0f}")

    def deploy_session(self, session: str, ce_strike: int, pe_strike: int) -> bool:
        ce, pe = self.find_contract(ce_strike, "CE"), self.find_contract(pe_strike, "PE")
        if not ce or not pe:
            tg(f"⚠️ {session} skipped: strikes {ce_strike}/{pe_strike} not in the master.")
            return False
        qty = NUM_LOTS * self.lot_size
        hedges = []
        if HEDGE_ENABLED:
            hce = self.find_contract(ce_strike + HEDGE_WIDTH_PTS, "CE")
            hpe = self.find_contract(pe_strike - HEDGE_WIDTH_PTS, "PE")
            if hce and hpe:
                hedges = [("CE", hce), ("PE", hpe)]
            else:
                tg(f"⚠️ {session}: protection strikes not found - trading unhedged.")

        toks = [ce["token"], pe["token"]] + [c["token"] for _, c in hedges]
        ltp = self.ltp_batch(FNO_SEG, toks)
        if ltp.get(ce["token"], 0) <= 0 or ltp.get(pe["token"], 0) <= 0:
            tg(f"⚠️ {session} aborted: no valid option prices.")
            return False

        if MIN_FREE_MARGIN > 0 and not PAPER_MODE:
            try:
                d = self.dhan.get_fund_limits().get("data") or {}
                free = float(d.get("availabelBalance", d.get("availableBalance", 0)) or 0)
                if free < MIN_FREE_MARGIN:
                    tg(f"🛑 {session} skipped: free margin ₹{free:,.0f} < MIN_FREE_MARGIN ₹{MIN_FREE_MARGIN:,.0f}.")
                    return False
            except Exception as e:
                print(f"⚠️ Margin check failed ({e}); continuing.")

        opened: List[Leg] = []

        def unwind(why: str):
            tg(f"↩️ {session} unwinding ({why}).")
            for l in [l for l in opened if l.side == "SELL"] + [l for l in opened if l.side == "BUY"]:
                self.close_leg(l, ltp.get(l.token, 0.0), "UNWIND")
            self.flush_sheet(force=True)

        # 1) Protection first (buying first lowers the margin needed for the sells)
        for opt, c in hedges:
            f = self.execute_order(c["symbol"], c["token"], "BUY", qty, ltp.get(c["token"], 0.0))
            if not f:
                unwind("protection leg failed")
                return False
            leg = Leg(session, opt, c["strike"], c["symbol"], c["token"], "BUY", f[1], f[0], is_hedge=True)
            self.legs.append(leg); opened.append(leg); self.save_state()

        # 2) Short legs
        for opt, c in (("CE", ce), ("PE", pe)):
            f = self.execute_order(c["symbol"], c["token"], "SELL", qty, ltp.get(c["token"], 0.0))
            if not f:
                unwind(f"{opt} sell failed")
                return False
            leg = Leg(session, opt, c["strike"], c["symbol"], c["token"], "SELL", f[1], f[0],
                      sl_px=round(f[0] * SL_MULT, 1))
            self.legs.append(leg); opened.append(leg); self.save_state()

        for leg in opened:
            if leg.side == "SELL":
                self.place_exchange_sl(leg)
        self.save_state()
        lines = [f"{'BUY ' if l.is_hedge else 'SELL'} {l.symbol} x{l.qty} @ ₹{l.entry_px:.2f}"
                 + ("" if l.is_hedge else f" (SL ₹{l.sl_px:.2f}{', exch' if l.sl_order_id else ''})")
                 for l in opened]
        tg(f"🟢 {session} DEPLOYED ({INDEX} {self.expiry})\n" + "\n".join(lines))
        return True

    # ----------------------------------------------------------
    # Monitor
    # ----------------------------------------------------------
    def _on_stop(self, leg: Leg, px: float, reason: str):
        """Book a stopped short leg and tighten the survivor (once per session)."""
        was_cost = leg.sl_moved_to_cost
        self._book_exit(leg, px, reason)
        if was_cost:
            return
        for other in [l for l in self.legs if l.active and l.session == leg.session and l.side == "SELL"]:
            new_sl = round(other.entry_px * SL_COST_BUFFER, 1)
            if new_sl < other.sl_px:
                other.sl_px, other.sl_moved_to_cost = new_sl, True
                self.move_exchange_sl(other)
                self.save_state()
                tg(f"🛡️ {other.symbol} stop tightened to cost + 10% (₹{other.sl_px:.2f})")

    def mtm(self, ltp: Dict[str, float]) -> float:
        return self.realized_pnl + sum(l.pnl(ltp.get(l.token, l.entry_px)) for l in self.legs if l.active)

    def close_all(self, ltp: Dict[str, float], reason: str):
        for l in [l for l in self.legs if l.active and l.side == "SELL"] + \
                 [l for l in self.legs if l.active and l.side == "BUY"]:
            self.close_leg(l, ltp.get(l.token, 0.0), reason)

    def monitor_until(self, exit_time: datetime.datetime):
        gap_through = {}
        while now_ist() < exit_time and not self.halted:
            self.flush_sheet()   # legs closed in the last loop (after the survivor's stop was moved)
            active = [l for l in self.legs if l.active]
            if not any(l.side == "SELL" for l in active):
                break
            ltp = self.ltp_batch(FNO_SEG, [l.token for l in active])

            # Catastrophe guard on total MTM
            if ltp and self.mtm(ltp) <= DAILY_HARD_STOP:
                tg(f"🚨 HARD STOP: day MTM ₹{self.mtm(ltp):,.0f} <= ₹{DAILY_HARD_STOP:,.0f}. Closing everything.")
                self.close_all(ltp, "HARD_STOP")
                self.halted = True
                self.save_state()
                break

            orders = {}
            if not PAPER_MODE and any(l.sl_order_id for l in active):
                try:
                    ob = self.dhan.get_order_list()
                    if isinstance(ob, dict) and ob.get("status") == "success":
                        orders = {str(o.get("orderId")): o for o in ob.get("data", []) or []}
                except Exception as e:
                    print(f"⚠️ order list error: {e}")

            for leg in [l for l in active if l.side == "SELL" and l.active]:
                px = ltp.get(str(leg.token), 0.0)
                reason = "COST_SL_HIT" if leg.sl_moved_to_cost else "50%_SL_HIT"
                if leg.sl_order_id:
                    o = orders.get(leg.sl_order_id)
                    status = str(o.get("orderStatus", "")).upper() if o else ""
                    if status == "TRADED":
                        fill = float(o.get("averageTradedPrice") or o.get("averagePrice") or leg.sl_px)
                        leg.sl_order_id = ""
                        self._on_stop(leg, fill, reason)
                        continue
                    if status in DEAD_STATUSES:
                        tg(f"⚠️ Exchange SL for {leg.symbol} is {status}; switching to software stop.")
                        leg.sl_order_id = ""
                    elif px > 0 and px > leg.sl_limit_px:
                        # Price gapped through the SL limit: chase with MARKET after 2 checks
                        gap_through[leg.token] = gap_through.get(leg.token, 0) + 1
                        if gap_through[leg.token] >= 2:
                            tg(f"⚡ {leg.symbol} gapped through SL limit (LTP ₹{px:.2f}); converting stop to MARKET.")
                            self._modify(leg.sl_order_id, self.dhan.MARKET, leg.qty)
                            gap_through[leg.token] = -10
                        continue
                    else:
                        continue
                # Software stop (paper mode, or exchange SL unavailable)
                if px > 0 and px >= leg.sl_px:
                    fill = self.execute_order(leg.symbol, leg.token, "BUY", leg.qty, px)
                    if fill:
                        self._on_stop(leg, fill[0], reason)
                    else:
                        tg(f"🚨 Stop on {leg.symbol} could not be executed - retrying next loop.")
            time.sleep(1.5)

        if not self.halted:
            rem = [l for l in self.legs if l.active]
            if rem:
                ltp = self.ltp_batch(FNO_SEG, [l.token for l in rem])
                self.close_all(ltp, "TIME_EXIT")
        self.flush_sheet(force=True)

    # ----------------------------------------------------------
    # Reconciliation after restart
    # ----------------------------------------------------------
    def _other_bots_expected(self) -> Dict[str, int]:
        """Net quantity per securityId that the OTHER Dhan bot expects to hold today.
        Both bots trade on the same Dhan account and can hold the same NIFTY option,
        so broker positions are the SUM of both bots' legs. Read from the other bot's
        state file (same STATE_DIR, written atomically)."""
        out: Dict[str, int] = {}
        for prefix in OTHER_BOT_STATE_PREFIXES:
            path = os.path.join(STATE_DIR, f"{prefix}{self.today:%Y%m%d}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path) as fh:
                    st = json.load(fh)
                for d in st.get("legs", []) or []:
                    if not d.get("active"):
                        continue
                    q = int(d.get("qty") or 0)
                    tok = str(d.get("token"))
                    out[tok] = out.get(tok, 0) + (-q if d.get("side") == "SELL" else q)
            except Exception as e:
                tg(f"⚠️ Could not read the other bot's state ({os.path.basename(path)}): {e}. "
                   f"Position check may be off - verify in Dhan.")
        return out

    def reconcile_positions(self):
        if PAPER_MODE or not any(l.active for l in self.legs):
            return
        try:
            res = self.dhan.get_positions()
            rows = res.get("data", []) if isinstance(res, dict) else []
        except Exception as e:
            tg(f"⚠️ Could not read positions for reconciliation: {e}")
            return
        net = {}
        for r in rows or []:
            net[str(r.get("securityId"))] = net.get(str(r.get("securityId")), 0) + int(r.get("netQty") or 0)
        # Remove what the other Dhan bot holds, so only THIS bot's share is compared.
        others = self._other_bots_expected()
        mine: Dict[str, int] = {}
        for l in self.legs:
            if l.active:
                mine[str(l.token)] = mine.get(str(l.token), 0) + (-l.qty if l.side == "SELL" else l.qty)
        for tok, own in mine.items():
            legs = [l for l in self.legs if l.active and str(l.token) == tok]
            have = net.get(tok, 0) - others.get(tok, 0)
            if have == own:
                continue
            if have == 0:
                for leg in legs:
                    if leg.sl_order_id:
                        st = self.order_status(leg.sl_order_id)
                        if st["status"] == "TRADED":
                            self._book_exit(leg, st["avg"] or leg.sl_px, "SL_HIT_EXCHANGE (while offline)")
                            continue
                    px = self.ltp_batch(FNO_SEG, [leg.token]).get(str(leg.token), leg.entry_px)
                    tg(f"⚠️ {leg.symbol} is flat at the broker (after the other bot's "
                       f"{others.get(tok, 0)}); marking closed at LTP ₹{px:.2f} (check fills).")
                    self._book_exit(leg, px, "FLAT_AT_BROKER")
            else:
                tg(f"⚠️ {legs[0].symbol}: broker net {net.get(tok, 0)}, other Dhan bot holds {others.get(tok, 0)}, "
                   f"so this bot has {have} but expected {own}. Please check manually.")

    # ----------------------------------------------------------
    # Logging
    # ----------------------------------------------------------
    def _sheet(self):
        if self._ws is None:
            sh = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2)
            try:
                self._ws = sh.worksheet("Lemonn_Dual_v3")
            except Exception:
                self._ws = sh.add_worksheet(title="Lemonn_Dual_v3", rows="2000", cols="12")
                self._ws.append_row(["Date", "Index", "Session", "Leg", "Side", "Symbol", "Qty", "Entry", "Exit",
                                     "Reason", "Leg_PnL", "Paper"])
        return self._ws

    def _row(self, l: Leg) -> list:
        return [str(self.today), INDEX, l.session, l.opt_type + (" hedge" if l.is_hedge else ""),
                l.side, l.symbol, l.qty, l.entry_px, l.exit_px, l.exit_reason,
                round(l.pnl(l.exit_px), 2) if not l.active else "", PAPER_MODE]

    def flush_sheet(self, force: bool = False, include_open: bool = False):
        """Write each leg to the sheet as soon as it closes (one row per leg, same
        columns as before). The sheet_logged flag is saved in the state file, so a
        restart or the end-of-day pass never writes a leg twice. A failed write is
        retried on a later loop (60 s back-off) and again at day end."""
        if not gspread or not GSHEET_ID_V2 or not os.path.exists(SERVICE_ACCOUNT_FILE):
            return
        todo = [l for l in self.legs if not l.sheet_logged and (include_open or not l.active)]
        if not todo or (not force and time.time() < self._sheet_retry_at):
            return
        try:
            self._sheet().append_rows([self._row(l) for l in todo])
            for l in todo:
                l.sheet_logged = True
            self.save_state()
        except Exception as e:
            self._ws = None
            self._sheet_retry_at = time.time() + 60
            print(f"⚠️ Sheet logging failed (will retry): {e}")

    def log_sheet(self):
        """Day end: write only what is still missing (normally nothing)."""
        self.flush_sheet(force=True, include_open=True)

    # ----------------------------------------------------------
    # Main flow
    # ----------------------------------------------------------
    def _in_window(self, first, last) -> bool:
        return at_ist(self.today, *first) <= now_ist() <= at_ist(self.today, *last)

    def run(self):
        if self.today.weekday() >= 5:
            return print("Weekend - nothing to trade.")
        try:
            import pandas_market_calendars as mcal
            if mcal.get_calendar(P["calendar"]).schedule(start_date=str(self.today), end_date=str(self.today)).empty:
                return tg("📅 NSE holiday. Sleeping.")
        except Exception:
            pass

        self.load_state()
        if self.phase == "done":
            self.log_sheet()   # writes only legs a crash kept out of the sheet
            return print("Today's sessions are already complete.")
        self.login()
        self.load_master()
        tg(f"🚀 LEMONN DUAL-SESSION v3.0 | {INDEX} | {NUM_LOTS} lots x {self.lot_size} | "
           f"hedge {'ON ' + str(HEDGE_WIDTH_PTS) + 'pts' if HEDGE_ENABLED else 'OFF'} | "
           f"exch SL {'ON' if USE_EXCHANGE_SL and not PAPER_MODE else 'OFF'}")
        self.reconcile_positions()

        # ---------------- MORNING ----------------
        if self.phase == "init":
            wait_until(at_ist(self.today, 9, 15))
            if self.gap_too_big():
                self.phase = "done"; self.save_state(); return
            if not self.orb_too_wide():
                wait_until(at_ist(self.today, *M_ENTRY))
                if self._in_window(M_ENTRY, M_ENTRY_LAST):
                    spot = self.get_spot()
                    if spot > 0:
                        ce_k, pe_k = self.select_strikes(spot, "MORNING_OTM2")
                        if self.deploy_session("MORNING_OTM2", ce_k, pe_k):
                            self.phase = "morning_open"; self.save_state()
                    else:
                        tg("⚠️ No spot price for the morning session.")
                else:
                    tg("⏰ Started after the 09:50-10:00 entry window - morning session skipped.")
            if self.phase == "init":
                self.phase = "morning_done"; self.save_state()

        if self.phase == "morning_open":
            self.monitor_until(at_ist(self.today, *M_EXIT))
            self.phase = "done" if self.halted else "morning_done"
            self.save_state()

        # ---------------- AFTERNOON ----------------
        if self.phase == "morning_done":
            m_sl = sum(1 for l in self.legs if l.session == "MORNING_OTM2" and l.side == "SELL"
                       and l.exit_reason.startswith(("50%_SL", "COST_SL", "SL_HIT")))
            if m_sl >= 2 or self.realized_pnl <= MORNING_MAX_LOSS_CB:
                tg(f"🛑 Chop breaker: {m_sl} morning stops, realised ₹{self.realized_pnl:,.0f}. Afternoon skipped.")
                self.phase = "done"; self.save_state()
            else:
                wait_until(at_ist(self.today, *A_ENTRY))
                if self._in_window(A_ENTRY, A_ENTRY_LAST):
                    spot = self.get_spot()
                    if spot > 0:
                        k, _ = self.select_strikes(spot, "AFTERNOON_ATM")
                        if self.deploy_session("AFTERNOON_ATM", k, k):
                            self.phase = "afternoon_open"; self.save_state()
                    else:
                        tg("⚠️ No spot price for the afternoon session.")
                else:
                    tg(f"⏰ Past the {A_ENTRY[0]}:{A_ENTRY[1]:02d}-{A_ENTRY_LAST[0]}:{A_ENTRY_LAST[1]:02d} entry window - afternoon session skipped.")
                if self.phase == "morning_done":
                    self.phase = "done"; self.save_state()

        if self.phase == "afternoon_open":
            self.monitor_until(at_ist(self.today, *A_EXIT))
            self.phase = "done"; self.save_state()

        self.log_sheet()
        tg(f"🏁 DAY COMPLETE | Realised P&L (before charges): ₹{self.realized_pnl:,.0f}")


if __name__ == "__main__":
    bot = None
    try:
        bot = LemonnDualSessionBot()
        bot.run()
    except Exception as e:
        open_legs = [l.symbol for l in (bot.legs if bot else []) if l.active]
        tg(f"🚨 FATAL BOT CRASH 🚨\n{e}\nOpen legs: {open_legs or 'none'}. "
           f"Exchange stops (if live) stay in place; restart the bot to resume.")
        sys.exit(1)
