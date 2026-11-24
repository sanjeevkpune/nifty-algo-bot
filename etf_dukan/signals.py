"""
signals.py  --  cross-check the bot's own plan with the Google Sheet tracker's Signals tab.

The tracker (Signals!A7:J10) shows today's order plan from GOOGLEFINANCE prices and the rows on Holdings / Sold / Core:
    row 7  Sell            C = symbol, I = qty (50%)
    row 8  Buy: ETF slot   C = symbol, F = NEW BUY / AVERAGE, I = qty
    row 9  Buy: stock slot C = symbol, F = NEW BUY / AVERAGE, I = qty
    row 10 Core sell (3x)  C = symbol (the sheet shows one 3x lot at a time), I = qty

EKD_SIGNAL_POLICY
    agree  trade only the actions where the sheet and the bot name the same action and symbol;
           every disagreement is skipped and sent on Telegram.
    sheet  (default, user's choice 24 Nov 2026) trade what the sheet says; the bot still sizes the order from the live AngelOne price and enforces
           its safety checks (holding exists, quantity, cash, max positions, daily limits, broker shortfall).
    bot    trade the bot's own plan; disagreements are only reported.
    off    don't read the sheet.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import strategy as S


def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def parse_signals(rows: List[list]) -> dict:
    """rows = values of Signals!A7:J10 (unformatted). Raises ValueError if the sheet shows errors / is still loading."""
    rows = [list(r) + [""] * (10 - len(r)) for r in (rows or [])] + [[""] * 10] * (4 - len(rows or []))
    bad = [str(c) for r in rows for c in r if str(c).startswith("#") or str(c).lower().startswith("loading")]
    if bad:
        raise ValueError(f"sheet shows {', '.join(sorted(set(bad))[:3])} (prices still loading or a formula error)")

    def slot(r):
        sym = str(r[2]).strip().upper()
        if not sym:
            return None
        act = str(r[5]).strip().upper()
        return {"action": "NEW" if act.startswith("NEW") else "AVG" if act.startswith("AVER") else act,
                "sym": sym, "qty": _num(r[8])}

    sell = str(rows[0][2]).strip().upper()
    core = str(rows[3][2]).strip().upper()
    return {"sell": {"sym": sell, "qty": _num(rows[0][8])} if sell else None,
            "etf": slot(rows[1]), "stock": slot(rows[2]),
            "core": {"sym": core, "qty": _num(rows[3][8])} if core else None}


def _slot_of(a: dict) -> str:
    return "etf" if a["type"] == S.ETF else "stock"


def _fmt(x: Optional[dict]) -> str:
    if not x:
        return "nothing"
    return f"{x.get('action', 'SELL')} {x['sym']}" + (f" x{int(x['qty'])}" if x.get("qty") else "")


def cross_check_sells(bot: List[dict], sheet: dict, policy: str, state: dict, ltp: Dict[str, float],
                      rules: S.Rules, today, blocked: set) -> Tuple[List[dict], List[str]]:
    lines: List[str] = []
    b_sell = next((a for a in bot if a["action"] == "SELL"), None)
    b_core = [a for a in bot if a["action"] == "CORE_SELL"]
    s_sell, s_core = sheet.get("sell"), sheet.get("core")
    out: List[dict] = []

    same = (b_sell or {}).get("sym", "") == (s_sell or {}).get("sym", "")
    if not same:
        lines.append(f"Target sell: bot says {_fmt(b_sell)}, sheet says {_fmt(s_sell)}")
    if policy == "bot" and b_sell:
        out.append(b_sell)
    elif policy == "agree" and same and b_sell:
        out.append(b_sell)
    elif policy == "sheet" and s_sell:
        a = sheet_sell_action(s_sell["sym"], state, ltp, rules, today, blocked, lines)
        if a:
            out.append(a)

    b_core_syms = {a["sym"] for a in b_core}
    if s_core and s_core["sym"] not in b_core_syms:
        lines.append(f"Core 3x sell: sheet says {s_core['sym']}, bot sees no lot of it at {rules.core_multiple:g}x")
    for a in b_core:
        if not s_core or a["sym"] != s_core["sym"]:
            lines.append(f"Core 3x sell: bot says {a['sym']}, sheet shows {s_core['sym'] if s_core else 'none'}")
    if policy == "bot":
        out += b_core
    elif policy == "agree":
        out += [a for a in b_core if s_core and a["sym"] == s_core["sym"]]
    elif policy == "sheet" and s_core:
        lots = [l for l in state["core"] if l["sym"] == s_core["sym"]]
        if s_core["sym"] in blocked:
            lines.append(f"Sheet Core sell {s_core['sym']} blocked (AngelOne shortfall)")
        elif not lots or s_core["sym"] not in ltp:
            lines.append(f"Sheet Core sell {s_core['sym']}: bot holds no Core lot of it; not sent")
        else:
            for l in lots:
                out.append({"action": "CORE_SELL", "sym": l["sym"], "type": l["type"], "qty": l["qty"], "core_id": l["id"],
                            "ref": ltp[l["sym"]], "avg": l["cost_price"], "gain": ltp[l["sym"]] / l["cost_price"] - 1,
                            "why": "sheet Core 3x signal"})
    return out, lines


def sheet_sell_action(sym, state, ltp, rules, today, blocked, lines) -> Optional[dict]:
    if len(S.todays(state, today, ("SELL",))) >= rules.max_sells_per_day:
        lines.append(f"Sheet sell {sym}: today's sell is already done; not sent")
        return None
    p = state["positions"].get(sym)
    if sym in blocked:
        lines.append(f"Sheet sell {sym} blocked (AngelOne shortfall)")
        return None
    if not p or sym not in ltp:
        lines.append(f"Sheet sell {sym}: the bot has no open position in it; not sent")
        return None
    q = math.floor(p["qty"] * rules.sell_fraction) or p["qty"]
    g = ltp[sym] / S.avg_cost(p) - 1
    return {"action": "SELL", "sym": sym, "type": p["type"], "qty": q, "keep": p["qty"] - q, "ref": ltp[sym],
            "avg": S.avg_cost(p), "gain": g, "why": f"sheet signal ({g * 100:+.2f}% on avg cost at AngelOne price)"}


def cross_check_buys(bot: List[dict], sheet: dict, policy: str, state: dict, universe: Dict[str, dict],
                     ltp: Dict[str, float], rsi: Dict[str, float], rules: S.Rules, today, rates: S.ChargeRates,
                     blocked: set, cash_cap: Optional[float]) -> Tuple[List[dict], List[str]]:
    lines: List[str] = []
    by_slot = {_slot_of(a): a for a in bot}
    out: List[dict] = []
    for slot in ("etf", "stock"):
        b, s = by_slot.get(slot), sheet.get(slot)
        same = (b is None and s is None) or (b is not None and s is not None and b["action"] == s["action"] and b["sym"] == s["sym"])
        if not same:
            lines.append(f"{slot.upper()} slot: bot says {_fmt(b)}, sheet says {_fmt(s)}")
        if policy == "bot" and b:
            out.append(b)
        elif policy == "agree" and same and b:
            out.append(b)
    if policy == "sheet":
        out = sheet_buy_actions(sheet, state, universe, ltp, rsi, rules, today, rates, blocked, cash_cap, lines)
    return out, lines


def sheet_buy_actions(sheet, state, universe, ltp, rsi, rules, today, rates, blocked, cash_cap, lines) -> List[dict]:
    avail = S.cash_for_buys(state, rules, today)
    if cash_cap is not None:
        avail = min(avail, cash_cap)
    done = S.todays(state, today, ("NEW", "AVG"))
    avg_left = rules.max_avg_per_day - sum(1 for j in done if j["kind"] == "AVG")
    n_open = len(state["positions"])
    out = []
    for slot, kind in (("etf", S.ETF), ("stock", S.STOCK)):
        s = sheet.get(slot)
        if not s:
            continue
        sym, act = s["sym"], s["action"]
        u = universe.get(sym)
        why = None
        if not u or u["type"] != kind:
            why = "not in the bot's universe for this slot"
        elif sym in blocked:
            why = "blocked (AngelOne shortfall)"
        elif sym not in ltp:
            why = "no AngelOne price"
        elif act == "NEW":
            if any(j["kind"] == "NEW" and j["type"] == kind for j in done):
                why = f"today's {kind} new buy is already done"
            elif sym in state["positions"]:
                why = "already an open position"
            elif n_open >= rules.max_positions:
                why = f"{n_open} positions open (max {rules.max_positions})"
        elif act == "AVG":
            if sym not in state["positions"]:
                why = "the bot has no open position to average"
            elif avg_left <= 0:
                why = "today's averaging buy is already done"
        else:
            why = f"unknown action '{act}'"
        amount = rules.buy_amount if act == "NEW" else rules.avg_amount
        if not why:
            q = math.floor(amount / ltp[sym])
            val = q * ltp[sym]
            ch = S.charges("BUY", kind, val, rates)
            if q < 1:
                why = "price above the buy amount"
            elif avail < val + ch:
                why = f"needs ₹{val + ch:,.0f}, only ₹{max(avail, 0):,.0f} free for buys"
        if why:
            lines.append(f"Sheet {act} {sym}: {why}; not sent")
            continue
        avail -= val + ch
        if act == "NEW":
            n_open += 1
        else:
            avg_left -= 1
        out.append({"action": act, "sym": sym, "type": kind, "qty": q, "ref": ltp[sym], "est_value": val,
                    "rsi": rsi.get(sym), "why": "sheet signal"})
    return out
