"""
notify.py — Telegram notifier for QuantSystem.

Usage (anywhere, including from the LLM orchestrator):
    from notify import notify
    notify("anything worth flagging")

Minimum guaranteed triggers (hardcoded in main.py / risk engine):
    - System start / clean stop
    - Unhandled crash
    - Kill switch activated (drawdown > 12% or daily loss > 3%)
    - Every ₹10,000 profit milestone crossed

Beyond those, Claude (regime_detector / orchestrator) decides what else
deserves a ping — e.g. significant daily P&L, regime shift to BEAR, etc.

Env vars required (in .env):
    TELEGRAM_BOT_TOKEN=123456:ABC-...
    TELEGRAM_CHAT_ID=-100123456789

If either var is missing, notify() is a silent no-op — nothing breaks.
"""

import os
import time
import requests
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_URL     = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
_ENABLED = bool(_TOKEN and _CHAT_ID)

if _ENABLED:
    logger.info("Telegram notifier: enabled")
else:
    logger.info("Telegram notifier: disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)")


def notify(message: str, parse_mode: str = "HTML") -> bool:
    """
    Send a Telegram message. Returns True on success, False on failure.
    Never raises — safe to call from anywhere.
    """
    if not _ENABLED:
        return False
    payload = {
        "chat_id": _CHAT_ID,
        "text": message[:3900],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(_URL, json=payload, timeout=10)
            if resp.ok:
                return True
            retry_after = 0
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 0))
            except Exception:
                pass
            logger.warning(f"Telegram | HTTP {resp.status_code}: {resp.text[:120]}")
            if resp.status_code == 429 and retry_after:
                time.sleep(min(retry_after, 30))
            elif attempt < 3:
                time.sleep(attempt)
        except Exception as e:
            err = str(e).replace(_TOKEN, "<telegram-token>")
            logger.warning(f"Telegram | send failed: {err}")
            if attempt < 3:
                time.sleep(attempt)
    return False
