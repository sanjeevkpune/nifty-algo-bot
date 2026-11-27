"""
notify.py  --  one Telegram sender for every bot on the VM.

Why: each bot used to post every message with sound, and a message repeated in a loop
(for example "retrying next loop") was posted again every few seconds.

What it does:
  * Routine messages (entries, exits, status, plans, summaries) are sent SILENTLY: they show in
    the chat but the phone does not ring. Problems (🚨 ❌ ⚠️ ⚡, or "MANUALLY") still ring.
  * The same text from the same bot is sent at most once per TG_DEDUP_SECS (default 600 s).
    It is still printed to the bot's log every time.
  * One retry on a network error (ETF bot and daily summary only; the option bots never wait).
  * Long messages are split under Telegram's 4096-character limit.
  * Never raises: a Telegram problem can never stop a bot.

Settings (config.env, all optional):
  TG_SILENT_ROUTINE=1   0 = every message rings, as before
  TG_DEDUP_SECS=600     0 = no de-duplication
"""
import os
import time

ALERT_MARKERS = ("🚨", "❌", "⚠️", "⚡", "MANUALLY")   # 🛑 filter skips / stops are routine: silent
_last_sent = {}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def is_alert(text: str) -> bool:
    return any(m in text for m in ALERT_MARKERS)


def send(tag: str, message: str, token=None, chat_id=None, loud=None, timeout=8, retry=True) -> bool:
    """Post `tag + message` to Telegram. loud=None decides from the message; True/False forces it.
    retry=False for the option bots' monitoring loops: one quick try, never a wait."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    dedup = _env_int("TG_DEDUP_SECS", 600)
    key = (tag, message)
    now = time.time()
    if dedup > 0 and now - _last_sent.get(key, 0) < dedup:
        print("(Telegram: same message sent in the last "
              f"{dedup // 60} min, not sent again)", flush=True)
        return False
    _last_sent[key] = now
    if loud is None:
        loud = is_alert(message) or os.environ.get("TG_SILENT_ROUTINE", "1").strip() == "0"
    try:
        import requests
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False
    ok = True
    for i in range(0, max(len(message), 1), 3800):
        payload = {"chat_id": chat_id, "text": tag + message[i:i + 3800], "disable_notification": not loud}
        for attempt in ((1, 2) if retry else (2,)):
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=timeout)
                if r.status_code == 429 and attempt == 1:     # rate limited: wait as asked, once
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                    except Exception:
                        wait = 3
                    time.sleep(min(wait, 10))
                    continue
                break
            except Exception as e:
                if attempt == 2:
                    print(f"Telegram error: {e}", flush=True)
                    ok = False
                else:
                    time.sleep(2)
    if not ok:
        _last_sent.pop(key, None)     # not delivered: let the next identical message try again
    return ok
