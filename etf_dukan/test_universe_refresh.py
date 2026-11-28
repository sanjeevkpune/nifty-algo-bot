"""
Offline tests for the 6-monthly universe refresh (no network, no credentials, fake AngelOne + fake sheet).
    python3 test_universe_refresh.py
"""
import datetime as dt
import json
import os
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="ekd_univ_")
os.environ.update(EKD_HOME=HOME, EKD_PAPER_MODE="1", EKD_GSHEET_ID="test-sheet", EKD_SHEET_MODE="tracker",
                  EKD_SERVICE_ACCOUNT_FILE="/nonexistent", TELEGRAM_BOT_TOKEN="", EKD_TELEGRAM_BOT_TOKEN="")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import universe_refresh as UR  # noqa: E402

OK = True


def check(cond, msg):
    global OK
    print(("PASS " if cond else "FAIL ") + msg)
    OK = OK and bool(cond)


# ---------------- pure functions ----------------
d = dt.date
check(UR.is_due(d(2026, 11, 30), None), "first run ever is due")
check(not UR.is_due(d(2026, 12, 15), "2026-11-30"), "not due again in the same half-year")
check(not UR.is_due(d(2027, 3, 31), "2026-11-30"), "not due on 31 Mar")
check(UR.is_due(d(2027, 4, 1), "2026-11-30"), "due on 1 Apr")
check(UR.is_due(d(2027, 4, 5), "2026-11-30") and not UR.is_due(d(2027, 4, 6), "2027-04-05"), "due once after 1 Apr")
check(UR.is_due(d(2027, 10, 1), "2027-04-05"), "due on 1 Oct")
check(UR.next_due(d(2026, 11, 30)) == d(2027, 4, 1) and UR.next_due(d(2027, 4, 1)) == d(2027, 10, 1), "next_due")

check(UR.norm_underlying("Nifty 50 Index") == UR.norm_underlying("NIFTY 50") == UR.norm_underlying("NIFTY50 TRI"),
      "Nifty 50 spellings group together")
check(UR.norm_underlying("S&P BSE SENSEX") == UR.norm_underlying("BSE Sensex"), "Sensex spellings group together")
check(UR.norm_underlying("Domestic Price of Gold") == UR.norm_underlying("Physical Gold") == "gold", "gold groups")
check(UR.norm_underlying("NIFTY 50") != UR.norm_underlying("Nifty 50 Equal Weight"), "different index, different group")
check(UR.is_debt("Nifty 1D Rate Index") and UR.is_debt("Nifty 8-13 yr G-Sec") and UR.is_debt("CPSE Bond Plus SDL")
      and not UR.is_debt("Nifty Bank") and not UR.is_debt("Nifty Private Bank"), "debt ETFs are recognised")
check(UR.guess_sector("Nifty IT") == "Sector - IT" and UR.guess_sector("NASDAQ 100") == "International"
      and UR.guess_sector("Nifty India Digital") == "Thematic", "sector guesses")

lq = UR.liquidity([("2026-01-%02d" % i, 100.0, 100000.0 if i != 5 else 10_000_000.0) for i in range(1, 21)])
check(abs(lq["median_cr"] - 1.0) < 1e-9 and lq["avg_cr"] > 5, "median ignores a one-day block deal (avg does not)")

n50 = UR.parse_nifty50("﻿Company Name,Industry,Symbol,Series,ISIN Code\nHDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
                       "Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026\n")
check(n50 == [{"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "industry": "Financial Services"},
              {"symbol": "M&M", "name": "Mahindra & Mahindra Ltd.", "industry": "Automobile and Auto Components"}],
      "NIFTY 50 csv parsed")
el = UR.parse_etf_list("Symbol,Underlying,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue\n"
                       "NIFTYBEES,Nifty 50 Index,Nippon India ETF Nifty 50 BeES,08-JAN-2002,1,INF204KB14I2,1\n")
check(el == [{"symbol": "NIFTYBEES", "underlying": "Nifty 50 Index", "name": "Nippon India ETF Nifty 50 BeES"}],
      "NSE ETF csv parsed")

# selection with caps and the incumbent edge
P = UR.Params()
st = lambda v, days=120: {"median_cr": v, "avg_cr": v, "avg_volume": 1, "last_close": 10.0, "days": days}  # noqa: E731
stocks = [{"symbol": f"BANK{i}", "industry": "Financial Services"} for i in range(6)] + \
         [{"symbol": f"IT{i}", "industry": "Information Technology"} for i in range(5)] + \
         [{"symbol": f"X{i}", "industry": f"Industry {i}"} for i in range(40)]
stats = {c["symbol"]: st(1000 - i) for i, c in enumerate(stocks)}
picked, notes = UR.select_stocks(stocks, stats, set(), P)
syms = [r["symbol"] for r in picked]
check(len(syms) == 30 and sum(s.startswith("BANK") for s in syms) == 4 and sum(s.startswith("IT") for s in syms) == 3,
      "30 stocks, max 4 financials, max 3 per industry")
etfs = [{"symbol": "A50", "underlying": "Nifty 50"}, {"symbol": "B50", "underlying": "NIFTY 50 Index"},
        {"symbol": "LIQ", "underlying": "Nifty 1D Rate Index"}, {"symbol": "THIN", "underlying": "Nifty Realty"},
        {"symbol": "NEWB", "underlying": "Nifty Bank", "name": "x"}, {"symbol": "GOLD1", "underlying": "Gold"},
        {"symbol": "GOLD2", "underlying": "Physical Gold"}]
es = {"A50": st(100), "B50": st(110), "LIQ": st(5000), "THIN": st(0.5), "NEWB": st(20, days=30), "GOLD1": st(50), "GOLD2": st(10)}
picked, _ = UR.select_etfs(etfs, es, {"A50"}, P)
check([r["symbol"] for r in picked] == ["A50", "GOLD1"],
      "one ETF per underlying, incumbent edge keeps A50 over B50 (+10%), debt / thin / too-new left out")
picked, _ = UR.select_etfs(etfs, dict(es, B50=st(130)), {"A50"}, P)
check(picked[0]["symbol"] == "B50", "a rival 30% more liquid replaces the incumbent")

# ---------------- integration: Bot.refresh_universe with fakes ----------------
import etf_dukan_bot as B  # noqa: E402
import strategy as S  # noqa: E402

base = B.load_universe(B.UNIVERSE_FILE)
etf_csv = ["Symbol,Underlying,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue"]
for s, u in base.items():
    if u["type"] == S.ETF:
        etf_csv.append(f"{s},{u['name'].replace(',', ' ')},{s} ETF,01-JAN-2020,1,X,1")
etf_csv += ["SETFNIF50,Nifty 50 Index,SBI Nifty 50 ETF,01-JAN-2016,1,X,1",
            "LIQUIDBEES,Nifty 1D Rate Index,Liquid BeES,01-JAN-2010,1,X,1",
            "NEWTHEME,Nifty India Tourism,New tourism ETF,01-JAN-2025,1,X,1"]
n50_csv = ["Company Name,Industry,Symbol,Series,ISIN Code"]
old_stocks = [s for s, u in base.items() if u["type"] == S.STOCK]
for s in old_stocks:
    if s == "NESTLEIND":          # pretend Nestle left the NIFTY 50
        continue
    ind = "Financial Services" if base[s]["sector"].startswith("Financials") else base[s]["sector"]
    n50_csv.append(f"{s} Ltd,{ind},{s},EQ,X")
n50_csv += ["Trent Ltd,Consumer Services,TRENT,EQ,X", "Kotak Bank,Financial Services,KOTAKBANK,EQ,X"]
for i in range(20):
    n50_csv.append(f"Filler {i},Filler industry {i},FILL{i},EQ,X")

VALUE = {"SETFNIF50": 50.0, "NIFTYBEES": 300.0, "NEWTHEME": 400.0, "LIQUIDBEES": 900.0, "TRENT": 5000.0,
         "KOTAKBANK": 4000.0, "NESTLEIND": 4500.0, "MAHKTECH": 0.3}      # Rs crore / day; others below


class Resp:
    def __init__(self, t):
        self.t = t.encode()

    def read(self):
        return self.t


def fake_urlopen(req, timeout=0):
    url = req if isinstance(req, str) else req.full_url
    if "etfseclist" in url:
        return Resp("\n".join(etf_csv))
    if "nifty50list" in url:
        return Resp("\n".join(n50_csv))
    raise OSError("blocked")


B.urllib.request.urlopen = fake_urlopen
SYMS = sorted(set(base) | {"SETFNIF50", "LIQUIDBEES", "NEWTHEME", "TRENT", "KOTAKBANK"} | {f"FILL{i}" for i in range(20)})
TOK = {s: str(1000 + i) for i, s in enumerate(SYMS)}


class FakeSmart:
    def getCandleData(self, p):
        s = {v: k for k, v in TOK.items()}[p["symboltoken"]]
        if s in VALUE:
            v = VALUE[s]
        elif s.startswith("FILL"):
            v = 10.0 + int(s[4:])
        else:
            v = 20.0 + (sum(map(ord, s)) % 100)        # every current name is comfortably liquid
        rows = []
        day = dt.date(2026, 5, 1)
        while day < dt.date(2026, 11, 30):
            if day.weekday() < 5:
                rows.append([f"{day}T00:00:00+05:30", 100, 100, 100, 100.0, v * 1e7 / 100.0])
            day += dt.timedelta(days=1)
        return {"status": True, "data": rows}


class FakeSheet:
    def __init__(self):
        self.universe_tab = None
        self.history = []
        self.watch = {}

    def read_universe(self):
        if self.universe_tab is None:
            raise RuntimeError("no Universe tab")
        return UR.universe_from_table(self.universe_tab), self.universe_tab[1][1]

    def write_universe(self, rows, summary, params, nd, source):
        self.universe_tab = [["t"], ["Last refresh", summary["date"]], [params], [""], [], UR.SHEET_HEADERS] + UR.sheet_table(rows)
        self.history.append(summary)

    def sync_watchlist(self, universe, rows, held):
        self.watch = {s: ("Y" if u["include"] else "N") for s, u in universe.items()}
        return []

    def flush(self, *a):
        return True

    def backup_state(self, *a):
        pass


sheet = FakeSheet()
B.SheetLogger = lambda: sheet
B.Broker.scrip_master_nse = lambda self: {f"{s}-EQ": {"token": TOK[s], "symbol": f"{s}-EQ"} for s in SYMS}
B.Broker.login = lambda self: setattr(self, "smart", FakeSmart())
B.Broker.logout = lambda self: None
msgs = []
B.tg = lambda m, loud=None: msgs.append(m)
B.time.sleep = lambda x: None

bot = B.Bot()
check(bot.universe == base, "before any refresh the bot uses the repo's universe.csv")
bot.state = S.new_state(4_000_000, dt.date(2026, 11, 2), "paper")
bot.state["positions"]["NESTLEIND"] = {"type": S.STOCK, "qty": 30, "cost": 39000.0, "buy_charges": 30.0, "last": 1300.0, "first": "2026-11-10", "buys": 1}
bot.state["core"].append({"id": 1, "sym": "MAHKTECH", "type": S.ETF, "qty": 100, "cost_price": 10.0, "since": "2026-11-10"})
bot.today = dt.date(2026, 11, 30)
bot.broker.login()
check(bot.refresh_universe(dry_run=True) and not os.path.exists(B.ACTIVE_UNIVERSE_FILE), "dry run changes nothing")
ok = bot.refresh_universe()
new = B.load_universe()
meta = B.load_universe_meta()
check(ok and os.path.exists(B.ACTIVE_UNIVERSE_FILE) and meta["last_refresh"] == "2026-11-30" and not meta["sheet_pending"],
      "refresh saved state/universe_active.csv, meta and the sheet")
inc = {s for s, u in new.items() if u["include"]}
check("NIFTYBEES" in inc and "SETFNIF50" not in inc, "NIFTYBEES kept for NIFTY 50, the less liquid SETFNIF50 not added")
check("LIQUIDBEES" not in new, "liquid (debt) ETF never picked")
check("NEWTHEME" in inc and new["NEWTHEME"]["sector"] == "Other", "a new liquid ETF on a new underlying is added")
check("TRENT" in inc, "a new, very liquid NIFTY 50 stock is added")
check(sum(1 for s in inc if new[s]["type"] == S.STOCK) == 30
      and sum(1 for s in inc if new[s]["type"] == S.STOCK and UR.is_financial(new[s]["sector"])) <= 4,
      "30 stocks, at most 4 financials (KOTAKBANK can't push the count past 4)")
check(new.get("NESTLEIND", {}).get("include") is False, "held stock that left NIFTY 50 stays, Include = N (no new buys)")
check(new.get("MAHKTECH", {}).get("include") is False, "illiquid ETF with a Core lot stays, Include = N")
check(sheet.watch.get("NESTLEIND") == "N" and sheet.watch.get("TRENT") == "Y", "Watchlist sync got the new flags")
check(any("Universe" in m or "list refresh" in m for m in msgs), "Telegram message sent")

# averaging / target sell still work for a dropped holding
ltp = {"NESTLEIND": 1190.0, "MAHKTECH": 31.0}
sells = S.plan_sells(bot.state, ltp, B.RULES, bot.today, set())
check(any(a["action"] == "CORE_SELL" and a["sym"] == "MAHKTECH" for a in sells), "dropped ETF's Core lot still sold at 3x")
buys, _ = S.plan_buys(bot.state, new, {}, ltp, B.RULES, bot.today, B.RATES, set(), None)
check(any(a["action"] == "AVG" and a["sym"] == "NESTLEIND" for a in buys), "dropped stock still averaged at -8%")

# not due again; a rebuilt server restores the list from the sheet
check(not B.UR.is_due(dt.date(2026, 12, 1), B.load_universe_meta()["last_refresh"]), "not due the next day")
os.remove(B.ACTIVE_UNIVERSE_FILE)
os.remove(B.UNIVERSE_META_FILE)
bot2 = B.Bot()
check(bot2.universe == new and B.load_universe_meta()["last_refresh"] == "2026-11-30",
      "rebuilt server: list and refresh date restored from the Universe tab")

# NSE unreachable: no change, retried; falls back after EKD_UNIVERSE_FALLBACK_DAYS
B.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))
bot2.state = bot.state
bot2.broker.login()
before = open(B.ACTIVE_UNIVERSE_FILE).read()
bot2.today = dt.date(2027, 4, 1)
check(bot2.refresh_universe() is False and open(B.ACTIVE_UNIVERSE_FILE).read() == before, "NSE down: list unchanged, retried later")
m = B.load_universe_meta()
m["list_fail_dates"] = [f"2027-04-{i:02d}" for i in range(1, 10)]
B.save_universe_meta(m)
bot2.today = dt.date(2027, 4, 14)
check(bot2.refresh_universe() is True and B.load_universe_meta()["last_refresh"] == "2027-04-14",
      "after 10 failed days the current names are re-ranked instead")

# ---------------- your Watchlist edits -> the bot's list ----------------
u0 = B.load_universe()
wl = [[i + 1, "ETF" if u["type"] == S.ETF else "Stock", "Y" if u["include"] else "N", s, "", "", u["name"], u["sector"]]
      for i, (s, u) in enumerate(u0.items()) if s != "NESTLEIND"]          # user deleted the held NESTLEIND row
wl[0][2] = "N"                                                               # first ETF: Include Y -> N
wl.append(["", "Stock", "Y", "zomato", "", "", "Zomato", "Consumer"])        # a new name, lower case
wl.append(["", "Bond", "Y", "ABC", "", "", "", ""])                          # bad type
wl.append(["", "ETF", "Y", wl[1][3], "", "", "", ""])                        # duplicate
new, ch, pr = UR.universe_from_watchlist(wl, u0, {"NESTLEIND", "MAHKTECH"})
check(new["ZOMATO"]["include"] and new["ZOMATO"]["type"] == S.STOCK and "ZOMATO added (Stock, Include Y)" in ch,
      "a name typed on the Watchlist is added")
check(new[wl[0][3]]["include"] is False and any("Y → N" in c for c in ch), "Include Y -> N is taken")
check(new["NESTLEIND"]["include"] is False and any("NESTLEIND is held" in p for p in pr), "deleted held row kept as N")
check("ABC" not in new and any("ABC" in p for p in pr) and any("listed twice" in p for p in pr), "bad rows reported")
cleared, _, pr2 = UR.universe_from_watchlist([[1, "ETF", "Y", "NIFTYBEES"]], u0, set())
check(cleared is None and "ignored" in pr2[-1], "an almost empty Watchlist is ignored")
same, ch3, _ = UR.universe_from_watchlist(
    [[0, "ETF" if u["type"] == S.ETF else "Stock", "Y" if u["include"] else "N", s, "", "", u["name"], u["sector"]]
     for s, u in u0.items()], u0, set())
check(ch3 == [], "unchanged Watchlist = no change")

# workflow side: sheet_sync.py with a fake gspread, writing into a temp copy of universe.csv
import shutil, types  # noqa: E401,E402
import sheet_sync as SS  # noqa: E402
tmp = tempfile.mkdtemp()
csvp, stampp = os.path.join(tmp, "universe.csv"), os.path.join(tmp, "universe_synced.txt")
shutil.copy(B.UNIVERSE_FILE, csvp)


class FakeWS:
    def __init__(self, rows):
        self.rows = rows

    def get(self, rng):
        return self.rows


def fake_gspread(rows):
    m = types.ModuleType("gspread")
    m.service_account = lambda filename: types.SimpleNamespace(
        open_by_key=lambda k: types.SimpleNamespace(worksheet=lambda t: FakeWS(rows)))
    sys.modules["gspread"] = m


same_rows = [[0, "ETF" if u["type"] == S.ETF else "Stock", "Y" if u["include"] else "N", s, "", "", u["name"], u["sector"]]
             for s, u in base.items()]
fake_gspread(same_rows)
sys.argv = ["x", "--sheet", "id", "--sa", "sa.json", "--csv", csvp, "--stamp", stampp, "--no-scrip-check"]
check(SS.main() == 0 and not os.path.exists(stampp) and open(csvp).read() == open(B.UNIVERSE_FILE).read(),
      "sync workflow: unchanged sheet = universe.csv untouched, no stamp")
fake_gspread(wl)
check(SS.main() == 0 and os.path.exists(stampp) and "ZOMATO" in SS.load_csv(csvp), "sync workflow writes the edited list + stamp")
fake_gspread([[1, "ETF", "Y", "NIFTYBEES"]])
before = open(csvp).read()
check(SS.main() == 2 and open(csvp).read() == before, "sync workflow rejects an almost empty sheet")

# bot side: the next run applies the synced list once
B.UNIVERSE_FILE, B.SHEET_SYNC_STAMP = csvp, stampp
sheet.log_universe_change = lambda c: sheet.history.append({"edit": c})
B.save_universe_meta(dict(B.load_universe_meta(), last_refresh="2026-11-27"))
bot3 = B.Bot()
bot3.state = bot.state
msgs.clear()
check(bot3.apply_sheet_sync() and B.load_universe()["ZOMATO"]["include"]
      and B.load_universe()["NESTLEIND"]["include"] is False
      and any("sheet list" in m for m in msgs) and "edit" in sheet.history[-1],
      "bot applies the synced list at its next run, keeps held NESTLEIND as N, reports it")
check(bot3.apply_sheet_sync() is False, "the same sync is applied only once")
with open(stampp, "w") as f:
    f.write("2026-01-01 10:00:00 IST\nold change\n")
check(bot3.apply_sheet_sync() is False and "ZOMATO" in B.load_universe(),
      "a sync older than the last 6-monthly refresh is not applied (e.g. on a rebuilt server)")

print("\nALL UNIVERSE TESTS PASS" if OK else "\nSOME UNIVERSE TESTS FAILED")
sys.exit(0 if OK else 1)
