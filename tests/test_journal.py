"""Jurnal trade tests: pnl long/short, fee/funding, summarize, hook loop."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
from trading import journal  # noqa: E402
from trading import loop as trading_loop  # noqa: E402
from trading.state import set_position  # noqa: E402


@pytest.fixture
def jfile(tmp_path, monkeypatch):
    p = str(tmp_path / "journal.jsonl")
    monkeypatch.setenv("JOURNAL_FILE", p)
    return p


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "trading_state.json")


@pytest.fixture
def client():
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.get_position.return_value = {"contracts": 0.0}
    m.place_entry.return_value = {"clientOrderId": "x", "status": "filled"}
    return m


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)


def make_long_gold(seed: int = 3, n: int = 75) -> pd.DataFrame:
    """OHLCV sintetis -> build_features -> evaluate() == LONG."""
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


def test_long_open_close_pnl(jfile):
    journal.record_open("t1", "LONG", 0.01, 60000.0, 59700.0, 60600.0,
                        "TREND_BREAKOUT_H1")
    journal.record_close("t1", 61000.0, "tp")
    s = journal.summarize()
    assert s["trades"] == 1
    assert s["wins"] == 1
    assert s["total_pnl"] == pytest.approx(10.0)
    assert s["total_fee"] == pytest.approx(0.0)


def test_short_open_close_pnl(jfile):
    journal.record_open("t2", "SHORT", 0.02, 60000.0, 60300.0, 59400.0,
                        "MEAN_REVERSION")
    journal.record_close("t2", 59000.0, "tp")
    s = journal.summarize()
    assert s["trades"] == 1
    assert s["wins"] == 1
    assert s["total_pnl"] == pytest.approx(20.0)


def test_fee_funding_reduce_pnl(jfile):
    journal.record_open("t3", "LONG", 0.01, 60000.0, 59700.0, 60600.0, "X")
    journal.record_close("t3", 61000.0, "tp", fee_paid=1.0, funding_paid=0.5)
    s = journal.summarize()
    assert s["total_pnl"] == pytest.approx(8.5)
    assert s["total_fee"] == pytest.approx(1.5)


def test_summarize_aggregate(jfile):
    journal.record_open("w1", "LONG", 0.01, 60000.0, 59700.0, 60600.0,
                        "TREND_BREAKOUT_H1")
    journal.record_close("w1", 61000.0, "tp", fee_paid=1.0)
    journal.record_open("l1", "SHORT", 0.01, 60000.0, 60300.0, 59400.0,
                        "MEAN_REVERSION")
    journal.record_close("l1", 61000.0, "sl", fee_paid=1.0, funding_paid=0.5)
    s = journal.summarize()
    assert s["trades"] == 2
    assert s["wins"] == 1
    assert s["total_pnl"] == pytest.approx(9.0 - 11.5)
    assert s["total_fee"] == pytest.approx(2.5)
    assert set(s["by_setup"]) == {"TREND_BREAKOUT_H1", "MEAN_REVERSION"}
    assert s["by_setup"]["TREND_BREAKOUT_H1"]["trades"] == 1
    assert s["by_setup"]["MEAN_REVERSION"]["pnl"] == pytest.approx(-11.5)


def test_loop_order_hook_records_open(client, state_file, jfile):
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=state_file)
    assert res["ordered"] is True
    lines = [json.loads(line) for line in Path(jfile).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    opens = [r for r in lines if r.get("event") == "open"
             and r.get("trace_id") == res["trace_id"]]
    assert len(opens) == 1
    assert opens[0]["side"] == "LONG"
    assert opens[0]["qty"] == pytest.approx(res["qty"])
    assert opens[0]["source"] == (res["signal"].get("setup") or "unknown")


def test_loop_closed_externally_hook_records_close(client, state_file, jfile):
    set_position({"side": "LONG", "qty": 0.03, "entry": 60000.0,
                  "sl": 59700.0, "tp": 60600.0, "trace_id": "prev",
                  "opened_ts": 1_700_000_000_000},
                 state_file)
    client.get_position.return_value = {"contracts": 0.0}
    client.fetch_realized.return_value = {
        "realized_pnl": 150.0, "fee": 0.9, "funding": 0.1,
        "exit_price": 60500.0, "exit_ts": 1_700_000_000_000,
    }
    res = trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    lines = [json.loads(line) for line in Path(jfile).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    closes = [r for r in lines if r.get("event") == "close"
              and r.get("trace_id") == "prev"]
    assert len(closes) == 1
    # exit 60500 bukan SL(59700) bukan juga TP(60600) -> manual/unknown
    assert closes[0]["reason"] == "manual/unknown", closes[0]
    assert closes[0]["exit_price"] == pytest.approx(60500.0)
    assert closes[0]["estimated"] is False
    assert closes[0]["fee_paid"] == pytest.approx(0.9)
    assert closes[0]["funding_paid"] == pytest.approx(0.1)


def test_loop_close_fallback_sl_when_exchange_unavailable(client, state_file, jfile):
    """Tanpa income history: exitprice dicurigai dari level STATE (SL/TP)."""
    set_position({"side": "LONG", "qty": 0.03, "entry": 60000.0,
                  "sl": 59700.0, "tp": 60600.0, "trace_id": "fb"},
                 state_file)
    client.get_position.return_value = {"contracts": 0.0}
    client.fetch_realized.return_value = {
        "realized_pnl": None, "fee": None, "funding": None,
        "exit_price": None, "exit_ts": None,
    }
    res = trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(),
                                 state_file=state_file)
    assert res["ordered"] is False
    lines = [json.loads(line) for line in Path(jfile).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    closes = [r for r in lines if r.get("event") == "close"
              and r.get("trace_id") == "fb"]
    assert len(closes) == 1
    assert closes[0]["estimated"] is True
    assert closes[0]["exit_price"] in (59700.0, 60600.0)
    assert closes[0]["reason"] in ("stop-loss", "take-profit")
