#!/usr/bin/env python3
"""
test_lemonn_dhan_connectivity.py
Pre-flight checks for lemonn_dual_session_final_dhan_v3.py against your real
Dhan account. READ-ONLY by default: nothing is bought or sold.

Usage
    python3 test_lemonn_dhan_connectivity.py                 # read-only checks, NIFTY + SENSEX
    python3 test_lemonn_dhan_connectivity.py --index NIFTY   # one index only
    python3 test_lemonn_dhan_connectivity.py --bot /path/lemonn_dual_session_final_dhan_v3.py
    python3 test_lemonn_dhan_connectivity.py --order-test    # ALSO places + cancels 2 test orders

--order-test (market hours only, asks you to type YES first):
    1. LIMIT BUY 1 lot of an OTM option at ₹0.05 (far below market, so it
       cannot fill), modifies it to ₹0.10, then cancels it.
    2. STOP_LOSS BUY 1 lot with the trigger at 3x the current price (cannot
       trigger), then cancels it. This proves Dhan accepts the resting
       stop-loss orders the bot relies on.
    Both are cancelled straight away and are not expected to fill. If one
    ever did, it would be a small long option position (bought, not sold) -
    the script tells you and you should close it in Dhan.

What is checked
    Config & environment : credentials present, state folder writable, holiday
                           calendar, Telegram, Google Sheet (optional)
    Account              : login/TOTP, fund limits, positions, order book,
                           order-status lookup on an unknown id
    Per index            : spot LTP + latency, spot OHLC (gap filter),
                           scrip master (symbol format, expiry, lot size),
                           option chain LTPs around ATM, bid/ask spread,
                           morning premium-matched strikes, afternoon
                           synthetic-ATM strike, stop prices, protection strikes
"""
import argparse
import importlib.util
import os
import sys
import tempfile
import time
import traceback

RESULTS = []  # (status, area, check, detail)


def rec(status, area, check, detail=""):
    RESULTS.append((status, area, check, detail))
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}[status]
    print(f"{icon} [{area}] {check}" + (f" — {detail}" if detail else ""), flush=True)


def safe(area, check, fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        rec("FAIL", area, check, f"{type(e).__name__}: {e}")
        if os.environ.get("TEST_DEBUG"):
            traceback.print_exc()
        return None


def load_bot(path, index):
    os.environ["LEMONN_FORCE_INDEX"] = index
    name = f"lemonn_bot_{index.lower()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# Environment / account checks
# ----------------------------------------------------------------------
def check_environment(m):
    area = "ENV"
    for k in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        rec("PASS" if os.environ.get(k) else "FAIL", area, f"{k} set",
            "" if os.environ.get(k) else "add it to /etc/trade-config/config.env")
    rec("INFO", area, "Paper mode", f"PAPER_MODE={m.PAPER_MODE} (forced on until {m.PAPER_MODE_END_DATE})")
    rec("INFO", area, "Settings", f"lots={m.NUM_LOTS}, hedge={'ON' if m.HEDGE_ENABLED else 'OFF'}, "
        f"exchange SL={'ON' if m.USE_EXCHANGE_SL else 'OFF'}, hard stop ₹{m.DAILY_HARD_STOP:,.0f}")

    try:
        os.makedirs(m.STATE_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=m.STATE_DIR, delete=True) as fh:
            fh.write(b"ok")
        rec("PASS", area, "State folder writable", m.STATE_DIR)
    except Exception as e:
        rec("FAIL", area, "State folder writable", f"{m.STATE_DIR}: {e} (set LEMONN_STATE_DIR)")

    try:
        import pandas_market_calendars as mcal
        for cal in ("NSE", "BSE"):
            sched = mcal.get_calendar(cal).schedule(start_date=str(m.now_ist().date()),
                                                    end_date=str(m.now_ist().date()))
            rec("PASS", area, f"{cal} holiday calendar", "trading day" if not sched.empty else "HOLIDAY today")
    except Exception as e:
        rec("WARN", area, "Holiday calendar", f"pandas_market_calendars not available ({e}); the bot's holiday check will be skipped")

    if m.TELEGRAM_BOT_TOKEN and m.TELEGRAM_CHAT_ID:
        try:
            r = m.requests.post(f"https://api.telegram.org/bot{m.TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": m.TELEGRAM_CHAT_ID, "text": "🧪 Lemonn connectivity test"},
                                timeout=8)
            rec("PASS" if r.ok else "FAIL", area, "Telegram message", f"HTTP {r.status_code}")
        except Exception as e:
            rec("FAIL", area, "Telegram message", str(e))
    else:
        rec("WARN", area, "Telegram", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - no alerts")

    if m.GSHEET_ID_V2:
        if not m.gspread or not os.path.exists(m.SERVICE_ACCOUNT_FILE):
            rec("WARN", area, "Google Sheet", "gspread or service account file missing")
        else:
            try:
                sh = m.gspread.service_account(filename=m.SERVICE_ACCOUNT_FILE).open_by_key(m.GSHEET_ID_V2)
                rec("PASS", area, "Google Sheet", f"opened '{sh.title}'")
            except Exception as e:
                rec("FAIL", area, "Google Sheet", str(e))
    else:
        rec("INFO", area, "Google Sheet", "GSHEET_ID_V2 not set (logging off)")


def check_account(bot):
    area = "ACCOUNT"
    t0 = time.time()
    safe(area, "Login (PIN + TOTP)", bot.login)
    if not bot.dhan:
        rec("FAIL", area, "Login", "cannot continue without a session")
        return False
    rec("PASS", area, "Login (PIN + TOTP)", f"{time.time() - t0:.1f}s")

    fl = safe(area, "Fund limits", bot.dhan.get_fund_limits)
    if isinstance(fl, dict) and fl.get("status") == "success":
        d = fl.get("data") or {}
        bal = d.get("availabelBalance", d.get("availableBalance"))
        rec("PASS", area, "Fund limits", f"available ₹{float(bal or 0):,.0f}, "
            f"utilised ₹{float(d.get('utilizedAmount') or 0):,.0f}")
        if bal is not None and float(bal) < 900000:
            rec("WARN", area, "Margin", f"₹{float(bal):,.0f} free; Tradetron's estimate for this 5-lot book was "
                "~₹9.4L - live 5-lot orders are likely to be rejected for margin")
    elif fl is not None:
        rec("FAIL", area, "Fund limits", str(fl)[:200])

    pos = safe(area, "Positions", bot.dhan.get_positions)
    if isinstance(pos, dict):
        rows = pos.get("data") or []
        open_rows = [r for r in rows if int(r.get("netQty") or 0) != 0]
        rec("PASS", area, "Positions", f"{len(rows)} rows, {len(open_rows)} open")
        for r in open_rows[:10]:
            rec("INFO", area, "Open position", f"{r.get('tradingSymbol')} net {r.get('netQty')}")

    ob = safe(area, "Order book", bot.dhan.get_order_list)
    if isinstance(ob, dict):
        rows = ob.get("data") or []
        rec("PASS" if ob.get("status") == "success" else "WARN", area, "Order book",
            f"{len(rows)} orders today")

    st = safe(area, "Order-status lookup (unknown id)", bot.order_status, "0000000000")
    if st is not None:
        rec("PASS", area, "Order-status lookup (unknown id)", f"handled cleanly -> {st}")
    return True


# ----------------------------------------------------------------------
# Per-index market data checks
# ----------------------------------------------------------------------
EXPECTED_LOT = {"NIFTY": 65, "SENSEX": 20}


def check_index(m, bot, index):
    area = index
    rec("INFO", area, "Profile", f"seg {m.FNO_SEG}, step {m.STRIKE_STEP}, ORB {m.MAX_ORB_PTS:.0f}, "
        f"morning exit {m.M_EXIT[0]}:{m.M_EXIT[1]:02d}, afternoon {m.A_ENTRY[0]}:{m.A_ENTRY[1]:02d}-"
        f"{m.A_ENTRY_LAST[0]}:{m.A_ENTRY_LAST[1]:02d}")

    t0 = time.time()
    spot = safe(area, "Spot LTP", bot.get_spot)
    if not spot:
        rec("FAIL", area, "Spot LTP", "0 / no data - check Dhan Data API subscription (market feed APIs need it)")
        return
    rec("PASS", area, "Spot LTP", f"{spot:,.2f} ({(time.time() - t0) * 1000:.0f} ms incl. throttle)")

    ohlc = safe(area, "Spot OHLC", bot.spot_ohlc) or {}
    if ohlc:
        o, c, h, l = (float(ohlc.get(k, 0) or 0) for k in ("open", "close", "high", "low"))
        detail = f"O {o:,.1f} H {h:,.1f} L {l:,.1f} prevC {c:,.1f}"
        if o > 0 and c > 0:
            gap = abs(o - c) / c * 100
            detail += f" | gap {gap:.2f}% -> {'SKIP day' if gap > m.MAX_GAP_PCT else 'OK'}"
        if h > 0 and l > 0:
            detail += f" | day range {h - l:.0f} pts"
        rec("PASS", area, "Spot OHLC (gap filter input)", detail)
    else:
        rec("FAIL", area, "Spot OHLC", "empty - gap filter will be bypassed")

    t0 = time.time()
    if safe(area, "Scrip master", bot.load_master) is None and bot.options_df is None:
        return
    df = bot.options_df
    rec("PASS", area, "Scrip master", f"{len(df)} contracts near spot, nearest expiry {bot.expiry}, "
        f"loaded in {time.time() - t0:.1f}s")
    sample = df["SEM_TRADING_SYMBOL"].iloc[0] if len(df) else ""
    rec("PASS" if sample.startswith(index + "-") else "FAIL", area, "Symbol format", sample)
    exp_lot = EXPECTED_LOT.get(index)
    rec("PASS" if bot.lot_size == exp_lot else "WARN", area, "Lot size",
        f"{bot.lot_size} (expected {exp_lot}); order qty = {m.NUM_LOTS * bot.lot_size}")
    days = (bot.expiry - m.now_ist().date()).days
    rec("INFO", area, "Expiry", f"{bot.expiry} ({days} day(s) away{' - EXPIRY DAY' if days == 0 else ''})")

    step = m.STRIKE_STEP
    atm = int(round(spot / step) * step)
    chain = {}
    for i in range(-4, 5):
        for opt in ("CE", "PE"):
            c = bot.find_contract(atm + i * step, opt)
            if c:
                chain[(atm + i * step, opt)] = c
    rec("PASS" if len(chain) == 18 else "WARN", area, "Option contracts ATM±4", f"{len(chain)}/18 found (ATM {atm})")
    if not chain:
        return
    t0 = time.time()
    ltp = safe(area, "Option LTP batch", bot.ltp_batch, m.FNO_SEG, [c["token"] for c in chain.values()]) or {}
    priced = {k: ltp.get(c["token"], 0) for k, c in chain.items() if ltp.get(c["token"], 0) > 0}
    rec("PASS" if len(priced) >= len(chain) - 2 else "FAIL", area, "Option LTP batch",
        f"{len(priced)}/{len(chain)} priced in one call ({(time.time() - t0) * 1000:.0f} ms)")
    for k in [(atm, "CE"), (atm, "PE"), (atm + 2 * step, "CE"), (atm - 2 * step, "PE")]:
        if k in priced:
            rec("INFO", area, f"LTP {k[0]} {k[1]}", f"₹{priced[k]:.2f}")

    # Bid/ask spread (quote endpoint) - how much slippage to expect
    atm_ce = chain.get((atm, "CE"))
    if atm_ce:
        q = safe(area, "Market depth (quote)", bot._feed, "quote", {m.FNO_SEG: [int(atm_ce["token"])]}) or {}
        info = q.get(m.FNO_SEG, {}).get(str(atm_ce["token"]), {})
        depth = info.get("depth", {}) if isinstance(info, dict) else {}
        try:
            bid = float(depth["buy"][0]["price"]); ask = float(depth["sell"][0]["price"])
            mid = (bid + ask) / 2
            rec("PASS", area, "ATM CE bid/ask", f"₹{bid:.2f} / ₹{ask:.2f} spread ₹{ask - bid:.2f} "
                f"({(ask - bid) / mid * 100:.2f}% of mid)" if mid > 0 else "zero mid")
        except Exception:
            rec("WARN", area, "ATM CE bid/ask", "depth not returned (normal outside market hours)")

    # Strategy strike logic
    ks = safe(area, "Morning strike selection", bot.select_strikes, spot, "MORNING_OTM2")
    if ks:
        ce_k, pe_k = ks
        lt = bot.ltp_batch(m.FNO_SEG, [c["token"] for c in (bot.find_contract(ce_k, "CE"),
                                                              bot.find_contract(pe_k, "PE")) if c])
        ce_c, pe_c = bot.find_contract(ce_k, "CE"), bot.find_contract(pe_k, "PE")
        cp = lt.get(ce_c["token"], 0) if ce_c else 0
        pp = lt.get(pe_c["token"], 0) if pe_c else 0
        rec("PASS", area, "Morning premium-matched strangle",
            f"SELL {ce_k} CE ₹{cp:.2f} (SL ₹{cp * m.SL_MULT:.2f}) + {pe_k} PE ₹{pp:.2f} (SL ₹{pp * m.SL_MULT:.2f}) "
            f"| OTM {(ce_k - atm) // step}/{(atm - pe_k) // step}")
        h1 = bot.find_contract(ce_k + m.HEDGE_WIDTH_PTS, "CE"); h2 = bot.find_contract(pe_k - m.HEDGE_WIDTH_PTS, "PE")
        rec("PASS" if (h1 and h2) else "WARN", area, "Protection strikes exist (if HEDGE_ENABLED)",
            f"{ce_k + m.HEDGE_WIDTH_PTS} CE / {pe_k - m.HEDGE_WIDTH_PTS} PE")
    ka = safe(area, "Afternoon synthetic ATM", bot.select_strikes, spot, "AFTERNOON_ATM")
    if ka:
        rec("PASS", area, "Afternoon synthetic-ATM straddle", f"strike {ka[0]} (spot ATM {atm})")


# ----------------------------------------------------------------------
# Optional live order test
# ----------------------------------------------------------------------
def order_test(m, bot, index):
    area = f"ORDERS {index}"
    now = m.now_ist()
    if not (now.weekday() < 5 and m.at_ist(now.date(), 9, 16) <= now <= m.at_ist(now.date(), 15, 20)):
        rec("WARN", area, "Order test", "skipped - run between 09:16 and 15:20 on a trading day")
        return
    print(f"\n⚠️  This will place and immediately cancel 2 REAL orders on {index} (1 lot each, BUY side).")
    if input("Type YES to continue: ").strip() != "YES":
        rec("INFO", area, "Order test", "cancelled by user")
        return
    spot = bot.get_spot()
    atm = int(round(spot / m.STRIKE_STEP) * m.STRIKE_STEP)
    c = bot.find_contract(atm + 4 * m.STRIKE_STEP, "CE")
    if not c:
        rec("FAIL", area, "Order test", "test contract not found"); return
    px = bot.ltp_batch(m.FNO_SEG, [c["token"]]).get(c["token"], 0)
    if px < 1.0:
        rec("WARN", area, "Order test", f"{c['symbol']} LTP ₹{px:.2f} too low for a safe non-filling test"); return
    qty = bot.lot_size
    d = bot.dhan

    # 1) far-away LIMIT BUY -> modify -> cancel
    res = d.place_order(security_id=c["token"], exchange_segment=m.FNO_SEG, transaction_type=d.BUY,
                        quantity=qty, order_type=d.LIMIT, product_type=d.INTRA, price=0.05)
    data = bot._data(res)
    if not data or not data.get("orderId"):
        rec("FAIL", area, "LIMIT order accepted", str(res)[:300])
    else:
        oid = str(data["orderId"]); time.sleep(1.5)
        st = bot.order_status(oid)
        rec("PASS" if st["status"] in m.OPEN_STATUSES else "WARN", area, "LIMIT order accepted",
            f"{c['symbol']} id {oid} status {st['status']}")
        ok = bot._modify(oid, d.LIMIT, qty, 0.10)
        rec("PASS" if ok else "FAIL", area, "Modify order", "price 0.05 -> 0.10")
        bot._cancel(oid); time.sleep(1.5)
        st = bot.order_status(oid)
        rec("PASS" if st["status"] == "CANCELLED" else "FAIL", area, "Cancel order", f"status {st['status']}")
        if st["filled"]:
            rec("FAIL", area, "UNEXPECTED FILL", f"{st['filled']} qty bought - close it in Dhan")

    # 2) resting STOP_LOSS BUY (trigger 3x LTP) -> cancel
    trig = m.tick(px * 3)
    res = d.place_order(security_id=c["token"], exchange_segment=m.FNO_SEG, transaction_type=d.BUY,
                        quantity=qty, order_type=getattr(d, "SL", "STOP_LOSS"), product_type=d.INTRA,
                        price=m.tick(trig * 1.05), trigger_price=trig)
    data = bot._data(res)
    if not data or not data.get("orderId"):
        rec("FAIL", area, "STOP_LOSS order accepted",
            f"{str(res)[:300]} -> bot will fall back to software stops (set USE_EXCHANGE_SL=0)")
    else:
        oid = str(data["orderId"]); time.sleep(1.5)
        st = bot.order_status(oid)
        rec("PASS", area, "STOP_LOSS order accepted", f"trigger ₹{trig:.2f}, status {st['status']}")
        ok = bot._modify(oid, getattr(d, "SL", "STOP_LOSS"), qty, m.tick(trig * 1.10), m.tick(trig * 1.05))
        rec("PASS" if ok else "FAIL", area, "Modify STOP_LOSS (survivor-stop move)", "")
        bot._cancel(oid); time.sleep(1.5)
        st = bot.order_status(oid)
        rec("PASS" if st["status"] == "CANCELLED" else "FAIL", area, "Cancel STOP_LOSS", f"status {st['status']}")
        if st["filled"]:
            rec("FAIL", area, "UNEXPECTED FILL", f"{st['filled']} qty bought - close it in Dhan")


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "lemonn_dual_session_final_dhan_v3.py"))
    ap.add_argument("--index", choices=["NIFTY", "SENSEX", "BOTH"], default="BOTH")
    ap.add_argument("--order-test", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.bot):
        sys.exit(f"Bot file not found: {args.bot}")

    indices = ["NIFTY", "SENSEX"] if args.index == "BOTH" else [args.index]
    first = load_bot(args.bot, indices[0])
    check_environment(first)
    base = first.LemonnDualSessionBot()
    if not check_account(base):
        return summary()

    for idx in indices:
        print(f"\n===== {idx} =====")
        m = first if idx == indices[0] else load_bot(args.bot, idx)
        bot = m.LemonnDualSessionBot()
        bot.dhan, bot.access_token = base.dhan, base.access_token   # reuse one login
        bot._last_feed_call = time.time()                            # keep the 1 req/sec throttle across indices
        check_index(m, bot, idx)
        if args.order_test and bot.options_df is not None:
            safe(f"ORDERS {idx}", "Order test", order_test, m, bot, idx)
    summary()


def summary():
    n = {s: sum(1 for r in RESULTS if r[0] == s) for s in ("PASS", "WARN", "FAIL")}
    print(f"\n===== SUMMARY: {n['PASS']} passed, {n['WARN']} warnings, {n['FAIL']} failed =====")
    for s, a, c, d in RESULTS:
        if s in ("FAIL", "WARN"):
            print(f"  {s}: [{a}] {c} — {d}")
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()
