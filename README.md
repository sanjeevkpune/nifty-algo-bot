# nifty-algo-bot

Trading bots for NIFTY options (AngelOne and Dhan) and the ETF ki Dukan delivery strategy (AngelOne).
They run on one Google Cloud VM that is started and stopped by a **GCP instance schedule**
(Compute Engine → Instance schedules). Every boot pulls `main`, so merged code goes live on the next start.

> This repository is **public**. Never commit credentials. All secrets live in GitHub Secrets and reach
> the VM through GCP instance metadata (see below). The root `.gitignore` blocks the usual runtime files.

## What runs (Mon to Fri, IST)

| Time | Bot | File | Broker | Mode | Log |
|---|---|---|---|---|---|
| 09:08 → 15:35 | NIFTY sideways ATM straddle (Lemonn v4; exit `DHAN_V4_EXIT`, default 15:35) + watchdog every 10 min | `lemonn_nifty_sideways_v4_dhan.py` | Dhan | Paper until 31 Dec 2026, then live only with `DHAN_V4_LIVE=1` | `dhan_v4.log` |
| 09:30 → 15:35 | NIFTY + SENSEX dual-session strangle (Lemonn v3; afternoon exit `DHAN_V3_PM_EXIT`, default 15:35) | `dhan_improved_with_filters.py` | Dhan | Paper until 31 Dec 2026, then `DHAN_PAPER_MODE` | `dhan_improved.log` |
| 09:39 → 15:35 | Adaptive strangle (exit time `EXIT_TIME`, default 15:35) | `adaptive_strangle_v2.py` | AngelOne | `PAPER_MODE` in config.env (startup.sh sets 0 = live) | `algo_trade_v2.log` |
| 14:45 | ETF ki Dukan | `etf_dukan/etf_dukan_bot.py run` | AngelOne | Paper unless repo variable `EKD_PAPER_MODE` is `0` | `etf_dukan/logs/cron.log` |
| 15:45 | Daily summary of all bots to Telegram (reads the bots' files only) | `daily_summary.py` | none | n/a | `daily_summary.log` |
| 09:00 daily, logrotate hourly | Disk housekeeping | `cleanup_logs.sh`, `bot_logrotate.conf` | none | n/a | `cleanup.log` |

The crontab is written by `startup.sh` (plus `etf_dukan/setup_etf_dukan.sh` for the ETF line and `setup_vm_extras.sh`
for the 15:45 summary) on every boot. Edit the schedule there, not on the VM.

### GCP instance schedule (VM on/off)

The schedule must keep the VM up from before the first bot to after the 15:45 summary. GCP can run a
schedule up to 15 minutes late, so use **start 08:15 IST, stop 16:00 IST (or later), Monday to Friday**.
The instance schedule needs the Compute Engine service agent to have permission to start/stop the VM
(GCP shows a warning on the schedule page if it does not).

### Telegram

All bots send through `notify.py`: routine messages (entries, exits, status, plans) arrive **silently**,
problems (🚨 ❌ ⚠️ ⚡ or "MANUALLY") ring, and the same message is not repeated within 10 minutes.
The adaptive strangle posts a P&L update every 90 minutes while in a trade (`STATUS_EVERY_MIN`).
The 15:45 summary rings once a day. Optional config.env settings: `TG_SILENT_ROUTINE=0` (every message rings),
`TG_DEDUP_SECS`, `STATUS_EVERY_MIN`, `DISK_WARN_PCT` (default 80).

### Disk

`bot_logrotate.conf`: every `*.log` is rotated weekly or at 5 MB, 8 gzip copies, nothing older than 60 days.
`cleanup_logs.sh` (09:00): SmartAPI `logs/<date>/` after 3 days, adaptive run locks after 10, Dhan daily state
after 45, ETF caches/backups, then a Telegram alert if the disk is still above 80%. `setup_vm_extras.sh`
caps the system journal at 200 MB and empties the apt cache on every boot.

## Repository layout

```
.github/workflows/
  vm-deploy.yml             Manual: push startup.sh + secrets to VM metadata (optionally re-run it now)
  etf-dukan-check.yml       Manual: runs etf_dukan/check_setup.py on the VM
  dashboard-network.yml     Manual, once: opens port 8080 for the dashboard
startup.sh                  VM boot script: packages, clone/pull main, /etc/trade-config/config.env, crontab
requirements.txt            Python packages for the root bots
cleanup_logs.sh             Daily 09:00: old log folders, run locks, old state files, ETF caches, disk alert
bot_logrotate.conf          Logrotate, checked hourly (weekly or 5 MB, keep 8 compressed)
setup_vm_extras.sh          Run on boot by etf_dukan/setup_etf_dukan.sh: 15:45 summary cron line, journald cap
notify.py                   Shared Telegram sender (silent routine messages, no repeats)
daily_summary.py            15:45 Telegram summary of every bot

adaptive_strangle_v2.py     AngelOne adaptive strangle (file name kept: cron uses it; code is V4.4)
dhan_improved_with_filters.py  Dhan Lemonn dual-session v3 (file name kept: cron uses it)
lemonn_nifty_sideways_v4_dhan.py  Dhan Lemonn v4 sideways straddle

test_adaptive_exit.py            Offline tests of the AngelOne strangle's exit (fake broker): python3 test_adaptive_exit.py
test_reliability.py              Read-only diagnostics for the AngelOne strangle bot
test_lemonn_dhan_connectivity.py Pre-flight checks for the Dhan v3 bot (--order-test places and cancels test orders)
test_lemonn_v4_connectivity.py   Read-only checks for the Dhan v4 bot

etf_dukan/                  ETF ki Dukan bot, its own README, setup script and setup check
dashboard/                  Strategy Desk: password-protected dashboard on port 8080 (see dashboard/README.md)
```

The bot scripts stay in the repository root on purpose: the crontab, the relative run-lock files
(`run_lock_*.json`), the `state/` folder (created next to each script) and `service_account.json`
all assume `~/trading-bot/<script>.py`. Moving a script means changing `startup.sh` in the same commit.

## How a deploy works

1. Merge to `main` (never push to `main` directly).
2. Next morning the GCP instance schedule starts the VM. It runs the `startup.sh` saved in its metadata, which runs
   `git reset --hard origin/main` in `~/trading-bot`, writes `/etc/trade-config/config.env`, decodes
   `service_account.json`, installs the crontab and runs `etf_dukan/setup_etf_dukan.sh` (which also runs
   `setup_vm_extras.sh` and the dashboard setup). Code changes need nothing else.
3. Only when a **GitHub Secret/Variable** or **`startup.sh` itself** changed, run the **Deploy to VM** workflow by hand
   (`update metadata only`, or `update + re-run now` to apply it to a running VM; refused 09:00-15:45 IST unless forced).

Merging during market hours is safe: the running VM only pulls on boot or a manual re-run.

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
