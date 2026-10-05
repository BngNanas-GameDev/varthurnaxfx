"""Test income history Binance via fapiPrivateGetIncome (ccxt 4.5.x).

Penting: ccxt 4.5.85 TIDAK punya `fetch_income` untuk Binance futures
(diverifikasi scripts/ccxt_income_probe.py). Yang dipakai adalah endpoint
implisit `fapiPrivateGetIncome` + `parse_income`.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution.binance_client import BinanceClient  # noqa: E402

EMPTY = {"realized_pnl": None, "fee": None, "funding": None,
         "exit_price": None, "exit_ts": None}


def _rows(*items):
    """Bentuk respons implisit ccxt: dict {"income": [...]}."""
    return {"income": [dict(i) for i in items]}


def _client(ex, realized=None):
    c = BinanceClient(dry_run=False, api_key="k", api_secret="s")
    c._exchange = ex
    c._call_with_retry = lambda fn, *a, **kw: getattr(ex, fn)(*a, **kw)
    if realized is not None:
        c.fetch_realized = lambda **kw: realized
    return c


def _ex(income=None, trades=None, income_exc=None, trades_exc=None):
    ex = MagicMock()
    ex.parse_income = lambda it: {
        "amount": it.get("income"), "type": it.get("incomeType"),
        "info": it,
    }
    ex.fapiPrivateGetIncome = MagicMock(
        side_effect=income_exc, return_value=income)
    ex.fetch_my_trades = MagicMock(
        side_effect=trades_exc, return_value=trades or [])
    return ex


def test_sums_income_and_last_trade():
    ex = _ex(income=_rows(
        {"income": "150.0", "incomeType": "REALIZED_PNL"},
        {"income": "-0.45", "incomeType": "COMMISSION"},
        {"income": "-0.90", "incomeType": "COMMISSION"},
        {"income": "-0.12", "incomeType": "FUNDING_FEE"},
    ), trades=[{"price": 60000.0, "timestamp": 1},
               {"price": 60500.0, "timestamp": 2}])
    out = _client(ex).fetch_realized(since_ms=1000)
    assert out["realized_pnl"] == pytest.approx(150.0)
    assert out["fee"] == pytest.approx(1.35)      # absolut
    assert out["funding"] == pytest.approx(-0.12)
    assert out["exit_price"] == pytest.approx(60500.0)
    assert out["exit_ts"] == 2


def test_sends_symbol_starttime_endtime():
    ex = _ex(income=_rows())
    _client(ex).fetch_realized(since_ms=1000)
    params = ex.fapiPrivateGetIncome.call_args[0][0]
    assert params["symbol"]
    assert params["startTime"] == 1000
    assert params["endTime"] > 1000          # jendela tertutup, bukan "sampai kapan saja"


def test_without_since_ms_is_refused():
    """Tanpa since_ms default window Binance 7 hari -> PnL trade lain tercampur."""
    ex = _ex(income=_rows({"income": "999.0", "incomeType": "REALIZED_PNL"}))
    out = _client(ex).fetch_realized()
    assert out == EMPTY
    ex.fapiPrivateGetIncome.assert_not_called()


def test_dry_run_returns_empty():
    out = BinanceClient(dry_run=True).fetch_realized(since_ms=1)
    assert out == EMPTY


def test_api_error_degrades_safely():
    ex = _ex(income_exc=RuntimeError("boom"), trades_exc=RuntimeError("boom"))
    out = _client(ex).fetch_realized(since_ms=1000)
    assert out == EMPTY


def test_skips_bad_amounts():
    ex = _ex(income=_rows(
        {"income": "bukan-angka", "incomeType": "REALIZED_PNL"},
        {"income": "12.0", "incomeType": "REALIZED_PNL"},
    ))
    out = _client(ex).fetch_realized(since_ms=1000)
    assert out["realized_pnl"] == pytest.approx(12.0)
    assert out["exit_price"] is None


def test_list_response_also_supported():
    ex = _ex(income=[{"income": "5.0", "incomeType": "REALIZED_PNL"}])
    out = _client(ex).fetch_realized(since_ms=1000)
    assert out["realized_pnl"] == pytest.approx(5.0)


def test_get_daily_pnl_returns_fraction():
    ex = _ex(income=_rows({"income": "-50.0", "incomeType": "REALIZED_PNL"}))
    c = _client(ex)
    c._call_with_retry = lambda fn, *a, **kw: (
        {"USDT": {"total": 1000.0}} if fn == "fetch_balance"
        else getattr(ex, fn)(*a, **kw))
    assert c.get_daily_pnl() == pytest.approx(-0.05)


def test_get_daily_pnl_fail_closed_none():
    """Tidak terbaca -> None (bukan 0.0) supaya rem daily loss tak buta."""
    ex = _ex(income_exc=RuntimeError("boom"))
    c = _client(ex)
    c._call_with_retry = lambda fn, *a, **kw: (
        {"USDT": {"total": 1000.0}} if fn == "fetch_balance"
        else getattr(ex, fn)(*a, **kw))
    assert c.get_daily_pnl() is None


def test_get_daily_pnl_zero_equity_returns_none():
    ex = _ex(income=_rows({"income": "0.0", "incomeType": "REALIZED_PNL"}))
    c = _client(ex)
    c._call_with_retry = lambda fn, *a, **kw: (
        {"USDT": {"total": 0.0}} if fn == "fetch_balance"
        else getattr(ex, fn)(*a, **kw))
    assert c.get_daily_pnl() is None
