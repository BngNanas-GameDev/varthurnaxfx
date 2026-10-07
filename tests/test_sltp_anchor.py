"""Regresi SL/TP: level harus di-anchor ke harga FILL, bukan close bar.

Bug live (7 Okt 2026): sinyal LONG dari close H1 83992 (SL 83567, TP 84628),
tapi market order fill di 84676 -> TP 84628 < fill -> Binance tolak dengan
-2021 "Order would immediately trigger", proteksi tak pernah terpasang, dan
flatten tanpa amount juga ditolak (BadRequest) -> posisi telanjang.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution.binance_client import (  # noqa: E402
    BinanceClient, EntryUnprotectedError, _filled_price, _reanchor_levels,
)


# --- helper murni ----------------------------------------------------------
def test_reanchor_long_keeps_risk_and_rr():
    # sinyal: entry 83992.2, sl 83567.68 (risk 424.52), tp 84975.63
    # RR sinyal = |tp-sl|/risk = 1407.95/424.52 = 3.3166
    sl, tp = _reanchor_levels("BUY", 84676.6, 83567.68, 84975.63,
                              signal_entry=83992.2)
    assert sl == pytest.approx(84676.6 - 424.52, abs=0.01)
    assert sl < 84676.6 < tp
    assert (tp - 84676.6) / (84676.6 - sl) == pytest.approx(3.3166, rel=1e-3)


def test_reanchor_short_keeps_risk_and_rr():
    # sinyal: entry 84600, sl 84800 (risk 200), tp 82800 -> RR 10.0
    sl, tp = _reanchor_levels("SELL", 84000.0, 84800.0, 82800.0,
                              signal_entry=84600.0)
    assert tp < 84000.0 < sl
    assert (84000.0 - tp) / (sl - 84000.0) == pytest.approx(10.0, rel=1e-3)


def test_reanchor_risk_comes_from_signal_not_gap():
    """Risk harus tetap milik sinyal walau fill jauh dari harga sinyal."""
    sl, tp = _reanchor_levels("BUY", 90000.0, 83567.0, 84975.0,
                              signal_entry=83992.0)
    assert 90000.0 - sl == pytest.approx(424.5, abs=0.5)  # bukan ~6000


def test_reanchor_zero_fill_is_noop():
    sl, tp = _reanchor_levels("BUY", 0.0, 83567.0, 84628.0)
    assert (sl, tp) == (83567.0, 84628.0)


def test_filled_price_reads_average_then_price():
    assert _filled_price({"average": 84676.6}) == pytest.approx(84676.6)
    assert _filled_price({"price": 100.0}) == pytest.approx(100.0)
    assert _filled_price({"info": {"avgPrice": "55.5"}}) == pytest.approx(55.5)
    assert _filled_price({}) is None
    assert _filled_price("bukan dict") is None


# --- integrasi place_entry -------------------------------------------------
def _client(order_responses, position_qty=0.023):
    c = BinanceClient(dry_run=False, api_key="k", api_secret="s")
    ex = MagicMock()
    c._exchange = ex
    it = iter(order_responses)

    def _create(*a, **kw):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v

    ex.create_order = MagicMock(side_effect=_create)
    calls = []

    def _retry(fn, *a, **kw):
        calls.append((fn, a, kw))
        if fn == "fetch_positions":
            return [{"symbol": "BTC/USDT:USDT", "contracts": position_qty}]
        if fn == "cancel_all_orders":
            return {}
        return getattr(ex, fn)(*a, **kw)

    c._call_with_retry = _retry
    c.calls = calls
    return c, ex


def test_stale_tp_is_reanchored_before_attach():
    c, ex = _client([
        {"average": 84676.6},                     # entry fill
        {"id": "sl"},                             # STOP_MARKET
        {"id": "tp"},                             # TAKE_PROFIT_MARKET
    ])
    res = c.place_entry(side="BUY", qty=0.023, trace_id="t1",
                        sl_price=83567.68, tp_price=84628.98)
    tp_sent = ex.create_order.call_args_list[2][0][0:2]
    assert tp_sent[1] == "TAKE_PROFIT_MARKET"
    tp_params = ex.create_order.call_args_list[2][0][5]
    assert float(tp_params["stopPrice"]) > 84676.6, "TP harus di atas harga fill"
    assert res["filled_price"] == pytest.approx(84676.6)
    assert res["tp_price"] > 84676.6


def test_invalid_levels_are_rejected_before_sending():
    """Guard -2021: level tak valid tak boleh dikirim ke exchange."""
    c, ex = _client([
        {"average": 84676.6},
        {"id": "sl"},
        {"id": "tp"},
    ])
    # SIGNAL dengan TP di bawah fill: re-anchor harus memulihkan ke atas
    res = c.place_entry(side="BUY", qty=0.01, trace_id="t2",
                        sl_price=84600.0, tp_price=84500.0)
    assert res["tp_price"] > 84676.6


def test_attach_guard_raises_when_level_still_invalid():
    """Guard -2021: level tak valid tak boleh dikirim ke exchange."""
    c, ex = _client([{"id": "flat"}])
    with pytest.raises(ValueError, match="level invalid"):
        c._attach_sltp_native("BTC/USDT:USDT", "sell", 83500.0, 84700.0,
                              "t3", entry_side="BUY", fill_px=84800.0)


def test_attach_guard_accepts_valid_long():
    c, ex = _client([{"id": "sl"}, {"id": "tp"}])
    c._attach_sltp_native("BTC/USDT:USDT", "sell", 84000.0, 86000.0,
                          "t3b", entry_side="BUY", fill_px=84676.6)


def test_attach_guard_accepts_valid_short():
    c, ex = _client([{"id": "sl"}, {"id": "tp"}])
    c._attach_sltp_native("BTC/USDT:USDT", "buy", 85300.0, 82600.0,
                          "t3c", entry_side="SELL", fill_px=84000.0)


def test_flatten_uses_explicit_qty():
    """reduceOnly tanpa amount ditolak Binance -> wajib qty dari posisi."""
    c, ex = _client([{"id": "flatorder"}])
    ok = c._force_flatten("BTC/USDT:USDT", "sell", "t4")
    assert ok is True
    args = ex.create_order.call_args[0]
    assert args[3] == pytest.approx(0.023)   # amount eksplisit
    assert args[5]["reduceOnly"] is True


def test_flatten_returns_false_when_flat():
    c, ex = _client([], position_qty=0.0)
    assert c._force_flatten("BTC/USDT:USDT", "sell", "t5") is False


def test_flatten_failure_still_raises_and_cancels():
    """Entry fill -> attach gagal -> flatten gagal: tetap raise + cancel order."""
    c, ex = _client([
        {"average": 84676.6},                 # entry fill
        RuntimeError("attach boom"),          # STOP_MARKET gagal
        RuntimeError("flatten boom"),         # flatten market gagal
    ])
    with pytest.raises(EntryUnprotectedError):
        c.place_entry(side="BUY", qty=0.023, trace_id="t6",
                      sl_price=83567.0, tp_price=84900.0,
                      signal_entry=83992.0)
    assert any(fn == "cancel_all_orders" for fn, _, _ in c.calls)
