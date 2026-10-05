"""Alert stubs: Telegram / Discord via webhook env.

If env is empty, log only (no crash, no secret leak).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Batas keras API: Telegram sendMessage menolak text >4096 char (HTTP 400),
# Discord webhook menolak content >2000 char. Tanpa pemecah, notifikasi PnL
# yang panjang hilang TANPA JEJAK ->alert "diam-diam gagal".
TELEGRAM_MAX_LEN = 4096
DISCORD_MAX_LEN = 2000


def _chunks(text: str, size: int) -> list[str]:
    """Potong ``text`` jadi potongan <= ``size`` char tanpa ada isi hilang."""
    size = max(1, int(size))
    return [text[i:i + size] for i in range(0, len(text), size)]


def _post_telegram(token: str, chat_id: str, text: str) -> None:
    """Kirim 1 chunk ke Telegram. Body WAJIB urlencoded: Telegram mem-parse
    application/x-www-form-urlencoded, jadi "+" mentah jadi SPASI (tanda PnL
    positif hilang) dan non-ASCII rusak."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(url, data=data, timeout=10)


def _post_discord(webhook: str, text: str) -> None:
    """Kirim 1 chunk ke Discord. Body harus JSON valid (json.dumps, bukan repr:
    repr Python pakai kutip tunggal -> JSON tidak valid -> webhook 400)."""
    payload = json.dumps({"content": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def send_alert(message: str) -> bool:
    """Send to configured webhooks. Returns True if any send attempted.

    Pesan panjang dipecah per batas channel (Telegram 4096 / Discord 2000);
    kegagalan per chunk tak pernah menggagalkan chunk lain maupun loop.
    """
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    discord = os.getenv("DISCORD_WEBHOOK_URL", "")
    text = "" if message is None else str(message)
    if not text.strip():
        # tak ada isi: jangan kirim "text=None"/"text=" ke webhook
        logger.info("ALERT kosong, dilewati (tak ada webhook dikirim)")
        return False
    sent = False
    if tg_token and tg_chat:
        tg_sent = False
        for part in _chunks(text, TELEGRAM_MAX_LEN):
            try:
                _post_telegram(tg_token, tg_chat, part)
                tg_sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram alert failed: %s", type(exc).__name__)
        sent = sent or tg_sent
    if discord:
        dc_sent = False
        for part in _chunks(text, DISCORD_MAX_LEN):
            try:
                _post_discord(discord, part)
                dc_sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("discord alert failed: %s", type(exc).__name__)
        sent = sent or dc_sent
    if not sent:
        logger.info("ALERT (no webhook configured, log only): %s", text)
    return sent
