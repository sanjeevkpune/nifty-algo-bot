# ETF ki Dukan bot for AngelOne (best setup) — v1

Places the tracker's daily sell (3:00 pm) and buys (3:10 pm) on AngelOne SmartAPI as delivery (CNC) orders.
Built in the same style as your strangle bot: `config.env`, TOTP login, Telegram alerts, Google Sheet via gspread,
lock and state files, fill checks, `PAPER_MODE`, crash handling.

**It starts in paper mode and sends no real orders until you set `EKD_PAPER_MODE=0` yourself.**

## Files

| File | What it is |
|---|---|
| `etf_dukan_bot.py` | The bot: login, data, plan, orders, reconciliation, sheet, CLI |
| `strategy.py` | The rules as pure functions (no broker, no clock) |
| `signals.py` | Reads the sheet's Signals tab and cross-checks it with the bot's plan |
| `universe.csv` | The tracker's Watchlist: 47 ETFs + 30 stocks with Include = Y (16 more ETFs at N) |
| `config.env.example` | Every setting with its default (the VM's `/etc/trade-config/etf_dukan.env` is written by the setup script) |
| `nse_holidays.txt` | Optional holiday list |
| `setup_etf_dukan.sh` | VM setup, called from `startup.sh` on every boot (idempotent) |

## The rules it follows (tracker Settings tab)

- **3:00 pm sell.** At most one target sell a day: the open position with the highest % gain on average cost
  that is at or above target (ETF +5%, stock +8%; GOLDBEES and SILVERBEES use the ETF levels). It sells 80% (rounded down;
  a 1-share position is sold whole). The other 20% becomes a Core lot at the position's average cost.
- **Core lots.** Sold whole when the price reaches 3x the lot's cost. This doesn't count as the day's sell.
- **3:10 pm buys.** There is one ETF slot and one stock slot, ₹40k each, in whole shares. Each slot buys the
  lowest-RSI name it doesn't already hold among the 8 lowest-RSI ETFs (or the 2 lowest-RSI stocks).
  There are at most 30 open positions, and Core lots don't count.
  A counter sold today (target sell or Core 3x sell) is never bought back the same day; the slot moves to the
  next unheld name on the shortlist. `EKD_REBUY_GAP_DAYS` (default 7: no new buy for 7 calendar days after the sell; 0 = same day only; -1 = off).
- **Averaging.** At most 1 averaging buy a day (₹40k), and only in a shop whose slot made no new buy. It buys
  the holding that is most negative vs its last buy price, if it's at or below -4% (ETF) or -8% (stock).
- **RSI(14).** Wilder RSI on AngelOne daily closes, with today's live price as the last close.
- **Cash.** Buys never use the owed monthly withdrawal or the ₹15k kept back. You take the ₹15k out yourself and
  record it with `withdraw 15000`.

**Proof it matches the backtest.** `tests/replay_test.py` runs `strategy.py` day by day over 24-Nov-2021 to
23-Nov-2026 on the same Yahoo closes and compares it with `backtest.py`. All 985 trades match (383 new, 196 averaging,
353 sells, 53 Core 3x), with the same ending value (₹72.64L) and the same withdrawals (₹8.25L).
The rules changed on 26-Nov-2026 from keep 50% / max 20 / ETF +4%/-3% / stock +6%/-6% to keep 20% / max 30 /
ETF +5%/-4% / stock +8%/-8%.

## Live signals from your Google Sheet

The bot reads the order plan on your tracker's **Signals** tab (rows 7–10: Sell, ETF slot, stock slot,
Core 3x) at 3:00 pm and 3:10 pm. It compares it with its own plan, which comes from AngelOne prices.
The two can differ near a threshold, because GOOGLEFINANCE prices run up to ~20 minutes late, and the sheet's
RSI comes from Google's closes, not AngelOne's.
`EKD_SIGNAL_POLICY` decides what is traded:

| Policy | What is traded |
|---|---|
| `agree` | Only the actions where the sheet and the bot name the same action and symbol. Each disagreement is skipped and sent on Telegram, e.g. `STOCK slot: bot says NEW TATASTEEL x245, sheet says NEW COALINDIA x104`. |
| `sheet` | The sheet's signals. The bot still sizes the order from the live AngelOne price and refuses anything unsafe: no such holding, broker shortfall, max positions, cash, or daily limits already used. |
| `bot` (default, your choice) | The bot's own AngelOne-based plan. Every disagreement with the sheet is sent on Telegram, and every executed trade is written into the sheet. |
| `off` | The sheet isn't read. |

- **Sheet unreadable.** With the default `bot` policy, the bot still trades its own plan and warns you on
  Telegram. Rows that couldn't be written are retried on the next run. (Under `agree` or `sheet`, an
  unreadable sheet means no orders, unless `EKD_SIGNAL_IF_SHEET_DOWN=bot`.)
- **Order of writes.** The 3:00 pm sell is written to Holdings, Sold and Core before the 3:10 pm buy signals are
  read, with a 30-second wait for the sheet to recalculate.
- **Don't type the bot's trades into the sheet.** The sheet's signals depend on its Holdings, Sold and Core rows,
  which the bot now fills (`EKD_SHEET_MODE=tracker`). A trade typed in by hand as well would be counted twice.
  Trades you make outside the bot go in by hand, and won't be in the bot's records.

## Daily flow (one cron start at 2:45 pm)

| Time | Step |
|---|---|
| 14:45 | Lock for the day, NSE trading-day check (weekend, `nse_holidays.txt`, and "has NIFTYBEES traded today?"), login, recovery of any order left in flight by a crash, reconciliation with AngelOne, ~400 days of daily closes (cached per day) |
| 14:50 | **Dry-run plan on Telegram**: cash, free cash, top 8 ETFs and top 2 stocks by RSI, the planned sell(s) and buys with quantity and reason, the withdrawal due, any blocked symbols |
| 15:00 | Fresh prices, cross-check with the sheet's Signals tab, then the target sell and Core 3x exits |
| 15:10 | Sell written to the sheet, fresh prices and RSI, cross-check with the sheet, then the ETF slot, the stock slot, or the 1 averaging buy |
| after | Telegram day summary, Google Sheet rows, reconciliation again |

### Order handling

Each order goes through these steps:

1. The bot places a LIMIT order at LTP +0.10% (buy) or -0.10% (sell), rounded to the tick, and waits 45 seconds.
2. If it isn't filled, the bot cancels it and reads the filled quantity.
3. For whatever is left, it places a MARKET order. It skips this step if the price has moved more than 1% from
   the plan (`EKD_MAX_CHASE_PCT`).

Fills are recorded at AngelOne's average price. If a sell fills only partly, the planned 20% Core lot is still kept,
and the unsold part stays an open position.

## Safety

- **Paper by default.** `EKD_PAPER_MODE=1` sends no orders. It fills at the live price ± 0.05%. Paper mode keeps
  its own `paper_state.json`, separate from `live_state.json`.
- **Prefixed settings.** All bot settings use the `EKD_` prefix, so a `PAPER_MODE=0` in the strangle bot's
  `config.env` can't switch this bot live.
- **Dry run.** `EKD_DRY_RUN=1` sends the 2:50 pm plan and stops. `plan` sends the plan any time, with no orders.
- **Idempotent.**
  - A daily lock file stops a second copy from starting.
  - Every order is saved as an intent before it's sent, and tagged with `ordertag` (e.g. `EKD261124S1`).
  - The limits ("1 sell", "1 averaging", "1 new per slot" a day) are counted from the trade journal, not from
    memory, so `--resume` after a crash can't repeat a trade.
- **Crash.** Positions are delivery, so nothing needs squaring off. On the next start (or with `recover`), the bot
  reads the order book by tag, records what filled, and cancels what's still open.
  Then `run --resume` finishes the day.
- **Reconciliation (live).** Bot quantity (open + Core) is compared with AngelOne holdings (settled + T1) plus
  today's DELIVERY positions.
  - If AngelOne has **fewer** shares than the bot's records, that symbol is blocked for the day and you get an alert.
    `EKD_RECON_STRICT=1` skips the whole day instead.
  - If AngelOne has **more** shares, they're treated as your own and never touched.
  - The bot never sells more than its own quantity.
- **Cash cap (live).** Buys use the lower of the bot's own cash ledger and AngelOne's available cash minus
  `EKD_BROKER_CASH_FLOOR` (₹5L, the option bot's buffer).

## Where the truth lives

- **`state/<mode>_state.json` is the source of truth** for lots, average cost, last buy price, Core lots, cash,
  withdrawals and the trade journal. It's written atomically, with a timestamped backup in `state/backups/` at
  every start. Each lock in `locks/` holds that day's status.
- **AngelOne is the check.** Reconciliation compares the two before any order.
- **The Google Sheet is the report and the second opinion.** The bot reads only its Signals tab, to cross-check
  the plan (see above). Holdings, cost and cash always come from the state file, so a sheet edit can't change
  what the bot thinks it owns.
  - Rows go to the **Bot Log** tab, which the bot creates.
  - With `EKD_SHEET_MODE=tracker`, it also fills the tracker's yellow input cells, the way the Read Me tells a
    person to: Holdings A–F and H on a buy; Sold A–C and I–J, plus Holdings I (sell date), on a sell;
    Core M–N on a 3x sell; and a Capital ledger row on `withdraw`.
  - If Sheets is down, rows wait in `state/<mode>_sheet_outbox.jsonl` and are sent on the next run.
    A local CSV copy is always in `logs/`.

## Setup on the GCP VM (automatic)

There's nothing to run by hand on the VM. The existing **Automated VM Start and Stop Scheduler** workflow pushes
`startup.sh` to the VM. On every boot (or "start_or_sync" run), `startup.sh` clones this repo into
`~/trading-bot/`, so this folder lands in `~/trading-bot/etf_dukan/`. Its new step 8 then runs
`etf_dukan/setup_etf_dukan.sh`. That script is safe to run any number of times. It:

1. Installs any missing Python packages (the same system `pip3 --break-system-packages` as the other bots).
2. Writes `/etc/trade-config/etf_dukan.env`, with paper mode, the sheet ID, the service account path and the
   home folder. The AngelOne login and Telegram are read from the existing `/etc/trade-config/config.env`.
   The strangle bot's `PAPER_MODE=0` there never applies to this bot.
3. Creates `state/`, `locks/`, `logs/` and `cache/`. They're git-ignored, so `git reset --hard` on boot keeps them.
4. Sets up the bot's records, without ever overwriting existing ones:
   - If this VM has no records file, it first tries to restore it from the sheet's hidden **Bot State** tab.
     The bot saves its records there after every run, so a rebuilt VM gets them back.
   - Otherwise it runs `init --if-missing` with ₹40L and the start date.
5. Adds the cron line `45 14 * * 1-5 … etf_dukan_bot.py run`. The VM clock is IST, and the line is replaced, not
   duplicated. The VM stops at 4:00 pm, after the bot's last order at 3:24 pm.

### Settings (GitHub repo → Settings → Secrets and variables → Actions)

All are optional, and the defaults are safe:

| Name | Kind | Default | Meaning |
|---|---|---|---|
| `EKD_PAPER_MODE` | Variable | `1` | Paper trading. Only the exact value `0` sends real orders. |
| `EKD_START_DATE` | Variable | `auto` | Start date of the records (`YYYY-MM-DD`). `auto` = the first day the VM sets the bot up. Withdrawals start 6 months later. |
| `EKD_CAPITAL` | Variable | `4000000` | Starting capital in the records |
| `EKD_GSHEET_ID` | Variable | the ETF ki Dukan tracker | Google Sheet for signals and trade logging |
| `EKD_API_KEY` | Secret | shared `API_KEY` | Optional separate SmartAPI app key for this bot |

A changed setting reaches the VM on the next boot or `start_or_sync` run. The start date and capital only matter
when the records are first created.

### One-time steps the scripts can't do

- **Share the Google Sheet** with the service account's email (`client_email` in the `GCP_SA_KEY` JSON) as
  **Editor**. Without it, the bot can't write trades to the sheet. It still trades, and alerts you on Telegram.
- **Before going live:**
  - Turn on DDPI at AngelOne, so the API can sell delivery holdings.
  - Clear the paper rows from Holdings, Sold, Core and Bot Log, and set Capital A20 to the start date.
  - Set the repo Variables `EKD_START_DATE` and `EKD_PAPER_MODE=0`.

  Live records are created on the next boot with that date. The paper records stay in `state/paper_state.json`.

### Tests

The offline tests (fake AngelOne, plus a 5-year replay against the backtest) live with the backtest data in the
ETF ki Dukan project folder and aren't needed on the VM.

## Commands

```bash
python etf_dukan_bot.py run [--resume]      # cron entry / finish a crashed day
python etf_dukan_bot.py plan                # plan now, no orders
python etf_dukan_bot.py status              # positions, Core lots, cash, owed withdrawal
python etf_dukan_bot.py reconcile           # bot vs AngelOne (prints raw holding fields too)
python etf_dukan_bot.py recover             # record fills of orders left in flight by a crash
python etf_dukan_bot.py withdraw 15000      # after you take the monthly withdrawal out
python etf_dukan_bot.py deposit 100000
python etf_dukan_bot.py split ITBEES 10     # corporate action: 1 share became 10
python etf_dukan_bot.py flush-sheet         # retry queued sheet rows
```

## Assumptions in v1 (tell me if any is wrong)

1. **Bot cash.** The bot uses its own cash ledger (₹40L + realised − withdrawals), not the whole AngelOne balance,
   because the option bot's ₹5L buffer sits in the same account. Charges are estimated with the tracker's rates,
   so expect the ledger to drift by a few rupees a month from AngelOne's contract notes.
2. **Withdrawal reserve.** The ₹15k is kept back from 1 month before withdrawals start, as in the backtest.
   The tracker starts it at the withdrawal date.
3. **Reconciliation fields.** "Holdings quantity + T1 quantity + today's DELIVERY net position" is assumed to be
   the right broker count, with pledged shares already inside `quantity`.
   Run `reconcile` once on live and check the printed fields. If pledged shares are missing, set
   `EKD_RECON_INCLUDE_COLLATERAL=1`.
4. **Pledged holdings.** Selling holdings that are pledged for the option bot's margin is assumed to work directly
   at AngelOne. If AngelOne asks for an unpledge first, the sell is rejected and you get an alert.
5. **Signals and universe.** The live signals come from the sheet's Signals tab, cross-checked as described
   above. The universe is the tracker's Watchlist at today's Include flags, exported to `universe.csv`. Edit that file
   when you change the Watchlist; the bot doesn't read the Watchlist tab.
6. **Times.** Sell orders stop at 15:09 and buy orders at 15:24, so no order is sent close to the 15:30 close.
   A start after 15:15 skips the day.
7. **One run a day.** If the server is off at 2:45 pm, that day is skipped. Nothing is caught up the next day,
   the same as missing a day by hand.

## Checking the setup

`check_setup.py` checks everything end to end and prints a PASS / WARN / FAIL / SKIP table (also sent on Telegram).
It is read-only: no orders, no changes to the state file or the tracker tabs. It writes one cell on a separate
"Bot Setup Check" tab to prove the service account can write.

* From GitHub: Actions → **ETF Bot Setup Check** → Run workflow (pick full, AngelOne only, or AngelOne + order test).
* On the VM: `cd ~/trading-bot/etf_dukan && python3 check_setup.py` (add `--angel-only`, `--quick`, `--order-test`).

AngelOne checks: login (PIN + TOTP), profile, scrip tokens, live prices, market-open probe, daily candles and RSI,
funds against the ₹5L floor, holdings and positions, order book, and today's plan (computed, never placed).
They are skipped from 9:30 am to 3:15 pm IST (a second login on the shared API key could log out the strangle bot)
and while the bot's own 2:45 pm run is going. Run at 9:15-9:30 am or after 3:30 pm.

`--order-test` is opt-in and places a REAL order even in paper mode: BUY 1 NIFTYBEES as a LIMIT 5% below the
live price (it cannot fill), cancels it at once and confirms it shows cancelled. Market hours only.
