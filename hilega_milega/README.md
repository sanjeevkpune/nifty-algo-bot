# Hilega-Milega weekly F&O alerts

**Alerts only.** Nothing here logs in to a broker or places an order. It runs on GitHub Actions, so the
trading VM can be off.

## The rule (best weekly rule from the 2016-2026 backtest: 14.9% a year, worst fall -26.7%)

Weekly candles. RSI(9) of weekly closes, **green** = EMA(3) of the RSI, **red** = WMA(21) of the RSI.

* **BUY**: the first week the RSI closes above 50 **and** above both green and red. Only stocks in the
  current NSE F&O list with 20-day average turnover above ₹5 crore, and only while NIFTY closes above its
  50-day average. With more signals than free slots, the biggest 3-month gain goes first. Max 15 stocks,
  equal size (1/15 of capital each).
* **SELL**: a held stock's weekly RSI closes below 50.
* Act at the next open (normally Monday).

## When it runs

`.github/workflows/hilega-milega-weekly.yml`: Friday 16:45 IST, after the close. Back-up runs Saturday
09:00 and Monday 08:00 send only if Friday's run did not. Run it by hand from Actions > "Hilega-Milega:
weekly F&O alerts" > Run workflow (tick *dry run* to only see the alert in the log, or *resend* to send the
latest week again).

## What you get

* **Telegram** (same bot and chat as the other bots): sells, buys, buy signals that did not fit,
  the 15 tracked holdings with their gain, holdings that are weakening (RSI under both lines but still
  above 50), and the tracked model's return against NIFTY. It rings when there is a buy or sell.
* **Google Sheet** "V2-Algo Trade Logs", three tabs rewritten each run (don't edit them by hand):
  * `HM_Trades`: every position: buy signal week and close, entry (next open), sell signal, exit, return,
    net return after 0.18% a side, ₹ P&L on a 1/15 slot, weeks held. `Source` = `live`, or
    `backtest seed` for the 14 stocks the backtest already held when tracking started.
  * `HM_Signals`: every alert line: BUY, SELL, BUY with no free slot, BUY paused by the NIFTY filter,
    and WATCH (a held stock turned weak).
  * `HM_Weekly`: one row a week: NIFTY vs its 50-day average, signals, holdings, model value and return
    vs NIFTY, closed trades, win rate.
* **Strategy Desk** dashboard: tab "Hilega-Milega alerts" reads those three tabs.

## Data

* F&O list: NSE `fo_mktlots.csv`, read every run. If NSE refuses GitHub's servers, the last list read is
  used, then `fno_list.txt` (28 Nov 2026), and the alert says so.
* Prices: Yahoo Finance daily candles (3 years), turned into weekly candles exactly as in the backtest.

## Tracked model and state

The model starts at ₹10 lakh (repository variable `HM_CAPITAL` changes it) on 20 Nov 2026 from
`seed_state.json`: the 14 stocks and 1 pending buy the backtest held that week (replayed from 2016 with the
point-in-time F&O list), rescaled to 14/15 invested. After each run the state is committed to the branch
`hilega-milega-state` (only `state.json`; main is never touched). To start over, delete that branch.

## Test

    python3 hilega_milega/test_hm_alerts.py        # offline, synthetic prices
