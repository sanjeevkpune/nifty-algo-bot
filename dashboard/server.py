#!/usr/bin/env python3
"""Strategy Desk: a small password-protected dashboard for every bot in this repo.

It reads the Google Sheets the bots already log to (with the same service account) and serves
one page on port 8080. Nothing secret lives in the repo:
  * sheet ids come from /etc/trade-config/config.env (GSHEET_ID_V2) and etf_dukan.env (EKD_GSHEET_ID)
  * the login comes from /etc/trade-config/dashboard.env, written by setup_dashboard.sh
  * Tradetron and ETF backtest summaries are tabs in the V2 sheet ("Tradetron Backtests",
    "Tradetron v4 Monthly", "ETF Backtest"), so the VM never needs a Tradetron login.

Run:  python3 server.py            (uses the files above)
      DASH_DEMO=1 python3 server.py  (no Google access; serves the page with sample numbers)
"""
import base64
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CACHE_SECS = int(os.environ.get("DASH_CACHE_SECS", "60"))
DEFAULT_ETF_SHEET = "1ZEWLugBHiPSsX5letLn_u7zU3dj06u9brASqJM1uo_k"


def read_env(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


CFG = {}
for p in ("/etc/trade-config/config.env", "/etc/trade-config/etf_dukan.env", "/etc/trade-config/dashboard.env"):
    CFG.update(read_env(p))
CFG.update({k: v for k, v in os.environ.items() if k.startswith(("DASH_", "GSHEET_", "EKD_"))})

PORT = int(CFG.get("DASH_PORT", "8080"))
USER = CFG.get("DASH_USER", "admin")
PASSWORD = CFG.get("DASH_PASSWORD", "")
DEMO = CFG.get("DASH_DEMO") == "1"
V2_SHEET = CFG.get("GSHEET_ID_V2", "")
ETF_SHEET = CFG.get("EKD_GSHEET_ID", DEFAULT_ETF_SHEET)
SA_FILE = CFG.get("EKD_SERVICE_ACCOUNT_FILE", os.path.join(REPO, "service_account.json"))

# ---------------------------------------------------------------- reading the sheets
NUM_RE = re.compile(r"^-?\s*₹?\s*-?[\d,]+(\.\d+)?\s*%?$")


def cell(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-"):
        return None
    if NUM_RE.match(s):
        try:
            return float(re.sub(r"[₹,%\s]", "", s))
        except ValueError:
            return s
    return s


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


def to_rows(values):
    """First row names the columns; drop empty rows; type numbers."""
    if not values:
        return []
    cols, seen = [], set()
    for i, h in enumerate(values[0]):
        k = slug(h) or f"col_{i + 1}"
        if k in seen:
            k = f"{k}_{i}"
        seen.add(k)
        cols.append(k)
    rows = []
    for r in values[1:]:
        if not any(str(c).strip() for c in r):
            continue
        rows.append({k: cell(r[i] if i < len(r) else None) for i, k in enumerate(cols)})
    return rows


CAPITAL_KEYS = [("Total capital added", "total_capital_added"), ("Total withdrawn", "total_withdrawn"),
                ("Realised net profit", "realised_net_profit"), ("Current capital", "current_capital"),
                ("Invested in open", "invested_open"), ("Held in Core", "held_in_core"),
                ("Free cash for buys", "free_cash_for_buys"), ("Free cash", "free_cash"),
                ("New buys", "new_buys_fundable"), ("Open positions", "open_positions"),
                ("Unrealised", "unrealised_pl"), ("Net worth", "net_worth"),
                ("Return on capital", "return_on_capital"), ("Withdrawal owed", "withdrawal_owed")]


def capital_row(values):
    row = {}
    for r in values or []:
        label = str(r[0]) if r else ""
        for prefix, key in CAPITAL_KEYS:
            if label.startswith(prefix) and key not in row:
                row[key] = cell(r[1] if len(r) > 1 else None)
                break
    return row


RANGES = {  # name: (sheet, range)
    "etf_holdings": ("etf", "Holdings!A4:R2000"),
    "etf_botlog": ("etf", "'Bot Log'!A1:P3000"),
    "etf_sold": ("etf", "Sold!A4:T2000"),
    "etf_capital": ("etf", "Capital!A3:B16"),
    "adaptive_log": ("v2", "Sheet1!A1:R3000"),
    "v3_legs": ("v2", "Lemonn_Dual_v3!A1:L5000"),
    "v3_sessions": ("v2", "Lemonn_Log!A1:L2000"),
    "v4_daily": ("v2", "Lemonn_v4_Daily!A1:U3000"),
    "tt_backtests": ("v2", "'Tradetron Backtests'!A1:M100"),
    "tt_v4_monthly": ("v2", "'Tradetron v4 Monthly'!A1:C200"),
    "etf_bt": ("v2", "'ETF Backtest'!A1:E30"),
    "hm_trades": ("v2", "HM_Trades!A1:T3000"),
    "hm_signals": ("v2", "HM_Signals!A1:J400"),
    "hm_weekly": ("v2", "HM_Weekly!A1:P1000"),
}


def fetch_raw():
    import gspread  # installed on the VM by startup.sh
    gc = gspread.service_account(filename=SA_FILE)
    out, errors = {}, {}
    for sid_key, sid in (("etf", ETF_SHEET), ("v2", V2_SHEET)):
        names = [n for n, (k, _) in RANGES.items() if k == sid_key]
        if not sid:
            for n in names:
                errors[n] = "sheet id not configured"
            continue
        try:
            sh = gc.open_by_key(sid)
            res = sh.values_batch_get([RANGES[n][1] for n in names])
            for n, vr in zip(names, res.get("valueRanges", [])):
                out[n] = vr.get("values", [])
        except Exception as e:  # one missing tab fails the whole batch: retry one by one
            for n in names:
                try:
                    out[n] = gc.open_by_key(sid).values_get(RANGES[n][1]).get("values", [])
                except Exception as e2:
                    errors[n] = str(e2)[:200]
    return out, errors


# ---------------------------------------------------------------- the numbers
def by_date_sum(pairs):
    d = {}
    for date, v in pairs:
        d[date] = d.get(date, 0.0) + v
    cum, rows = 0.0, []
    for date in sorted(d):
        cum += d[date]
        rows.append({"date": date, "pnl": round(d[date], 2), "cum": round(cum, 2)})
    return rows


def num(v):
    return v if isinstance(v, (int, float)) else 0.0


def build(raw, errors):
    R = {k: to_rows(v) for k, v in raw.items() if k != "etf_capital"}
    cap = capital_row(raw.get("etf_capital"))
    botlog = R.get("etf_botlog", [])

    nav = {}
    for r in botlog:
        if r.get("event") == "DAY DONE":
            m = re.search(r"net worth (-?\d+(?:\.\d+)?)", str(r.get("note") or ""))
            if m:
                nav[str(r.get("date"))] = float(m.group(1))
    etf_nav = [{"date": d, "net_worth": nav[d]} for d in sorted(nav)]
    trade_days = {str(r.get("date")) for r in botlog if r.get("event") in ("NEW", "AVG", "SELL", "CORE 3X")}
    etf_mode = str((botlog[-1] if botlog else {}).get("mode") or "paper").lower()

    v4 = sorted(R.get("v4_daily", []), key=lambda r: str(r.get("date")))
    v4_days = by_date_sum((str(r.get("date")), num(r.get("day_p_l_before_charges"))) for r in v4)
    v4_traded = [r for r in v4 if r.get("decision") and not str(r["decision"]).upper().startswith("SKIP")]

    legs, sess = R.get("v3_legs", []), R.get("v3_sessions", [])
    v3_days = by_date_sum([(str(r.get("date")), r["leg_pnl"]) for r in legs if isinstance(r.get("leg_pnl"), float)] +
                          [(str(r.get("date")), r["session_pnl"]) for r in sess if isinstance(r.get("session_pnl"), float)])

    ad = R.get("adaptive_log", [])
    ad_days = by_date_sum((str(r.get("date")), num(r.get("net_pnl"))) for r in ad if r.get("session_status") == "TRADED")
    status_by_day = {}
    for r in ad:
        status_by_day.setdefault(str(r.get("date")), set()).add(r.get("session_status"))
    last_day = max(status_by_day) if status_by_day else None
    open_now = (f"Open since {last_day}" if last_day and "ENTERED" in status_by_day[last_day]
                and "TRADED" not in status_by_day[last_day] else "Flat")
    skipped = sum(1 for s in status_by_day.values() if s == {"SKIPPED"})

    def summ(days, mode, last):
        return {"pnl": round(sum(d["pnl"] for d in days), 2), "trade_days": len(days),
                "wins": sum(1 for d in days if d["pnl"] > 0), "mode": mode, "last": last}

    last_v4 = v4[-1] if v4 else {}
    strategies = [
        {"id": "etf", "name": "ETF ki Dukan", "broker": "AngelOne", "basis": "realised + unrealised",
         "mode": "Live" if etf_mode == "live" else "Paper",
         "pnl": round(num(cap.get("realised_net_profit")) + num(cap.get("unrealised_pl")), 2),
         "trade_days": len(trade_days), "wins": None, "last": str(botlog[-1].get("date")) if botlog else ""},
        dict(id="v4", name="Lemonn v4 straddle", broker="Dhan", basis="before charges",
             **summ(v4_days, "Live" if str(last_v4.get("paper", "TRUE")).upper() == "FALSE" else "Paper",
                    str(last_v4.get("date") or "")), days_logged=len(v4), traded=len(v4_traded),
             last_decision=str(last_v4.get("decision") or "none yet")),
        dict(id="v3", name="Lemonn v3 dual session", broker="Dhan", basis="before charges",
             **summ(v3_days, "Live" if legs and str(legs[-1].get("paper")).upper() == "FALSE" else "Paper",
                    v3_days[-1]["date"] if v3_days else "")),
        dict(id="adaptive", name="Adaptive strangle", broker="AngelOne", basis="net of charges",
             **summ(ad_days, "Live" if ad and "LIVE" in str(ad[-1].get("strategy_version", "")).upper() else "Paper",
                    last_day or ""), open_now=open_now, skipped=skipped),
    ]

    etf_bt_vals = raw.get("etf_bt", [])
    etf_bt_summary = {}
    for r in etf_bt_vals[1:]:
        if not r or not str(r[0]).strip():
            break
        etf_bt_summary[str(r[0])] = cell(r[1] if len(r) > 1 else None)
    yr_at = next((i for i, r in enumerate(etf_bt_vals) if r and r[0] == "Year"), None)
    etf_bt_yearly = to_rows(etf_bt_vals[yr_at:]) if yr_at is not None else []
    for r in etf_bt_yearly:
        if isinstance(r.get("year"), float):
            r["year"] = str(int(r["year"]))

    # Hilega-Milega weekly alerts (hilega_milega/hm_alerts.py on GitHub Actions): every signal, no cap
    hm_t, hm_w = R.get("hm_trades", []), R.get("hm_weekly", [])
    hm_open = [r for r in hm_t if r.get("status") != "CLOSED"]
    hm_closed = [r for r in hm_t if r.get("status") == "CLOSED"]
    last_w = hm_w[0] if hm_w else {}
    hm_curve = sorted(({"date": str(r.get("week_ending")), "total": r.get("total_p_l_live"), "booked": r.get("booked_p_l_live")}
                       for r in hm_w if isinstance(r.get("total_p_l_live"), float)), key=lambda r: r["date"])
    hm = {"summary": {"week": str(last_w.get("week_ending") or ""), "nifty_filter": last_w.get("nifty_filter"),
                      "open_all": last_w.get("open_signals_all"), "open_live": last_w.get("open_live_signals"),
                      "closed_live": last_w.get("closed_live_signals"), "win_rate": last_w.get("win_rate"),
                      "avg_net": last_w.get("avg_net_return"), "vs_nifty": last_w.get("avg_net_vs_nifty"),
                      "open_pnl": last_w.get("open_p_l_live"), "booked_pnl": last_w.get("booked_p_l_live"),
                      "buys": last_w.get("buy_signals"), "sells": last_w.get("sell_signals")},
          "curve": hm_curve, "open": hm_open, "closed": hm_closed, "signals": R.get("hm_signals", [])[:200]}

    return {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "errors": errors,
        "total_pnl": round(sum(s["pnl"] for s in strategies), 2),
        "strategies": strategies,
        "etf": {"capital": cap, "nav": etf_nav,
                "open": [r for r in R.get("etf_holdings", []) if r.get("status") == "OPEN"],
                "botlog": list(reversed(botlog))[:80], "sold": list(reversed(R.get("etf_sold", [])))},
        "v4": {"days": v4_days, "log": list(reversed(v4))},
        "v3": {"days": v3_days, "legs": list(reversed(legs)), "sessions": list(reversed(sess))},
        "adaptive": {"days": ad_days, "log": list(reversed(ad))},
        "hm": hm,
        "backtests": {"tradetron": R.get("tt_backtests", []), "v4_monthly": R.get("tt_v4_monthly", []),
                      "etf_summary": etf_bt_summary, "etf_yearly": etf_bt_yearly},
    }


def demo_raw():
    return {
        "etf_capital": [["Total capital added", "₹4,000,000"], ["Realised net profit", "-"], ["Free cash", "₹3,920,235"],
                        ["Open positions / max", "2 / 30"], ["Unrealised P/L (open + core)", "₹130"],
                        ["Net worth (capital + unrealised)", "₹4,000,130"]],
        "etf_holdings": [["Buy date", "NSE symbol", "Type", "Qty", "Buy price", "Status", "CMP", "Current value", "Lot P/L %", "row id"],
                         ["25-Nov-2026", "SAMPLE1", "ETF", "288", "₹138.52", "OPEN", "₹138.58", "₹39,911", "0.0%", "5"]],
        "etf_botlog": [["Date", "Time", "Mode", "Event", "Symbol", "Qty", "Price", "Value", "Net P&L", "Note"],
                       ["2026-11-25", "15:10:07", "paper", "DAY DONE", "", "", "", "", "0", "cash 1; net worth 4000000"]],
        "v4_daily": [["Date", "Decision", "Day P&L (before charges)", "Paper"], ["2026-11-25", "SKIP: gap", "0", "TRUE"]],
        "v3_legs": [["Date", "Session", "Symbol", "Leg_PnL", "Paper"], ["2026-11-23", "AFTERNOON_ATM", "SAMPLE", "-100", "TRUE"]],
        "adaptive_log": [["Date", "Timestamp", "Strategy_Version", "Session_Status", "Net_PnL"],
                         ["2026-11-24", "15:20:00", "SAMPLE LIVE", "TRADED", "500"]],
        "hm_trades": [["Trade id", "Symbol", "Status", "Source", "NIFTY above 50-DMA at signal", "Buy signal week",
                       "Entry date (next open)", "Entry price", "Last price", "Return %", "Net return % (after 0.18%/side)",
                       "NIFTY same period %", "Net vs NIFTY %", "P&L ₹ per signal", "Weeks held"],
                      ["", "SAMPLE2", "BUY AT NEXT OPEN", "live", "Y", "2026-11-27", "", "", "", "", "", "", "", "", ""],
                      ["SAMPLE1-2026-11-06", "SAMPLE1", "OPEN", "backtest seed", "Y", "2026-11-06", "2026-11-09", "100", "104",
                       "4", "", "1.2", "", "3800", "3"]],
        "hm_signals": [["Alert time (IST)", "Signal week", "Signal", "Symbol", "Weekly close", "RSI(9)"],
                       ["2026-11-27 16:45", "2026-11-27", "BUY", "SAMPLE2", "250", "61.2"]],
        "hm_weekly": [["Week ending", "NIFTY filter", "Buy signals", "Sell signals", "Open signals (all)", "Open live signals",
                       "Open P&L ₹ (live)", "Booked P&L ₹ (live)", "Total P&L ₹ (live)", "Closed live signals"],
                      ["2026-12-04", "ON", "9", "3", "140", "9", "12000", "0", "12000", "0"],
                      ["2026-11-27", "ON", "8", "12", "138", "0", "0", "0", "0", "0"]],
    }


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def get_data():
    with _lock:
        if _cache["data"] is None or time.time() - _cache["at"] > CACHE_SECS:
            if DEMO:
                raw, errors = demo_raw(), {}
            else:
                raw, errors = fetch_raw()
            _cache["data"] = build(raw, errors)
            _cache["at"] = time.time()
        return _cache["data"]


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    server_version = "StrategyDesk/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def authed(self):
        if not PASSWORD:
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASSWORD)

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if not self.authed():
            time.sleep(1)  # slows password guessing
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Strategy Desk", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/data":
            try:
                body = json.dumps(get_data()).encode()
                return self.send(200, body, "application/json")
            except Exception as e:
                return self.send(500, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
        self.send(404, b"not found", "text/plain")


if __name__ == "__main__":
    if not PASSWORD:
        raise SystemExit("DASH_PASSWORD is not set (setup_dashboard.sh writes /etc/trade-config/dashboard.env).")
    print(f"Strategy Desk on port {PORT} (demo={DEMO})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
