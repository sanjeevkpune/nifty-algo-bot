#!/usr/bin/env python3
"""
Hilega-Milega weekly alerts for NSE F&O stocks  --  ALERTS ONLY, no orders are ever placed.

Indicator (weekly candles, Friday close):  RSI(9) of weekly closes,
    green = EMA(3) of that RSI,  red = WMA(21) of that RSI.

Rule (weekly "B2" rule from the 2016-2026 backtest), EVERY signal, no 15-stock cap:
  BUY   first week RSI is above 50 AND above both green and red (it was not the week before),
        stock is in today's NSE F&O list, 20-day average turnover above Rs 5 crore.
        Every such stock is alerted and tracked. NIFTY above/below its 50-day average is shown
        and recorded (the backtest only bought when above), so both can be compared later.
  SELL  an open signal's weekly RSI closes below 50.
  Both are acted on at the next trading day's open (normally Monday).
  Performance is per signal: net return after 0.18% a side, the same period's NIFTY return,
  and Rs P&L on a notional HM_PER_SIGNAL per signal.

Each run:
  1. reads the F&O list from NSE (fallbacks: last saved list, then fno_list.txt here),
  2. reads ~3 years of daily prices from Yahoo Finance,
  3. processes every finished week since the last run (state file), filling last week's
     signals at Monday's open, then marking every open signal at Friday's close,
  4. posts the alert to Telegram and rewrites the HM_* tabs of the Google Sheet,
  5. saves the state (the workflow commits it to the `hilega-milega-state` branch).

Environment:
  HM_STATE         state file path (default hm_state.json)
  HM_PER_SIGNAL    notional Rs per signal for the Rs P&L columns (default 100000)
  HM_FORCE=1       redo the latest finished week even if it was already sent
  HM_DRY_RUN=1     print the alert only: no Telegram, no sheet, state not saved
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   Telegram (skipped if missing)
  GSHEET_ID / GCP_SA_FILE                 Google Sheet + service-account file (skipped if missing)
"""
import copy
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # repo root, for notify.py

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PER_SIGNAL = float(os.environ.get("HM_PER_SIGNAL", "100000"))   # notional ₹ per signal, for ₹ P&L
COST = 0.0018                                     # per side, same as the backtest
MIN_TURNOVER = 5e7                                # Rs 5 crore, 20-day average
WEEK_DONE_AT = dt.time(15, 45)                    # a Friday's candle counts as final after this (IST)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ indicator (same as the backtest)
def rsi(c, n):
    ch = c.diff()
    up = ch.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-ch.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def wma(x, n):
    w = np.arange(1, n + 1)
    return x.rolling(n).apply(lambda a: np.dot(a, w) / w.sum(), raw=True)


def indic(c):
    r = rsi(c, 9)
    return r, r.ewm(span=3, adjust=False).mean(), wma(r, 21)


# ------------------------------------------------------------------ F&O list
def parse_mktlots(text):
    """fo_mktlots.csv: index rows first, then a 'Symbol' header row, then the stocks."""
    rows = [[x.strip() for x in line.split(",")] for line in text.splitlines()]
    start = next((i for i, r in enumerate(rows) if len(r) > 1 and r[1] == "Symbol"), None)
    if start is None:
        return []
    out = []
    for r in rows[start + 1:]:
        if len(r) > 1 and r[1] and r[1] not in out:
            out.append(r[1])
    return out


def fetch_fno_list(state):
    import requests
    try:
        s = requests.Session()
        s.headers.update(UA)
        r = s.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", timeout=20)
        syms = parse_mktlots(r.text) if r.status_code == 200 else []
        if len(syms) >= 100:
            return syms, f"NSE ({len(syms)} stocks, read today)"
        log(f"NSE list: status {r.status_code}, {len(syms)} symbols")
    except Exception as e:
        log(f"NSE list failed: {e}")
    if state.get("fno_list"):
        return state["fno_list"], f"⚠️ NSE not reachable, using the list saved on {state.get('fno_list_date')}"
    with open(os.path.join(HERE, "fno_list.txt")) as f:
        syms = [x.strip() for x in f if x.strip() and not x.startswith("#")]
    return syms, "⚠️ NSE not reachable, using the list in the repo (fno_list.txt, Nov 2026)"


# ------------------------------------------------------------------ prices
def yahoo_daily(ysym, rng="3y"):
    import requests
    last = None
    for attempt in range(4):
        host = "query1" if attempt % 2 == 0 else "query2"
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(ysym)}",
                             params={"range": rng, "interval": "1d"}, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(3 + 3 * attempt)
                continue
            j = r.json()
            res = (j.get("chart") or {}).get("result")
            if not res:
                return None
            res = res[0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({k: q.get(k) for k in ("open", "high", "low", "close", "volume")},
                              index=pd.to_datetime([dt.datetime.fromtimestamp(t, IST).date() for t in ts]))
            df = df.dropna(subset=["close"])
            return df[~df.index.duplicated(keep="last")]
        except Exception as e:
            last = e
            time.sleep(1 + attempt)
    log(f"Yahoo {ysym}: {last}")
    return None


def load_prices(symbols):
    """Returns (O, C, V) daily pivots for the stocks and the NIFTY close series."""
    frames = {}

    def one(s):
        return s, yahoo_daily(s + ".NS")

    with ThreadPoolExecutor(max_workers=6) as ex:
        for s, df in ex.map(one, symbols):
            if df is not None and len(df) > 60:
                frames[s] = df
    nifty = yahoo_daily("^NSEI")
    if nifty is None or len(nifty) < 100:
        raise RuntimeError("could not read NIFTY 50 (^NSEI) from Yahoo")
    return pivots(frames, nifty["close"])


def pivots(frames, nifty_close):
    C = pd.DataFrame({s: f["close"] for s, f in frames.items()}).sort_index()
    O = pd.DataFrame({s: f["open"] for s, f in frames.items()}).reindex(C.index)
    V = pd.DataFrame({s: f["volume"] for s, f in frames.items()}).reindex(C.index)
    nifty_close = nifty_close.sort_index()
    cal = nifty_close.index.union(C.index)
    return O.reindex(cal), C.reindex(cal), V.reindex(cal), nifty_close.reindex(cal).ffill()


# ------------------------------------------------------------------ weekly tables
class Weekly:
    """Everything the rule needs, by finished week (index = week's last trading day)."""

    def __init__(self, O, C, V, nifty, last_week_end):
        keep = C.index <= last_week_end
        O, C, V, nifty = O[keep], C[keep], V[keep], nifty[keep]
        self.O, self.C = O, C
        dates = pd.Series(C.index, index=C.index)
        wk = dates.dt.to_period("W-FRI")
        last_day = dates.groupby(wk.values).max()
        Cw = C.groupby(wk.values).last()
        Cw.index = last_day.loc[Cw.index].values
        self.R, self.E, self.W = indic(Cw)
        self.Cw = Cw
        above = (self.R > 50) & (self.R > self.E) & (self.R > self.W)
        self.buy = above & ~above.shift(fill_value=False)
        self.weeks = list(Cw.index)
        turn = (C * V).rolling(20).mean()
        self.liquid = turn.reindex(self.weeks) > MIN_TURNOVER
        self.roc63 = (C / C.shift(63) - 1).reindex(self.weeks)
        self.nifty = nifty.reindex(self.weeks)
        self.nifty50 = nifty.rolling(50).mean().reindex(self.weeks)
        self.nifty_daily = nifty

    def nifty_on(self, day):
        v = self.nifty_daily.loc[:day].dropna()
        return float(v.iloc[-1]) if len(v) else None

    def open_after(self, week_end, sym):
        """First trading day after `week_end` and that day's open for `sym` (fallback: its close)."""
        pos = self.C.index.searchsorted(week_end, side="right")
        for i in range(pos, min(pos + 5, len(self.C.index))):
            px = self.O[sym].iloc[i] if sym in self.O else np.nan
            if not np.isnan(px) and px > 0:
                return self.C.index[i], float(px)
            c = self.C[sym].iloc[i] if sym in self.C else np.nan
            if not np.isnan(c):
                return self.C.index[i], float(c)
        return None, None

    def close_at(self, week_end, sym):
        if sym not in self.Cw:
            return None
        v = self.Cw[sym].loc[:week_end].dropna()
        return float(v.iloc[-1]) if len(v) else None


def last_finished_week_end(now_ist, trading_days):
    """Last trading day of the newest week that is over (a Friday counts after 15:45 IST)."""
    today = now_ist.date()
    days = [d for d in trading_days if d.date() <= today]
    if not days:
        return None
    wk_today = pd.Timestamp(today).to_period("W-FRI")
    friday = wk_today.end_time.date()
    week_over = today > friday or (today == friday and now_ist.time() >= WEEK_DONE_AT)
    if not week_over:   # this week is still running: use the week before
        days = [d for d in days if d.to_period("W-FRI") < wk_today]
    if days and days[-1].date() == today and not week_over:
        days = days[:-1]
    return days[-1] if days else None


# ------------------------------------------------------------------ state
def new_state():
    return {"version": 2, "per_signal": PER_SIGNAL, "last_week": None, "positions": {}, "pending_buys": [],
            "pending_sells": [], "trades": [], "signals": [], "weekly": [], "fno_list": [], "fno_list_date": None,
            "start_week": None, "prev": None}


def d2s(d):
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def perf(trades, flt=None):
    """Per-signal numbers for LIVE signals (closed: net return; open: return so far).
    Signals carried in from the backtest seed get alerts but are left out of these numbers."""
    trades = [t for t in trades if t.get("origin", "live") == "live"]
    closed = [t for t in trades if t["status"] == "CLOSED" and (flt is None or t.get("nifty_ok") == flt)]
    opn = [t for t in trades if t["status"] == "OPEN" and (flt is None or t.get("nifty_ok") == flt)]
    nets = [t["net_ret_pct"] for t in closed if t.get("net_ret_pct") is not None]
    alpha = [t["vs_nifty_pct"] for t in closed if t.get("vs_nifty_pct") is not None]
    return {"closed": len(closed), "open": len(opn),
            "win_rate_pct": r2(100 * sum(n > 0 for n in nets) / len(nets), 1) if nets else None,
            "avg_net_pct": r2(np.mean(nets)) if nets else None,
            "avg_vs_nifty_pct": r2(np.mean(alpha)) if alpha else None,
            "avg_open_pct": r2(np.mean([t["ret_pct"] for t in opn if t.get("ret_pct") is not None])) if opn else None,
            "realised": r2(sum(t["pnl"] or 0 for t in closed), 0), "unrealised": r2(sum(t["pnl"] or 0 for t in opn), 0)}


def process_week(st, W, wend, fno, now_str):
    """Advance one finished week. Every buy signal is tracked until its own sell signal (no cap)."""
    wkey = d2s(wend)
    per = st.get("per_signal", PER_SIGNAL)
    events = {"week": wkey, "filled_buys": [], "filled_sells": [], "sells": [], "buys": [], "missing": [], "weak": [],
              "revised": []}
    trade = lambda tid: next((t for t in st["trades"] if t["id"] == tid), None)
    hold = lambda d0, d1: int(round((pd.Timestamp(d1) - pd.Timestamp(d0)).days / 7))

    def still(series_df, s, week):   # does today's data (split-adjusted) still show last week's signal?
        try:
            v = series_df[s].loc[pd.Timestamp(week)]
            return None if pd.isna(v) else v
        except KeyError:
            return None

    # 0) Yahoo back-adjusts prices after a split or bonus: re-read each open signal's entry price
    for s, p in st["positions"].items():
        if s in W.O and pd.Timestamp(p["entry_date"]) in W.O.index:
            px0 = W.O[s].loc[pd.Timestamp(p["entry_date"])]
            if not pd.isna(px0) and px0 > 0 and abs(px0 / p["entry_px"] - 1) > 0.02:
                p["entry_px"], p["units"] = float(px0), p["cost"] / (float(px0) * (1 + COST))
                t = trade(p["id"])
                if t:
                    t["entry_px"] = r2(px0)

    # 1) last week's signals are filled at this week's first open
    for o in st["pending_sells"]:
        p = st["positions"].get(o["sym"])
        if not p:
            continue
        r = still(W.R, o["sym"], o["signal_week"])
        if r is not None and r >= 50:
            events["revised"].append(o["sym"])
            continue
        day, px = W.open_after(pd.Timestamp(o["signal_week"]), o["sym"])
        if px is None:
            events["missing"].append(o["sym"])
            continue
        st["positions"].pop(o["sym"])
        t = trade(p["id"])
        if t:
            net = (1 - COST) * px / ((1 + COST) * p["entry_px"]) - 1
            n_ret = W.nifty_on(day) / t["nifty_entry"] - 1 if t.get("nifty_entry") else None
            t.update(status="CLOSED", sell_signal_week=o["signal_week"], sell_signal_close=o["close"], sell_rsi=o["rsi"],
                     exit_date=d2s(day), exit_px=r2(px), last_px=r2(px), ret_pct=r2((px / p["entry_px"] - 1) * 100),
                     net_ret_pct=r2(net * 100), nifty_ret_pct=r2(n_ret * 100) if n_ret is not None else None,
                     vs_nifty_pct=r2((net - n_ret) * 100) if n_ret is not None else None,
                     pnl=r2(p["units"] * px * (1 - COST) - p["cost"], 0), weeks_held=hold(p["entry_date"], day))
        events["filled_sells"].append(o["sym"])
    for o in st["pending_buys"]:
        if o["sym"] in st["positions"]:
            continue
        b = still(W.buy, o["sym"], o["signal_week"])
        if b is not None and not bool(b):
            events["revised"].append(o["sym"])
            continue
        day, px = W.open_after(pd.Timestamp(o["signal_week"]), o["sym"])
        if px is None:
            events["missing"].append(o["sym"])
            continue
        tid = f"{o['sym']}-{o['signal_week']}"
        st["positions"][o["sym"]] = {"id": tid, "entry_date": d2s(day), "entry_px": px,
                                     "units": per / (px * (1 + COST)), "cost": per, "signal_week": o["signal_week"]}
        st["trades"].append({"id": tid, "sym": o["sym"], "status": "OPEN", "origin": o.get("origin", "live"),
                             "nifty_ok": o.get("nifty_ok", "Y"), "buy_signal_week": o["signal_week"],
                             "buy_signal_close": o["close"], "buy_rsi": o["rsi"], "roc63_pct": o["roc63_pct"],
                             "entry_date": d2s(day), "entry_px": r2(px), "nifty_entry": r2(W.nifty_on(day)),
                             "last_px": r2(px), "ret_pct": 0.0, "net_ret_pct": None, "nifty_ret_pct": None,
                             "vs_nifty_pct": None, "pnl": None, "weeks_held": 0, "sell_signal_week": None,
                             "sell_signal_close": None, "sell_rsi": None, "exit_date": None, "exit_px": None})
        events["filled_buys"].append(o["sym"])
    st["pending_buys"], st["pending_sells"] = [], []

    # 2) mark every open signal at this week's close
    nifty, n50 = float(W.nifty.loc[wend]), float(W.nifty50.loc[wend])
    for s, p in st["positions"].items():
        c = W.close_at(wend, s) or p["entry_px"]
        t = trade(p["id"])
        if t:
            t.update(last_px=r2(c), ret_pct=r2((c / p["entry_px"] - 1) * 100), pnl=r2(p["units"] * c - p["cost"], 0),
                     weeks_held=hold(p["entry_date"], wend),
                     nifty_ret_pct=r2((nifty / t["nifty_entry"] - 1) * 100) if t.get("nifty_entry") else None)
    if st["start_week"] is None:
        st["start_week"] = wkey
    market_ok = bool(nifty > n50)

    def facts(s):
        return {"sym": s, "signal_week": wkey, "close": r2(W.Cw[s].loc[wend]), "rsi": r2(W.R[s].loc[wend], 1),
                "green": r2(W.E[s].loc[wend], 1), "red": r2(W.W[s].loc[wend], 1),
                "roc63_pct": r2(W.roc63[s].loc[wend] * 100, 1) if s in W.roc63 else None}

    # 3) SELL: an open signal's weekly RSI closed below 50
    for s, p in st["positions"].items():
        if s not in W.R or np.isnan(W.R[s].loc[wend]):
            events["missing"].append(s)
            continue
        if W.R[s].loc[wend] < 50:
            f = facts(s)
            f.update(entry_px=r2(p["entry_px"]), entry_date=p["entry_date"],
                     ret_pct=r2((W.Cw[s].loc[wend] / p["entry_px"] - 1) * 100, 1))
            st["pending_sells"].append(f)
            events["sells"].append(f)

    # 3b) WATCH: open signals that turned weak this week (RSI under both lines, still above 50)
    sells_now = {f["sym"] for f in events["sells"]}
    for s in st["positions"]:
        if s in sells_now or s not in W.R:
            continue
        r, e, w = W.R[s], W.E[s], W.W[s]
        weak = (r < e) & (r < w) & (r >= 50)
        if bool(weak.loc[wend]) and not bool(weak.shift(fill_value=False).loc[wend]):
            events["weak"].append(facts(s))

    # 4) BUY: every F&O stock with a fresh signal (no cap). NIFTY filter is shown, not applied.
    for s in fno:
        if s in st["positions"] or s not in W.buy:
            continue
        if bool(W.buy[s].loc[wend]) and bool(W.liquid[s].loc[wend]):
            f = facts(s)
            f["nifty_ok"] = "Y" if market_ok else "N"
            events["buys"].append(f)
    events["buys"].sort(key=lambda f: -(f["roc63_pct"] if f["roc63_pct"] is not None else -1e9))
    st["pending_buys"] = [dict(f, origin="live") for f in events["buys"]]

    # 5) logs
    for kind, lst in (("SELL", events["sells"]), ("BUY", events["buys"]), ("WATCH: RSI under both lines", events["weak"])):
        for f in lst:
            note = ""
            if kind == "SELL" and f.get("ret_pct") is not None:
                note = f"bought {f['entry_date']} at {f['entry_px']}, {f['ret_pct']:+.1f}% to this close"
            elif kind == "BUY" and f.get("nifty_ok") == "N":
                note = "NIFTY below its 50-day average (the backtest skipped these)"
            st["signals"].append({"alert_time": now_str, "signal_week": wkey, "signal": kind, "sym": f["sym"],
                                  "close": f["close"], "rsi": f["rsi"], "green": f["green"], "red": f["red"],
                                  "roc63_pct": f["roc63_pct"], "note": note})
    a, y = perf(st["trades"]), perf(st["trades"], "Y")
    st["weekly"].append({"week": wkey, "alert_time": now_str, "nifty": r2(nifty), "nifty_50dma": r2(n50),
                         "nifty_filter": "ON" if market_ok else "OFF", "buy_signals": len(events["buys"]),
                         "sell_signals": len(events["sells"]), "open_signals": len(st["positions"]),
                         "open_live": a["open"], "invested": r2(a["open"] * per, 0), "unrealised": a["unrealised"],
                         "realised": a["realised"], "total_pnl": r2((a["unrealised"] or 0) + (a["realised"] or 0), 0),
                         "closed": a["closed"], "win_rate_pct": a["win_rate_pct"], "avg_net_pct": a["avg_net_pct"],
                         "avg_vs_nifty_pct": a["avg_vs_nifty_pct"], "avg_open_pct": a["avg_open_pct"],
                         "rule_closed": y["closed"], "rule_win_rate_pct": y["win_rate_pct"], "rule_avg_net_pct": y["avg_net_pct"]})
    events.update(market_ok=market_ok, nifty=nifty, n50=n50)
    st["last_week"] = wkey
    return events


# ------------------------------------------------------------------ the message
def fmt_day(s):
    return pd.Timestamp(s).strftime("%a %d %b %Y")


def message(st, ev, W, wend, fno_src):
    L = [f"📈 Hilega-Milega weekly · week ending {fmt_day(ev['week'])}",
         f"NIFTY {ev['nifty']:,.0f} vs 50-day avg {ev['n50']:,.0f}: "
         + ("above (backtest rule takes buys)" if ev["market_ok"] else "BELOW (the backtest skipped buys in such weeks)"),
         "Alerts only, no orders placed. Every signal, no cap. Act at the next open (normally Monday).", ""]
    if ev["sells"]:
        L.append(f"🔴 SELL {len(ev['sells'])} · weekly RSI closed below 50")
        for f in ev["sells"]:
            L.append(f"• {f['sym']}  close {f['close']:,} · RSI {f['rsi']} · bought {pd.Timestamp(f['entry_date']).strftime('%d %b %y')} "
                     f"@ {f['entry_px']:,} · {f['ret_pct']:+.1f}%")
        L.append("")
    if ev["buys"]:
        L.append(f"🟢 BUY {len(ev['buys'])} · RSI above 50 and above green & red (biggest 3-month gain first)"
                 + ("" if ev["market_ok"] else " ⚠️ NIFTY below 50-DMA"))
        for f in ev["buys"]:
            L.append(f"• {f['sym']}  close {f['close']:,} · RSI {f['rsi']} (green {f['green']}, red {f['red']}) · 3M {f['roc63_pct']:+.1f}%")
        L.append("")
    if not (ev["sells"] or ev["buys"]):
        L.append("No new buy or sell signal this week.")
        L.append("")
    sells = {f["sym"] for f in ev["sells"]}
    held = sorted((s for s in st["positions"] if s not in sells), key=lambda s: st["positions"][s]["entry_date"])
    txt, watch = [], []
    for s in held:
        p = st["positions"][s]
        c = W.close_at(wend, s) or p["entry_px"]
        txt.append(f"{s} {(c / p['entry_px'] - 1) * 100:+.1f}%")
        try:
            if W.R[s].loc[wend] < W.E[s].loc[wend] and W.R[s].loc[wend] < W.W[s].loc[wend]:
                watch.append(s)
        except KeyError:
            pass
    L.append(f"📋 Open buy signals: {len(held) + len(ev['buys'])} ({len(held)} earlier + {len(ev['buys'])} new)")
    if txt:
        L.append("  " + " · ".join(txt))
    if watch:
        L.append("Weakening (RSI under both lines, still above 50): " + ", ".join(watch))
    wk = st["weekly"][-1]
    L.append("")
    L.append(f"Live tracking (signals from {fmt_day(st['start_week'])} on, ₹{st.get('per_signal', PER_SIGNAL):,.0f} each): "
             f"{wk['open_live']} open, P&L ₹{wk['unrealised']:,.0f} · {wk['closed']} closed, booked ₹{wk['realised']:,.0f}"
             + (f", win rate {wk['win_rate_pct']}%, avg net {wk['avg_net_pct']:+.2f}%, vs NIFTY {wk['avg_vs_nifty_pct']:+.2f}%"
                if wk["closed"] else ""))
    L.append(f"F&O list: {fno_src}")
    if ev["revised"]:
        L.append("Last week's signal no longer shows on corrected (split-adjusted) prices, not acted on: "
                 + ", ".join(ev["revised"]))
    if ev["missing"]:
        L.append("⚠️ No price data for: " + ", ".join(sorted(set(ev["missing"]))))
    return "\n".join(L)


# ------------------------------------------------------------------ Google Sheet
TABS = {
    "HM_Trades": ["Trade id", "Symbol", "Status", "Source", "NIFTY above 50-DMA at signal", "Buy signal week",
                  "Buy signal close", "Buy RSI", "3M gain % at signal", "Entry date (next open)", "Entry price",
                  "Sell signal week", "Sell signal close", "Sell RSI", "Exit date (next open)", "Exit price", "Last price",
                  "Return %", "Net return % (after 0.18%/side)", "NIFTY same period %", "Net vs NIFTY %",
                  "P&L ₹ per signal", "Weeks held"],
    "HM_Signals": ["Alert time (IST)", "Signal week", "Signal", "Symbol", "Weekly close", "RSI(9)", "Green EMA3",
                   "Red WMA21", "3M gain %", "Note"],
    "HM_Weekly": ["Week ending", "Alert time (IST)", "NIFTY", "NIFTY 50-DMA", "NIFTY filter", "Buy signals",
                  "Sell signals", "Open signals (all)", "Open live signals", "Invested ₹ (live)", "Open P&L ₹ (live)",
                  "Booked P&L ₹ (live)", "Total P&L ₹ (live)", "Closed live signals", "Win rate %", "Avg net return %", "Avg net vs NIFTY %", "Avg open return %",
                  "Closed (filter on)", "Win rate % (filter on)", "Avg net % (filter on)"],
}
T_KEYS = ["id", "sym", "status", "origin", "nifty_ok", "buy_signal_week", "buy_signal_close", "buy_rsi", "roc63_pct",
          "entry_date", "entry_px", "sell_signal_week", "sell_signal_close", "sell_rsi", "exit_date", "exit_px", "last_px",
          "ret_pct", "net_ret_pct", "nifty_ret_pct", "vs_nifty_pct", "pnl", "weeks_held"]
S_KEYS = ["alert_time", "signal_week", "signal", "sym", "close", "rsi", "green", "red", "roc63_pct", "note"]
W_KEYS = ["week", "alert_time", "nifty", "nifty_50dma", "nifty_filter", "buy_signals", "sell_signals", "open_signals",
          "open_live", "invested", "unrealised", "realised", "total_pnl", "closed", "win_rate_pct", "avg_net_pct", "avg_vs_nifty_pct",
          "avg_open_pct", "rule_closed", "rule_win_rate_pct", "rule_avg_net_pct"]


def sheet_rows(st):
    def rows(items, keys):
        return [["" if it.get(k) is None else it.get(k) for k in keys] for it in items]
    trades = sorted(st["trades"], key=lambda t: (t["status"] != "OPEN", t["entry_date"]), reverse=False)
    open_t = [t for t in trades if t["status"] == "OPEN"]
    closed_t = sorted([t for t in trades if t["status"] != "OPEN"], key=lambda t: t["exit_date"] or "", reverse=True)
    pend = [{"id": "", "sym": o["sym"], "status": "BUY AT NEXT OPEN", "origin": o.get("origin", "live"),
             "nifty_ok": o.get("nifty_ok"), "buy_signal_week": o["signal_week"],
             "buy_signal_close": o["close"], "buy_rsi": o["rsi"], "roc63_pct": o["roc63_pct"]} for o in st["pending_buys"]]
    open_rows = []
    for t in open_t:
        t2 = dict(t)
        if any(o["sym"] == t["sym"] for o in st["pending_sells"]):
            o = next(o for o in st["pending_sells"] if o["sym"] == t["sym"])
            t2.update(status="SELL AT NEXT OPEN", sell_signal_week=o["signal_week"], sell_signal_close=o["close"], sell_rsi=o["rsi"])
        open_rows.append(t2)
    return {
        "HM_Trades": [TABS["HM_Trades"]] + rows(pend + open_rows + closed_t, T_KEYS),
        "HM_Signals": [TABS["HM_Signals"]] + rows(list(reversed(st["signals"])), S_KEYS),
        "HM_Weekly": [TABS["HM_Weekly"]] + rows(list(reversed(st["weekly"])), W_KEYS),
    }


def write_sheet(st):
    sid, sa = os.environ.get("GSHEET_ID", "").strip(), os.environ.get("GCP_SA_FILE", "").strip()
    if not (sid and sa and os.path.exists(sa)):
        log("Google Sheet not configured (GSHEET_ID / GCP_SA_FILE): skipped")
        return None
    import gspread
    sh = gspread.service_account(filename=sa).open_by_key(sid)
    have = {ws.title: ws for ws in sh.worksheets()}
    for name, vals in sheet_rows(st).items():
        ws = have.get(name) or sh.add_worksheet(title=name, rows=max(200, len(vals) + 50), cols=len(vals[0]) + 2)
        if ws.row_count < len(vals) + 10:
            ws.add_rows(len(vals) + 50 - ws.row_count)
        ws.batch_clear(["A1:Z"])
        ws.update(range_name="A1", values=vals, value_input_option="RAW")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    return True


# ------------------------------------------------------------------ main
def main():
    path = os.environ.get("HM_STATE", "hm_state.json")
    dry = os.environ.get("HM_DRY_RUN", "0") == "1"
    force = os.environ.get("HM_FORCE", "0") == "1"
    now = dt.datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    st = new_state()
    if os.path.exists(path):
        with open(path) as f:
            st = json.load(f)
    fno, fno_src = fetch_fno_list(st)
    held = set(st["positions"]) | {o["sym"] for o in st["pending_buys"]}
    symbols = sorted(set(fno) | held)
    log(f"{now_str} IST · {len(fno)} F&O stocks ({fno_src}) · {len(held)} held/pending")
    O, C, V, nifty = load_prices(symbols)
    wend = last_finished_week_end(now, list(C.index))
    if wend is None:
        log("no finished week in the data")
        return 1
    log(f"latest finished week ends {d2s(wend)}; prices for {C.shape[1]} of {len(symbols)} stocks")
    if force and st.get("last_week") == d2s(wend) and st.get("prev"):
        log("HM_FORCE: redoing that week from the saved snapshot")
        st = st["prev"]
    W = Weekly(O, C, V, nifty, wend)
    todo = [w for w in W.weeks if st["last_week"] is None and w == W.weeks[-1]
            or st["last_week"] is not None and d2s(w) > st["last_week"]]
    if not todo:
        log(f"week ending {d2s(wend)} was already sent ({st['last_week']}); nothing to do (HM_FORCE=1 to resend)")
        return 0
    sent = []
    for w in todo[-4:]:                      # catch up at most 4 missed weeks
        prev = copy.deepcopy({k: v for k, v in st.items() if k != "prev"})
        ev = process_week(st, W, w, fno, now_str)
        st["prev"] = prev
        msg = message(st, ev, W, w, fno_src)
        log("\n" + msg + "\n")
        sent.append((msg, bool(ev["buys"] or ev["sells"])))
    st["fno_list"], st["fno_list_date"] = (fno, now.strftime("%Y-%m-%d")) if fno_src.startswith("NSE") else (st["fno_list"], st["fno_list_date"])
    if dry:
        log("HM_DRY_RUN: no Telegram, no sheet, state not saved")
        return 0
    sheet_note = ""
    try:
        write_sheet(st)
    except Exception as e:
        log(f"Google Sheet write failed: {e}")
        sheet_note = f"\n⚠️ Google Sheet not updated: {str(e)[:150]}"
    import notify
    for i, (msg, loud) in enumerate(sent):
        notify.send("", msg + (sheet_note if i == len(sent) - 1 else ""), loud=loud, retry=True)
    with open(path, "w") as f:
        json.dump(st, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
