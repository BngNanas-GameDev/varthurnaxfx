"""Kontrak pesan Telegram: apa yang WAJIB muncul di notif (dan tak boleh bocor).

OFFLINE total: client palsu (tanpa MagicMock) + urllib.request.urlopen di-mock.

Regression utama: notif CLOSE yang menampilkan PnL 0.00 karena markPrice tak ada
saat posisi sudah flat. Di sini pesannya di-assert secara kontrak:
  - selalu memuat angka PnL numerik (tak ada "None"/"nan"/"undefined"),
  - data exchange kosong -> WAJIB ada penanda "(est)" (tak pernah mengklaim pasti),
  - net = gross - fee + funding konsisten,
  - tak ada secret (BINANCE_API_KEY/SECRET) di pesan mana pun.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # paket src.* (dipakai loop -> alerts)
sys.path.insert(0, str(ROOT / "src"))

from test_loop import make_flat_gold, make_long_gold  # noqa: E402
from trading import loop as trading_loop  # noqa: E402
from trading.state import set_position  # noqa: E402

EMPTY_INCOME = {"realized_pnl": None, "fee": None, "funding": None,
                "exit_price": None, "exit_ts": None}

API_KEY = "AKIA-SENTINEL-API-KEY-DO-NOT-LEAK"
API_SECRET = "SENTINEL-SECRET-DO-NOT-LEAK"
PNL_RE = re.compile(r"PnL ([+-]?\d+\.\d{2}) USDT")
BAD_TOKENS = ("None", "nan", "NaN", "undefined", "Infinity", "inf")
LONG_POS = {"side": "LONG", "qty": 0.03, "entry": 60000.0, "sl": 59700.0,
            "tp": 60600.0, "trace_id": "prev"}


class StubClient:
    """Client palsu: seluruh respons exchange Deterministik (bukan MagicMock)."""

    def __init__(self, contracts=0.0, side=None, entry_price=None, mark=None,
                 realized=EMPTY_INCOME, daily=0.0):
        self.contracts = contracts
        self.side = side
        self.entry_price = entry_price
        self.mark = mark
        self.realized = realized
        self.daily = daily
        self.orders: list[dict] = []

    def get_position(self, symbol="BTCUSDT-PERP"):
        pos = {"symbol": symbol, "contracts": self.contracts}
        if self.side:
            pos["side"] = self.side
        if self.entry_price is not None:
            pos["entryPrice"] = self.entry_price
        if self.mark is not None:
            pos["markPrice"] = self.mark
        return pos

    def fetch_realized(self, symbol="BTC/USDT:USDT", since_ms=None):
        return dict(self.realized) if isinstance(self.realized, dict) else self.realized

    def get_daily_pnl(self):
        return self.daily

    def place_entry(self, **kw):
        self.orders.append(kw)
        return {"clientOrderId": kw.get("trace_id"), "status": "filled"}


class _FakeResp:
    def __init__(self, payload: bytes):
        self._raw = payload

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_llm(*contents):
    """urlopen palsu untuk OpenRouter: panggilan ke-n -> contents[n]."""
    seq = list(contents)

    def _fake(req, timeout=None):
        content = seq.pop(0) if len(seq) > 1 else seq[0]
        body = {"choices": [{"message": {"content": content}}]}
        return _FakeResp(json.dumps(body).encode("utf-8"))

    return _fake


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    monkeypatch.setenv("JOURNAL_FILE", str(tmp_path / "journal.jsonl"))
    monkeypatch.setenv("BINANCE_API_KEY", API_KEY)
    monkeypatch.setenv("BINANCE_API_SECRET", API_SECRET)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Tangkap setiap pesan notifikasi yang dikirim loop."""
    import src.ops.alerts as alerts_mod

    box: list[str] = []
    monkeypatch.setattr(alerts_mod, "send_alert", lambda m: box.append(m) or True)
    return box


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "trading_state.json")


def only(box, prefix):
    msgs = [m for m in box if m.startswith(prefix)]
    assert len(msgs) == 1, box
    return msgs[0]


def assert_no_junk(msg: str):
    """Tak boleh ada NaN/None/undefined/inf bocor ke notifikasi."""
    for bad in BAD_TOKENS:
        assert bad not in msg, f"{bad!r} bocor ke notif: {msg!r}"


# -- CLOSE: PnL ------------------------------------------------------------

def test_close_message_pnl_is_numeric_not_none_nan(sent, state_file):
    client = StubClient(contracts=0.0, realized={
        "realized_pnl": 150.45, "fee": 0.45, "funding": -0.12,
        "exit_price": 60500.0, "exit_ts": 1})
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert PNL_RE.search(msg), msg
    assert "PnL +150.45 USDT" in msg, msg       # tanda + wajib, bukan "150.45"
    assert msg.count("nan") == 0


def test_close_message_contains_all_required_labels(sent, state_file):
    client = StubClient(realized={"realized_pnl": 150.0, "fee": 0.45,
                                  "funding": -0.12, "exit_price": 60500.0})
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    for label in ("PnL", "gross", "fee", "funding", "net", "exit=", "qty=", "entry="):
        assert label in msg, (label, msg)
    assert "manual/unknown" in msg              # reason exit ikut terkirim
    assert_no_junk(msg)


def test_close_message_net_matches_fee_and_funding(sent, state_file):
    client = StubClient(realized={"realized_pnl": 150.45, "fee": 0.45,
                                  "funding": -0.12, "exit_price": 60500.0})
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert "funding -0.12" in msg               # funding negatif tetap bertanda
    assert "net +149.88" in msg                 # 150.45 - 0.45 - 0.12
    assert_no_junk(msg)


def test_close_without_any_exchange_data_is_marked_estimated(sent, state_file):
    """Inilah regresi PnL 0.00: markPrice absen + income history kosong.

    PnL WAJIB dihitung dari level STATE (tak boleh 0.00) dan penanda (est) WAJIB
    muncul karena angka ini bukan hasil eksekusi nyata.
    """
    client = StubClient(contracts=0.0, realized=EMPTY_INCOME)   # markPrice absen
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert "(est)" in msg, msg                  # tak mengklaim pasti
    assert PNL_RE.search(msg), msg
    assert "PnL -9.00 USDT" in msg, msg         # (59700-60000)*0.03, bukan 0.00
    assert "net -9.00" in msg, msg
    assert "stop-loss" in msg


@pytest.mark.parametrize("realized", [
    EMPTY_INCOME, {}, None,
    {"realized_pnl": float("nan"), "fee": float("nan"), "funding": float("nan"),
     "exit_price": float("nan")},
    {"realized_pnl": None, "fee": None, "funding": None, "exit_price": None},
])
def test_close_message_never_becomes_nan_or_none(sent, state_file, realized):
    client = StubClient(realized=realized)
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert PNL_RE.search(msg), msg              # selalu ada angka 2 desimal
    assert "(est)" in msg                       # data exchange kosong -> est
    assert "None USDT" not in msg and "nan" not in msg


def test_close_with_nan_markprice_still_reports_pnl(sent, state_file):
    client = StubClient(contracts=0.0, mark=float("nan"), realized=EMPTY_INCOME)
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert "(est)" in msg
    assert PNL_RE.search(msg), msg


def test_close_short_fallback_to_sl_reports_loss(sent, state_file):
    """SHORT tanpa data exchange -> fallback ke SL -> rugi (tanda minus)."""
    client = StubClient(contracts=0.0, realized=EMPTY_INCOME)
    set_position({"side": "SHORT", "qty": 0.03, "entry": 60000.0, "sl": 60300.0,
                  "tp": 59400.0, "trace_id": "prev-s"}, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert "SHORT" in msg
    assert "PnL -9.00 USDT" in msg               # (60300-60000)*0.03*(-1)
    assert "(est)" in msg and "stop-loss" in msg


def test_close_short_take_profit_reports_gain(sent, state_file):
    """SHORT keluar di bawah entry -> untung, tanda plus + alasan take-profit."""
    client = StubClient(contracts=0.0, mark=59400.0, realized=EMPTY_INCOME)
    set_position({"side": "SHORT", "qty": 0.03, "entry": 60000.0, "sl": 60300.0,
                  "tp": 59400.0, "trace_id": "prev-s"}, state_file)
    trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(), state_file=state_file)

    msg = only(sent, "CLOSE")
    assert_no_junk(msg)
    assert "PnL +18.00 USDT" in msg               # (59400-60000)*0.03*(-1)
    assert "take-profit" in msg


# -- ENTRY / HALT / ADOPT ---------------------------------------------------

def test_entry_message_has_numeric_prices_and_no_nan(sent, state_file):
    client = StubClient(contracts=0.0)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["ordered"] is True
    msg = only(sent, "ENTRY")
    assert_no_junk(msg)
    assert "BUY" in msg and "entry=" in msg and "sl=" in msg
    assert re.search(r"qty=\d+\.\d+", msg), msg


def test_halt_message_has_numeric_daily_pnl_and_no_nan(sent, state_file):
    client = StubClient(contracts=0.0)
    trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                           state_file=state_file, daily_pnl=-0.06)
    msg = only(sent, "HALT")
    assert_no_junk(msg)
    assert "daily_stop:-0.0600" in msg           # angka, bukan teks kosong
    assert client.orders == []


def test_adopt_message_has_numeric_entry_and_no_nan(sent, state_file):
    client = StubClient(contracts=0.001, side="long", entry_price=84000.0,
                        realized=EMPTY_INCOME)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    msg = only(sent, "ADOPT")
    assert_no_junk(msg)
    assert "84000" in msg and "LONG" in msg


# -- LLM: VETO / ARBITER ----------------------------------------------------

def test_veto_message_has_no_nan(sent, state_file, monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(urllib.request, "urlopen", fake_llm(
        '{"verdict":"VETO","confidence_mult":1.0,"reason":"struktur rusak"}'))
    client = StubClient(contracts=0.0)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)

    assert res["ordered"] is False
    msg = only(sent, "VETO")
    assert_no_junk(msg)
    assert "llm-veto" in msg and "struktur rusak" in msg
    assert client.orders == []


def test_arbiter_message_has_no_nan(sent, state_file, monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(urllib.request, "urlopen", fake_llm(
        '{"pick":"LONG","confidence_mult":1.0,"reason":"tren naik"}',
        '{"verdict":"CONFIRM","confidence_mult":1.0,"reason":"ok"}'))
    monkeypatch.setattr(trading_loop, "evaluate", lambda df: {
        "action": "NO_TRADE", "setup": None, "entry": None, "sl": None, "tp": None,
        "confidence": 0.0, "thesis": "konflik", "thesis_breaker": "n/a",
        "reason": "konflik LONG vs SHORT",
        "candidates": [
            {"action": "LONG", "entry": 60000.0, "sl": 59700.0, "tp": 60600.0,
             "confidence": 0.7, "reason": "up"},
            {"action": "SHORT", "entry": 60000.0, "sl": 60300.0, "tp": 59400.0,
             "confidence": 0.6, "reason": "down"}]})
    client = StubClient(contracts=0.0)
    trading_loop.run_cycle(client, 1000.0, df=make_long_gold(), state_file=state_file)

    msg = only(sent, "ARBITER")
    assert_no_junk(msg)
    assert "LONG" in msg and "tren naik" in msg


# -- tidak ada secret di pesan mana pun --------------------------------------

def test_no_message_ever_contains_api_secret(sent, state_file):
    """Kumpulkan pesan dari semua jalur notifikasi, cek tak ada key bocor."""
    # CLOSE tanpa data exchange
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(StubClient(contracts=0.0, realized=EMPTY_INCOME),
                           1000.0, df=make_flat_gold(), state_file=state_file)
    # ADOPT posisi exchange
    trading_loop.run_cycle(
        StubClient(contracts=0.001, side="long", entry_price=84000.0),
        1000.0, df=make_long_gold(), state_file=state_file)
    # ENTRY
    trading_loop.run_cycle(StubClient(contracts=0.0), 1000.0,
                           df=make_long_gold(), state_file=state_file)
    # HALT
    trading_loop.run_cycle(StubClient(contracts=0.0), 1000.0,
                           df=make_long_gold(), state_file=state_file,
                           daily_pnl=-0.5)

    assert len(sent) >= 4, sent
    for msg in sent:
        assert API_KEY not in msg, msg
        assert API_SECRET not in msg, msg
        assert "BINANCE_API" not in msg, msg
        assert_no_junk(msg)


def test_all_messages_carry_mode_label(sent, state_file, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    set_position(LONG_POS, state_file)
    trading_loop.run_cycle(StubClient(contracts=0.0, realized=EMPTY_INCOME),
                           1000.0, df=make_flat_gold(), state_file=state_file)
    assert sent, "tidak ada pesan terkirim"
    for msg in sent:
        assert "[DRY_RUN]" in msg, msg          # mode wajib, tak pernah ambigu