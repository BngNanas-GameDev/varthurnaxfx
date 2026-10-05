"""Fail-closed guards yang ditemukan code review (bug class: rem buta).

C1/H1/H2/H3/C2 di并没浑 ada test sebelumnya karena semua test lama memakai
mock yang SELALU mengembalikan data, sehingga jalur "exchange tidak
memberi data" tak pernah tersentuh.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.binance_client import BinanceClient, EntryUnprotectedError  # noqa: E402
from test_notify_contract import StubClient, make_flat_gold, make_long_gold  # noqa: E402
from trading import loop as trading_loop  # noqa: E402
from trading.state import load, set_position  # noqa: E402

POS = {"side": "LONG", "qty": 0.03, "entry": 60000.0, "sl": 59700.0,
       "tp": 60600.0, "trace_id": "prev", "opened_ts": 1_700_000_000_000}


class TracingClient(StubClient):
    """StubClient yang mencatat since_ms tiap panggilan fetch_realized."""

    def __init__(self, realized=None, **kw):
        super().__init__(realized=realized, **kw)
        self.since_calls: list = []

    def fetch_realized(self, symbol="BTC/USDT:USDT", since_ms=None):
        self.since_calls.append(since_ms)
        return super().fetch_realized(symbol=symbol, since_ms=since_ms)


# --- H1: daily PnL tak terbaca -> order DIBLOKIR, bukan dianggap 0.0 -------
def test_daily_pnl_unknown_blocks_order(tmp_path):
    """Rem daily -5% tak boleh buta: tak terbaca = tak boleh order."""
    sf = str(tmp_path / "st.json")
    client = StubClient(contracts=0.0, daily=None)   # None = "tidak tahu"
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=sf)
    assert res["ordered"] is False
    assert "daily_pnl_unknown" in res["reason"]
    assert load(sf)["daily_pnl"] is None


def test_daily_pnl_known_allows_order(tmp_path):
    sf = str(tmp_path / "st.json")
    client = StubClient(contracts=0.0, daily=0.0)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=sf)
    assert res["ordered"] is True


def test_daily_pnl_exception_blocks_order(tmp_path):
    class Boom(StubClient):
        def get_daily_pnl(self):
            raise RuntimeError("boom")

    sf = str(tmp_path / "st.json")
    res = trading_loop.run_cycle(Boom(contracts=0.0), 1000.0,
                                 df=make_long_gold(), state_file=sf)
    assert res["ordered"] is False
    assert "daily_pnl_unknown" in res["reason"]


# --- H3: tanpa opened_ts, income history tak boleh dipanggil ---------------
def test_no_opened_ts_no_income_call():
    client = TracingClient({"realized_pnl": 999.0, "fee": 0.0, "funding": 0.0,
                            "exit_price": 60600.0, "exit_ts": 1})
    out = trading_loop._resolve_exit(
        client, {"side": "LONG", "qty": 0.03, "entry": 60000.0}, {})
    assert client.since_calls == []
    assert out["reason"] != "realized"
    assert out["pnl"] != pytest.approx(999.0)


def test_opened_ts_present_income_used():
    client = TracingClient({"realized_pnl": 150.0, "fee": 0.45, "funding": 0.0,
                            "exit_price": 60500.0, "exit_ts": 1})
    out = trading_loop._resolve_exit(client, dict(POS), {})
    assert client.since_calls == [POS["opened_ts"]]
    assert out["pnl"] == pytest.approx(150.0)
    assert out["estimated"] is False


# --- H2: gate live WAJIB ada di client, bukan cuma di loop -----------------
def test_live_without_allow_live_raises(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BINANCE_DEMO", "false")
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    with pytest.raises(RuntimeError, match="ALLOW_LIVE"):
        BinanceClient()


def test_live_with_allow_live_passes_gate(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BINANCE_DEMO", "false")
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    monkeypatch.setenv("ALLOW_LIVE", "true")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    # hanya melewati gate; build exchange butuh ccxt (ada di requirements)
    c = BinanceClient()
    assert c.dry_run is False


def test_demo_does_not_need_allow_live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BINANCE_DEMO", "true")
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    c = BinanceClient()
    assert c.dry_run is False


# --- C2: entry fill tapi SL/TP gagal -> flatten + STATE tetap mencatat -----
def test_attach_failure_flattens_and_raises():
    c = BinanceClient(dry_run=False, api_key="k", api_secret="s")
    ex = MagicMock()
    c._exchange = ex
    # urutan: entry fill -> STOP_MARKET gagal -> cancel -> flatten market
    ex.create_order = MagicMock(side_effect=[{"id": 1}, RuntimeError("sl fail"),
                                              {"id": 99}])
    calls = []

    def _retry(fn, *a, **kw):
        calls.append(fn)
        return getattr(ex, fn)(*a, **kw)

    c._call_with_retry = _retry
    with pytest.raises(EntryUnprotectedError):
        c.place_entry(side="BUY", qty=0.01, trace_id="t1",
                      sl_price=59700.0, tp_price=60600.0)
    assert "cancel_all_orders" in calls
    assert calls[0] == "create_order"          # entry dicoba dulu
    assert calls[1] == "cancel_all_orders"     # proteksi dibersihkan


def test_attach_failure_raises_even_if_flatten_also_fails():
    """Entry fill -> attach gagal -> cancel gagal -> flatten gagal.

    Tetap HARUS raise EntryUnprotectedError supaya STATE mencatat posisi
    telanjang. Kehilangan sinyal "periksa manual" = risiko tak terbatas.
    """
    c = BinanceClient(dry_run=False, api_key="k", api_secret="s")
    ex = MagicMock()
    c._exchange = ex
    # entry fill sukses (create_order langsung), flatten market gagal
    ex.create_order = MagicMock(side_effect=[{"id": 1},
                                              RuntimeError("flatten gagal")])
    # attach SL/TP dan cancel_all_orders sama-sama gagal
    c._call_with_retry = lambda fn, *a, **kw: (_ for _ in ()).throw(
        RuntimeError("gagal: " + fn))
    with pytest.raises(EntryUnprotectedError):
        c.place_entry(side="BUY", qty=0.01, trace_id="t2",
                      sl_price=59700.0, tp_price=60600.0)


def test_entry_unprotected_records_position_and_notifies(tmp_path, monkeypatch):
    """Posisi sempat telanjang HARUS tercatat + notif keras, bukan dilupakan."""
    import src.ops.alerts as alerts_mod

    sent = []
    monkeypatch.setattr(alerts_mod, "send_alert", lambda m: sent.append(m) or True)
    sf = str(tmp_path / "st.json")

    class Unprotected(StubClient):
        def place_entry(self, **kw):
            raise EntryUnprotectedError("attach gagal")

    client = Unprotected(contracts=0.0, daily=0.0)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=sf)
    assert res["ordered"] is True
    assert res["reason"] == "filled-but-unprotected"
    st = load(sf)
    assert st["open_position"] is not None
    assert st["open_position"]["unprotected"] is True
    assert any(s.startswith("UNPROTECTED") for s in sent)


def test_plain_order_error_does_not_record_position(tmp_path):
    class Failing(StubClient):
        def place_entry(self, **kw):
            raise RuntimeError("ditolak exchange")

    sf = str(tmp_path / "st.json")
    res = trading_loop.run_cycle(Failing(contracts=0.0, daily=0.0), 1000.0,
                                 df=make_long_gold(), state_file=sf)
    assert res["ordered"] is False
    assert load(sf)["open_position"] is None
