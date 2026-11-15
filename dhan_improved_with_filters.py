#!/usr/bin/env python3
"""
lemonn_dual_session_dhan.py (v2.1 Institutional Upgrade)
Nifty & Sensex Dual-Session Options Selling Bot via DhanHQ V2 REST & SDK.
Includes:
  1. Batched Multi-Token LTP & OHLC Fetching (Zero HTTP 429s)
  2. 0.50% Opening Gap & 9:15-9:50 AM (140-Pt Nifty / 450-Pt Sensex) ORB Filter
  3. Premium-Matched Strangle & Synthetic-Future ATM Straddle Strike Selection
  4. Surviving-Leg SL-to-Cost (+10% Buffer) on 50% Stop-Loss Hit
  5. Morning-to-Afternoon Chop Circuit Breaker
  6. Limit-Chase Order Execution with Market Fallback
"""
import sys
import os
import time
import datetime
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import pyotp
import gspread
from dhanhq import DhanContext, dhanhq

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
PAPER_MODE_END_DATE = datetime.date(2026, 12, 31)
if now_ist().date() <= PAPER_MODE_END_DATE:
    PAPER_MODE = True
else:
    PAPER_MODE = os.environ.get("DHAN_PAPER_MODE", "0").strip() in ("1", "true")
# ==============================================================

NUM_LOTS = int(os.environ.get("DHAN_NUM_LOTS", "5"))
PAPER_SLIPPAGE_PTS = 0.25  # Reduced from 0.50 due to Limit-Chase execution
MAX_GAP_PCT = 0.50         # Halt day if opening gap > 0.50%
MORNING_MAX_LOSS_CB = -12000.0  # Circuit breaker threshold for Afternoon session
SL_COST_BUFFER = 1.10      # Move surviving leg SL to Entry + 10% buffer

def tg(message: str):
    tag = "[PAPER] " if PAPER_MODE else ""
    print(f"{tag}{message}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{tag}{message}"},
                timeout=5
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
    side: str
    qty: int
    entry_px: float
    sl_px: float
    exch_seg: str
    active: bool = True
    sl_moved_to_cost: bool = False
    exit_px: float = 0.0
    exit_reason: str = ""

class LemonnDhanBot:
    def __init__(self):
        self.dhan = None
        self.access_token = None
        self.options_df = None
        self.expiry = None
        self.legs: List[Leg] = []
        self.traded_symbols = set()
        self.realized_pnl = 0.0

        wd = now_ist().weekday()
        if wd in [0, 1, 4]:  # Mon, Tue, Fri -> NIFTY
            self.idx_name = "NIFTY"
            self.exch_seg = "NFO"
            self.dhan_exch_seg = "NSE_FNO"
            self.spot_exch = "IDX_I"
            self.spot_token = "13"
            self.strike_step = 50
            self.max_orb_pts = 140.0
            # Morning entry shifted to 09:50 after 09:15-09:50 ORB check
            self.m_entry, self.m_exit = (9, 50), (14, 15)
            self.a_entry, self.a_exit = (14, 16), (15, 28)
        elif wd in [2, 3]:  # Wed, Thu -> SENSEX
            self.idx_name = "SENSEX"
            self.exch_seg = "BFO"
            self.dhan_exch_seg = "BSE_FNO"
            self.spot_exch = "IDX_I"
            self.spot_token = "51"
            self.strike_step = 100
            self.max_orb_pts = 450.0
            self.m_entry, self.m_exit = (9, 50), (13, 30)
            self.a_entry, self.a_exit = (13, 35), (15, 28)
        else:
            self.idx_name = "NONE"

    def login(self):
        if not DHAN_CLIENT_ID or not DHAN_PIN or not DHAN_TOTP_SECRET:
            raise RuntimeError("Missing Dhan API credentials in /etc/trade-config/config.env")

        print(f"🔑 Authenticating Dhan HQ API for {DHAN_CLIENT_ID}...")
        for attempt in range(1, 6):
            totp_code = pyotp.TOTP(DHAN_TOTP_SECRET).now()
            url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={DHAN_CLIENT_ID}&pin={DHAN_PIN}&totp={totp_code}"
            headers = {"Accept": "application/json"}

            try:
                response = requests.post(url, headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    if "accessToken" in data:
                        self.access_token = data["accessToken"]
                        context = DhanContext(DHAN_CLIENT_ID, self.access_token)
                        self.dhan = dhanhq(context)

                        if "data" in self.dhan.get_fund_limits():
                            print("✅ Dhan SDK Initialized Successfully.")
                            return
                print(f"⚠️ Attempt {attempt} Rejected - HTTP {response.status_code}: {response.text}")
            except Exception as e:
                print(f"⚠️ Attempt {attempt} Error: {e}")

            if attempt < 5:
                print("⏳ Waiting 10 seconds before retry...")
                time.sleep(10)

        raise RuntimeError("Dhan authentication failed after 5 attempts.")

    def load_master(self):
        print("📥 Fetching latest Dhan master CSV...")
        df = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master.csv", low_memory=False)

        df = df[df["SEM_TRADING_SYMBOL"].str.startswith(self.idx_name, na=False)].copy()
        df = df[df["SEM_TRADING_SYMBOL"].str.contains(r"(?:CE|PE)$", regex=True, na=False)].copy()

        if df.empty:
            raise RuntimeError(f"Database contains no options matching {self.idx_name} CE/PE.")

        df["strike"] = pd.to_numeric(df["SEM_STRIKE_PRICE"], errors="coerce")
        df["expirydate"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce").dt.date

        lot_col = next((c for c in ["SEM_LOT_UNITS", "SEM_LOT_SIZE", "SEM_LOT_REGULAR", "LOT_SIZE"] if c in df.columns), None)
        df["lotsize"] = pd.to_numeric(df[lot_col], errors="coerce") if lot_col else 0

        today = now_ist().date()
        valid_options = df[df["expirydate"] >= today].copy()

        if valid_options.empty:
            db_min = df["expirydate"].min()
            db_max = df["expirydate"].max()
            print(f"⚠️ VM Date ({today}) is out of bounds for Dhan DB (Max: {db_max}). Adjusting...")
            valid_options = df[df["expirydate"] >= db_min].copy()

        self.options_df = valid_options
        expiries = sorted(self.options_df["expirydate"].dropna().unique())
        if not expiries:
            raise RuntimeError("No valid expiries found after date adjustment.")
        self.expiry = expiries[0]
        print(f"✅ Master loaded. Targeted Expiry: {self.expiry}")

    # ==============================================================
    # UPGRADE 1: BATCHED MULTI-TOKEN LTP & OHLC FETCHING
    # ==============================================================
    def get_dhan_ltp_batch(self, exch: str, tokens: List[str]) -> Dict[str, float]:
        """Fetches LTP for multiple tokens in a single HTTP request to prevent HTTP 429."""
        if not tokens:
            return {}
        time.sleep(1.15)  # Strict <1 req/sec throttle
        int_tokens = [int(t) for t in set(tokens)]
        for _ in range(3):
            try:
                url = "https://api.dhan.co/v2/marketfeed/ltp"
                headers = {
                    "access-token": self.access_token,
                    "client-id": DHAN_CLIENT_ID,
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
                payload = {exch: int_tokens}
                response = requests.post(url, headers=headers, json=payload, timeout=10)
                if response.status_code == 200:
                    resp_data = response.json()
                    if resp_data.get("status") == "success":
                        exch_data = resp_data.get("data", {}).get(exch, {})
                        return {
                            str(k): float(v.get("last_price", 0.0))
                            for k, v in exch_data.items()
                            if isinstance(v, dict) and v.get("last_price") is not None
                        }
                print(f"⚠️ Dhan Batch LTP Warning: HTTP {response.status_code} - {response.text}")
            except Exception as e:
                print(f"⚠️ Dhan Batch LTP REST Error ({exch}:{int_tokens}): {e}")
            time.sleep(1.2)
        return {}

    def get_dhan_ltp(self, exch: str, token: str) -> float:
        res = self.get_dhan_ltp_batch(exch, [str(token)])
        return res.get(str(token), 0.0)

    def get_spot(self) -> float:
        return self.get_dhan_ltp(self.spot_exch, self.spot_token)

    # ==============================================================
    # UPGRADE 2: OPENING GAP (0.50%) & ORB (140-PT) FILTERS
    # ==============================================================
    def check_market_gap(self) -> bool:
        """Returns True if Opening Gap exceeds MAX_GAP_PCT (0.50%), halting the day."""
        time.sleep(1.15)
        try:
            url = "https://api.dhan.co/v2/marketfeed/ohlc"
            headers = {
                "access-token": self.access_token,
                "client-id": DHAN_CLIENT_ID,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            payload = {self.spot_exch: [int(self.spot_token)]}
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                resp_data = response.json()
                spot_info = resp_data.get("data", {}).get(self.spot_exch, {}).get(str(self.spot_token), {})
                ohlc = spot_info.get("ohlc", {})
                open_px = float(ohlc.get("open", 0.0))
                prev_close = float(ohlc.get("close", 0.0))
                if open_px > 0 and prev_close > 0:
                    gap_pct = abs(open_px - prev_close) / prev_close * 100.0
                    if gap_pct > MAX_GAP_PCT:
                        tg(f"🛑 Gap Filter Triggered: {self.idx_name} opened with {gap_pct:.2f}% gap (Open: {open_px}, Prev Close: {prev_close}) > {MAX_GAP_PCT}%. Halting for today.")
                        return True
                    tg(f"✅ Gap Filter Passed: {self.idx_name} Opening Gap is {gap_pct:.2f}% (Open: {open_px}).")
                    return False
        except Exception as e:
            print(f"⚠️ Gap check warning: {e}")
        return False

    def check_opening_range(self, today: datetime.date) -> bool:
        """Monitors 09:15-09:50 range via OHLC + live polling. Returns True if range > max_orb_pts."""
        orb_end = at_ist(today, *self.m_entry)
        high_px, low_px = 0.0, float("inf")

        # Seed initial High/Low from Dhan's intraday OHLC endpoint
        try:
            time.sleep(1.15)
            url = "https://api.dhan.co/v2/marketfeed/ohlc"
            headers = {
                "access-token": self.access_token,
                "client-id": DHAN_CLIENT_ID,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            payload = {self.spot_exch: [int(self.spot_token)]}
            resp = requests.post(url, headers=headers, json=payload, timeout=10).json()
            ohlc = resp.get("data", {}).get(self.spot_exch, {}).get(str(self.spot_token), {}).get("ohlc", {})
            if float(ohlc.get("high", 0)) > 0 and float(ohlc.get("low", 0)) > 0:
                high_px = float(ohlc["high"])
                low_px = float(ohlc["low"])
        except Exception:
            pass

        if now_ist() < orb_end:
            tg(f"🔍 Monitoring {self.idx_name} Opening Range until {self.m_entry[0]:02d}:{self.m_entry[1]:02d} (Limit: {self.max_orb_pts} pts)...")

        while now_ist() < orb_end:
            spot = self.get_spot()
            if spot > 0:
                high_px = max(high_px, spot)
                low_px = min(low_px, spot)
                curr_range = high_px - low_px
                if curr_range > self.max_orb_pts:
                    tg(f"🛑 ORB Filter Triggered: {self.idx_name} range hit {curr_range:.1f} pts (High: {high_px}, Low: {low_px}) > {self.max_orb_pts} pts. Skipping Morning Session.")
                    return True
            time.sleep(10)

        if high_px > 0 and low_px < float("inf"):
            final_range = high_px - low_px
            if final_range > self.max_orb_pts:
                tg(f"🛑 ORB Filter Triggered: {self.idx_name} range is {final_range:.1f} pts > {self.max_orb_pts} pts. Skipping Morning Session.")
                return True
            tg(f"✅ Opening Range Safe: {final_range:.1f} pts (High: {high_px}, Low: {low_px}).")
        return False

    def find_contract(self, strike: int, opt_type: str) -> Optional[Dict]:
        m = self.options_df[
            (self.options_df["expirydate"] == self.expiry) &
            (self.options_df["strike"] == float(strike)) &
            (self.options_df["SEM_OPTION_TYPE"] == opt_type)
        ]
        if m.empty:
            return None
        return {
            "symbol": str(m.iloc[0]["SEM_TRADING_SYMBOL"]),
            "token": str(m.iloc[0]["SEM_SMST_SECURITY_ID"]),
            "lotsize": int(m.iloc[0]["lotsize"]),
            "strike": int(strike)
        }

    # ==============================================================
    # UPGRADE 3: PREMIUM-MATCHED & SYNTHETIC-FUTURE STRIKE SELECTION
    # ==============================================================
    def select_optimal_strikes(self, spot: float, session_name: str) -> Tuple[int, int]:
        base_atm = round(spot / self.strike_step) * self.strike_step

        if session_name == "MORNING_OTM2":
            # Candidate OTM strikes: +1 to +4 steps for CE, -1 to -4 steps for PE
            ce_candidates = [base_atm + (i * self.strike_step) for i in range(1, 5)]
            pe_candidates = [base_atm - (i * self.strike_step) for i in range(1, 5)]

            ce_contracts = {s: self.find_contract(s, "CE") for s in ce_candidates}
            pe_contracts = {s: self.find_contract(s, "PE") for s in pe_candidates}

            tokens = [c["token"] for c in ce_contracts.values() if c] + [c["token"] for c in pe_contracts.values() if c]
            ltp_map = self.get_dhan_ltp_batch(self.dhan_exch_seg, tokens)

            # Default fallback is OTM2
            default_ce, default_pe = base_atm + (2 * self.strike_step), base_atm - (2 * self.strike_step)
            ce_otm2_c = ce_contracts.get(default_ce)
            pe_otm2_c = pe_contracts.get(default_pe)
            if not ce_otm2_c or not pe_otm2_c:
                return default_ce, default_pe

            target_prem = (ltp_map.get(ce_otm2_c["token"], 0.0) + ltp_map.get(pe_otm2_c["token"], 0.0)) / 2.0
            if target_prem <= 0:
                return default_ce, default_pe

            # Find CE and PE pair with minimum premium mismatch around target_prem
            best_pair = (default_ce, default_pe)
            best_score = float("inf")
            for cs, cc in ce_contracts.items():
                for ps, pc in pe_contracts.items():
                    if not cc or not pc:
                        continue
                    c_px = ltp_map.get(cc["token"], 0.0)
                    p_px = ltp_map.get(pc["token"], 0.0)
                    if c_px > 5.0 and p_px > 5.0:
                        diff = abs(c_px - p_px)
                        dist_from_target = abs(((c_px + p_px) / 2.0) - target_prem)
                        score = diff + (0.5 * dist_from_target)
                        if score < best_score:
                            best_score = score
                            best_pair = (cs, ps)
            return best_pair

        else:
            # AFTERNOON_ATM: Calculate Synthetic Future = Spot + CE_ATM - PE_ATM
            ce_atm = self.find_contract(base_atm, "CE")
            pe_atm = self.find_contract(base_atm, "PE")
            if ce_atm and pe_atm:
                ltp_map = self.get_dhan_ltp_batch(self.dhan_exch_seg, [ce_atm["token"], pe_atm["token"]])
                ce_px = ltp_map.get(ce_atm["token"], 0.0)
                pe_px = ltp_map.get(pe_atm["token"], 0.0)
                if ce_px > 0 and pe_px > 0:
                    synth_fut = spot + ce_px - pe_px
                    synth_atm = round(synth_fut / self.strike_step) * self.strike_step
                    return synth_atm, synth_atm
            return base_atm, base_atm

    # ==============================================================
    # UPGRADE 5: LIMIT-CHASE EXECUTION WITH MARKET FALLBACK
    # ==============================================================
    def execute_order(self, symbol: str, token: str, side: str, qty: int, ref_ltp: float = 0.0) -> float:
        px = ref_ltp if ref_ltp > 0 else self.get_dhan_ltp(self.dhan_exch_seg, token)
        if PAPER_MODE:
            self.traded_symbols.add(symbol)
            return round(px + PAPER_SLIPPAGE_PTS if side == "BUY" else max(0.05, px - PAPER_SLIPPAGE_PTS), 2)

        try:
            dhan_side = self.dhan.SELL if side == "SELL" else self.dhan.BUY
            # Aggressive limit price to capture fill without crossing deep slippage
            limit_buf = -0.15 if side == "SELL" else 0.25
            limit_px = round(max(0.05, px + limit_buf) * 20) / 20.0  # Round to 0.05 tick

            res = self.dhan.place_order(
                security_id=str(token),
                exchange_segment=self.dhan_exch_seg,
                transaction_type=dhan_side,
                quantity=int(qty),
                order_type=self.dhan.LIMIT,
                product_type=self.dhan.INTRA,
                price=limit_px
            )
            if isinstance(res, dict) and res.get("status") == "success":
                oid = res["data"].get("orderId")
                # Poll for 3 seconds for Limit Fill, then convert to Market on step 4
                for step in range(8):
                    time.sleep(1.0)
                    ob = self.dhan.get_order_list()
                    if isinstance(ob, dict) and ob.get("status") == "success":
                        for o in ob.get("data", []):
                            if str(o.get("orderId")) == str(oid):
                                if o.get("orderStatus") == "TRADED":
                                    return float(o.get("averagePrice") or limit_px)
                                elif step == 3 and o.get("orderStatus") in ("PENDING", "TRANSIT"):
                                    # Fallback to MARKET order to guarantee execution
                                    try:
                                        self.dhan.modify_order(
                                            order_id=oid,
                                            order_type=self.dhan.MARKET,
                                            leg_name="ENTRY_LEG",
                                            quantity=int(qty),
                                            price=0,
                                            trigger_price=0,
                                            disclosed_quantity=0,
                                            validity="DAY"
                                        )
                                    except Exception:
                                        pass
        except Exception as e:
            print(f"⚠️ Order execution error for {symbol}: {e}")
        return px

    def deploy_session(self, session_name: str, ce_strike: int, pe_strike: int):
        ce = self.find_contract(ce_strike, "CE")
        pe = self.find_contract(pe_strike, "PE")
        if not ce or not pe:
            return tg(f"⚠️ {session_name} Skipped: Strikes {ce_strike}/{pe_strike} not found.")

        qty = NUM_LOTS * ce["lotsize"]
        ltp_map = self.get_dhan_ltp_batch(self.dhan_exch_seg, [ce["token"], pe["token"]])
        ce_ref = ltp_map.get(ce["token"], 0.0)
        pe_ref = ltp_map.get(pe["token"], 0.0)

        if ce_ref <= 0 or pe_ref <= 0:
            return tg(f"⚠️ {session_name} Aborted: Could not fetch valid option LTPs.")

        ce_px = self.execute_order(ce["symbol"], ce["token"], "SELL", qty, ce_ref)
        pe_px = self.execute_order(pe["symbol"], pe["token"], "SELL", qty, pe_ref)

        ce_sl = round(ce_px * 1.50, 1)
        pe_sl = round(pe_px * 1.50, 1)

        self.legs.extend([
            Leg(session_name, "CE", ce_strike, ce["symbol"], ce["token"], "SELL", qty, ce_px, ce_sl, self.dhan_exch_seg),
            Leg(session_name, "PE", pe_strike, pe["symbol"], pe["token"], "SELL", qty, pe_px, pe_sl, self.dhan_exch_seg)
        ])
        tg(
            f"🟢 {session_name} DEPLOYED ({self.idx_name})\n"
            f"CE {ce['symbol']} @ ₹{ce_px:.2f} (SL: ₹{ce_sl:.2f})\n"
            f"PE {pe['symbol']} @ ₹{pe_px:.2f} (SL: ₹{pe_sl:.2f})"
        )

    def close_leg(self, leg: Leg, ref_ltp: float, reason: str):
        px = self.execute_order(leg.symbol, leg.token, "BUY", leg.qty, ref_ltp)
        leg.exit_px, leg.active, leg.exit_reason = px, False, reason
        pnl = (leg.entry_px - px) * leg.qty
        self.realized_pnl += pnl
        tg(f"🔴 CLOSED {leg.symbol} @ ₹{px:.2f} | Reason: {reason} | P&L: ₹{pnl:,.2f}")

    # ==============================================================
    # UPGRADE 4: SURVIVING-LEG SL-TO-COST (+10% BUFFER) IN MONITOR
    # ==============================================================
    def monitor_until(self, exit_time: datetime.datetime):
        while now_ist() < exit_time:
            active_legs = [l for l in self.legs if l.active]
            if not active_legs:
                break

            tokens = [l.token for l in active_legs]
            ltp_map = self.get_dhan_ltp_batch(self.dhan_exch_seg, tokens)

            for leg in active_legs:
                current_px = ltp_map.get(str(leg.token), 0.0)
                if current_px > 0 and current_px >= leg.sl_px:
                    reason = "COST_SL_HIT" if leg.sl_moved_to_cost else "50%_SL_HIT"
                    self.close_leg(leg, current_px, reason)

                    # Tighten surviving leg SL to Entry Cost + 10% Buffer
                    if reason == "50%_SL_HIT":
                        for other in [l for l in self.legs if l.active and l.session == leg.session]:
                            new_sl = round(other.entry_px * SL_COST_BUFFER, 1)
                            if new_sl < other.sl_px:
                                other.sl_px = new_sl
                                other.sl_moved_to_cost = True
                                tg(f"🛡️ {other.symbol} SL tightened to Cost+10% (₹{other.sl_px:.2f})")

            time.sleep(1.5)

        # Time exit for remaining active legs in a single batched fetch
        rem_legs = [l for l in self.legs if l.active]
        if rem_legs:
            ltp_map = self.get_dhan_ltp_batch(self.dhan_exch_seg, [l.token for l in rem_legs])
            for leg in rem_legs:
                ref_px = ltp_map.get(str(leg.token), 0.0) or self.get_dhan_ltp(leg.exch_seg, leg.token)
                self.close_leg(leg, ref_px, "TIME_EXIT")

    def log_sheet(self, date_str: str):
        if not GSHEET_ID_V2 or not os.path.exists(SERVICE_ACCOUNT_FILE):
            return
        try:
            ws = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2).worksheet("Lemonn_Log")
        except Exception:
            try:
                sh = gspread.service_account(filename=SERVICE_ACCOUNT_FILE).open_by_key(GSHEET_ID_V2)
                ws = sh.add_worksheet(title="Lemonn_Log", rows="1000", cols="12")
                ws.append_row(["Date", "Index", "Session", "CE_Symbol", "CE_Entry", "CE_Exit", "CE_Reason", "PE_Symbol", "PE_Entry", "PE_Exit", "PE_Reason", "Session_PnL"])
            except Exception:
                return

        for session in ["MORNING_OTM2", "AFTERNOON_ATM"]:
            s_legs = [l for l in self.legs if l.session == session]
            if len(s_legs) == 2:
                ce, pe = s_legs[0], s_legs[1]
                pnl = ((ce.entry_px - ce.exit_px) * ce.qty) + ((pe.entry_px - pe.exit_px) * pe.qty)
                ws.append_row([date_str, self.idx_name, session, ce.symbol, ce.entry_px, ce.exit_px, ce.exit_reason, pe.symbol, pe.entry_px, pe.exit_px, pe.exit_reason, round(pnl, 2)])

    def run(self):
        if self.idx_name == "NONE":
            return tg("Weekend - Nothing to trade.")
        today = now_ist().date()
        try:
            import pandas_market_calendars as mcal
            if mcal.get_calendar("BSE").schedule(start_date=str(today), end_date=str(today)).empty:
                return tg("📅 Exchange Holiday. Sleeping.")
        except Exception:
            pass

        self.login()
        self.load_master()
        tg(f"🚀 LEMONN DUAL-SESSION v2.1 (DhanHQ | {self.idx_name} | {NUM_LOTS} Lots)")

        # Check Opening Gap at 09:15 AM
        wait_until(at_ist(today, 9, 15))
        if self.check_market_gap():
            return

        # 1. MORNING OTM2 SESSION (Monitors ORB until 09:50 AM)
        orb_breached = self.check_opening_range(today)
        if not orb_breached:
            spot = self.get_spot()
            if spot > 0:
                ce_strike, pe_strike = self.select_optimal_strikes(spot, "MORNING_OTM2")
                self.deploy_session("MORNING_OTM2", ce_strike, pe_strike)
                self.monitor_until(at_ist(today, *self.m_exit))
            else:
                tg("⚠️ Unable to fetch Spot Price for Morning Session.")

        # ==============================================================
        # UPGRADE 5: MORNING-TO-AFTERNOON CHOP CIRCUIT BREAKER
        # ==============================================================
        morning_sl_hits = sum(
            1 for l in self.legs
            if l.session == "MORNING_OTM2" and l.exit_reason in ("50%_SL_HIT", "COST_SL_HIT")
        )
        if morning_sl_hits >= 2 or self.realized_pnl <= MORNING_MAX_LOSS_CB:
            tg(
                f"🛑 Chop Circuit Breaker Triggered: Morning session hit {morning_sl_hits} SLs "
                f"(Realized P&L: ₹{self.realized_pnl:,.2f}). Skipping Afternoon ATM Session."
            )
        else:
            # 2. AFTERNOON ATM SESSION (Synthetic Future Centered)
            wait_until(at_ist(today, *self.a_entry))
            spot = self.get_spot()
            if spot > 0:
                ce_strike, pe_strike = self.select_optimal_strikes(spot, "AFTERNOON_ATM")
                self.deploy_session("AFTERNOON_ATM", ce_strike, pe_strike)
                self.monitor_until(at_ist(today, *self.a_exit))
            else:
                tg("⚠️ Unable to fetch Spot Price for Afternoon Session.")

        self.log_sheet(str(today))
        tg(f"🏁 DUAL-SESSION COMPLETE | Total Gross PnL: ₹{self.realized_pnl:,.2f}")

if __name__ == "__main__":
    try:
        LemonnDhanBot().run()
    except Exception as e:
        tg(f"🚨 FATAL BOT CRASH 🚨\n{e}")
