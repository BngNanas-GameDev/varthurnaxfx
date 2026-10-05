"""Alert transport tests: OFFLINE total (``urllib.request.urlopen`` di-mock).

Kelas bug yang ditutup di sini: "notifikasi diam-diam gagal" — token bocor ke
log, body Telegram tidak di-urlencode (tanda "+" PnL jadi spasi, emoji rusak),
payload Discord bukan JSON valid, dan pesan >4096 char ditolak HTTP 400 tanpa
jejak. Tidak ada socket: setiap panggilan urlopen direkam, bukan dikirim.
"""

import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))            # paket `src.*` (dipakai loop/daemon)
sys.path.insert(0, str(ROOT / "src"))    # paket top-level

import src.ops.alerts as alerts  # noqa: E402

TOKEN = "123456:SECRET-TOKEN-AAA"
CHAT = "-1001234567890"
DISCORD_URL = "https://discord.com/api/webhooks/999/SECRET-DISCORD-TOKEN"

# Reason Fin berbahasa Indonesia + emoji: pesan notifikasiFin yang sebenarnya.
REASON_FIN = "rekkonsiliasi tak sinkron — ᐸ curiga tak ada data exchange"


class _Resp:
    """Fake respons urlopen (cukup read() + context manager)."""

    def __init__(self, payload: bytes = b'{"ok":true}'):
        self._payload = payload

    def read(self, *_a, **_kw):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    """urlopen palsu: catat (url, data, timeout), tak pernah buka socket."""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._exc = exc

    def __call__(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return _Resp()

    def urls(self) -> list:
        return [c["url"] for c in self.calls]


def tg_calls(rec: Recorder) -> list[dict]:
    return [c for c in rec.calls if isinstance(c["url"], str)]


def dc_calls(rec: Recorder) -> list[dict]:
    return [c for c in rec.calls if not isinstance(c["url"], str)]


def tg_texts(rec: Recorder) -> list[str]:
    """Teks pesan Telegram setelah di-decode seperti yang dilakukan Telegram."""
    out = []
    for c in tg_calls(rec):
        raw = bytes(c["data"]).decode("utf-8")
        out.append(urllib.parse.parse_qs(raw, keep_blank_values=True)["text"][0])
    return out


def tg_form(rec: Recorder) -> list[dict]:
    return [urllib.parse.parse_qs(bytes(c["data"]).decode("utf-8"))
            for c in tg_calls(rec)]


def dc_payloads(rec: Recorder) -> list[dict]:
    """Body Discord ada di ``Request.data`` (bukan kwarg data=)."""
    return [json.loads(bytes(c["url"].data).decode("utf-8")) for c in dc_calls(rec)]


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _logs(caplog):
    caplog.set_level(logging.DEBUG, logger="src.ops.alerts")
    return caplog


def test_telegram_success_returns_true(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert alerts.send_alert("CLOSE PnL +150.00 USDT") is True
    assert len(rec.calls) == 1, rec.calls
    assert rec.urls()[0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert rec.calls[0]["timeout"] == 10
    form = tg_form(rec)[0]
    assert form["chat_id"] == [CHAT]
    assert tg_texts(rec) == ["CLOSE PnL +150.00 USDT"]


def test_telegram_needs_token_and_chat_id_together(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert alerts.send_alert("x") is False
    assert rec.calls == []


def test_no_webhook_configured_is_log_only(monkeypatch, _logs):
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert alerts.send_alert("CLOSE tanpa webhook") is False
    assert rec.calls == []                       # tak ada network sama sekali
    assert "no webhook configured" in _logs.text


def test_telegram_urlerror_returns_false_without_crash(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder(exc=urllib.error.URLError("connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert alerts.send_alert("CLOSE PnL +1.00 USDT") is False
    assert len(rec.calls) == 1  # tetap mencoba


def test_telegram_failure_never_logs_token_or_url(monkeypatch, _logs):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        Recorder(exc=urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 400,
            "Bad Request: chat not found", None, None)))

    assert alerts.send_alert("CLOSE PnL +1.00 USDT") is False
    blob = "\n".join(
        "%s|%s|%s" % (r.getMessage(), r.msg, r.args) for r in _logs.records)
    assert "telegram alert failed" in blob          # tetap ada jejak kegagalan
    assert TOKEN not in blob                        # token tak bocor
    assert CHAT not in blob                         # chat_id tak bocor
    assert "api.telegram.org" not in blob           # URL berkey tak bocor


def test_discord_exception_returns_false_without_crash(monkeypatch, _logs):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    monkeypatch.setattr(urllib.request, "urlopen",
                        Recorder(exc=urllib.error.URLError("down")))

    assert alerts.send_alert("CLOSE PnL -3.00 USDT") is False
    assert "discord alert failed" in _logs.text
    assert "SECRET-DISCORD-TOKEN" not in _logs.text
    assert "discord.com" not in _logs.text


def test_one_channel_ok_means_alert_sent(monkeypatch):
    """Telegram gagal tapi Discord sukses -> True (kanal parsial tetap nagih)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)

    def _mixed(url, data=None, timeout=None):
        if isinstance(url, str):                     # telegram
            raise urllib.error.URLError("tg down")
        return _Resp()                               # discord

    monkeypatch.setattr(urllib.request, "urlopen", _mixed)
    assert alerts.send_alert("CLOSE PnL +2.00 USDT") is True


def test_empty_or_none_message_is_not_sent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert alerts.send_alert("") is False
    assert alerts.send_alert("   ") is False
    assert alerts.send_alert(None) is False          # tak crash, tak spam "text=None"
    assert rec.calls == []


def test_non_string_message_is_coerced(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert alerts.send_alert(150) is True
    assert tg_texts(rec) == ["150"]


def test_unicode_emoji_roundtrip(monkeypatch):
    """Emoji + teks Indonesia harus tiba utuh (tak ada encoding rusak)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = f"CLOSE 🔒 PnL -9.00 USDT — {REASON_FIN} ✅ ₿"
    assert alerts.send_alert(msg) is True
    assert tg_texts(rec) == [msg]                    # tanda +/- & emoji utuh
    assert dc_payloads(rec)[0]["content"] == msg


def test_plus_sign_and_specials_survive_telegram_encoding(monkeypatch):
    """Tanda "+" (PnL positif) tak boleh berubah jadi spasi saat di-encode."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = "CLOSE | PnL +150.00 USDT | fee 0.45 net +149.55 | a&b=c 100%"
    assert alerts.send_alert(msg) is True
    assert tg_texts(rec) == [msg]                    # byte-identik setelah decode
    assert "%2B150.00" in bytes(rec.calls[0]["data"]).decode("utf-8")


def test_discord_payload_is_valid_json(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert alerts.send_alert(f"VETO — {REASON_FIN}") is True
    payload = dc_payloads(rec)[0]                   # json.loads tak meledak
    assert payload["content"].startswith("VETO")
    assert REASON_FIN in payload["content"]         # non-ASCII tak di-escape kasar
    assert dc_calls(rec)[0]["url"].data             # body bytes JSON


def test_long_message_split_into_chunks(monkeypatch):
    """Pesan >4096 char dipecah rapi (Telegram menolak >4096 -> alert hilang)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = "CLOSE " + ("x" * 9000)
    assert alerts.send_alert(msg) is True
    texts = tg_texts(rec)
    assert len(texts) == 3, [len(t) for t in texts]
    assert all(len(t) <= alerts.TELEGRAM_MAX_LEN for t in texts)
    assert "".join(texts) == msg                    # tak ada isi yang hilang


def test_message_at_limit_is_sent_as_single_chunk(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = "y" * alerts.TELEGRAM_MAX_LEN
    assert alerts.send_alert(msg) is True
    texts = tg_texts(rec)
    assert len(texts) == 1 and texts[0] == msg


def test_discord_long_message_also_split(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = "z" * 4500
    assert alerts.send_alert(msg) is True
    contents = [p["content"] for p in dc_payloads(rec)]
    assert len(contents) == 3
    assert all(len(c) <= alerts.DISCORD_MAX_LEN for c in contents)
    assert "".join(contents) == msg


def test_split_message_with_unicode_keeps_all_parts_valid(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    msg = ("CLOSE 🔒 " * 1200)[:5000]                # multi-byte, >4096 char
    assert alerts.send_alert(msg) is True
    texts = tg_texts(rec)
    assert all(len(t) <= alerts.TELEGRAM_MAX_LEN for t in texts)
    assert "".join(texts) == msg