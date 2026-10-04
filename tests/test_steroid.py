"""Unit test pola STEROID_3C (new-low/high tengah + close lampaui)."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategy.steroid import evaluate_steroid  # noqa: E402


def _df(bars, ema_f=61000.0, ema_s=60000.0):
    pad = [(60000.0, 60100.0, 59900.0, 60050.0)] * 70
    all_b = pad + bars
    n = len(all_b)
    return pd.DataFrame({
        "open_time": [1_700_000_000_000 + i * 3600_000 for i in range(n)],
        "open": [b[0] for b in all_b], "high": [b[1] for b in all_b],
        "low": [b[2] for b in all_b], "close": [b[3] for b in all_b],
        "volume": [100.0] * n, "ema20": [ema_f] * n, "ema50": [ema_s] * n,
        "atr14": [300.0] * n, "rsi14": [55.0] * n,
    })


def test_steroid_short_fires():
    df = _df([(65000.0, 65200.0, 64800.0, 65100.0),
              (65100.0, 65500.0, 64700.0, 64800.0),
              (64800.0, 64900.0, 64600.0, 64650.0)])
    sig = evaluate_steroid(df, funding_rate=0.0)
    assert sig is not None and sig["action"] == "SHORT", sig
    assert sig["entry"] == 64700.0 and sig["sl"] == 65500.0
    assert sig["tp"] < sig["entry"]


def test_steroid_long_fires():
    df = _df([(65100.0, 65200.0, 64900.0, 64950.0),
              (64950.0, 65000.0, 64500.0, 64900.0),
              (64900.0, 65200.0, 64850.0, 65150.0)],
             ema_f=59000.0, ema_s=60000.0)
    sig = evaluate_steroid(df, funding_rate=0.0)
    assert sig is not None and sig["action"] == "LONG", sig
    assert sig["entry"] == 65000.0 and sig["sl"] == 64500.0


def test_steroid_none_without_close_through():
    # C1 new-low tapi C2 tak close di atas high C1
    df = _df([(65100.0, 65200.0, 64900.0, 64950.0),
              (64950.0, 65000.0, 64500.0, 64900.0),
              (64900.0, 64950.0, 64850.0, 64920.0)],
             ema_f=59000.0, ema_s=60000.0)
    assert evaluate_steroid(df, funding_rate=0.0) is None


def test_steroid_blocked_wrong_trend():
    # pola bullish tapi ema uptrend -> diblokir konteks
    df = _df([(65100.0, 65200.0, 64900.0, 64950.0),
              (64950.0, 65000.0, 64500.0, 64900.0),
              (64900.0, 65200.0, 64850.0, 65150.0)],
             ema_f=61000.0, ema_s=60000.0)
    assert evaluate_steroid(df, funding_rate=0.0) is None
