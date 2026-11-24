# nifty-algo-bot

Trading bots for NIFTY options (AngelOne and Dhan) and the ETF ki Dukan delivery strategy (AngelOne).
They run on one Google Cloud VM that is started, synced and stopped by GitHub Actions.

> This repository is **public**. Never commit credentials. All secrets live in GitHub Secrets and reach
> the VM through GCP instance metadata (see below). The root `.gitignore` blocks the usual runtime files.

## What runs (Mon to Fri, IST)

| Time | Bot | File | Broker | Mode | Log |
|---|---|---|---|---|---|
| 09:08 → 15:20 | NIFTY sideways ATM straddle (Lemonn v4) + watchdog every 10 min | `lemonn_nifty_sideways_v4_dhan.py` | Dhan | Paper until 31 Dec 2026, then live only with `DHAN_V4_LIVE=1` | `dhan_v4.log` |
| 09:30 | NIFTY + SENSEX dual-session strangle (Lemonn v3) | `dhan_improved_with_filters.py` | Dhan | Paper until 31 Dec 2026, then `DHAN_PAPER_MODE` | `dhan_improved.log` |
| 09:39 | Adaptive strangle | `adaptive_strangle_v2.py` | AngelOne | `PAPER_MODE` in config.env (startup.sh sets 0 = live) | `algo_trade_v2.log` |
| 14:45 | ETF ki Dukan | `etf_dukan/etf_dukan_bot.py run` | AngelOne | Paper unless repo variable `EKD_PAPER_MODE` is `0` | `etf_dukan/logs/cron.log` |

The crontab is written by `startup.sh` (and `etf_dukan/setup_etf_dukan.sh` for the ETF line) on every boot or sync.
Edit the schedule there, not on the VM.

## Repository layout

```
.github/workflows/
  vm-scheduler.yml          Start 08:30 / stop 16:00 IST; pushes startup.sh + secrets to VM metadata
  etf-dukan-check.yml       Manual: runs etf_dukan/check_setup.py on the VM
startup.sh                  VM boot script: packages, clone/pull main, /etc/trade-config/config.env, crontab
requirements.txt            Python packages for the root bots
cleanup_logs.sh             Daily: old log folders, run locks, ETF caches
bot_logrotate.conf          Hourly logrotate (10 MB, keep 3)

adaptive_strangle_v2.py     AngelOne adaptive strangle (file name kept: cron uses it; code is V4.2)
dhan_improved_with_filters.py  Dhan Lemonn dual-session v3 (file name kept: cron uses it)
lemonn_nifty_sideways_v4_dhan.py  Dhan Lemonn v4 sideways straddle

test_reliability.py              Read-only diagnostics for the AngelOne strangle bot
test_lemonn_dhan_connectivity.py Pre-flight checks for the Dhan v3 bot (--order-test places and cancels test orders)
test_lemonn_v4_connectivity.py   Read-only checks for the Dhan v4 bot

etf_dukan/                  ETF ki Dukan bot, its own README, setup script and setup check
```

The bot scripts stay in the repository root on purpose: the crontab, the relative run-lock files
(`run_lock_*.json`), the `state/` folder (created next to each script) and `service_account.json`
all assume `~/trading-bot/<script>.py`. Moving a script means changing `startup.sh` in the same commit.

## How a deploy works

1. Merge to `main` (never push to `main` directly).
2. The **VM Scheduler** workflow (`start_or_sync`, scheduled 08:30 IST or run by hand) copies `startup.sh` and
   the secrets into the VM metadata, then starts the VM, or re-runs the startup script if it is already up.
3. `startup.sh` runs `git reset --hard origin/main` in `~/trading-bot`, writes `/etc/trade-config/config.env`,
   decodes `service_account.json`, installs the crontab and runs `etf_dukan/setup_etf_dukan.sh`.

A sync during market hours swaps the code under a running bot. Prefer merging after 15:30 IST.

## Secrets and variables (GitHub → Settings → Secrets and variables → Actions)

Secrets: `GCP_SA_KEY`, `API_KEY`, `CLIENT_CODE`, `PIN`, `TOTP_SECRET`, `DHAN_CLIENT_ID`, `DHAN_PIN`,
`DHAN_TOTP_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GSHEET_ID_V2`, `EKD_API_KEY` (optional).

Variables (ETF bot): `EKD_PAPER_MODE`, `EKD_START_DATE`, `EKD_CAPITAL`, `EKD_GSHEET_ID`.

Non-secret bot settings (lots, filters, charges) are written into `config.env` by `startup.sh`.

## On the VM

```
~/trading-bot/                   this repo (main)
~/trading-bot/service_account.json   Google service account, from metadata (git-ignored)
~/trading-bot/state/             Dhan bots' daily state + token cache (git-ignored)
/etc/trade-config/config.env     all credentials and settings, rewritten on every boot
```
