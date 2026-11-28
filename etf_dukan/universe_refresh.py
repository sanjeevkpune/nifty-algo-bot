"""
Six-monthly refresh of the ETF ki Dukan universe (which ETFs and stocks the bot may NEW-buy).

Pure selection rules live here (no network, easy to test). etf_dukan_bot.py does the I/O:
downloads the NSE lists, fetches 6 months of daily candles from AngelOne, then calls build().

Rules (defaults; each can be changed with an EKD_UNIV_* setting, see config.env.example):
  When     : on the first bot run (after the 3:10 pm buys) on/after 1 April and 1 October, i.e. just after
             NSE's March / September index rebalances. The very first run after this feature is installed
             also refreshes, so the lists are current from the start.
  Liquidity: MEDIAN daily traded value (close x volume) over the last ~6 months (125 trading days).
             The median ignores one-off block deals that inflate an average.
  ETFs     : every NSE-listed ETF (NSE's ETF list), except debt / liquid / G-Sec / bond ETFs.
             One ETF per underlying index (or per commodity): the most liquid one.
             It must trade at least Rs 1 crore a day (median) and have 60+ trading days of history.
             At most 50 ETFs, the most liquid first.
  Stocks   : the current NIFTY 50 members, most liquid first, 30 picked, at most 4 financials and at most
             3 from any other NSE industry.
  Stability: a counter already in the list gets a 25% edge (its liquidity counts x1.25 when it is compared
             with newcomers), so names do not flip in and out on small differences.
  Holdings : a held counter that drops out is NOT sold. It stays in the bot's universe with include = N:
             no new buys, but its target sell, averaging and Core 3x sell carry on as normal.
"""
import csv
import datetime as dt
import io
import re
import statistics
from typing import Dict, List, Optional, Tuple

ETF, STOCK = "ETF", "Stock"

DEBT_WORDS = ("liquid", "gilt", "g-sec", "gsec", "g sec", "sdl", "bond", "debt", "overnight", "money market",
              "treasury", "t-bill", "tbill", "t bill", "1d rate", "rate index", "crisil", "psu bank bond",
              "government securities", "gov sec", "constant maturity", "corporate", "target maturity")


# ---------------- schedule ----------------
def window_start(today: dt.date, months=(4, 10)) -> dt.date:
    """The latest refresh date (1st of one of `months`) on or before today."""
    cands = []
    for y in (today.year - 1, today.year):
        for m in months:
            d = dt.date(y, m, 1)
            if d <= today:
                cands.append(d)
    return max(cands)


def next_due(today: dt.date, months=(4, 10)) -> dt.date:
    cands = [dt.date(y, m, 1) for y in (today.year, today.year + 1) for m in months]
    return min(d for d in cands if d > today)


def is_due(today: dt.date, last_refresh: Optional[str], months=(4, 10)) -> bool:
    if not last_refresh:
        return True
    try:
        last = dt.date.fromisoformat(str(last_refresh)[:10])
    except ValueError:
        return True
    return last < window_start(today, months)


# ---------------- parsing the NSE lists ----------------
def _rows(text: str) -> List[Dict[str, str]]:
    text = text.lstrip("﻿")
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        out.append({re.sub(r"[^a-z]", "", (k or "").lower()): (v or "").strip() for k, v in r.items()})
    return out


def parse_nifty50(text: str) -> List[dict]:
    """niftyindices / NSE archives ind_nifty50list.csv: Company Name, Industry, Symbol, Series, ISIN Code."""
    out = []
    for r in _rows(text):
        sym = (r.get("symbol") or "").upper()
        if not sym or (r.get("series") and r["series"].upper() != "EQ"):
            continue
        out.append({"symbol": sym, "name": r.get("companyname") or sym, "industry": r.get("industry") or ""})
    return out


def parse_etf_list(text: str) -> List[dict]:
    """NSE eq_etfseclist.csv: Symbol, Underlying, SecurityName, DateofListing, ... (column names vary a little)."""
    out = []
    for r in _rows(text):
        sym = (r.get("symbol") or "").upper()
        und = r.get("underlying") or r.get("underlyingasset") or r.get("assets") or ""
        name = r.get("securityname") or r.get("nameofsecurity") or r.get("name") or sym
        if sym:
            out.append({"symbol": sym, "underlying": und, "name": name})
    return out


def is_debt(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in DEBT_WORDS)


def norm_underlying(u: str) -> str:
    """Group key for 'same underlying'. Gold and silver ETFs all group together."""
    t = (u or "").lower().replace("&", " and ")
    if "silver" in t:
        return "silver"
    if "gold" in t and "goldman" not in t:
        return "gold"
    t = re.sub(r"\b(s and p|index|indices|tri|total returns?|etf|fund|the|ltd|limited)\b", " ", t)
    t = t.replace("nifty50", "nifty 50")
    return re.sub(r"[^a-z0-9]", "", t)


SECTOR_GUESS = [  # for ETFs that are not in the current universe file
    ("silver", "Commodity - silver"), ("gold", "Commodity - gold"),
    ("nasdaq", "International"), ("s and p 500", "International"), ("s&p 500", "International"),
    ("hang seng", "International"), ("nyse", "International"), ("fang", "International"), ("us", "International"),
    ("psu bank", "Sector - Banking"), ("private bank", "Sector - Banking"), ("bank", "Sector - Banking"),
    ("financial", "Sector - Financials"), ("it", "Sector - IT"), ("pharma", "Sector - Healthcare"),
    ("health", "Sector - Healthcare"), ("fmcg", "Sector - FMCG"), ("auto", "Sector - Auto"),
    ("metal", "Sector - Metals"), ("infra", "Sector - Infrastructure"), ("energy", "Sector - Energy"),
    ("oil", "Sector - Energy"), ("consum", "Sector - Consumption"), ("realty", "Sector - Realty"),
    ("commodit", "Sector - Commodities"), ("cpse", "PSU / Thematic"), ("bharat 22", "PSU / Thematic"),
    ("psu", "PSU / Thematic"), ("defence", "Thematic"), ("rail", "Thematic"), ("digital", "Thematic"),
    ("manufactur", "Thematic"), ("momentum", "Strategy / factor"), ("alpha", "Strategy / factor"),
    ("low vol", "Strategy / factor"), ("quality", "Strategy / factor"), ("value", "Strategy / factor"),
    ("dividend", "Strategy / factor"), ("equal weight", "Broad market - large cap"),
    ("smallcap", "Broad market - small cap"), ("small cap", "Broad market - small cap"),
    ("midcap", "Broad market - mid cap"), ("mid cap", "Broad market - mid cap"),
    ("500", "Broad market - multi cap"), ("next 50", "Broad market - large cap"), ("sensex", "Broad market - large cap"),
    ("nifty 50", "Broad market - large cap"), ("nifty 100", "Broad market - large cap"),
]


def guess_sector(underlying: str) -> str:
    t = " " + (underlying or "").lower() + " "
    for k, v in SECTOR_GUESS:
        if (" " + k + " ") in t or (len(k) > 3 and k in t):
            return v
    return "Other"


def is_financial(industry: str) -> bool:
    t = (industry or "").lower()
    return t.startswith("financial") or "bank" in t or "nbfc" in t or "insurance" in t


# ---------------- liquidity ----------------
def liquidity(bars: List[Tuple[str, float, float]], days: int = 125) -> Optional[dict]:
    """bars: (date, close, volume) oldest first. Returns median/avg traded value in Rs crore per day."""
    bars = [b for b in bars if b[1] > 0][-days:]
    if not bars:
        return None
    vals = [c * v / 1e7 for _, c, v in bars]
    return {"median_cr": round(statistics.median(vals), 3), "avg_cr": round(sum(vals) / len(vals), 3),
            "avg_volume": round(sum(v for _, _, v in bars) / len(bars)), "last_close": bars[-1][1],
            "days": len(bars), "last_date": bars[-1][0]}


# ---------------- selection ----------------
class Params:
    def __init__(self, etf_min_cr=1.0, min_days=60, max_etfs=50, max_stocks=30, max_financials=4,
                 max_per_sector=3, incumbent_edge=1.25, exclude=()):
        self.etf_min_cr, self.min_days, self.max_etfs = etf_min_cr, min_days, max_etfs
        self.max_stocks, self.max_financials, self.max_per_sector = max_stocks, max_financials, max_per_sector
        self.incumbent_edge = incumbent_edge
        self.exclude = {s.upper() for s in exclude}

    def text(self) -> str:
        return (f"Liquidity = median daily traded value (close x volume) over the last ~6 months. "
                f"ETFs: one per underlying index / commodity (the most liquid), no debt/liquid ETFs, "
                f"median >= Rs {self.etf_min_cr:g} cr/day, >= {self.min_days} trading days of history, max {self.max_etfs}. "
                f"Stocks: NIFTY 50 members, most liquid {self.max_stocks}, max {self.max_financials} financials and "
                f"max {self.max_per_sector} per other industry. Counters already in the list get a "
                f"{(self.incumbent_edge - 1) * 100:.0f}% edge. Held counters that drop out are not sold: "
                f"no new buys, target sell / averaging / Core 3x continue.")


def select_etfs(cands: List[dict], stats: Dict[str, dict], incumbents: set, p: Params) -> Tuple[List[dict], List[str]]:
    """cands: {symbol, underlying, name, sector}. Returns (picked rows with stats, notes)."""
    notes = []
    groups: Dict[str, List[dict]] = {}
    for c in cands:
        s = c["symbol"]
        if s in p.exclude or is_debt(c.get("underlying", "")) or is_debt(c.get("name", "")):
            continue
        st = stats.get(s)
        if not st or st["days"] < p.min_days or st["median_cr"] < p.etf_min_cr:
            continue
        score = st["median_cr"] * (p.incumbent_edge if s in incumbents else 1.0)
        groups.setdefault(norm_underlying(c.get("underlying") or c.get("name") or s), []).append(dict(c, score=score, **st))
    best = []
    for key, rows in groups.items():
        rows.sort(key=lambda r: -r["score"])
        best.append(rows[0])
        if len(rows) > 1:
            notes.append(f"{rows[0]['symbol']} chosen for {rows[0].get('underlying') or key} over "
                         + ", ".join(r["symbol"] for r in rows[1:4]))
    best.sort(key=lambda r: -r["score"])
    if len(best) > p.max_etfs:
        notes.append(f"{len(best) - p.max_etfs} ETFs above the floor left out by the {p.max_etfs}-ETF cap: "
                     + ", ".join(r["symbol"] for r in best[p.max_etfs:p.max_etfs + 10]))
    return best[:p.max_etfs], notes


def select_stocks(cands: List[dict], stats: Dict[str, dict], incumbents: set, p: Params) -> Tuple[List[dict], List[str]]:
    """cands: {symbol, name, industry, sector}. Industry drives the caps."""
    notes = []
    rows = []
    for c in cands:
        s = c["symbol"]
        st = stats.get(s)
        if s in p.exclude or not st or st["days"] < p.min_days:
            continue
        rows.append(dict(c, score=st["median_cr"] * (p.incumbent_edge if s in incumbents else 1.0), **st))
    rows.sort(key=lambda r: -r["score"])
    picked, per, fin = [], {}, 0
    capped = []
    for r in rows:
        if len(picked) >= p.max_stocks:
            break
        ind = r.get("industry") or r.get("sector") or "Other"
        if is_financial(ind):
            if fin >= p.max_financials:
                capped.append(r["symbol"])
                continue
            fin += 1
        else:
            k = ind.lower()
            if per.get(k, 0) >= p.max_per_sector:
                capped.append(r["symbol"])
                continue
            per[k] = per.get(k, 0) + 1
        picked.append(r)
    if capped:
        notes.append("Left out by the sector caps: " + ", ".join(capped))
    return picked, notes


def build(old: Dict[str, dict], held: set, etf_cands: List[dict], stock_cands: List[dict],
          stats: Dict[str, dict], p: Params, today: dt.date) -> Tuple[Dict[str, dict], List[dict], dict]:
    """Returns (new universe dict for the bot, rows for the sheet, summary).

    old: current universe (sym -> {type, include, name, sector, angel_symbol}).
    held: symbols with an open position or a Core lot (never removed from the universe).
    """
    inc_etf = {s for s, u in old.items() if u["type"] == ETF and u["include"]}
    inc_stk = {s for s, u in old.items() if u["type"] == STOCK and u["include"]}
    etfs, n1 = select_etfs(etf_cands, stats, inc_etf, p)
    stocks, n2 = select_stocks(stock_cands, stats, inc_stk, p)
    new: Dict[str, dict] = {}
    rows: List[dict] = []
    for kind, picked in ((ETF, etfs), (STOCK, stocks)):
        for r in picked:
            s = r["symbol"]
            o = old.get(s, {})
            new[s] = {"type": kind, "include": True, "name": o.get("name") or r.get("underlying") or r.get("name") or s,
                      "sector": o.get("sector") or r.get("sector") or r.get("industry") or "Other",
                      "angel_symbol": o.get("angel_symbol") or f"{s}-EQ"}
            rows.append(dict(new[s], symbol=s, status="KEPT" if (s in inc_etf or s in inc_stk) else "NEW",
                             **{k: r.get(k) for k in ("median_cr", "avg_cr", "avg_volume", "last_close", "days")}))
    removed, kept_held = [], []
    for s, o in old.items():
        if s in new:
            continue
        st = stats.get(s, {})
        if s in held:
            new[s] = dict(o, include=False)
            kept_held.append(s)
            status = "DROPPED - held (no new buys; sell/average as normal)"
        elif o["include"]:
            removed.append(s)
            status = "DROPPED"
        else:
            continue
        rows.append(dict(o, include=s in new and new[s]["include"], symbol=s, status=status,
                         **{k: st.get(k) for k in ("median_cr", "avg_cr", "avg_volume", "last_close", "days")}))
    added = [r["symbol"] for r in rows if r["status"] == "NEW"]
    summary = {"date": today.isoformat(), "etfs": len(etfs), "stocks": len(stocks), "added": added,
               "removed": removed, "held_dropped": kept_held, "notes": n1 + n2}
    return new, rows, summary


# ---------------- files ----------------
FIELDS = ["symbol", "type", "include", "name", "sector", "angel_symbol"]


def to_csv(universe: Dict[str, dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    order = sorted(universe.items(), key=lambda kv: (kv[1]["type"] != ETF, not kv[1]["include"]))
    for s, u in order:
        w.writerow([s, u["type"], "Y" if u["include"] else "N", u.get("name", ""), u.get("sector", ""),
                    u.get("angel_symbol") or f"{s}-EQ"])
    return buf.getvalue()


SHEET_HEADERS = ["Type", "Include (Y/N)", "NSE symbol", "Name / underlying", "Sector / category",
                 "Median traded value (Rs cr/day, 6m)", "Avg traded value (Rs cr/day, 6m)", "Avg daily volume (6m)",
                 "Last close", "Trading days used", "Status", "AngelOne symbol"]


def sheet_table(rows: List[dict]) -> List[list]:
    def k(r):
        return (r["type"] != ETF, not r.get("include", True), r["status"].startswith("DROPPED"),
                -(r.get("median_cr") or 0))
    out = []
    for r in sorted(rows, key=k):
        out.append([r["type"], "Y" if r.get("include", True) else "N", r["symbol"], r.get("name", ""), r.get("sector", ""),
                    r.get("median_cr") if r.get("median_cr") is not None else "",
                    r.get("avg_cr") if r.get("avg_cr") is not None else "",
                    r.get("avg_volume") if r.get("avg_volume") is not None else "",
                    r.get("last_close") if r.get("last_close") is not None else "",
                    r.get("days") if r.get("days") is not None else "", r["status"],
                    r.get("angel_symbol") or f"{r['symbol']}-EQ"])
    return out


def universe_from_table(values: List[list]) -> Dict[str, dict]:
    """Reads the bot's Universe tab back (for a rebuilt VM). Finds the header row by its first cell."""
    out = {}
    start = None
    for i, row in enumerate(values):
        if row and str(row[0]).strip() == SHEET_HEADERS[0] and len(row) > 2 and str(row[2]).strip() == SHEET_HEADERS[2]:
            start = i + 1
            break
    if start is None:
        return {}
    for row in values[start:]:
        row = list(row) + [""] * 12
        sym = str(row[2]).strip().upper()
        if not sym or str(row[10]).strip() == "DROPPED":
            continue
        out[sym] = {"type": ETF if str(row[0]).strip().upper() == "ETF" else STOCK,
                    "include": str(row[1]).strip().upper() == "Y", "name": row[3], "sector": row[4],
                    "angel_symbol": str(row[11]).strip() or f"{sym}-EQ"}
    return out


# ---------------- your own edits on the sheet's Watchlist ----------------
def universe_from_watchlist(values: List[list], old: Dict[str, dict], held: set,
                            min_included: int = 10) -> Tuple[Optional[Dict[str, dict]], List[str], List[str]]:
    """values: Watchlist rows from row 5, columns A..H (#, Type, Include, Symbol, Google, Yahoo, Name, Sector).

    Returns (universe or None if the sheet can't be trusted, changes, problems). Rules:
      * Type must be ETF or Stock and Include Y or N; other rows are skipped and reported.
      * A symbol listed twice: the first row wins.
      * A held counter missing from the sheet stays in the list with Include = N (it is never orphaned).
      * Fewer than `min_included` names with Include = Y (e.g. the tab was cleared by mistake): ignored.
    """
    new: Dict[str, dict] = {}
    problems: List[str] = []
    for i, row in enumerate(values):
        row = list(row) + [""] * 8
        sym = str(row[3]).strip().upper()
        if not sym:
            continue
        typ = str(row[1]).strip().upper()
        inc = str(row[2]).strip().upper()
        rn = i + 5
        if typ not in ("ETF", "STOCK"):
            problems.append(f"row {rn} {sym}: Type '{row[1]}' is not ETF or Stock (skipped)")
            continue
        if inc not in ("Y", "N"):
            problems.append(f"row {rn} {sym}: Include '{row[2]}' is not Y or N (treated as N)")
            inc = "N"
        if not re.fullmatch(r"[A-Z0-9&\-_.]+", sym):
            problems.append(f"row {rn}: '{sym}' does not look like an NSE symbol (skipped)")
            continue
        if sym in new:
            problems.append(f"row {rn} {sym}: listed twice, the first row is used")
            continue
        o = old.get(sym, {})
        new[sym] = {"type": ETF if typ == "ETF" else STOCK, "include": inc == "Y",
                    "name": str(row[6]).strip() or o.get("name", sym), "sector": str(row[7]).strip() or o.get("sector", ""),
                    "angel_symbol": o.get("angel_symbol") or f"{sym}-EQ"}
    n_inc = sum(1 for u in new.values() if u["include"])
    if n_inc < min_included:
        return None, [], problems + [f"only {n_inc} names have Include = Y on the Watchlist; the sheet list was ignored"]
    for s in sorted(held):
        if s not in new and s in old:
            new[s] = dict(old[s], include=False)
            problems.append(f"{s} is held but missing from the Watchlist: kept with Include = N (sell/average only)")
    changes = []
    for s in sorted(set(new) | set(old)):
        a, b = old.get(s), new.get(s)
        if a and not b:
            if a["include"]:
                changes.append(f"{s} removed")
        elif b and not a:
            if b["include"]:                       # an Include = N row the bot doesn't have changes nothing
                changes.append(f"{s} added ({b['type']}, Include Y)")
        elif a["include"] != b["include"]:
            changes.append(f"{s} Include {'N → Y' if b['include'] else 'Y → N'}")
        elif a["type"] != b["type"]:
            changes.append(f"{s} type {a['type']} → {b['type']}")
    return new, changes, problems
