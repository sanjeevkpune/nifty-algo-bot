"""
strategy.py  --  ETF ki Dukan final rules (Nov 2026: max 30, keep 20%) as pure functions.

No broker, no network, no clock. The live bot (etf_dukan_bot.py) and the 5-year replay test
(tests/replay_test.py) both call these same functions, so what the bot does is what was backtested.

Rules (tracker Settings tab, best setup):
  * 3:00 pm  at most 1 target sell a day: the highest % gainer at/above target on AVERAGE COST
             (ETF +5%, stock +8%). Sell 80% (floor; a 1-share position is sold whole).
             The other 20% becomes a Core lot at the position's average cost.
  * 3:00 pm  a Core lot whose price >= 3x its cost is sold whole (extra to the 1 sell a day).
  * 3:10 pm  ETF slot and stock slot, Rs 40k each, whole shares.
             Slot = the lowest-RSI UNHELD name among the 8 lowest-RSI ETFs / 2 lowest-RSI stocks.
             Max 30 open positions (Core lots don't count).
             A counter sold today (target sell or Core 3x sell) is NOT bought back as a new buy the same day;
             rebuy_gap_days > 0 extends that wait (calendar days), -1 turns the block off (old behaviour).
             At most 1 averaging buy a day, only in a shop whose slot had no new buy: the holding that is
             most negative vs its LAST buy price, if at/below -4% (ETF) / -8% (stock). Rs 40k.
  * Cash     buys never use the owed monthly withdrawal or the Rs 15k kept back.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ETF, STOCK = "ETF", "Stock"


# ---------------------------------------------------------------------------
# Rules and charges
# ---------------------------------------------------------------------------
@dataclass
class Rules:
    buy_amount: float = 40_000.0      # every new buy and every averaging buy
    avg_amount: float = 40_000.0
    etf_target: float = 0.05
    etf_avg: float = -0.04
    stock_target: float = 0.08
    stock_avg: float = -0.08
    sell_fraction: float = 0.8        # sold at target; the rest becomes a Core lot
    core_multiple: float = 3.0        # Core lot sold whole at 3x its cost
    max_positions: int = 30
    etf_shortlist: int = 8
    stock_shortlist: int = 2
    rsi_period: int = 14
    max_sells_per_day: int = 1
    max_avg_per_day: int = 1
    withdrawal: float = 15_000.0
    withdraw_after_months: int = 6
    keep_back: float = 15_000.0
    rebuy_gap_days: int = 0           # no new buy of a counter sold within this many calendar days (0 = same day only, -1 = off)
    keep_back_months_before: int = 1  # backtest kept the Rs 15k back from 1 month before withdrawals start
    # per-symbol overrides: {"GOLDBEES": (target, avg_level)}
    symbol_levels: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    def target(self, sym: str, kind: str) -> float:
        if sym in self.symbol_levels:
            return self.symbol_levels[sym][0]
        return self.etf_target if kind == ETF else self.stock_target

    def avg_level(self, sym: str, kind: str) -> float:
        if sym in self.symbol_levels:
            return self.symbol_levels[sym][1]
        return self.etf_avg if kind == ETF else self.stock_avg


@dataclass
class ChargeRates:
    brokerage_per_order: float = 20.0
    brokerage_pct_cap: float = 0.001      # AngelOne delivery: Rs 20 or 0.1%, whichever is lower
    stt_stock: float = 0.001              # delivery, both sides
    stt_etf_sell: float = 0.00001         # equity ETF, sell only
    exchange: float = 0.0000297
    sebi: float = 0.000001
    stamp_buy: float = 0.00015
    gst: float = 0.18


def charges(side: str, kind: str, value: float, r: ChargeRates = ChargeRates()) -> float:
    """Estimated AngelOne delivery charges for one executed order. side = 'BUY' / 'SELL'."""
    side = side.upper()
    br = min(r.brokerage_per_order, value * r.brokerage_pct_cap)
    if kind == ETF:
        stt = value * r.stt_etf_sell if side == "SELL" else 0.0
    else:
        stt = value * r.stt_stock
    ex = value * r.exchange
    sebi = value * r.sebi
    stamp = value * r.stamp_buy if side == "BUY" else 0.0
    gst = r.gst * (br + ex + sebi)
    return br + stt + ex + sebi + stamp + gst


# ---------------------------------------------------------------------------
# RSI (Wilder, seeded with a simple average) -- same maths as backtest.py
# ---------------------------------------------------------------------------
def rsi_wilder(closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    ag = al = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        ag += max(ch, 0.0)
        al += max(-ch, 0.0)
    ag /= n
    al /= n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# state = {
#   "version": 1, "mode": "paper"|"live", "start_date": "YYYY-MM-DD",
#   "cash": float,                         # bot's own cash ledger (not the broker's whole balance)
#   "deposited": float, "withdrawn": float,
#   "positions": {SYM: {"type", "qty", "cost", "last", "first", "pid", "buys", "buy_charges"}},
#   "core": [{"id", "sym", "type", "qty", "cost_price", "since", "pid", "sold_row"}],
#   "journal": [ {trade record}, ... ],      # append-only: every fill, deposit, withdrawal
#   "pending": {tag: {intent}},             # order sent, fill not yet applied (crash recovery)
#   "applied_tags": [tag, ...],
#   "seq": {"pid": n, "core": n},
# }

def new_state(capital: float, start_date: dt.date, mode: str) -> dict:
    s = {"version": 1, "mode": mode, "start_date": start_date.isoformat(), "cash": 0.0,
         "deposited": 0.0, "withdrawn": 0.0, "positions": {}, "core": [], "journal": [],
         "pending": {}, "applied_tags": [], "seq": {"pid": 0, "core": 0}}
    deposit(s, capital, start_date, "Initial")
    return s


def deposit(state: dict, amount: float, date: dt.date, note: str = "Deposit") -> None:
    state["cash"] += amount
    state["deposited"] += amount
    state["journal"].append({"date": date.isoformat(), "kind": "DEPOSIT", "amount": round(amount, 2), "note": note})


def withdraw(state: dict, amount: float, date: dt.date, note: str = "Withdrawal") -> None:
    state["cash"] -= amount
    state["withdrawn"] += amount
    state["journal"].append({"date": date.isoformat(), "kind": "WITHDRAWAL", "amount": round(amount, 2), "note": note})


def avg_cost(p: dict) -> float:
    return p["cost"] / p["qty"] if p["qty"] else 0.0


def _add_months(d: dt.date, m: int) -> dt.date:
    mm = d.month - 1 + m
    y, mo = d.year + mm // 12, mm % 12 + 1
    return dt.date(y, mo, min(d.day, 28))


def withdrawal_start(state: dict, rules: Rules) -> dt.date:
    return _add_months(dt.date.fromisoformat(state["start_date"]), rules.withdraw_after_months)


def withdrawal_owed(state: dict, rules: Rules, today: dt.date) -> float:
    """Rs due so far minus Rs taken (the Capital tab's 'Withdrawal owed now')."""
    if rules.withdrawal <= 0:
        return 0.0
    ws = withdrawal_start(state, rules)
    if today < ws:
        return 0.0
    months = (today.year * 12 + today.month) - (ws.year * 12 + ws.month) + 1
    return max(0.0, months * rules.withdrawal - state["withdrawn"])


def keep_back(state: dict, rules: Rules, today: dt.date) -> float:
    if rules.withdrawal <= 0 or rules.keep_back <= 0:
        return 0.0
    ws = withdrawal_start(state, rules)
    return rules.keep_back if today >= ws - dt.timedelta(days=31 * rules.keep_back_months_before) else 0.0


def cash_for_buys(state: dict, rules: Rules, today: dt.date) -> float:
    return state["cash"] - withdrawal_owed(state, rules, today) - keep_back(state, rules, today)


def recently_sold(state: dict, today: dt.date, gap_days: int) -> set:
    """Symbols with a target sell or Core sell on or after today - gap_days (gap_days < 0: none)."""
    if gap_days < 0:
        return set()
    since = (today - dt.timedelta(days=gap_days)).isoformat()
    return {j["sym"] for j in state["journal"]
            if j["kind"] in ("SELL", "CORE_SELL") and since <= j["date"] <= today.isoformat()}


def todays(state: dict, today: dt.date, kinds) -> List[dict]:
    d = today.isoformat()
    return [j for j in state["journal"] if j["date"] == d and j["kind"] in kinds]


# ---------------------------------------------------------------------------
# Decisions (pure): return planned actions, never mutate state
# ---------------------------------------------------------------------------
def plan_sells(state: dict, ltp: Dict[str, float], rules: Rules, today: dt.date,
               blocked: Optional[set] = None) -> List[dict]:
    """3:00 pm. Target sell (max 1/day, counted from the journal so a re-run can't double-sell) + Core 3x exits."""
    blocked = blocked or set()
    out: List[dict] = []
    done = len(todays(state, today, ("SELL",)))
    # a planned sell that was only partly filled earlier today is NOT re-planned the same day
    for _ in range(max(0, rules.max_sells_per_day - done)):
        best = None
        for sym, p in state["positions"].items():
            if sym in blocked or sym not in ltp or p["qty"] <= 0:
                continue
            if any(a["sym"] == sym for a in out):
                continue
            g = ltp[sym] / avg_cost(p) - 1
            if g >= rules.target(sym, p["type"]) and (best is None or g > best[1]):
                best = (sym, g)
        if not best:
            break
        sym, g = best
        p = state["positions"][sym]
        q = math.floor(p["qty"] * rules.sell_fraction) or p["qty"]
        out.append({"action": "SELL", "sym": sym, "type": p["type"], "qty": q, "keep": p["qty"] - q,
                    "ref": ltp[sym], "avg": avg_cost(p), "gain": g,
                    "why": f"{g * 100:+.2f}% on avg cost >= target {rules.target(sym, p['type']) * 100:+.1f}%"})
    for lot in state["core"]:
        sym = lot["sym"]
        if sym in blocked or sym not in ltp:
            continue
        if ltp[sym] >= lot["cost_price"] * rules.core_multiple:
            out.append({"action": "CORE_SELL", "sym": sym, "type": lot["type"], "qty": lot["qty"], "core_id": lot["id"],
                        "ref": ltp[sym], "avg": lot["cost_price"], "gain": ltp[sym] / lot["cost_price"] - 1,
                        "why": f"Core lot at {ltp[sym] / lot['cost_price']:.2f}x cost (>= {rules.core_multiple:g}x)"})
    return out


def shortlist(universe: Dict[str, dict], rsi: Dict[str, float], ltp: Dict[str, float], kind: str, k: int) -> List[str]:
    c = [s for s, u in universe.items() if u["type"] == kind and u.get("include", True) and s in rsi and s in ltp]
    return sorted(c, key=lambda s: rsi[s])[:k]


def plan_buys(state: dict, universe: Dict[str, dict], rsi: Dict[str, float], ltp: Dict[str, float],
              rules: Rules, today: dt.date, rates: ChargeRates = ChargeRates(),
              blocked: Optional[set] = None, cash_cap: Optional[float] = None) -> Tuple[List[dict], List[str]]:
    """3:10 pm. Returns (actions, notes). Mirrors backtest.py with max_avg_day=1.

    cash_cap: an extra ceiling from the broker's available cash (live mode); the bot uses the lower of the two.
    """
    blocked = blocked or set()
    notes: List[str] = []
    avail = cash_for_buys(state, rules, today)
    if cash_cap is not None and cash_cap < avail:
        notes.append(f"Broker cash ₹{cash_cap:,.0f} is below the bot's ledger ₹{avail:,.0f}; using the broker figure.")
        avail = cash_cap
    pos = state["positions"]
    done_today = todays(state, today, ("NEW", "AVG"))
    new_done = {ETF: any(j["kind"] == "NEW" and j["type"] == ETF for j in done_today),
                STOCK: any(j["kind"] == "NEW" and j["type"] == STOCK for j in done_today)}
    avg_left = rules.max_avg_per_day - sum(1 for j in done_today if j["kind"] == "AVG")
    out: List[dict] = []
    n_open = len(pos)
    slot_filled = dict(new_done)
    cooling = recently_sold(state, today, rules.rebuy_gap_days)

    def cost_of(sym, kind, amount):
        px = ltp[sym]
        q = math.floor(amount / px)
        val = q * px
        return q, val, charges("BUY", kind, val, rates)

    for kind, k in ((ETF, rules.etf_shortlist), (STOCK, rules.stock_shortlist)):
        if new_done[kind]:
            continue
        sl = shortlist(universe, rsi, ltp, kind, k)
        cand = next((s for s in sl if s not in pos and s not in blocked and s not in cooling), None)
        skipped = [s for s in sl if s not in pos and s in cooling]
        if skipped and (cand is None or sl.index(skipped[0]) < sl.index(cand)):
            when = "today" if rules.rebuy_gap_days == 0 else f"in the last {rules.rebuy_gap_days} day(s)"
            notes.append(f"{kind} slot: {', '.join(skipped)} skipped, sold {when} (no buy-back).")
        if n_open >= rules.max_positions:
            if cand:
                notes.append(f"{kind} slot: {cand} skipped, {n_open} positions open (max {rules.max_positions}).")
            continue
        if not cand:
            notes.append(f"{kind} slot: every name on the shortlist ({', '.join(sl) or 'none'}) is already held"
                         + (" or was just sold." if skipped else "."))
            continue
        q, val, ch = cost_of(cand, kind, rules.buy_amount)
        if q < 1:
            notes.append(f"{kind} slot: {cand} at ₹{ltp[cand]:,.2f} costs more than ₹{rules.buy_amount:,.0f}.")
            continue
        if avail < val + ch:
            notes.append(f"{kind} slot: {cand} needs ₹{val + ch:,.0f}, only ₹{max(avail, 0):,.0f} free for buys.")
            continue
        avail -= val + ch
        n_open += 1
        slot_filled[kind] = True
        out.append({"action": "NEW", "sym": cand, "type": kind, "qty": q, "ref": ltp[cand], "est_value": val,
                    "rsi": rsi.get(cand), "why": f"lowest-RSI unheld {kind} (RSI {rsi.get(cand, 0):.1f}) in top {k}"})

    if avg_left > 0:
        elig = [kind for kind in (ETF, STOCK) if not slot_filled[kind]]
        cands = []
        for sym, p in pos.items():
            if p["type"] not in elig or sym in blocked or sym not in ltp or p["qty"] <= 0:
                continue
            drop = ltp[sym] / p["last"] - 1
            if drop <= rules.avg_level(sym, p["type"]):
                cands.append((drop, sym))
        cands.sort()
        for drop, sym in cands:
            if avg_left <= 0:
                break
            p = pos[sym]
            q, val, ch = cost_of(sym, p["type"], rules.avg_amount)
            if q < 1 or avail < val + ch:
                notes.append(f"Averaging: {sym} ({drop * 100:+.2f}% vs last buy) needs ₹{val + ch:,.0f}, only ₹{max(avail, 0):,.0f} free.")
                continue
            avail -= val + ch
            avg_left -= 1
            out.append({"action": "AVG", "sym": sym, "type": p["type"], "qty": q, "ref": ltp[sym], "est_value": val,
                        "drop": drop, "rsi": rsi.get(sym),
                        "why": f"{drop * 100:+.2f}% vs last buy ₹{p['last']:,.2f} (level {rules.avg_level(sym, p['type']) * 100:+.0f}%)"})
        if not cands and elig:
            notes.append("Averaging: no holding is at its averaging level.")
    return out, notes


# ---------------------------------------------------------------------------
# Applying fills (mutate state). Every fill is journalled exactly once per order tag.
# ---------------------------------------------------------------------------
def _journal(state, rec):
    state["journal"].append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in rec.items()})


def apply_buy(state: dict, sym: str, kind: str, qty: int, price: float, chg: float, date: dt.date,
              buy_kind: str, tag: str = "", order_ids: str = "", rsi: Optional[float] = None) -> dict:
    pos = state["positions"]
    if sym not in pos:
        state["seq"]["pid"] += 1
        pos[sym] = {"type": kind, "qty": 0, "cost": 0.0, "last": 0.0, "first": date.isoformat(),
                    "pid": state["seq"]["pid"], "buys": 0, "buy_charges": 0.0}
    p = pos[sym]
    prev_last = p["last"]
    val = qty * price
    p["qty"] += qty
    p["cost"] += val
    p["last"] = price
    p["buys"] += 1
    p["buy_charges"] += chg
    state["cash"] -= val + chg
    rec = {"date": date.isoformat(), "kind": buy_kind, "sym": sym, "type": kind, "qty": qty, "price": price,
           "value": val, "charges": chg, "avg_after": avg_cost(p), "pid": p["pid"], "tag": tag,
           "order_ids": order_ids, "rsi": rsi, "drop": (price / prev_last - 1) if prev_last else None}
    _journal(state, rec)
    if tag:
        state["applied_tags"].append(tag)
    return rec


def apply_sell(state: dict, sym: str, qty: int, price: float, chg: float, date: dt.date,
               planned_qty: int, keep_qty: int, tag: str = "", order_ids: str = "") -> dict:
    """Target sell. keep_qty (planned at 3:00 pm) becomes a Core lot. If the order filled only partly,
    the unfilled part of the planned sell stays an open position (avg cost and last buy price unchanged)."""
    p = state["positions"][sym]
    avg = avg_cost(p)
    val = qty * price
    alloc_buy_chg = p["buy_charges"] * qty / p["qty"] if p["qty"] else 0.0
    gross = (price - avg) * qty
    net = gross - chg - alloc_buy_chg
    state["cash"] += val - chg
    core_id = None
    keep_qty = min(keep_qty, p["qty"] - qty)
    if keep_qty > 0:
        state["seq"]["core"] += 1
        core_id = state["seq"]["core"]
        state["core"].append({"id": core_id, "sym": sym, "type": p["type"], "qty": keep_qty, "cost_price": avg,
                              "since": date.isoformat(), "pid": p["pid"], "first": p["first"], "sold_row": None})
    left = p["qty"] - qty - keep_qty
    rec = {"date": date.isoformat(), "kind": "SELL", "sym": sym, "type": p["type"], "qty": qty, "price": price,
           "value": val, "charges": chg, "avg": avg, "gross": gross, "buy_charges_alloc": alloc_buy_chg, "net": net,
           "gain": price / avg - 1, "held_qty": p["qty"], "kept": keep_qty, "core_id": core_id, "left_open": left,
           "first": p["first"], "pid": p["pid"], "tag": tag, "order_ids": order_ids, "planned_qty": planned_qty}
    if left > 0:
        frac = left / p["qty"]
        p["buy_charges"] *= frac
        p["cost"] = avg * left
        p["qty"] = left
    else:
        del state["positions"][sym]
    _journal(state, rec)
    if tag:
        state["applied_tags"].append(tag)
    return rec


def apply_core_sell(state: dict, core_id: int, qty: int, price: float, chg: float, date: dt.date,
                    tag: str = "", order_ids: str = "") -> dict:
    lot = next(l for l in state["core"] if l["id"] == core_id)
    val = qty * price
    gross = (price - lot["cost_price"]) * qty
    state["cash"] += val - chg
    rec = {"date": date.isoformat(), "kind": "CORE_SELL", "sym": lot["sym"], "type": lot["type"], "qty": qty,
           "price": price, "value": val, "charges": chg, "avg": lot["cost_price"], "gross": gross, "net": gross - chg,
           "gain": price / lot["cost_price"] - 1, "core_id": core_id, "since": lot["since"],
           "sold_row": lot.get("sold_row"), "tag": tag, "order_ids": order_ids}
    lot["qty"] -= qty
    if lot["qty"] <= 0:
        state["core"].remove(lot)
    _journal(state, rec)
    if tag:
        state["applied_tags"].append(tag)
    return rec


def bot_quantities(state: dict) -> Dict[str, int]:
    """Shares the bot believes it owns per symbol (open + Core)."""
    q: Dict[str, int] = {}
    for s, p in state["positions"].items():
        q[s] = q.get(s, 0) + p["qty"]
    for l in state["core"]:
        q[l["sym"]] = q.get(l["sym"], 0) + l["qty"]
    return q


def summary(state: dict, ltp: Dict[str, float], rules: Rules, today: dt.date) -> dict:
    omv = sum(p["qty"] * ltp.get(s, p["last"]) for s, p in state["positions"].items())
    ocost = sum(p["cost"] for p in state["positions"].values())
    cmv = sum(l["qty"] * ltp.get(l["sym"], l["cost_price"]) for l in state["core"])
    ccost = sum(l["qty"] * l["cost_price"] for l in state["core"])
    realised = sum(j.get("net", 0.0) for j in state["journal"] if j["kind"] in ("SELL", "CORE_SELL"))
    return {"cash": state["cash"], "free_for_buys": cash_for_buys(state, rules, today),
            "owed": withdrawal_owed(state, rules, today), "kept_back": keep_back(state, rules, today),
            "open_positions": len(state["positions"]), "open_cost": ocost, "open_value": omv,
            "core_lots": len(state["core"]), "core_cost": ccost, "core_value": cmv,
            "net_worth": state["cash"] + omv + cmv, "realised_net": realised,
            "deposited": state["deposited"], "withdrawn": state["withdrawn"]}
