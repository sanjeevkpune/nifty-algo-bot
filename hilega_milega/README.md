# Hilega-Milega weekly F&O alerts

**Alerts only.** Nothing here logs in to a broker or places an order. It runs on GitHub Actions, so the
trading VM can be off. **Every signal is alerted, no 15-stock cap** (a separate setup from the backtest's
15-slot portfolio).

## The rule

Weekly candles. RSI(9) of weekly closes, **green** = EMA(3) of the RSI, **red** = WMA(21) of the RSI.

* **BUY**: the first week the RSI closes above 50 **and** above both green and red. Every stock in the
  current NSE F&O list with 20-day average turnover above ₹5 crore. The alert also says whether NIFTY is
  above its 50-day average (the backtest only bought then); signals are tracked either way and the sheet
  keeps a Y/N column so both can be compared.
* **SELL**: an open signal's weekly RSI closes below 50.
* Act at the next open (normally Monday). Each signal is measured from that open to the open after its
  sell signal.

Backtest of exactly this, 2016 to Nov 2026, point-in-time F&O list: 5,914 signals, 30% winners,
average +2.7% net per signal (median -2.9%), +0.8% better than NIFTY over the same weeks, about 9 weeks
held. With the NIFTY filter on: +3.0% average, +1.0% vs NIFTY. A few big winners carry the average.

## When it runs

`.github/workflows/hilega-milega-weekly.yml`: Friday 16:45 IST, after the close. Back-up runs Saturday
09:00 and Monday 08:00 send only if Friday's run did not. Run it by hand from Actions > "Hilega-Milega:
weekly F&O alerts" > Run workflow (tick *dry run* to only see the alert in the log, or *resend* to send the
latest week again).

## What you get

* **Telegram** (same bot and chat as the other bots): sells, buys (biggest 3-month gain first), every
  stock now in a buy state with its gain, the ones weakening (RSI under both lines but still above 50),
  and live performance. It rings when there is a buy or sell.
* **Google Sheet** "V2-Algo Trade Logs", three tabs rewritten each run (don't edit them by hand):
  * `HM_Trades`: one row per signal: buy signal week and close, entry (next open), sell signal, exit,
    return, net return after 0.18% a side, NIFTY over the same period, net vs NIFTY, ₹ P&L on a notional
    ₹1 lakh per signal (repository variable `HM_PER_SIGNAL`), weeks held. `Source` = `live`, or
    `backtest seed` for stocks already in a buy state when tracking started (they get sell alerts but are
    left out of the performance numbers).
  * `HM_Signals`: every alert line: BUY, SELL, WATCH (an open signal turned weak).
  * `HM_Weekly`: one row a week: NIFTY vs its 50-day average, signals, open signals, live P&L, closed
    signals, win rate, average net return, average vs NIFTY, the same for NIFTY-filter-on signals.
* **Strategy Desk** dashboard: tab "Hilega-Milega alerts" reads those three tabs.

## Data

* F&O list: NSE `fo_mktlots.csv`, read every run. If NSE refuses GitHub's servers, the last list read is
  used, then `fno_list.txt` (28 Nov 2026), and the alert says so.
* Prices: Yahoo Finance daily candles (3 years), split-adjusted, turned into weekly candles as in the
  backtest. After a split or bonus, an open signal's entry price is re-read from the adjusted prices, and
  last week's signal is re-checked before it is filled.

## State

`seed_state.json`: the stocks in a buy state on 20 Nov 2026 (backtest replay from 2016 with the
point-in-time F&O list) and that week's pending orders. After each run the state is committed to the
branch `hilega-milega-state` (only `state.json`; main is never touched). To start over, delete that branch.

## Test

    python3 hilega_milega/test_hm_alerts.py        # offline, synthetic prices
