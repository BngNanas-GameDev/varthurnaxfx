"""Test fetch_realized: income history Binance -> PnL/fee/funding riil."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution.binance_client import BinanceClient  # noqa: E402


def _client(fake_exchange):
    c = BinanceClient(dry_run=False, api_key="k", api_secret="s")
    c._exchange = fake_exchange
    c._call_with_retry = lambda fn, *a, **kw: getattr(fake_exchange, fn)(*a, **kw)
    return c


def test_fetch_realized_sums_income_and_last_trade():
    ex = MagicMock()
    ex.fetch_income = MagicMock(return_value=[
        {"amount": 150.0, "info": {"incomeType": "REALIZED_PNL"}},
        {"amount": -0.45, "info": {"incomeType": "COMMISSION"}},
        {"amount": -0.90, "info": {"incomeType": "COMMISSION"}},
        {"amount": -0.12, "info": {"incomeType": "FUNDING_FEE"}},
    ])
    ex.fetch_my_trades = MagicMock(return_value=[
        {"price": 60000.0, "timestamp": 1},
        {"price": 60500.0, "timestamp": 2},
    ])
    out = _client(ex).fetch_realized(since_ms=1000)
    assert out["realized_pnl"] == pytest.approx(150.0)
    assert out["fee"] == pytest.approx(1.35)      # absolut
    assert out["funding"] == pytest.approx(-0.12)
    assert out["exit_price"] == pytest.approx(60500.0)
    assert out["exit_ts"] == 2


def test_fetch_realized_dry_run_returns_empty():
    out = BinanceClient(dry_run=True).fetch_realized()
    assert out["realized_pnl"] is None
    assert out["exit_price"] is None


def test_fetch_realized_api_error_degrades_safely():
    ex = MagicMock()
    ex.fetch_income = MagicMock(side_effect=RuntimeError("boom"))
    ex.fetch_my_trades = MagicMock(side_effect=RuntimeError("boom"))
    out = _client(ex).fetch_realized()
    assert out["realized_pnl"] is None
    assert out["exit_price"] is None


def test_fetch_realized_skips_bad_amounts():
    ex = MagicMock()
    ex.fetch_income = MagicMock(return_value=[
        {"amount": "bukan-angka", "info": {"incomeType": "REALIZED_PNL"}},
        {"amount": 12.0, "info": {"incomeType": "REALIZED_PNL"}},
    ])
    ex.fetch_my_trades = MagicMock(return_value=[])
    out = _client(ex).fetch_realized()
    assert out["realized_pnl"] == pytest.approx(12.0)
    assert out["exit_price"] is None
