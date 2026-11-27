# Strategy Desk (dashboard on the VM)

One password-protected page for every bot in this repo, served by the trading VM on port 8080.

| Tab | Reads |
|---|---|
| Overview | running P&L of all four bots |
| ETF ki Dukan | tracker sheet (`EKD_GSHEET_ID`): Capital, Holdings, Sold, Bot Log |
| Lemonn v4 straddle | V2 sheet (`GSHEET_ID_V2`): `Lemonn_v4_Daily` |
| Lemonn v3 dual session | V2 sheet: `Lemonn_Dual_v3`, `Lemonn_Log` |
| Adaptive strangle | V2 sheet: `Sheet1` |
| Backtests | V2 sheet: `Tradetron Backtests`, `Tradetron v4 Monthly`, `ETF Backtest` |

The backtest tabs are copies of the Tradetron reports (and the ETF new-setup report), so the VM needs
no Tradetron login. Ask Claude to refresh them after a new backtest.

Nothing private is in the repo: sheet ids come from `/etc/trade-config/*.env`, the sheets are read with the
existing `service_account.json`, and the login lives in `/etc/trade-config/dashboard.env` on the VM.

## How it gets there

* `etf_dukan/setup_etf_dukan.sh` (already run by `startup.sh` on every boot or sync) calls
  `dashboard/setup_dashboard.sh`, which installs the `strategy-dashboard` systemd service.
  The first time, it makes a random password and sends the address and password to Telegram.
* Port 8080 is opened by a Google Cloud firewall rule made by hand in the console (VPC network ->
  Firewall): `allow-strategy-dashboard-8080`, ingress, allow TCP 8080, target tag `strategy-dashboard`.
  The VM carries the network tag `strategy-dashboard` (VM -> Edit -> Network tags). Set the rule's
  source range to `<your IP>/32` to allow only your own connection.
* Address: `http://<VM static IP>:8080`, user `admin`. It is up only while the VM runs (the GCP instance schedule, about 08:15 to 16:00 IST).

The page refreshes every minute; the server caches sheet reads for 60 seconds.
Plain HTTP: the password crosses the network unencrypted, so prefer a firewall rule locked to your own IP.

## On the VM

```
sudo systemctl status strategy-dashboard      # is it running
journalctl -u strategy-dashboard -n 50        # its log
sudo nano /etc/trade-config/dashboard.env     # change DASH_PASSWORD, then:
sudo systemctl restart strategy-dashboard
```

Local test without Google access: `DASH_DEMO=1 DASH_PASSWORD=x python3 dashboard/server.py`, then open
`http://localhost:8080` (user `admin`, password `x`). Demo numbers are samples.
