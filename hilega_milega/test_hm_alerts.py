"""Offline tests for hm_alerts.py (no NSE, Yahoo, Telegram or Google access).

    python3 hilega_milega/test_hm_alerts.py
"""
import datetime as dt
import os
import sys
import types

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hm_alerts as H  # noqa: E402


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        check.failed = True


check.failed = False


def test_mktlots():
    txt = ("UNDERLYING ,SYMBOL ,DEC-26\nNIFTY 50 ,NIFTY ,65\nNIFTY FPI ,NIFTYFPI ,1\n"
           "Derivatives on Individual Securities,Symbol ,DEC-26\nRELIANCE INDUSTRIES ,RELIANCE ,500\n"
           "MAHINDRA ,M&M ,200\n")
    check(H.parse_mktlots(txt) == ["RELIANCE", "M&M"], "F&O list keeps stocks, drops index rows")


def test_week_end():
    days = list(pd.bdate_range("2026-11-16", "2026-11-27"))
    f = lambda y, m, d, hh, mm: H.last_finished_week_end(dt.datetime(y, m, d, hh, mm, tzinfo=H.IST), days)
    check(f(2026, 11, 27, 16, 45) == pd.Timestamp("2026-11-27"), "Friday 16:45 uses this week")
    check(f(2026, 11, 27, 14, 0) == pd.Timestamp("2026-11-20"), "Friday 14:00 uses last week")
    check(f(2026, 11, 28, 9, 0) == pd.Timestamp("2026-11-27"), "Saturday uses the week just ended")
    days2 = [d for d in days if d != pd.Timestamp("2026-11-27")]   # Friday holiday
    check(H.last_finished_week_end(dt.datetime(2026, 11, 28, 9, 0, tzinfo=H.IST), days2) == pd.Timestamp("2026-11-26"),
          "Friday holiday: week ends Thursday")


def synthetic():
    """Two stocks over 3 years: UP trends up from week 100, then falls hard; FLAT stays flat."""
    idx = pd.bdate_range("2023-01-02", periods=780)
    n = len(idx)
    rng = np.random.default_rng(1)
    up = 100 * np.cumprod(1 + rng.normal(0, 0.004, n))
    up[500:640] *= np.linspace(1, 1.6, 140)
    up[640:] *= np.linspace(1.6, 1.0, n - 640) * 1.0
    up[640:] = up[639] * np.linspace(1, 0.7, n - 640)
    flat = 100 * np.cumprod(1 + rng.normal(0, 0.004, n))
    fr = {s: pd.DataFrame({"open": c, "close": c, "volume": 1e7}, index=idx) for s, c in (("UP", up), ("FLAT", flat))}
    nifty = pd.Series(np.linspace(20000, 26000, n), index=idx)
    return H.pivots(fr, nifty)


def test_process():
    O, C, V, N = synthetic()
    W = H.Weekly(O, C, V, N, C.index[-1])
    st = H.new_state()
    bought = sold = None
    for w in W.weeks[60:]:
        ev = H.process_week(st, W, w, ["UP", "FLAT"], "t")
        if "UP" in [f["sym"] for f in ev["buys"]] and bought is None:
            bought = w
        if "UP" in [f["sym"] for f in ev["sells"]] and bought is not None and sold is None:
            sold = w
    check(bought is not None, f"UP gets a BUY signal ({bought})")
    check(sold is not None and sold > bought, f"UP gets a SELL signal later ({sold})")
    t = [t for t in st["trades"] if t["sym"] == "UP"]
    check(t and t[0]["status"] == "CLOSED" and t[0]["entry_px"] and t[0]["exit_px"], "trade closed with entry and exit prices")
    check(abs(st["weekly"][-1]["nav"] - (st["cash"] + sum(
        p["units"] * W.close_at(W.weeks[-1], s) for s, p in st["positions"].items()))) < 2, "model value = cash + holdings")
    rows = H.sheet_rows(st)
    check(rows["HM_Trades"][0][:3] == ["Trade id", "Symbol", "Status"] and len(rows["HM_Weekly"]) == len(st["weekly"]) + 1,
          "sheet tabs have headers and one weekly row per week")
    check(all(len(r) == len(rows["HM_Trades"][0]) for r in rows["HM_Trades"]), "trade rows match the header width")


def test_main_dry(tmp="/tmp/hm_test_state.json"):
    O, C, V, N = synthetic()
    H.fetch_fno_list = lambda st: (["UP", "FLAT"], "NSE (test)")
    H.load_prices = lambda syms: (O, C, V, N)
    if os.path.exists(tmp):
        os.remove(tmp)
    sent = []
    sys.modules["notify"] = types.SimpleNamespace(send=lambda tag, msg, **k: sent.append(msg) or True)
    os.environ.update(HM_STATE=tmp, HM_DRY_RUN="0", HM_FORCE="0", GSHEET_ID="", GCP_SA_FILE="")
    check(H.main() == 0 and len(sent) == 1 and "Hilega-Milega weekly" in sent[0], "first run sends one alert")
    check(H.main() == 0 and len(sent) == 1, "second run for the same week sends nothing")
    os.environ["HM_FORCE"] = "1"
    check(H.main() == 0 and len(sent) == 2, "resend sends the week again")
    os.environ["HM_FORCE"] = "0"
    os.remove(tmp)


if __name__ == "__main__":
    test_mktlots()
    test_week_end()
    test_process()
    test_main_dry()
    print("FAILED" if check.failed else "all passed")
    sys.exit(1 if check.failed else 0)
