#!/usr/bin/env python3
"""
daily_summary.py  --  one Telegram message at 3:45 pm IST with the day of every bot on the VM.

It only READS the files the bots already write (no broker login, no Google Sheet calls), so it can
never disturb a bot:
  Adaptive strangle (AngelOne)  trade_log_local.csv (today's last row) + run_lock_<date>_<mode>.json
  Lemonn v3 dual-session (Dhan) state/lemonn_dual_<yyyymmdd>.json
  Lemonn v4 sideways (Dhan)     state/lemonn_v4_<yyyymmdd>.json
  ETF ki Dukan (AngelOne)       etf_dukan/logs/<mode>_bot_log.csv + etf_dukan/locks/run_<date>_<mode>.json
plus the VM's disk use.

Totals are REALISED P&L only, live and paper kept apart (never mixed with unrealised gains).
The Dhan bots report P&L before charges; the adaptive strangle and the ETF bot report it after charges.

Usage:  python3 daily_summary.py              send today's summary
        python3 daily_summary.py --print      print only, no Telegram
        python3 daily_summary.py --date 2026-11-27 --print
"""
import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
IST = ZoneInfo("Asia/Kolkata")


def load_env(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except OSError:
            pass


load_env("/etc/trade-config/config.env")
load_env("/etc/trade-config/etf_dukan.env")


def env_on(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "y")


def inr(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return f"₹{x:+,.0f}"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def read_csv_rows(path, date_str):
    try:
        with open(path, newline="") as f:
            return [r for r in csv.DictReader(f) if (r.get("Date") or "").strip() == date_str]
    except Exception:
        return []


class Summary:
    def __init__(self, day: dt.date):
        self.day = day
        self.lines = []
        self.issues = []
        self.totals = {"LIVE": 0.0, "PAPER": 0.0}
        self.ran = 0

    def add(self, text, pnl=None, mode=None):
        self.lines.append(text)
        if pnl is not None and mode in self.totals:
            self.totals[mode] += pnl

    # ---------------- AngelOne adaptive strangle ----------------
    def adaptive(self):
        head = "🟠 Adaptive Strangle"
        rows = read_csv_rows(os.path.join(HERE, os.environ.get("LOCAL_LOG_FILE", "trade_log_local.csv")), str(self.day))
        mode = "PAPER" if env_on("PAPER_MODE", "1") else "LIVE"
        locks = {m: read_json(os.path.join(HERE, f"run_lock_{self.day}_{m}.json")) for m in ("live", "paper")}
        if not rows:
            lock = locks["live"] or locks["paper"]
            if lock and lock.get("status") == "active":
                self.add(f"{head} [{mode}]: ⚠️ still marked OPEN, no result row")
                self.issues.append("Adaptive strangle: positions may still be open. Check AngelOne.")
            else:
                self.add(f"{head} [{mode}]: did not run today")
                self.issues.append("Adaptive strangle did not run (no log row today).")
            return
        self.ran += 1
        r = rows[-1]
        mode = "PAPER" if "PAPER" in (r.get("Strategy_Version") or "") else "LIVE"
        status = (r.get("Session_Status") or "").upper()
        reason = r.get("Exit_Reason") or ""
        if status == "TRADED":
            net = num(r.get("Net_PnL"))
            rolled = r.get("Rolled_Status") or "No"
            self.add(f"{head} [{mode}]: traded · net {inr(net)} (charges ₹{num(r.get('Charges')):,.0f})"
                     f"{'' if rolled == 'No' else ' · ' + rolled}\n   exit: {reason}", net, mode)
            if "NOT VERIFIED FLAT" in reason:
                self.issues.append("Adaptive strangle: exit NOT verified flat. Check AngelOne.")
        elif status == "SKIPPED":
            self.add(f"{head} [{mode}]: no trade · {reason}", 0.0, mode)
        elif status == "CRASHED":
            self.add(f"{head} [{mode}]: 🚨 crashed · {reason}")
            self.issues.append("Adaptive strangle crashed today. Check AngelOne positions.")
        else:   # ENTERED with no exit row
            self.add(f"{head} [{mode}]: ⚠️ entered, no exit row yet")
            self.issues.append("Adaptive strangle has no exit row. Check AngelOne positions.")

    # ---------------- Dhan bots (shared state format) ----------------
    def dhan(self, head, fname, paper, done_phase="done"):
        mode = "PAPER" if paper else "LIVE"
        st = read_json(os.path.join(os.environ.get("LEMONN_STATE_DIR", os.path.join(HERE, "state")), fname))
        if not st:
            self.add(f"{head} [{mode}]: did not run today")
            self.issues.append(f"{head.split(' ', 1)[1]} did not run (no state file today).")
            return
        self.ran += 1
        legs = st.get("legs") or []
        sells = [l for l in legs if l.get("side") == "SELL"]
        open_legs = [l for l in legs if l.get("active")]
        pnl = num(st.get("realized_pnl"))
        decision = (st.get("info") or {}).get("decision", "")
        if open_legs:
            self.add(f"{head} [{mode}]: ⚠️ {len(open_legs)} leg(s) still open · realised {inr(pnl)}")
            self.issues.append(f"{head.split(' ', 1)[1]}: legs still open at 3:45 pm.")
            return
        if not sells:
            why = decision.replace("SKIP: ", "") if decision else ("halted" if st.get("halted") else "filters said no")
            self.add(f"{head} [{mode}]: no trade · {why}", 0.0, mode)
            return
        sessions = list(dict.fromkeys(l.get("session", "") for l in sells if l.get("session")))
        reasons = sorted({l.get("exit_reason", "") for l in sells if l.get("exit_reason")})
        self.add(f"{head} [{mode}]: traded {', '.join(s.split('_')[0].title() for s in sessions) or ''}"
                 f" · {inr(pnl)} before charges\n   exits: {', '.join(reasons) or 'n/a'}", pnl, mode)
        if st.get("phase") != done_phase:
            self.issues.append(f"{head.split(' ', 1)[1]} did not finish its day (phase {st.get('phase')}).")

    # ---------------- ETF ki Dukan ----------------
    def etf(self):
        head = "🟢 ETF ki Dukan"
        paper = os.environ.get("EKD_PAPER_MODE", "1").strip() != "0"     # same rule as the bot: only 0 is live
        mname = "paper" if paper else "live"
        mode = mname.upper()
        home = os.environ.get("EKD_HOME", os.path.join(HERE, "etf_dukan"))
        lock = read_json(os.path.join(home, "locks", f"run_{self.day}_{mname}.json")) or {}
        rows = read_csv_rows(os.path.join(home, "logs", f"{mname}_bot_log.csv"), str(self.day))
        if not lock and not rows:
            self.add(f"{head} [{mode}]: did not run today")
            self.issues.append("ETF ki Dukan did not run (no lock or log row today).")
            return
        self.ran += 1
        ev = lambda r: (r.get("Event") or "").upper()
        sells = [r for r in rows if ev(r).startswith(("SELL", "CORE_SELL"))]
        buys = [r for r in rows if ev(r) in ("NEW", "AVG")]
        skipped = [r for r in rows if ev(r) == "SKIPPED"]
        done = [r for r in rows if ev(r) == "DAY DONE"]
        realised = sum(num(r.get("Net P&L")) for r in sells)
        if skipped and not (sells or buys):
            self.add(f"{head} [{mode}]: skipped · {skipped[-1].get('Note', '')}", 0.0, mode)
            return
        parts = []
        if sells:
            parts.append("sold " + ", ".join(f"{r.get('Symbol')} {inr(r.get('Net P&L'))}" for r in sells))
        if buys:
            parts.append("bought " + ", ".join(f"{r.get('Symbol')} ({r.get('Event', '').title()})" for r in buys))
        txt = f"{head} [{mode}]: " + ("; ".join(parts) if parts else "no trades")
        if sells:
            txt += f"\n   realised today {inr(realised)} after charges"
        if done:
            txt += f"\n   {etf_book(done[-1].get('Note', ''))}"
        self.add(txt, realised, mode)
        status = lock.get("status")
        if status and status not in ("done", "skipped"):
            self.issues.append(f"ETF ki Dukan stopped part-way (status {status}). Check the AngelOne order book.")

    # ---------------- VM ----------------
    def vm(self):
        try:
            du = shutil.disk_usage("/")
            pct = du.used / du.total * 100
            free_gb = du.free / 1e9
            warn = int(os.environ.get("DISK_WARN_PCT", "80"))
            self.add(f"🖥️ VM disk {pct:.0f}% used ({free_gb:.1f} GB free)")
            if pct >= warn:
                self.issues.append(f"VM disk is {pct:.0f}% full. Old logs are cleaned daily; check what is growing.")
        except Exception as e:
            self.add(f"🖥️ VM disk: could not read ({e})")

    def text(self):
        live, paper = self.totals["LIVE"], self.totals["PAPER"]
        out = [f"📊 DAILY SUMMARY · {self.day:%a %d-%b-%Y}", ""] + self.lines[:-1] + [
            "",
            f"Realised today: LIVE {inr(live)} | PAPER {inr(paper)}",
            self.lines[-1],
        ]
        if self.issues:
            out += ["", "⚠️ Needs a look:"] + [f"· {i}" for i in self.issues]
        else:
            out += ["✅ No problems found."]
        return "\n".join(out)


def etf_book(note):
    """'cash 1200000; open 12; core 3; net worth 4100000' -> 'cash ₹12.00L · open 12 · core 3 · net worth ₹41.00L'"""
    out = []
    for part in note.split(";"):
        k, _, v = part.strip().rpartition(" ")
        if not k:
            continue
        out.append(f"{k} ₹{num(v) / 1e5:,.2f}L" if k in ("cash", "net worth") else f"{k} {v}")
    return " · ".join(out) or note


def is_holiday(day):
    if day.weekday() >= 5:
        return True
    try:
        with open(os.path.join(HERE, "etf_dukan", "nse_holidays.txt")) as f:
            if day.isoformat() in {l.split("#")[0].strip() for l in f}:
                return True
    except OSError:
        pass
    try:
        import pandas_market_calendars as mcal
        return mcal.get_calendar("XNSE").schedule(start_date=str(day), end_date=str(day)).empty
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD (default today, IST)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print only, do not send")
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(IST).date()

    s = Summary(day)
    s.adaptive()
    s.dhan("🔷 Lemonn v3 Dual-Session", f"lemonn_dual_{day:%Y%m%d}.json",
           paper=True if day <= dt.date(2026, 12, 31) else env_on("DHAN_PAPER_MODE"))
    s.dhan("🔶 Lemonn v4 Sideways", f"lemonn_v4_{day:%Y%m%d}.json",
           paper=not (day > dt.date(2026, 12, 31) and env_on("DHAN_V4_LIVE")))
    s.etf()
    s.vm()

    if is_holiday(day):
        msg = f"📊 DAILY SUMMARY · {day:%a %d-%b-%Y}\n📅 Market holiday: no bot traded today.\n{s.lines[-1]}"
        loud = False
    else:
        msg = s.text()
        loud = True          # the one message a day that should ring
    print(msg)
    if a.print_only:
        return
    try:
        import notify
        notify.send("", msg, loud=loud)
    except Exception as e:
        print(f"Telegram error: {e}")


if __name__ == "__main__":
    sys.exit(main())
