#!/usr/bin/env python3
"""
Read-only checks for lemonn_nifty_sideways_v4_dhan.py. Places NO orders.
Run it in market hours (best after 10:00) from the same folder as the bot:
    python3 test_lemonn_v4_connectivity.py
It checks every data feed the v4 filters need: NIFTY spot, India VIX id,
current-month future + VWAP, 1-min and 5-min intraday candles, ADX,
ATM straddle prices, Telegram and the Google Sheet tab.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lemonn_nifty_sideways_v4_dhan as M

ok_n = fail_n = warn_n = 0


def res(kind, name, detail=""):
    global ok_n, fail_n, warn_n
    mark = {"ok": "✅ PASS", "fail": "❌ FAIL", "warn": "⚠️ WARN"}[kind]
    ok_n += kind == "ok"; fail_n += kind == "fail"; warn_n += kind == "warn"
    print(f"{mark}  {name}  {detail}")


bot = M.LemonnV4Bot()
try:
    bot.login(); res("ok", "Dhan login")
except Exception as e:
    res("fail", "Dhan login", str(e)); sys.exit(1)

spot = bot.get_spot()
res("ok" if spot > 0 else "fail", "NIFTY spot", f"{spot}")
o = bot.spot_ohlc()
res("ok" if o.get("open") and o.get("close") else "warn", "NIFTY open / prev close", f"{o}")

try:
    bot.load_master(); res("ok", "Scrip master", f"expiry {bot.expiry}, lot {bot.lot_size}, DTE {(bot.expiry - bot.today).days}")
except Exception as e:
    res("fail", "Scrip master", str(e)); sys.exit(1)

v = bot.ohlc_of(M.SPOT_EXCH, M.VIX_TOKEN)
if v.get("ltp") and 5 < v["ltp"] < 60:
    res("ok", f"India VIX (IDX_I id {M.VIX_TOKEN})", f"LTP {v['ltp']} prev {v['prev_close']}")
else:
    res("fail", f"India VIX (IDX_I id {M.VIX_TOKEN})",
        f"got {v}. Find the INDIA VIX security id in the scrip master and set DHAN_INDIA_VIX_ID in config.env")

if bot.fut:
    q = bot.quote(M.FNO_SEG, bot.fut["token"])
    lp, vw = q.get("last_price"), q.get("average_price")
    res("ok" if lp and vw else "fail", f"Future {bot.fut['symbol']} LTP / VWAP", f"{lp} / {vw}")
else:
    res("fail", "Current-month NIFTY future", "not found in scrip master")

d = bot.today
b1 = bot.candles(M.SPOT_TOKEN, M.SPOT_EXCH, "INDEX", 1, M.at_ist(d, 9, 15), M.now_ist())
if b1:
    orb = [b for b in b1 if b["t"] <= M.at_ist(d, 9, 59)]
    rng = (max(b["h"] for b in orb) - min(b["l"] for b in orb)) if orb else None
    res("ok", "1-min candles", f"{len(b1)} bars, first {b1[0]['t']:%H:%M}, 09:15-09:59 range {rng}")
else:
    res("warn", "1-min candles", "none (before 09:16, or charts API not enabled) - bot falls back to day high-low")

b5 = bot.candles(M.SPOT_TOKEN, M.SPOT_EXCH, "INDEX", 5, M.at_ist(d - datetime.timedelta(days=7), 9, 15), M.now_ist())
if b5:
    a = bot.adx([b["h"] for b in b5], [b["l"] for b in b5], [b["c"] for b in b5], M.ADX_PERIOD)
    res("ok" if a is not None else "warn", "5-min candles + ADX(14)", f"{len(b5)} bars, ADX {a and round(a, 2)}")
else:
    res("fail", "5-min candles", "none - ADX filter will always score 0")

k = int(round(spot / M.STRIKE_STEP) * M.STRIKE_STEP) if spot else 0
c, p = bot.straddle_px(k) if k else (0, 0)
res("ok" if c > 0 and p > 0 else "fail", f"ATM {k} straddle", f"CE {c} + PE {p} = {c + p:.2f}")

if M.TELEGRAM_BOT_TOKEN and M.TELEGRAM_CHAT_ID:
    M.tg("🧪 v4 connectivity test message"); res("ok", "Telegram", "test message sent - check your phone")
else:
    res("warn", "Telegram", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

if M.gspread and M.GSHEET_ID_V2 and os.path.exists(M.SERVICE_ACCOUNT_FILE):
    try:
        sh = M.gspread.service_account(filename=M.SERVICE_ACCOUNT_FILE).open_by_key(M.GSHEET_ID_V2)
        tabs = [w.title for w in sh.worksheets()]
        res("ok", "Google Sheet", f"'{sh.title}'; tabs " + ", ".join(
            f"'{t}' " + ("exists" if t in tabs else "will be created") for t in (M.TRADES_TAB, M.SHEET_TAB)))
    except Exception as e:
        res("fail", "Google Sheet", str(e))
else:
    res("warn", "Google Sheet", "gspread / GSHEET_ID_V2 / service account file missing")

try:
    os.makedirs(M.STATE_DIR, exist_ok=True)
    t = os.path.join(M.STATE_DIR, ".w"); open(t, "w").write("x"); os.remove(t)
    res("ok", "State folder writable", M.STATE_DIR)
except Exception as e:
    res("fail", "State folder writable", f"{M.STATE_DIR}: {e} (set LEMONN_STATE_DIR)")

print(f"\nPaper mode: {M.PAPER_MODE} | lots {M.NUM_LOTS}")
print(f"Summary: {ok_n} pass, {fail_n} fail, {warn_n} warn")
