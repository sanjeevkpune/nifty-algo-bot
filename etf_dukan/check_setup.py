#!/usr/bin/env python3
"""
check_setup.py  --  end-to-end check of the ETF ki Dukan bot on the VM. Read-only by default.

    cd ~/trading-bot/etf_dukan
    python3 check_setup.py                 # full check (AngelOne, Google Sheet, Telegram, cron, state)
    python3 check_setup.py --angel-only    # only the AngelOne checks
    python3 check_setup.py --quick         # RSI for 8 symbols instead of the whole universe
    python3 check_setup.py --order-test    # ALSO place 1 real far-away LIMIT buy and cancel it (opt-in)

Read-only by default: no order is placed, modified or cancelled, and the bot's state file and the tracker
tabs (Holdings / Sold / Core / Capital / Signals / Bot Log) are never written. The only writes are:
  * one cell on a separate tab named "Bot Setup Check" (proves the service account can write),
  * today's price cache in cache/ (the bot would download the same data at 2:45 pm),
  * one Telegram message starting with "TEST".

--order-test (never on by default) places a REAL order on AngelOne, even in paper mode: BUY 1 unit of
NIFTYBEES (or --order-symbol) as a LIMIT order 5% below the live price, so it cannot fill, then cancels it
at once and confirms AngelOne shows it cancelled. It needs market hours (9:15 am to 3:28 pm) and about ₹300
of free cash for a few seconds. Order tag EKDTEST, which the bot ignores.

AngelOne login: a new SmartAPI session on the same API key may log out the strangle bot. Between 09:30 and
15:15 IST on weekdays the AngelOne checks are skipped unless a separate key (EKD_API_KEY) is set or you
pass --force-login. They are also skipped while the ETF bot's own 2:45 pm run is still going.
Good times to run: 9:15-9:30 am (the order test works then) or after 3:30 pm.

Exit code 0 = no FAIL. Prints a PASS / WARN / FAIL / SKIP table and sends it on Telegram.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RESULTS = []
PROBE = ["NIFTYBEES", "GOLDBEES", "SILVERBEES", "BANKBEES", "HDFCBANK", "TCS", "ITC", "BEL"]


def rec(status, name, detail=""):
    RESULTS.append((status, name, detail))
    print(f"[{status:4s}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def guarded(name, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        rec("FAIL", name, f"{type(e).__name__}: {e}")
        if os.environ.get("EKD_CHECK_DEBUG"):
            traceback.print_exc()


def mask(v):
    return "(missing)" if not v else (v[:2] + "…" + v[-2:] if len(v) > 6 else "set")


# ============================================================ local checks
def check_local(B):
    for d in ("state", "locks", "logs", "cache"):
        p = os.path.join(B.HOME, d)
        ok = os.path.isdir(p) and os.access(p, os.W_OK)
        rec("PASS" if ok else "FAIL", f"Folder {d}/ writable", p)

    files = os.environ.get("EKD_CONFIG_FILES") or ":".join([os.path.join(HERE, "etf_dukan.env"),
                                                            "/etc/trade-config/etf_dukan.env", "/etc/trade-config/config.env"])
    found = [f for f in files.split(":") if f and os.path.exists(f)]
    rec("PASS" if "/etc/trade-config/etf_dukan.env" in found else "WARN", "Config files found", ", ".join(found) or "none")
    rec("PASS", "Sheet / signal modes",
        f"sheet mode {B.SHEET_MODE}, signal policy {B.SIGNAL_POLICY}, cash floor ₹{B.BROKER_CASH_FLOOR:,.0f}")

    off = time.strftime("%z")
    rec("PASS" if off == "+0530" else "WARN", "VM time zone", f"UTC{off} (the cron time assumes IST)")
    for mod in ("gspread", "requests"):
        try:
            __import__(mod)
            rec("PASS", f"Python package {mod}")
        except Exception as e:
            rec("FAIL", f"Python package {mod}", str(e))

    try:
        ct = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
        line = next((l for l in ct.splitlines() if "EKD-ETF-DUKAN" in l and not l.strip().startswith("#")), None)
        if line and "etf_dukan_bot.py run" in line:
            rec("PASS", "Cron line", " ".join(line.split()[:5]) + " … etf_dukan_bot.py run")
        else:
            rec("FAIL", "Cron line", "no '# EKD-ETF-DUKAN' line in this user's crontab")
    except Exception as e:
        rec("FAIL", "Cron line", f"crontab -l failed: {e}")

    st = None
    try:
        st = B.load_state()
        if not st:
            rec("FAIL", "State file", f"{B.STATE_FILE} missing (setup_etf_dukan.sh should create it)")
        else:
            okmode = st.get("mode") == B.MODE
            rec("PASS" if okmode else "WARN", "State file",
                f"{B.MODE}: start {st['start_date']}, cash ₹{st['cash']:,.0f}, {len(st['positions'])} open, "
                f"{len(st['core'])} Core" + ("" if okmode else f" (file says mode {st.get('mode')})"))
            rec("PASS" if not st.get("pending") else "FAIL", "No orders left in flight",
                "none" if not st.get("pending") else f"pending {list(st['pending'])}: run `python3 etf_dukan_bot.py recover`")
    except Exception as e:
        rec("FAIL", "State file", f"unreadable: {e}")

    lock = os.path.join(B.LOCK_DIR, f"run_{B.now_ist().date()}_{B.MODE}.json")
    if os.path.exists(lock):
        try:
            ls = json.load(open(lock))
            bad = ls.get("status") not in ("done", "skipped")
            rec("WARN" if bad else "PASS", "Today's run lock",
                f"status {ls.get('status')}" + (" (a run is in progress or crashed)" if bad else ""))
        except Exception:
            rec("WARN", "Today's run lock", "exists but is empty or unreadable")
    else:
        rec("PASS", "Today's run lock", "none yet (normal before 2:45 pm)")
    ob = os.path.join(B.HOME, "state", f"{B.MODE}_sheet_outbox.jsonl")
    n_ob = sum(1 for l in open(ob) if l.strip()) if os.path.exists(ob) else 0
    rec("PASS" if n_ob == 0 else "WARN", "Sheet outbox", f"{n_ob} row(s) waiting to be written" if n_ob else "empty")
    return st


def check_sheet(B):
    sl = B.SheetLogger()
    sh = sl._book()
    tabs = [w.title for w in sh.worksheets()]
    lack = [t for t in ("Signals", "Watchlist", "Holdings", "Sold", "Core", "Capital") if t not in tabs]
    rec("FAIL" if lack else "PASS", "Sheet opened, tracker tabs present",
        f"missing tabs {lack}" if lack else f"'{sh.title}', {len(tabs)} tabs")
    try:
        sig = sl.read_signals()
        f = lambda x: x["sym"] if x else "-"
        rec("PASS", "Signals tab readable",
            f"sell {f(sig['sell'])}, ETF {f(sig['etf'])}, stock {f(sig['stock'])}, Core {f(sig['core'])}")
    except Exception as e:
        rec("WARN", "Signals tab readable", f"{e} (the bot still trades its own plan and tells you)")
    try:
        ws = sh.worksheet("Bot Setup Check")
    except Exception:
        ws = sh.add_worksheet(title="Bot Setup Check", rows=5, cols=3)
    stamp = f"setup check {B.now_ist():%Y-%m-%d %H:%M:%S} IST"
    ws.update(range_name="A1:B1", values=[["TEST ROW - safe to delete this tab", stamp]], value_input_option="RAW")
    back = ws.acell("B1").value
    rec("PASS" if back == stamp else "FAIL", "Sheet writable by the service account",
        "wrote and read back one cell on tab 'Bot Setup Check'" if back == stamp else f"read back {back!r}")


# ============================================================ AngelOne
def check_angel(B, S, st, a):
    bot = B.Bot()
    br = bot.broker
    try:
        br.login()
        rec("PASS", "AngelOne login (PIN + TOTP)", f"client {mask(B.CLIENT_CODE)}")
    except Exception as e:
        rec("FAIL", "AngelOne login (PIN + TOTP)", f"{e} (check the TOTP secret, PIN and the VM clock)")
        return
    try:
        guarded("AngelOne profile", angel_profile, br)
        guarded("Scrip master tokens", angel_tokens, bot, br)
        ltp = guarded("Live prices (LTP)", angel_ltp, br) or {}
        guarded("Market-open probe", angel_market, br)
        guarded("Daily candles + RSI", angel_candles, B, S, br, ltp)
        guarded("Funds (read only)", angel_funds, B, br)
        guarded("Holdings + positions (read only)", angel_holdings, br)
        guarded("Order book (read only)", angel_orderbook, br)
        if not a.no_plan:
            guarded("RSI for the universe and today's plan", angel_plan, B, S, bot, st, a)
        if a.order_test:
            guarded("Order test", angel_order_test, B, br, a)
        else:
            rec("SKIP", "Order test", "not asked (add --order-test to place and cancel one far-away 1-unit LIMIT buy)")
    finally:
        br.logout()
        rec("PASS", "AngelOne session closed")


def angel_profile(br):
    tok = getattr(br.smart, "refresh_token", None) or getattr(br.smart, "refreshToken", None)
    r = br.smart.getProfile(tok)
    d = (r or {}).get("data") or {}
    if d:
        ex = d.get("exchanges") or []
        prods = d.get("products") or []
        ok = "NSE" in ex and (not prods or "DELIVERY" in prods)
        rec("PASS" if ok else "WARN", "AngelOne profile",
            f"exchanges {','.join(ex) or '?'}" + ("" if ok else " (NSE / DELIVERY not listed)"))
    else:
        rec("WARN", "AngelOne profile", f"getProfile gave no data: {str(r)[:120]}")


def angel_tokens(bot, br):
    br.load_tokens()
    inc = [s for s, u in bot.universe.items() if u["include"]]
    miss = [s for s in inc if s not in br.tokens]
    rec("PASS" if not miss else "WARN", "Scrip master tokens",
        f"{len(inc) - len(miss)}/{len(inc)} universe symbols found" + (f"; missing {miss}" if miss else ""))


def angel_ltp(br):
    ltp = br.ltp_many([s for s in PROBE if s in br.tokens])
    rec("PASS" if len(ltp) >= 6 else "FAIL", "Live prices (LTP)",
        ", ".join(f"{s} ₹{p:,.2f}" for s, p in ltp.items()) or "no prices returned")
    return ltp


def angel_market(br):
    ok, msg = br.market_open_today()
    rec("PASS", "Market-open probe", ("traded today" if ok else "no trade today (holiday or weekend?)") + f"; {msg}")


def angel_candles(B, S, br, ltp):
    cl = br.daily_closes("NIFTYBEES", B.now_ist().date())
    closes = [c for _, c in cl] + ([ltp["NIFTYBEES"]] if "NIFTYBEES" in ltp else [])
    r = S.rsi_wilder(closes, B.RULES.rsi_period)
    if len(cl) >= 100 and r is not None:
        rec("PASS", "Daily candles + RSI", f"NIFTYBEES {len(cl)} days to {cl[-1][0]}, RSI(14) {r:.1f}")
    else:
        rec("FAIL", "Daily candles + RSI", f"only {len(cl)} daily candles for NIFTYBEES")


def angel_funds(B, br):
    cash = br.available_cash()
    if cash is None:
        rec("FAIL", "Funds (read only)", "rmsLimit failed")
    else:
        room = cash - B.BROKER_CASH_FLOOR
        rec("PASS" if room >= 0 or B.PAPER_MODE else "WARN", "Funds (read only)",
            f"available ₹{cash:,.0f}; above the ₹{B.BROKER_CASH_FLOOR:,.0f} floor: ₹{max(room, 0):,.0f}"
            + (" (live buys would be skipped)" if room < 0 else ""))


def angel_holdings(br):
    q, notes = br.broker_quantities()
    if q is None:
        rec("FAIL", "Holdings + positions (read only)", "; ".join(notes))
    else:
        held = {s: n for s, n in q.items() if n}
        rec("PASS", "Holdings + positions (read only)",
            f"{len(held)} universe symbol(s) held at AngelOne" + (f": {dict(list(held.items())[:6])}" if held else ""))


def angel_orderbook(br):
    ob = br.order_book()
    if ob is None:
        rec("FAIL", "Order book (read only)", "orderBook failed")
    else:
        mine = [o for o in ob if str(o.get("ordertag", "")).startswith("EKD")]
        rec("PASS", "Order book (read only)", f"{len(ob)} order(s) today, {len(mine)} from this bot")


def angel_plan(B, S, bot, st, a):
    bot.state = json.loads(json.dumps(st)) if st else S.new_state(4_000_000, B.now_ist().date(), B.MODE)
    if a.quick:
        bot.universe = {s: u for s, u in bot.universe.items() if s in PROBE}
    t0 = time.time()
    bot.load_history()
    ltp, rsi = bot.snapshot()
    sells = S.plan_sells(bot.state, ltp, B.RULES, bot.today, set())
    buys, _ = S.plan_buys(bot.state, bot.universe, rsi, ltp, B.RULES, bot.today, B.RATES, set(), None)
    etf = S.shortlist(bot.universe, rsi, ltp, S.ETF, B.RULES.etf_shortlist)
    stk = S.shortlist(bot.universe, rsi, ltp, S.STOCK, B.RULES.stock_shortlist)
    n_inc = sum(1 for u in bot.universe.values() if u["include"])
    rec("PASS" if len(rsi) >= 0.9 * n_inc else "WARN", "RSI for the universe and today's plan",
        f"{len(rsi)}/{n_inc} RSIs in {time.time() - t0:.0f}s; lowest-RSI ETFs {', '.join(etf[:3])}; stocks {', '.join(stk)}; "
        f"plan {len(sells)} sell(s), buys {[b['action'] + ' ' + b['sym'] for b in buys]} (not placed)")


def angel_order_test(B, br, a):
    sym = a.order_symbol.upper()
    now = B.now_ist()
    if now.weekday() >= 5 or not (dt.time(9, 15) <= now.time() <= dt.time(15, 28)):
        rec("SKIP", "Order test", "needs market hours, 9:15 am to 3:28 pm on a trading day")
        return
    if sym not in br.tokens:
        rec("FAIL", "Order test", f"{sym} not in the scrip master")
        return
    p = br.ltp_many([sym]).get(sym)
    if not p:
        rec("FAIL", "Order test", f"no live price for {sym}")
        return
    price = br.tick_round(sym, p * 0.95, "BUY")
    print(f"➜ Order test: BUY 1 {sym} LIMIT ₹{price:.2f} (live ₹{p:.2f}), tag EKDTEST; cancelling at once")
    try:
        oid = br.place(sym, "BUY", 1, "LIMIT", price, "EKDTEST")
    except Exception as e:
        rec("FAIL", "Order test: place", str(e))
        return
    rec("PASS", "Order test: place", f"order {oid} accepted (BUY 1 {sym} @ ₹{price:.2f})")
    time.sleep(2)
    info = br.order_info(oid)
    s0 = br.status(info)
    if s0 == "rejected":
        rec("FAIL", "Order test: status", f"rejected: {(info or {}).get('text') or info}")
        return
    rec("PASS" if s0 in ("open", "open pending", "validation pending", "put order req received") else "WARN",
        "Order test: status", f"AngelOne shows '{s0 or 'unknown'}'")
    br.cancel(oid)
    info = br.wait_terminal(oid, 15)
    s1 = br.status(info)
    filled = int(float((info or {}).get("filledshares") or 0))
    if filled:
        rec("FAIL", "Order test: cancel", f"{filled} unit of {sym} FILLED; sell it from the AngelOne app")
    elif s1 in ("cancelled", "canceled"):
        rec("PASS", "Order test: cancel", f"order {oid} cancelled, nothing bought")
    else:
        rec("FAIL", "Order test: cancel", f"status '{s1}' after 15s; cancel order {oid} in the AngelOne app")


# ============================================================ main
def main():
    ap = argparse.ArgumentParser(description="Check the ETF ki Dukan bot setup (read-only by default).")
    ap.add_argument("--angel-only", action="store_true", help="only the AngelOne checks")
    ap.add_argument("--quick", action="store_true", help="RSI for 8 symbols instead of the whole universe")
    ap.add_argument("--no-plan", action="store_true", help="skip the universe RSI and plan step")
    ap.add_argument("--skip-login", action="store_true", help="skip everything that logs in to AngelOne")
    ap.add_argument("--force-login", action="store_true", help="log in even while the strangle bot is running")
    ap.add_argument("--order-test", action="store_true", help="place and cancel 1 far-away LIMIT buy (REAL order)")
    ap.add_argument("--order-symbol", default="NIFTYBEES")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()

    missing = [f for f in ("etf_dukan_bot.py", "strategy.py", "signals.py", "universe.csv")
               if not os.path.exists(os.path.join(HERE, f))]
    rec("FAIL" if missing else "PASS", "Bot files present", f"missing: {missing}" if missing else HERE)
    try:
        import etf_dukan_bot as B
        import strategy as S
    except Exception as e:
        rec("FAIL", "Import bot modules", f"{type(e).__name__}: {e}")
        return finish(None, a)
    rec("PASS", "Import bot modules", B.STRATEGY_VERSION)

    rec("PASS" if B.PAPER_MODE else "WARN", "Paper mode",
        "ON: the bot sends no real orders" if B.PAPER_MODE else "OFF: the bot sends LIVE orders at 2:45 pm")
    key_src = "separate EKD_API_KEY" if os.environ.get("EKD_API_KEY") else "the strangle bot's API_KEY (shared)"
    creds = {"API_KEY": B.API_KEY, "CLIENT_CODE": B.CLIENT_CODE, "PIN": B.PIN, "TOTP_SECRET": B.TOTP_SECRET}
    cmiss = [k for k, v in creds.items() if not v]
    rec("FAIL" if cmiss else "PASS", "AngelOne credentials resolve", f"missing {cmiss}" if cmiss else f"using {key_src}")
    try:
        import pyotp
        code = pyotp.TOTP(B.TOTP_SECRET).now() if B.TOTP_SECRET else None
        rec("PASS" if code else "FAIL", "TOTP code generates", "6-digit code OK" if code else "no TOTP secret")
    except Exception as e:
        rec("FAIL", "TOTP code generates", f"{e} (bad TOTP secret or pyotp missing)")

    st = None
    if not a.angel_only:
        tgm = [k for k, v in {"TELEGRAM_BOT_TOKEN": B.TELEGRAM_BOT_TOKEN, "TELEGRAM_CHAT_ID": B.TELEGRAM_CHAT_ID}.items() if not v]
        rec("FAIL" if tgm else "PASS", "Telegram settings", f"missing {tgm}" if tgm else "token and chat id set")
        rec("PASS" if B.GSHEET_ID else "FAIL", "Google Sheet id", B.GSHEET_ID or "EKD_GSHEET_ID missing")
        sa_email = None
        if os.path.exists(B.SERVICE_ACCOUNT_FILE):
            try:
                sa_email = json.load(open(B.SERVICE_ACCOUNT_FILE)).get("client_email")
                rec("PASS", "Service account file", f"{B.SERVICE_ACCOUNT_FILE} ({sa_email})")
            except Exception as e:
                rec("FAIL", "Service account file", f"unreadable JSON: {e}")
        else:
            rec("FAIL", "Service account file", f"not found: {B.SERVICE_ACCOUNT_FILE}")
        st = guarded("Local setup", check_local, B)
        if B.GSHEET_ID and sa_email:
            n0 = len(RESULTS)
            guarded("Google Sheet access", check_sheet, B)
            if any(s == "FAIL" for s, _, _ in RESULTS[n0:]):
                rec("WARN", "Sheet sharing", f"make sure the sheet is shared with {sa_email} as Editor")
        else:
            rec("SKIP", "Google Sheet access", "no sheet id or service account file")
    else:
        try:
            st = B.load_state()
        except Exception:
            st = None

    now = B.now_ist()
    in_hours = now.weekday() < 5 and dt.time(9, 30) <= now.time() <= dt.time(15, 15)
    lock = os.path.join(B.LOCK_DIR, f"run_{now.date()}_{B.MODE}.json")
    bot_running = False
    if os.path.exists(lock) and now.time() < dt.time(15, 35):
        try:
            bot_running = json.load(open(lock)).get("status") not in ("done", "skipped")
        except Exception:
            bot_running = True
    if a.skip_login:
        rec("SKIP", "AngelOne checks", "--skip-login")
    elif bot_running:
        rec("SKIP", "AngelOne checks", "the ETF bot's run for today is still going; try again after 3:30 pm")
    elif cmiss:
        rec("SKIP", "AngelOne checks", "credentials missing")
    elif in_hours and not os.environ.get("EKD_API_KEY") and not a.force_login:
        rec("SKIP", "AngelOne checks", f"it is {now:%H:%M} IST: a second login on the shared API key could log out "
            "the strangle bot. Run after 3:15 pm (or before 9:30 am), or pass --force-login")
    else:
        check_angel(B, S, st, a)
    return finish(B, a)


def finish(B, a):
    n = {k: sum(1 for s, _, _ in RESULTS if s == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    head = f"ETF bot setup check: {n['PASS']} PASS, {n['WARN']} WARN, {n['FAIL']} FAIL, {n['SKIP']} SKIP"
    print("\n" + "=" * len(head) + "\n" + head + "\n" + "=" * len(head))
    for s, name, d in RESULTS:
        if s != "PASS":
            print(f"  {s}: {name} -- {d}")
    if B is not None and not a.no_telegram and B.TELEGRAM_BOT_TOKEN and B.TELEGRAM_CHAT_ID:
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭️"}
        body = "\n".join(f"{icon[s]} {nm}" + (f": {d}" if s != "PASS" and d else "") for s, nm, d in RESULTS)
        try:
            import requests
            r = requests.post(f"https://api.telegram.org/bot{B.TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": B.TELEGRAM_CHAT_ID, "text": f"🧪 TEST: {head}\n{body}"[:4000]}, timeout=10)
            ok = r.ok and r.json().get("ok")
            print(f"[{'PASS' if ok else 'FAIL'}] Telegram test message -- " + ("sent; check your chat" if ok else r.text[:200]))
            n["FAIL"] += 0 if ok else 1
        except Exception as e:
            print(f"[FAIL] Telegram test message -- {e}")
            n["FAIL"] += 1
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()
