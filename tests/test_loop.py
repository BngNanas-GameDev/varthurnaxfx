"""Loop strategy tests: no-order default, single entry, halt/position blocks."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
from trading import loop as trading_loop  # noqa: E402
from trading.state import clear_position, load, set_position, trip_halt  # noqa: E402


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "trading_state.json")


@pytest.fixture
def client():
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.place_entry.return_value = {"clientOrderId": "x", "status": "filled"}
    return m


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)


def make_long_gold(seed: int = 3, n: int = 75) -> pd.DataFrame:
    """OHLCV sintetis -> build_features -> evaluate() == LONG (breakout H1)."""
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from data.pipeline import build_features

    rng = np.random.default_rng(seed)
    base = 60000.0
    closes = [base + 3.0 * i + 300.0 * np.sin(i / 2.5)
              + rng.normal(0, 20.0) for i in range(n - 1)]
    prior_hh = max(c + 5 for c in closes[-21:-1])
    closes.append(prior_hh + 30.0)
    df = pd.DataFrame({
        "open_time": [int(1_700_000_000_000 + i * 3600_000) for i in range(n)],
        "open": [c - 10 for c in closes],
        "high": [c + 5 for c in closes],
        "low": [c - 120 for c in closes],
        "close": closes,
        "volume": [10.0] * n,
        "close_time": [0] * n, "trades": [10] * n, "is_gap": [False] * n,
    })
    return build_features(df)


def make_flat_gold(n: int = 75) -> pd.DataFrame:
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from data.pipeline import build_features

    closes = [60000.0 + float(np.sin(i)) for i in range(n)]
    df = pd.DataFrame({
        "open_time": [int(1_700_000_000_000 + i * 3600_000) for i in range(n)],
        "open": closes, "high": [c + 5 for c in closes],
        "low": [c - 5 for c in closes], "close": closes,
        "volume": [10.0] * n,
        "close_time": [0] * n, "trades": [10] * n, "is_gap": [False] * n,
    })
    return build_features(df)


def test_long_signal_evaluates_long():
    from strategy.setups import evaluate

    sig = evaluate(make_long_gold())
    assert sig["action"] == "LONG", sig


def test_flat_no_order(client, state_file):
    res = trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    client.place_entry.assert_not_called()


def test_long_places_single_entry_and_records_state(client, state_file):
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["action"] == "LONG", res
    assert res["ordered"] is True
    assert client.place_entry.call_count == 1
    _, kwargs = client.place_entry.call_args
    assert kwargs["side"] == "BUY"
    assert kwargs["trace_id"] == res["trace_id"]  # idempotency clientOrderId
    assert kwargs["sl_price"] > 0
    st = load(state_file)
    assert st["open_position"]["side"] == "LONG"
    assert st["open_position"]["trace_id"] == res["trace_id"]


def test_halt_latched_blocks_order(client, state_file):
    trip_halt("test latch", state_file)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    assert "halt" in res["reason"].lower()
    client.place_entry.assert_not_called()


def test_open_position_blocks_double_entry(client, state_file):
    set_position({"side": "LONG", "qty": 0.03, "entry": 60000.0,
                  "sl": 59700.0, "tp": 60600.0, "trace_id": "prev"},
                 state_file)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    assert "open" in res["reason"].lower()
    client.place_entry.assert_not_called()
    # posisi lama tidak tertimpa
    assert load(state_file)["open_position"]["trace_id"] == "prev"


def test_no_data_no_order(client, state_file, tmp_path):
    empty_gold = tmp_path / "gold_empty"
    empty_gold.mkdir()
    res = trading_loop.run_cycle(client, 1000.0, df=None,
                                 state_file=state_file, gold_dir=empty_gold)
    assert res["action"] == "NO_DATA"
    assert res["ordered"] is False
    client.place_entry.assert_not_called()


def test_daily_stop_blocks_and_latches(client, state_file):
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file, daily_pnl=-0.06)
    assert res["ordered"] is False
    assert "daily_stop" in res["reason"]
    assert load(state_file)["halt_latched"] is True
    client.place_entry.assert_not_called()
    clear_position(state_file)  # hygiene, no-op bila None
