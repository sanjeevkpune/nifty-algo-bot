#!/usr/bin/env python3
"""
test_adaptive_exit.py  --  offline tests for the live exit path of adaptive_strangle_v2.py.

No broker, no network: a fake Angel One SmartConnect plays the order book and positions.
Run:  python3 test_adaptive_exit.py        (exit code 0 = all passed)

Scenarios
  1. 27 Nov 2026 failure: MARKET orders sit "open" and never fill. The bot must still go flat
     (it now exits with re-priced LIMIT orders and cancels whatever does not fill).
  2. Limit fills only after re-pricing (offer moves away), with a partial fill on the way.
  3. Cancel is slow ("cancel pending" for a few polls): the bot must never stack a second order.
  4. Limit rejected (price band): one MARKET attempt follows and fills.
  5. Full run of the time exit (close_leg for CE + PE, then verify_flat) on a stuck-market broker.
"""
import os
import sys
import types
import time

# --- stubs so the bot module imports without its live dependencies / credentials --------------
for k in ("API_KEY", "CLIENT_CODE", "PIN", "TOTP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GSHEET_ID_V2"):
    os.environ.setdefault(k, "x")
os.environ["PAPER_MODE"] = "0"
os.environ.setdefault("EXIT_WAIT_SECS", "1")
os.environ.setdefault("CANCEL_CONFIRM_SECS", "3")
for name in ("SmartApi", "pyotp", "gspread"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            m = types.ModuleType(name)
            m.SmartConnect = object
            sys.modules[name] = m

import adaptive_strangle_v2 as bot_mod   # noqa: E402

bot_mod.ORDER_POLL_SECS = 0.05
bot_mod.tg = lambda msg: print(f"  [tg] {msg.splitlines()[0]}")
_real_sleep = time.sleep
bot_mod.time.sleep = lambda s: _real_sleep(min(s, 0.05))   # keep the suite fast


class FakeAngel:
    """Tiny Angel One: orders, positions and FULL quotes for NFO options."""

    def __init__(self, positions, ask=70.0, bid=69.5, market_fills=False, cancel_lag_polls=0,
                 ask_moves=None, partial_first=0, reject_limits=0, upper=None):
        self.net = dict(positions)            # symbol -> netqty
        self.tokens = {s: str(1000 + i) for i, s in enumerate(positions)}
        self.ask, self.bid, self.upper = ask, bid, upper
        self.market_fills = market_fills
        self.cancel_lag_polls = cancel_lag_polls
        self.ask_moves = list(ask_moves or [])   # ask after each limit placed
        self.partial_first = partial_first
        self.reject_limits = reject_limits
        self.orders = []
        self.placed = []
        self.max_live = 0

    # orders
    def placeOrder(self, p):
        oid = str(len(self.orders) + 1)
        o = {"orderid": oid, "tradingsymbol": p["tradingsymbol"], "transactiontype": p["transactiontype"],
             "ordertype": p["ordertype"], "price": float(p["price"]), "quantity": int(p["quantity"]),
             "filledshares": 0, "averageprice": 0.0, "orderstatus": "open", "variety": "NORMAL", "text": "",
             "_cancel_at": None}
        self.orders.append(o)
        self.placed.append((p["ordertype"], p["transactiontype"], int(p["quantity"]), float(p["price"])))
        live = [x for x in self.orders if x["orderstatus"] in ("open", "cancel pending")
                and x["tradingsymbol"] == o["tradingsymbol"]]
        self.max_live = max(self.max_live, len(live))
        if p["ordertype"] == "LIMIT" and self.reject_limits > 0:
            self.reject_limits -= 1
            o["orderstatus"], o["text"] = "rejected", "Price is out of the price band"
            return oid
        self._match(o)
        if p["ordertype"] == "LIMIT" and self.ask_moves:
            self.ask = self.ask_moves.pop(0)
            self.bid = self.ask - 0.5
        return oid

    def _fill(self, o, qty, px):
        sign = 1 if o["transactiontype"] == "BUY" else -1
        self.net[o["tradingsymbol"]] = self.net.get(o["tradingsymbol"], 0) + sign * qty
        prev = o["filledshares"]
        o["averageprice"] = (o["averageprice"] * prev + px * qty) / (prev + qty)
        o["filledshares"] = prev + qty
        if o["filledshares"] == o["quantity"]:
            o["orderstatus"] = "complete"

    def _match(self, o):
        if o["orderstatus"] != "open":
            return
        if o["ordertype"] == "MARKET":
            if self.market_fills:
                self._fill(o, o["quantity"] - o["filledshares"], self.ask if o["transactiontype"] == "BUY" else self.bid)
            return
        crosses = o["price"] >= self.ask if o["transactiontype"] == "BUY" else o["price"] <= self.bid
        if crosses:
            px = self.ask if o["transactiontype"] == "BUY" else self.bid
            if self.partial_first:
                q, self.partial_first = self.partial_first, 0
                self._fill(o, q, px)
            else:
                self._fill(o, o["quantity"] - o["filledshares"], px)

    def cancelOrder(self, oid, variety):
        for o in self.orders:
            if o["orderid"] == oid and o["orderstatus"] == "open":
                o["orderstatus"] = "cancel pending"
                o["_cancel_at"] = self.cancel_lag_polls
        return {"status": True, "data": {"orderid": oid}}

    def orderBook(self):
        for o in self.orders:
            if o["orderstatus"] == "cancel pending":
                if o["_cancel_at"] <= 0:
                    o["orderstatus"] = "cancelled"
                else:
                    o["_cancel_at"] -= 1
        return {"status": True, "data": [dict(o) for o in self.orders]}

    def position(self):
        return {"status": True, "data": [
            {"exchange": "NFO", "tradingsymbol": s, "symboltoken": self.tokens[s], "producttype": "INTRADAY",
             "netqty": str(q)} for s, q in self.net.items()]}

    def getMarketData(self, mode, req):
        tok = req["NFO"][0]
        d = {"symbolToken": tok, "ltp": (self.ask + self.bid) / 2,
             "depth": {"buy": [{"price": self.bid, "quantity": 650}], "sell": [{"price": self.ask, "quantity": 650}]}}
        if self.upper:
            d["upperCircuit"], d["lowerCircuit"] = self.upper, 0.05
        return {"status": True, "data": {"fetched": [d]}}


def make_bot(fake):
    b = bot_mod.Bot()
    b.smart = fake
    b.traded_symbols = set(fake.net)
    return b


PE = "NIFTY08DEC2625150PE"
CE = "NIFTY08DEC2626550CE"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def no_live_orders(fake):
    return all(o["orderstatus"] in ("complete", "cancelled", "rejected") for o in fake.orders)


# 1. 27 Nov: market orders never fill
f = FakeAngel({PE: -260}, market_fills=False)
b = make_bot(f)
ok, fills, prob = b.flatten_symbol(PE, f.tokens[PE])
check("1 stuck MARKET broker -> flat with LIMIT", ok and f.net[PE] == 0 and no_live_orders(f), f"orders={f.placed}")

# 2. needs re-pricing + partial fill
f = FakeAngel({PE: -260}, ask=70, ask_moves=[80, 95, 95, 95], partial_first=65)
b = make_bot(f)
ok, fills, prob = b.flatten_symbol(PE, f.tokens[PE])
check("2 re-price after offer moves, partial fill not doubled", ok and f.net[PE] == 0 and no_live_orders(f),
      f"orders={f.placed} fills={fills}")

# 3. slow cancel: never two live orders at once
f = FakeAngel({PE: -260}, ask=70, ask_moves=[90, 90, 90], cancel_lag_polls=3)
b = make_bot(f)
ok, fills, prob = b.flatten_symbol(PE, f.tokens[PE])
check("3 slow cancel -> no stacked orders", ok and f.net[PE] == 0 and f.max_live == 1, f"max_live={f.max_live}")

# 4. limit rejected -> market fallback
f = FakeAngel({PE: -260}, market_fills=True, reject_limits=1)
b = make_bot(f)
ok, fills, prob = b.flatten_symbol(PE, f.tokens[PE])
check("4 LIMIT rejected -> MARKET next", ok and f.net[PE] == 0 and f.placed[1][0] == "MARKET", f"orders={f.placed}")

# 4b. limit price respects the upper circuit and the tick
q = {"ltp": 64.0, "bid": 63.9, "ask": 64.1, "upper": 66.0}
px = bot_mod.Bot._exit_limit_price("BUY", q, 0)
check("4b limit capped at circuit, on 0.05 tick", px <= 66.0 and abs(px * 20 - round(px * 20)) < 1e-6, f"px={px}")

# 5. full time exit on a stuck-market broker (CE + PE short, 4 lots)
f = FakeAngel({CE: -260, PE: -260}, market_fills=False)
b = make_bot(f)
b.legs = [bot_mod.Leg("SHORT_CE", "CE", 26550, CE, f.tokens[CE], "SELL", 260, 59.09),
          bot_mod.Leg("SHORT_PE", "PE", 25150, PE, f.tokens[PE], "SELL", 260, 64.05)]
b.cancel_pending_orders("at exit time")
for leg in b.active():
    b.close_leg(leg, 70.0)
flat = b.verify_flat()
check("5 full time-exit goes flat and verified", flat and f.net == {CE: 0, PE: 0} and not b.active() and no_live_orders(f),
      f"realized={b.realized:.0f} net={f.net}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
