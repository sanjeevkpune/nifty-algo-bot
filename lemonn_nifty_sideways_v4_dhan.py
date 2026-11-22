#!/usr/bin/env python3
"""
lemonn_nifty_sideways_v4_dhan.py  (v1.0)
NIFTY intraday sideways-day ATM short straddle for DhanHQ v2.

Mirrors Tradetron strategy "Nifty Sideways-Day ATM Straddle Seller v4"
(template 999505270, backtest BID 999417618261122):

  * NIFTY weekly options (nearest expiry), 5 lots, intraday (MIS), one trade a day.
  * Entry 10:00-10:05, only if ALL of these hold:
      - opening gap vs previous close <= 0.5%
      - 09:15-09:59 NIFTY range <= 140 points
      - 0 to 3 calendar days to the weekly expiry
      - at least 2 of these 4 "sideways" signs:
          1. India VIX <= previous close + 3%
          2. NIFTY current-month future within 0.25% of its VWAP
          3. ATM straddle premium at 10:00 below the same strike's premium at 09:20
          4. 5-minute ADX(14) of NIFTY below 20
  * Sell the ATM CE + PE (strike = spot rounded to 50).
  * Combined stop: exit both legs when CE + PE premium rises 40% above the
    combined entry premium. Hard exit 15:20.

Same safety framework as lemonn_dual_session_final_dhan_v3.py:
  fill-checked orders (limit -> stepped limit -> market), one-leg unwind,
  crash-safe state with broker reconciliation on restart, daily token cache,
  catastrophe guard (DAILY_HARD_STOP) and paper mode until 31 Dec 2026.

Differences to know:
  * The combined 40% stop needs both legs' prices, so it cannot rest at the
    exchange. The bot checks it every ~2 seconds. As crash protection it also
    places a far "backup" STOP_LOSS order on each leg at BACKUP_SL_MULT x its
    entry price (default 2.5x); if a backup ever fills, the other leg is closed
    at once.
  * Telegram: a morning filter report (each check with its value), entry,
    exit and end-of-day summary.
  * Google Sheet: the same spreadsheet as your v3 bot (GSHEET_ID_V2), two new tabs:
      - "Lemonn_v4_Trades": one row per leg, same columns as Lemonn_Dual_v3
      - "Lemonn_v4_Daily": one row per trading day, traded or skipped, with
        every filter value, so paper results can be compared with the backtest.
  * Paper mode: always on until 31 Dec 2026; after that it stays on unless
    you add DHAN_V4_LIVE=1 to config.env.
"""
import sys
import os
import json
import time
import datetime
from dataclasses import dataclass, asdict
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
TRADES_TAB = os.environ.get("LEMONN_V4_TRADES_TAB", "Lemonn_v4_Trades")   # same columns as Lemonn_Dual_v3
SHEET_TAB = os.environ.get("LEMONN_V4_SHEET_TAB", "Lemonn_v4_Daily")      # one row per day, traded or skipped
STATE_DIR = os.environ.get("LEMONN_STATE_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))

# Paper trading until the end of 2026, then controlled by DHAN_PAPER_MODE
# v4 is a new strategy: it stays in PAPER mode until you deliberately switch it.
# Live trading needs BOTH: the date is after 31 Dec 2026 AND DHAN_V4_LIVE=1 in config.env.
PAPER_MODE_END_DATE = datetime.date(2026, 12, 31)
PAPER_MODE = not (now_ist().date() > PAPER_MODE_END_DATE and env_bool("DHAN_V4_LIVE", "0"))

NUM_LOTS = int(os.environ.get("DHAN_V4_NUM_LOTS", os.environ.get("DHAN_NUM_LOTS", "5")))
PAPER_SLIPPAGE_PTS = 0.25

# --- Strategy rules (match the Tradetron v4 backtest) ---
INDEX = "NIFTY"
STRIKE_STEP = 50
MAX_GAP_PCT = 0.50
MAX_ORB_PTS = 140.0
MAX_DTE = 3                    # calendar days to the weekly expiry
MIN_SCORE = 2                  # of the 4 sideways signs
VIX_MAX_RISE = 1.03            # VIX <= previous close + 3%
VWAP_BAND = 0.0025             # future within 0.25% of VWAP
ADX_PERIOD, ADX_MAX = 14, 20.0
COMBINED_SL = 1.40             # exit when CE+PE >= 1.4 x entry
REF_TIME, REF_LAST = (9, 20), (9, 30)
ENTRY, ENTRY_LAST, EXIT = (10, 0), (10, 5), (15, 20)

# --- Risk / execution extras (do not change the rules) ---
DAILY_HARD_STOP = float(os.environ.get("DAILY_HARD_STOP", "-45000"))
BACKUP_SL_MULT = float(os.environ.get("BACKUP_SL_MULT", "2.5"))
SL_LIMIT_BUFFER_PCT = float(os.environ.get("SL_LIMIT_BUFFER_PCT", "0.05"))
USE_EXCHANGE_SL = env_bool("USE_EXCHANGE_SL", "1")
MIN_FREE_MARGIN = float(os.environ.get("MIN_FREE_MARGIN", "0"))

SPOT_EXCH, SPOT_TOKEN = "IDX_I", "13"          # NIFTY 50 index
VIX_TOKEN = os.environ.get("DHAN_INDIA_VIX_ID", "21")  # India VIX on IDX_I
FNO_SEG = "NSE_FNO"

OPEN_STATUSES = ("PENDING", "TRANSIT", "PART_TRADED", "TRIGGER_PENDING")
DEAD_STATUSES = ("REJECTED", "CANCELLED", "EXPIRED")


def tick(px: float) -> float:
    return round(max(0.05, px) * 20) / 20.0


def tg(message: str):
    tag = "[PAPER] " if PAPER_MODE else ""
    print(f"{now_ist():%H:%M:%S} {tag}{message}", flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
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

    def pnl(self, px: float) -> float:
        return (self.entry_px - px) * self.qty if self.side == "SELL" else (px - self.entry_px) * self.qty


class LemonnV4Bot:
    def __init__(self):
        self.dhan = None
        self.access_token = None
        self.options_df = None
        self.expiry = None
        self.lot_size = 0
        self.fut = None
        self.today = now_ist().date()
        self.legs: List[Leg] = []
        self.realized_pnl = 0.0
        self.phase = "init"     # init -> open -> done
        self.halted = False
        self.info: Dict = {}    # the day's filter values and decision (logged to the sheet)
        self.logged = False
        self._last_feed_call = 0.0
        self.state_path = os.path.join(STATE_DIR, f"lemonn_v4_{self.today:%Y%m%d}.json")

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
                    "info": self.info, "logged": self.logged,
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
            self.info = st.get("info", {}) or {}
            self.logged = bool(st.get("logged", False))
            tg(f"♻️ Resumed state: phase={self.phase}, active legs={sum(l.active for l in self.legs)}, "
               f"realised ₹{self.realized_pnl:,.0f}")
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

    # ----------------------------------------------------------
    # Scrip master: NIFTY weekly options + current-month future
    # ----------------------------------------------------------
    def load_master(self):
        print("📥 Fetching latest Dhan master CSV...")
        raw = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master.csv", low_memory=False)
        if "SEM_EXM_EXCH_ID" in raw.columns:
            raw = raw[raw["SEM_EXM_EXCH_ID"].astype(str).str.upper().eq("NSE")]
        raw = raw[raw["SEM_TRADING_SYMBOL"].astype(str).str.startswith(INDEX + "-", na=False)].copy()
        raw["expirydate"] = pd.to_datetime(raw["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        lot_col = next((c for c in ["SEM_LOT_UNITS", "SEM_LOT_SIZE", "SEM_LOT_REGULAR", "LOT_SIZE"] if c in raw.columns), None)
        raw["lotsize"] = pd.to_numeric(raw[lot_col], errors="coerce") if lot_col else 0
        inst = raw["SEM_INSTRUMENT_NAME"].astype(str).str.upper() if "SEM_INSTRUMENT_NAME" in raw.columns else None

        opt = raw[(inst == "OPTIDX") if inst is not None else raw.index.notna()]
        opt = opt[opt["SEM_TRADING_SYMBOL"].str.contains(r"(?:CE|PE)$", regex=True, na=False)].copy()
        opt["strike"] = pd.to_numeric(opt["SEM_STRIKE_PRICE"], errors="coerce")
        ref = self.get_spot() or float(self.spot_ohlc().get("close", 0) or 0)
        if ref > 0:
            opt = opt[(opt["strike"] > ref * 0.85) & (opt["strike"] < ref * 1.15)]
        opt = opt[opt["expirydate"] >= self.today]
        if opt.empty:
            raise RuntimeError(f"Scrip master has no {INDEX} options on/after {self.today}.")
        self.options_df = opt
        self.expiry = sorted(opt["expirydate"].dropna().unique())[0]
        lots = opt[opt["expirydate"] == self.expiry]["lotsize"].dropna()
        self.lot_size = int(lots.mode().iloc[0]) if not lots.empty else 0
        if self.lot_size <= 0:
            raise RuntimeError("Could not read the NIFTY lot size from the scrip master.")

        if inst is not None:
            fut = raw[(inst == "FUTIDX") & (raw["expirydate"] >= self.today)]
            if not fut.empty:
                f0 = fut.sort_values("expirydate").iloc[0]
                self.fut = {"symbol": str(f0["SEM_TRADING_SYMBOL"]), "token": str(f0["SEM_SMST_SECURITY_ID"])}
        print(f"✅ Master loaded. Expiry {self.expiry}, lot {self.lot_size}, future {self.fut and self.fut['symbol']}")

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

    def quote(self, exch: str, token: str) -> dict:
        d = self._feed("quote", {exch: [int(token)]})
        return d.get(exch, {}).get(str(token), {}) or {}

    def candles(self, security_id: str, seg: str, instrument: str, interval: int,
                start: datetime.datetime, end: datetime.datetime) -> List[dict]:
        """Dhan v2 intraday charts. Returns [{t, o, h, l, c, v}] with t = candle start (IST)."""
        for _ in range(3):
            wait = 1.15 - (time.time() - self._last_feed_call)
            if wait > 0:
                time.sleep(wait)
            self._last_feed_call = time.time()
            try:
                r = requests.post(
                    "https://api.dhan.co/v2/charts/intraday",
                    headers={"access-token": self.access_token, "client-id": DHAN_CLIENT_ID,
                             "Content-Type": "application/json", "Accept": "application/json"},
                    json={"securityId": str(security_id), "exchangeSegment": seg, "instrument": instrument,
                          "interval": str(interval), "oi": False,
                          "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
                          "toDate": end.strftime("%Y-%m-%d %H:%M:%S")},
                    timeout=15)
                if r.status_code == 200:
                    j = r.json()
                    ts = j.get("timestamp") or []
                    out = []
                    for i, t in enumerate(ts):
                        out.append({"t": datetime.datetime.fromtimestamp(float(t), IST),
                                    "o": float(j["open"][i]), "h": float(j["high"][i]),
                                    "l": float(j["low"][i]), "c": float(j["close"][i]),
                                    "v": float((j.get("volume") or [0] * len(ts))[i] or 0)})
                    return out
                print(f"⚠️ charts/intraday HTTP {r.status_code}: {r.text[:150]}")
            except Exception as e:
                print(f"⚠️ charts/intraday error: {e}")
        return []

    @staticmethod
    def adx(h: List[float], l: List[float], c: List[float], n: int = 14) -> Optional[float]:
        """Wilder ADX (same method as TA-Lib)."""
        if len(c) < 2 * n + 1:
            return None
        tr, pdm, mdm = [], [], []
        for i in range(1, len(c)):
            up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
            pdm.append(up if up > dn and up > 0 else 0.0)
            mdm.append(dn if dn > up and dn > 0 else 0.0)
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        atr, p, m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
        dx = []
        for i in range(n, len(tr) + 1):
            if i > n:
                atr = atr - atr / n + tr[i - 1]
                p = p - p / n + pdm[i - 1]
                m = m - m / n + mdm[i - 1]
            pdi = 100 * p / atr if atr else 0.0
            mdi = 100 * m / atr if atr else 0.0
            dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
        if len(dx) < n:
            return None
        a = sum(dx[:n]) / n
        for v in dx[n:]:
            a = (a * (n - 1) + v) / n
        return a

    def straddle_px(self, strike: int) -> Tuple[float, float]:
        ce, pe = self.find_contract(strike, "CE"), self.find_contract(strike, "PE")
        if not ce or not pe:
            return 0.0, 0.0
        ltp = self.ltp_batch(FNO_SEG, [ce["token"], pe["token"]])
        return ltp.get(ce["token"], 0.0), ltp.get(pe["token"], 0.0)

    # ----------------------------------------------------------
    # The 10:00 decision
    # ----------------------------------------------------------
    def check_gap(self) -> bool:
        ohlc = self.spot_ohlc()
        o, pc = float(ohlc.get("open", 0) or 0), float(ohlc.get("close", 0) or 0)
        if o > 0 and pc > 0:
            gap = abs(o - pc) / pc * 100.0
            self.info.update(gap_pct=round(gap, 3), open=o, prev_close=pc)
            return gap <= MAX_GAP_PCT
        self.info.update(gap_pct=None)
        tg("⚠️ Could not read NIFTY open / previous close - gap check treated as passed.")
        return True

    def capture_ref_straddle(self):
        spot = self.get_spot()
        if spot <= 0:
            return
        k = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        c, p = self.straddle_px(k)
        if c > 0 and p > 0:
            self.info.update(ref_strike=k, straddle_920=round(c + p, 2))
            self.save_state()
            print(f"📌 09:20 reference straddle {k}: ₹{c + p:.2f}")

    def evaluate(self) -> bool:
        I = self.info
        d = self.today
        # Opening range 09:15-09:59 from 1-minute candles (fallback: day OHLC)
        bars = self.candles(SPOT_TOKEN, SPOT_EXCH, "INDEX", 1, at_ist(d, 9, 15), at_ist(d, 10, 0))
        bars = [b for b in bars if at_ist(d, 9, 15) <= b["t"] <= at_ist(d, 9, 59)]
        if bars:
            I["orb_pts"] = round(max(b["h"] for b in bars) - min(b["l"] for b in bars), 2)
        else:
            o = self.spot_ohlc()
            I["orb_pts"] = round(float(o.get("high", 0) or 0) - float(o.get("low", 0) or 0), 2)
            I["orb_note"] = "day OHLC fallback"
        I["dte"] = (self.expiry - d).days

        # 1. India VIX vs previous close
        vq = self.ohlc_of(SPOT_EXCH, VIX_TOKEN)
        I["vix"], I["vix_prev"] = vq.get("ltp"), vq.get("prev_close")
        s1 = bool(I["vix"] and I["vix_prev"] and I["vix"] <= I["vix_prev"] * VIX_MAX_RISE)
        # 2. Future within 0.25% of VWAP
        s2 = False
        if self.fut:
            q = self.quote(FNO_SEG, self.fut["token"])
            fl, vw = float(q.get("last_price") or 0), float(q.get("average_price") or 0)
            I["fut"], I["fut_vwap"] = fl or None, vw or None
            if fl and vw:
                I["fut_vwap_pct"] = round((fl - vw) / vw * 100, 3)
                s2 = abs(fl - vw) / vw <= VWAP_BAND
        # 3. Straddle decaying since 09:20 (same strike)
        s3 = False
        if I.get("ref_strike") and I.get("straddle_920"):
            c, p = self.straddle_px(int(I["ref_strike"]))
            if c > 0 and p > 0:
                I["straddle_1000"] = round(c + p, 2)
                s3 = (c + p) < I["straddle_920"]
        # 4. 5-minute ADX(14) of NIFTY, last closed bar
        b5 = self.candles(SPOT_TOKEN, SPOT_EXCH, "INDEX", 5, at_ist(d - datetime.timedelta(days=7), 9, 15),
                          at_ist(d, 10, 0))
        b5 = [b for b in b5 if b["t"] + datetime.timedelta(minutes=5) <= now_ist()]
        a = self.adx([b["h"] for b in b5], [b["l"] for b in b5], [b["c"] for b in b5], ADX_PERIOD) if b5 else None
        I["adx"] = round(a, 2) if a is not None else None
        s4 = a is not None and a < ADX_MAX

        I["score"] = int(s1) + int(s2) + int(s3) + int(s4)
        checks = {
            "gap": I.get("gap_pct") is None or I["gap_pct"] <= MAX_GAP_PCT,
            "orb": I["orb_pts"] <= MAX_ORB_PTS,
            "dte": I["dte"] <= MAX_DTE,
            "score": I["score"] >= MIN_SCORE,
        }
        ok = all(checks.values())
        mk = lambda b: "✅" if b else "❌"
        tg("🧭 NIFTY v4 10:00 check\n"
           f"{mk(checks['gap'])} Gap {I.get('gap_pct')}% (max {MAX_GAP_PCT}%)\n"
           f"{mk(checks['orb'])} 09:15-09:59 range {I['orb_pts']} pts (max {MAX_ORB_PTS:.0f})\n"
           f"{mk(checks['dte'])} {I['dte']} day(s) to expiry {self.expiry} (max {MAX_DTE})\n"
           f"{mk(checks['score'])} Sideways score {I['score']}/4 (need {MIN_SCORE}):\n"
           f"   {mk(s1)} VIX {I.get('vix')} vs prev {I.get('vix_prev')}\n"
           f"   {mk(s2)} Future vs VWAP {I.get('fut_vwap_pct')}%\n"
           f"   {mk(s3)} Straddle {I.get('straddle_1000')} vs 09:20 {I.get('straddle_920')}\n"
           f"   {mk(s4)} ADX(14, 5m) {I.get('adx')}\n"
           f"➡️ {'TRADE' if ok else 'NO TRADE today'}")
        I["decision"] = "TRADE" if ok else "SKIP: " + ", ".join(k for k, v in checks.items() if not v)
        self.save_state()
        return ok

    def ohlc_of(self, exch: str, token: str) -> dict:
        d = self._feed("ohlc", {exch: [int(token)]}).get(exch, {}).get(str(token), {}) or {}
        o = d.get("ohlc", {}) or {}
        return {"ltp": float(d.get("last_price") or 0) or None, "prev_close": float(o.get("close") or 0) or None}

    # ----------------------------------------------------------
    # Trade
    # ----------------------------------------------------------
    def deploy(self) -> bool:
        spot = self.get_spot()
        if spot <= 0:
            tg("⚠️ No NIFTY price at entry - no trade.")
            return False
        k = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        ce, pe = self.find_contract(k, "CE"), self.find_contract(k, "PE")
        if not ce or not pe:
            tg(f"⚠️ ATM {k} contracts not found - no trade.")
            return False
        if MIN_FREE_MARGIN > 0 and not PAPER_MODE:
            try:
                d = self.dhan.get_fund_limits().get("data") or {}
                free = float(d.get("availabelBalance", d.get("availableBalance", 0)) or 0)
                if free < MIN_FREE_MARGIN:
                    tg(f"🛑 Free margin ₹{free:,.0f} < MIN_FREE_MARGIN ₹{MIN_FREE_MARGIN:,.0f} - no trade.")
                    return False
            except Exception as e:
                print(f"⚠️ Margin check failed ({e}); continuing.")
        qty = NUM_LOTS * self.lot_size
        ltp = self.ltp_batch(FNO_SEG, [ce["token"], pe["token"]])
        opened: List[Leg] = []
        for opt, c in (("CE", ce), ("PE", pe)):
            f = self.execute_order(c["symbol"], c["token"], "SELL", qty, ltp.get(c["token"], 0.0))
            if not f:
                tg(f"↩️ {opt} sell failed - unwinding.")
                for l in opened:
                    self.close_leg(l, ltp.get(l.token, 0.0), "UNWIND")
                return False
            leg = Leg("STRADDLE", opt, k, c["symbol"], c["token"], "SELL", f[1], f[0],
                      sl_px=round(f[0] * BACKUP_SL_MULT, 1))
            self.legs.append(leg); opened.append(leg); self.save_state()
        for leg in opened:
            self.place_exchange_sl(leg)
        e = sum(l.entry_px for l in opened)
        self.info.update(strike=k, ce_entry=opened[0].entry_px, pe_entry=opened[1].entry_px,
                         combined_entry=round(e, 2), stop_level=round(e * COMBINED_SL, 2), entry_time=f"{now_ist():%H:%M}")
        self.save_state()
        tg(f"🟢 SOLD NIFTY {k} straddle ({self.expiry}) x{qty}\n"
           f"CE ₹{opened[0].entry_px:.2f} + PE ₹{opened[1].entry_px:.2f} = ₹{e:.2f}\n"
           f"Combined stop ₹{e * COMBINED_SL:.2f} (+40%), exit 15:20"
           + (f"\nBackup exchange stops at {BACKUP_SL_MULT}x each leg" if any(l.sl_order_id for l in opened) else ""))
        return True

    def close_all(self, ltp: Dict[str, float], reason: str):
        for l in [l for l in self.legs if l.active]:
            self.close_leg(l, ltp.get(l.token, 0.0), reason)

    def monitor(self):
        exit_at = at_ist(self.today, *EXIT)
        shorts = [l for l in self.legs if l.side == "SELL"]
        entry = sum(l.entry_px for l in shorts)
        stop = entry * COMBINED_SL
        while now_ist() < exit_at and not self.halted:
            active = [l for l in self.legs if l.active]
            if not active:
                break
            ltp = self.ltp_batch(FNO_SEG, [l.token for l in active])
            # Backup exchange stop filled? -> close the rest
            if not PAPER_MODE and any(l.sl_order_id for l in active):
                try:
                    ob = self.dhan.get_order_list()
                    orders = {str(o.get("orderId")): o for o in (ob.get("data") or [])} if isinstance(ob, dict) else {}
                except Exception:
                    orders = {}
                hit = False
                for l in active:
                    o = orders.get(l.sl_order_id)
                    if o and str(o.get("orderStatus", "")).upper() == "TRADED":
                        l.sl_order_id = ""
                        self._book_exit(l, float(o.get("averageTradedPrice") or o.get("averagePrice") or l.sl_px), "BACKUP_SL")
                        hit = True
                if hit:
                    self.close_all(ltp, "BACKUP_SL_OTHER_LEG")
                    break
            if ltp and all(ltp.get(l.token, 0) > 0 for l in shorts if l.active):
                cur = sum(ltp.get(l.token, l.exit_px) if l.active else l.exit_px for l in shorts)
                if cur >= stop:
                    tg(f"🛑 Combined stop: CE+PE ₹{cur:.2f} >= ₹{stop:.2f}")
                    self.close_all(ltp, "COMBINED_STOP_40")
                    break
                mtm = self.realized_pnl + sum(l.pnl(ltp.get(l.token, l.entry_px)) for l in self.legs if l.active)
                if mtm <= DAILY_HARD_STOP:
                    tg(f"🚨 HARD STOP: MTM ₹{mtm:,.0f} <= ₹{DAILY_HARD_STOP:,.0f}")
                    self.close_all(ltp, "HARD_STOP")
                    self.halted = True
                    break
            time.sleep(1.5)
        if any(l.active for l in self.legs):
            ltp = self.ltp_batch(FNO_SEG, [l.token for l in self.legs if l.active])
            self.close_all(ltp, "TIME_EXIT_1520")

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


    # ----------------------------------------------------------
    # Reconciliation after restart
    # ----------------------------------------------------------
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
        for leg in [l for l in self.legs if l.active]:
            expected = -leg.qty if leg.side == "SELL" else leg.qty
            have = net.get(str(leg.token), 0)
            if have == 0:
                if leg.sl_order_id:
                    st = self.order_status(leg.sl_order_id)
                    if st["status"] == "TRADED":
                        self._book_exit(leg, st["avg"] or leg.sl_px, "SL_HIT_EXCHANGE (while offline)")
                        continue
                px = self.ltp_batch(FNO_SEG, [leg.token]).get(str(leg.token), leg.entry_px)
                tg(f"⚠️ {leg.symbol} is flat at the broker; marking closed at LTP ₹{px:.2f} (check fills).")
                self._book_exit(leg, px, "FLAT_AT_BROKER")
            elif have != expected:
                tg(f"⚠️ {leg.symbol}: broker net qty {have}, expected {expected}. Please check manually.")

    # ----------------------------------------------------------
    # Google Sheet logging: same spreadsheet as the v3 bot (GSHEET_ID_V2), two v4 tabs
    #   Lemonn_v4_Trades : one row per leg, same columns as your Lemonn_Dual_v3 tab
    #   Lemonn_v4_Daily  : one row per trading day with every filter value + decision
    # ----------------------------------------------------------
    TRADES_HEADER = ["Date", "Index", "Session", "Leg", "Side", "Symbol", "Qty", "Entry", "Exit",
                     "Reason", "Leg_PnL", "Paper"]
    HEADER = ["Date", "Gap %", "Range 09:15-09:59", "Days to expiry", "VIX", "VIX prev", "Fut vs VWAP %",
              "Straddle 09:20", "Straddle 10:00", "ADX 5m", "Score", "Decision", "Strike", "Combined entry",
              "Stop level", "Entry time", "Exit time", "Exit reason", "Day P&L (before charges)", "Lots", "Paper"]

    @staticmethod
    def _tab(sh, title, header):
        try:
            return sh.worksheet(title)
        except Exception:
            ws = sh.add_worksheet(title=title, rows="2000", cols=str(len(header)))
            ws.append_row(header)
            return ws

    def log_sheet(self):
        if self.logged or not gspread or not GSHEET_ID_V2 or not os.path.exists(SERVICE_ACCOUNT_FILE):
            return
        I = self.info
        try:
            sh = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2)
            if self.legs:
                ws = self._tab(sh, TRADES_TAB, self.TRADES_HEADER)
                ws.append_rows([[str(self.today), INDEX, l.session, l.opt_type, l.side, l.symbol, l.qty,
                                 l.entry_px, l.exit_px, l.exit_reason,
                                 round(l.pnl(l.exit_px), 2) if not l.active else "", PAPER_MODE]
                                for l in self.legs], value_input_option="USER_ENTERED")
            row = [str(self.today), I.get("gap_pct"), I.get("orb_pts"), I.get("dte"), I.get("vix"), I.get("vix_prev"),
                   I.get("fut_vwap_pct"), I.get("straddle_920"), I.get("straddle_1000"), I.get("adx"), I.get("score"),
                   I.get("decision", ""), I.get("strike"), I.get("combined_entry"), I.get("stop_level"),
                   I.get("entry_time"), I.get("exit_time"),
                   ", ".join(sorted({l.exit_reason for l in self.legs if l.exit_reason})),
                   round(self.realized_pnl, 2) if self.legs else 0, NUM_LOTS, PAPER_MODE]
            self._tab(sh, SHEET_TAB, self.HEADER).append_row(
                ["" if v is None else v for v in row], value_input_option="USER_ENTERED")
            self.logged = True
            self.save_state()
        except Exception as e:
            print(f"⚠️ Sheet logging failed: {e}")

    def finish(self, note: str = ""):
        self.phase = "done"
        self.save_state()
        self.log_sheet()
        tg(f"🏁 NIFTY v4 day complete{(' - ' + note) if note else ''} | P&L (before charges): ₹{self.realized_pnl:,.0f}")

    # ----------------------------------------------------------
    # Main flow
    # ----------------------------------------------------------
    def run(self):
        if self.today.weekday() >= 5:
            return print("Weekend - nothing to trade.")
        try:
            import pandas_market_calendars as mcal
            if mcal.get_calendar("NSE").schedule(start_date=str(self.today), end_date=str(self.today)).empty:
                return tg("📅 NSE holiday. NIFTY v4 sleeping.")
        except Exception:
            pass
        self.load_state()
        if self.phase == "done":
            return print("Today's NIFTY v4 session is already complete.")
        self.login()
        self.load_master()
        tg(f"🚀 LEMONN NIFTY v4 (sideways straddle) | {NUM_LOTS} lots x {self.lot_size} | expiry {self.expiry} "
           f"({(self.expiry - self.today).days}d) | {'PAPER' if PAPER_MODE else 'LIVE'}")
        self.reconcile_positions()

        if self.phase == "init":
            wait_until(at_ist(self.today, 9, 15))
            if not self.check_gap():
                self.info["decision"] = "SKIP: gap"
                return self.finish(f"gap {self.info.get('gap_pct')}% > {MAX_GAP_PCT}%, no trade")
            if (self.expiry - self.today).days > MAX_DTE:
                self.info["dte"] = (self.expiry - self.today).days
                self.info["decision"] = "SKIP: dte"
                return self.finish(f"{self.info['dte']} days to expiry (> {MAX_DTE}), no trade")
            if now_ist() <= at_ist(self.today, *REF_LAST) and not self.info.get("straddle_920"):
                wait_until(at_ist(self.today, *REF_TIME))
                self.capture_ref_straddle()
            wait_until(at_ist(self.today, *ENTRY))
            if now_ist() > at_ist(self.today, *ENTRY_LAST):
                self.info["decision"] = "SKIP: started after 10:05"
                return self.finish("started after the 10:00-10:05 window")
            if not self.evaluate():
                return self.finish("filters not met")
            if not self.deploy():
                self.info["decision"] = "SKIP: entry failed"
                return self.finish("entry failed")
            self.phase = "open"
            self.save_state()

        if self.phase == "open":
            self.monitor()
            closed = [l for l in self.legs if not l.active]
            if closed:
                self.info["exit_time"] = f"{now_ist():%H:%M}"
            self.finish()


if __name__ == "__main__":
    bot = None
    try:
        bot = LemonnV4Bot()
        bot.run()
    except Exception as e:
        open_legs = [l.symbol for l in (bot.legs if bot else []) if l.active]
        tg(f"🚨 NIFTY v4 BOT CRASH 🚨\n{e}\nOpen legs: {open_legs or 'none'}. "
           f"Backup exchange stops (if live) stay in place; restart the bot to resume.")
        sys.exit(1)
