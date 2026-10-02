"""Alert stubs: Telegram / Discord via webhook env.

If env is empty, log only (no crash, no secret leak).
"""

from __future__ import annotations

import logging
import os
import urllib.request

logger = logging.getLogger(__name__)


def send_alert(message: str) -> bool:
    """Send to configured webhooks. Returns True if any send attempted."""
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    discord = os.getenv("DISCORD_WEBHOOK_URL", "")
    sent = False
    if tg_token and tg_chat:
        try:
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            data = f"chat_id={tg_chat}&text={message}".encode()
            urllib.request.urlopen(url, data=data, timeout=10)
            sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram alert failed: %s", type(exc).__name__)
    if discord:
        try:
            payload = f'{{"content": {message!r}}}'.encode()
            req = urllib.request.Request(discord, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("discord alert failed: %s", type(exc).__name__)
    if not sent:
        logger.info("ALERT (no webhook configured, log only): %s", message)
    return sent
