#!/usr/bin/env python3
"""
Sync the ETF/stock list from your Google Sheet's Watchlist tab into etf_dukan/universe.csv.

Run by the GitHub workflow "ETF Bot: Sync list from sheet" (only when you start it). It:
  1. reads Watchlist B (Type), C (Include), D (NSE symbol), G (name), H (sector), rows 5-200,
  2. checks every row (ETF/Stock, Y/N, a symbol AngelOne knows),
  3. writes universe.csv + universe_synced.txt (time + what changed).
The workflow commits both files; the bot applies them at the start of its next run.
Held counters you removed from the sheet are kept by the bot with Include = N (sell / average only).

    python3 sheet_sync.py --sheet SHEET_ID --sa service_account.json [--no-scrip-check]
Exit code 0 = list written (or unchanged), 2 = sheet rejected (nothing written).
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import universe_refresh as UR  # noqa: E402

SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def load_csv(path):
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            s = r["symbol"].strip().upper()
            out[s] = {"type": UR.ETF if r["type"].strip().upper() == "ETF" else UR.STOCK,
                      "include": r["include"].strip().upper() == "Y", "name": r.get("name", ""),
                      "sector": r.get("sector", ""), "angel_symbol": (r.get("angel_symbol") or f"{s}-EQ").strip()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--sa", required=True, help="service account JSON file")
    ap.add_argument("--tab", default="Watchlist")
    ap.add_argument("--csv", default=os.path.join(HERE, "universe.csv"))
    ap.add_argument("--stamp", default=os.path.join(HERE, "universe_synced.txt"))
    ap.add_argument("--no-scrip-check", action="store_true")
    a = ap.parse_args()

    import gspread
    vals = gspread.service_account(filename=a.sa).open_by_key(a.sheet).worksheet(a.tab).get("A5:H200")
    old = load_csv(a.csv)
    new, changes, problems = UR.universe_from_watchlist(vals, old, set())
    if new is None:
        print("REJECTED:\n  " + "\n  ".join(problems))
        return 2
    if not a.no_scrip_check:
        try:
            raw = json.loads(urllib.request.urlopen(SCRIP_URL, timeout=120).read())
            known = {r["symbol"] for r in raw if r.get("exch_seg") == "NSE"}
            for s, u in list(new.items()):
                if u["angel_symbol"] not in known:
                    problems.append(f"{s}: not found at AngelOne as {u['angel_symbol']} (left out)")
                    del new[s]
                    changes = [c for c in changes if not c.startswith(s + " ")]
        except Exception as e:
            problems.append(f"AngelOne scrip master not checked ({e}); the bot will report unknown symbols")
    for p in problems:
        print("WARN", p)
    body = UR.to_csv(new)
    if not changes and open(a.csv).read() == body:
        print("No change: the sheet's list already matches universe.csv.")
        return 0
    with open(a.csv, "w", newline="") as f:
        f.write(body)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    with open(a.stamp, "w") as f:
        f.write(now.strftime("%Y-%m-%d %H:%M:%S IST") + "\n" + "\n".join(changes or ["names / sectors updated"]) + "\n")
    inc = [s for s, u in new.items() if u["include"]]
    print(f"Written: {sum(1 for s in inc if new[s]['type'] == UR.ETF)} ETFs + "
          f"{sum(1 for s in inc if new[s]['type'] == UR.STOCK)} stocks with Include = Y.")
    print("Changes:\n  " + "\n  ".join(changes or ["names / sectors only"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
