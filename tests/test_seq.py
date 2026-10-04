"""Unit test pola SEQ_REVERSAL_H1 pada candle sintetis eksplisit."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategy.seq import evaluate_seq  # noqa: E402


def _df(bars, ema_f=61000.0, ema_s=60000.0, atr=300.0, vol=100.0):
    """bars: list (o,h,l,c). Pad 70 bar flat di depan agar min_bars lolos."""
    pad = [(60000.0, 60100.0, 59900.0, 60050.0)] * 70
    all_b = pad + bars
    n = len(all_b)
    vols = [vol * 0.8] * (n - 3) + [vol * 1.6] * 3  # C2 >> median
    return pd.DataFrame({
        "open_time": [1_700_000_000_000 + i * 3600_000 for i in range(n)],
        "open": [b[0] for b in all_b], "high": [b[1] for b in all_b],
        "low": [b[2] for b in all_b], "close": [b[3] for b in all_b],
        "volume": vols, "ema20": [ema_f] * n, "ema50": [ema_s] * n,
        "atr14": [atr] * n, "rsi14": [55.0] * n,
    })


def test_seq_short_fires():
    # C0 shooting star: o=65000 h=65600 l=64800 c=64900 (upper 700/800, body 100, pos 0.125)
    # C1 doji inside: o=64950 h=65000 l=64850 c=64960
    # C2 bearish engulf break: o=64980 h=65010 l=64600 c=64650
    df = _df([(65000.0, 65600.0, 64800.0, 64900.0),
              (64950.0, 65000.0, 64850.0, 64960.0),
              (64980.0, 65010.0, 64600.0, 64650.0)])
    sig = evaluate_seq(df, funding_rate=0.0)
    assert sig is not None and sig["action"] == "SHORT", sig
    assert sig["setup"] == "SEQ_REVERSAL_H1"
    assert sig["sl"] > sig["entry"] > sig["tp"]


def test_seq_long_fires():
    # C0 hammer: o=65100 h=65200 l=64400 c=65150 (lower 700/800, pos 0.9375)
    # C1 inside: o=65100 h=65180 l=65000 c=65120
    # C2 bullish engulf break: o=65090 h=65400 l=65080 c=65350
    df = _df([(65100.0, 65200.0, 64400.0, 65150.0),
              (65100.0, 65180.0, 65000.0, 65120.0),
              (65090.0, 65400.0, 65080.0, 65350.0)],
             ema_f=59000.0, ema_s=60000.0)
    sig = evaluate_seq(df, funding_rate=0.0)
    assert sig is not None and sig["action"] == "LONG", sig
    assert sig["sl"] < sig["entry"] < sig["tp"]


def test_seq_none_without_pattern():
    df = _df([(65000.0, 65100.0, 64900.0, 65050.0),
              (65050.0, 65150.0, 64950.0, 65100.0),
              (65100.0, 65200.0, 65000.0, 65150.0)])
    assert evaluate_seq(df, funding_rate=0.0) is None


def test_seq_blocked_by_low_volume():
    df = _df([(65000.0, 65600.0, 64800.0, 64900.0),
              (64950.0, 65000.0, 64850.0, 64960.0),
              (64980.0, 65010.0, 64600.0, 64650.0)])
    df.loc[df.index[-3]:, "volume"] = 1.0  # pola ok tapi volume mati
    assert evaluate_seq(df, funding_rate=0.0) is None
