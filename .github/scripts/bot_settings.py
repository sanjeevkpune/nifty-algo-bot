#!/usr/bin/env python3
"""Works out the new paper/live and size settings for the "Bot settings (paper/live, lots)" workflow.

The settings live in the VM's GCP metadata key "bot-settings" as KEY=VALUE lines (numbers only, no
secrets). On every boot, etf_dukan/setup_etf_dukan.sh writes them over the defaults that startup.sh
put in /etc/trade-config/config.env (option bots) and /etc/trade-config/etf_dukan.env (ETF bot).

Reads (environment):  CURRENT     the current bot-settings text ("" if never set)
                      EKD_META    the ekd-paper-mode metadata value (from the EKD_PAPER_MODE repo variable)
                      STRATEGY, MODE, LOTS, ETF_AMOUNT, CONFIRM   the workflow inputs
Writes: --out FILE    the new bot-settings text
        --summary FILE   a plain-text report (also used as the Telegram message)
        GITHUB_OUTPUT    changed=true|false
Exits 1 (and changes nothing) on a bad input or a missing LIVE confirmation.
"""
import argparse
import os
import sys

ALL = "All strategies"
BOTS = {
    # name in the dropdown: (label, mode key, value meaning LIVE, value meaning PAPER, size key)
    "Adaptive Strangle (AngelOne)": ("Adaptive Strangle", "PAPER_MODE", "0", "1", "NUM_LOTS"),
    "Lemonn v3 Dual-Session (Dhan)": ("Lemonn v3 Dual-Session", "DHAN_PAPER_MODE", "0", "1", "DHAN_NUM_LOTS"),
    "Lemonn v4 Sideways Straddle (Dhan)": ("Lemonn v4 Sideways", "DHAN_V4_LIVE", "1", "0", "DHAN_V4_NUM_LOTS"),
    "ETF ki Dukan (AngelOne)": ("ETF ki Dukan", "EKD_PAPER_MODE", "0", "1", "EKD_BUY_AMOUNT"),
}
ETF = "ETF ki Dukan (AngelOne)"
# What startup.sh / setup_etf_dukan.sh use when the workflow has never set a value.
DEFAULTS = {"PAPER_MODE": "0", "NUM_LOTS": "4", "DHAN_PAPER_MODE": "1", "DHAN_NUM_LOTS": "5",
            "DHAN_V4_LIVE": "0", "DHAN_V4_NUM_LOTS": "5", "EKD_PAPER_MODE": "1",
            "EKD_BUY_AMOUNT": "40000", "EKD_AVG_AMOUNT": "40000"}
OPTION_KEYS = ["PAPER_MODE", "NUM_LOTS", "DHAN_PAPER_MODE", "DHAN_NUM_LOTS", "DHAN_V4_LIVE", "DHAN_V4_NUM_LOTS"]
ETF_KEYS = ["EKD_PAPER_MODE", "EKD_BUY_AMOUNT", "EKD_AVG_AMOUNT"]
MAX_LOTS = 20                     # NIFTY freeze limit is 1800 qty (27 lots of 65); 20 leaves room
ETF_MIN, ETF_MAX = 5_000, 200_000


def parse(text):
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.strip().partition("=")
        if k in DEFAULTS and v.strip().isdigit():
            out[k] = v.strip()
    return out


def effective(stored, ekd_meta):
    eff = dict(DEFAULTS)
    if ekd_meta.strip() == "0":
        eff["EKD_PAPER_MODE"] = "0"
    if "DHAN_V4_NUM_LOTS" not in stored and "DHAN_NUM_LOTS" in stored:
        eff["DHAN_V4_NUM_LOTS"] = stored["DHAN_NUM_LOTS"]       # v4 falls back to DHAN_NUM_LOTS
    eff.update(stored)
    return eff


def is_live(s, name):
    _, key, live, _, _ = BOTS[name]
    return s[key] == live


def describe(s):
    rows = []
    for name, (label, _, _, _, size_key) in BOTS.items():
        mode = "🔴 LIVE" if is_live(s, name) else "🟢 PAPER"
        size = f"₹{int(s[size_key]):,} per buy" if name == ETF else f"{s[size_key]} lots"
        rows.append(f"{mode}  {label}: {size}")
    return "\n".join(rows)


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    a = ap.parse_args()

    strategy = os.environ.get("STRATEGY", ALL)
    mode = os.environ.get("MODE", "keep as is").strip().lower()
    lots = os.environ.get("LOTS", "").strip()
    etf_amount = os.environ.get("ETF_AMOUNT", "").strip().replace(",", "")
    confirm = os.environ.get("CONFIRM", "").strip()

    stored = parse(os.environ.get("CURRENT", ""))
    old = effective(stored, os.environ.get("EKD_META", ""))
    new = dict(old)

    if strategy != ALL and strategy not in BOTS:
        fail(f"Unknown strategy '{strategy}'.")
    targets = list(BOTS) if strategy == ALL else [strategy]
    option_targets = [t for t in targets if t != ETF]

    if mode not in ("keep as is", "paper", "live"):
        fail(f"Unknown mode '{mode}'.")
    if mode != "keep as is":
        for t in targets:
            _, key, live, paper, _ = BOTS[t]
            new[key] = live if mode == "live" else paper

    if lots:
        if not option_targets:
            fail("The ETF bot has no lots. Leave 'lots' empty and use 'ETF buy amount' instead.")
        if not lots.isdigit() or not 1 <= int(lots) <= MAX_LOTS:
            fail(f"Lots must be a whole number from 1 to {MAX_LOTS} (got '{lots}').")
        for t in option_targets:
            new[BOTS[t][4]] = str(int(lots))

    if etf_amount:
        if ETF not in targets:
            fail("'ETF buy amount' only applies when the strategy is 'ETF ki Dukan' or 'All strategies'.")
        if not etf_amount.isdigit() or not ETF_MIN <= int(etf_amount) <= ETF_MAX:
            fail(f"ETF buy amount must be a whole rupee amount from {ETF_MIN} to {ETF_MAX} (got '{etf_amount}').")
        new["EKD_BUY_AMOUNT"] = new["EKD_AVG_AMOUNT"] = str(int(etf_amount))   # one size for new and averaging buys

    # Anything that makes real orders bigger or starts them needs the word LIVE.
    risky = []
    for name, (label, _, _, _, size_key) in BOTS.items():
        if not is_live(new, name):
            continue
        if not is_live(old, name):
            risky.append(f"{label} goes LIVE")
        elif int(new[size_key]) > int(old[size_key]):
            risky.append(f"{label} size goes up while live ({old[size_key]} → {new[size_key]})")
    if risky and confirm != "LIVE":
        fail("This places real orders: " + "; ".join(risky) + ". Type LIVE in the 'confirm' box and run again.")

    changes = []
    for name, (label, key, _, _, size_key) in BOTS.items():
        if old[key] != new[key]:
            changes.append(f"{label}: {'PAPER' if not is_live(old, name) else 'LIVE'} → "
                           f"{'LIVE' if is_live(new, name) else 'PAPER'}")
        if old[size_key] != new[size_key]:
            unit = "₹/buy" if name == ETF else "lots"
            changes.append(f"{label}: {old[size_key]} → {new[size_key]} {unit}")
    changed = bool(changes)

    # Store every option-bot key (so v3 and v4 lots can never follow each other by accident), and the
    # ETF keys only once the workflow has set one (until then the EKD_PAPER_MODE repo variable rules).
    keep = OPTION_KEYS + (ETF_KEYS if any(k in stored for k in ETF_KEYS) or
                          any(new[k] != old[k] for k in ETF_KEYS) else [])
    with open(a.out, "w") as f:
        f.write("".join(f"{k}={new[k]}\n" for k in keep))

    lines = []
    if changed:
        lines += ["⚙️ BOT SETTINGS CHANGED"] + [f"• {c}" for c in changes] + [""]
    else:
        lines += ["⚙️ BOT SETTINGS (no change)", ""]
    lines += ["Now:", describe(new)]
    if changed and is_live(new, ETF) and not is_live(old, ETF):
        lines += ["", "ETF going live: check DDPI is active and EKD_START_DATE is set (etf_dukan/README.md step 7)."]
    with open(a.summary, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
