"""Setup 4 — STEROID_3C: three-candle reversal sederhana (TradingView desc).

Bullish: low C1 < low C0 DAN low C1 < low C2 (new low tengah), DAN
         close C2 > high C1. Entry = high C1, SL = low C1, TP = 1.5R.
Bearish: mirror. Konteks: searah trend EMA + funding tidak crowded
(sama seperti MR). Tanpa syarat wick/doji/volume — pola lebih sering fire.

Backtest H1 real: IS 40 trade net -225.47 | OOS 12 trade, winrate 0.667,
net +651.29, dd -0.0251 -> lolos gate OOS tapi IS negatif: bukti LEMAH,
wajib forward-paper demo sebelum pertimbangan live.
M5: IS/OOS negatif -> REJECT untuk M5.
"""

from __future__ import annotations

import pandas as pd

STEROID_PARAMS = {
    "timeframe": "1h",
    "tp_mult": 1.5,
    "funding_long_max": 0.0001,
    "funding_short_min": -0.0001,
    "min_bars": 60,
    "min_confidence": 0.5,
}


def evaluate_steroid(df: pd.DataFrame, funding_rate: float | None = None,
                     p: dict = STEROID_PARAMS) -> dict | None:
    """3 bar terakhir (sudah close) -> sinyal / None."""
    if df is None or len(df) < p["min_bars"]:
        return None
    need = ["open_time", "high", "low", "close", "ema20", "ema50"]
    if any(c not in df.columns for c in need):
        return None
    d = df.sort_values("open_time").reset_index(drop=True)
    r0, r1, r2 = d.iloc[-3], d.iloc[-2], d.iloc[-1]
    try:
        ema_f, ema_s = float(r2["ema20"]), float(r2["ema50"])
    except (TypeError, ValueError):
        return None
    if pd.isna(ema_f) or pd.isna(ema_s):
        return None
    funding = 0.0
    if "funding_rate" in d.columns and funding_rate is None:
        try:
            funding = float(r2["funding_rate"])
        except (TypeError, ValueError):
            funding = 0.0
    elif funding_rate is not None:
        funding = float(funding_rate)
    out = None
    if (r1["high"] > r0["high"] and r1["high"] > r2["high"]
            and r2["close"] < r1["low"]):
        if ema_f > ema_s and funding >= p["funding_short_min"]:
            entry, sl = float(r1["low"]), float(r1["high"])
            tp = entry - (sl - entry) * p["tp_mult"]
            out = {"action": "SHORT", "setup": "STEROID_3C", "entry": entry,
                   "sl": sl, "tp": tp, "confidence": 0.6,
                   "thesis": (f"Steroid bearish: C1 new-high {sl:.1f}, C2 close "
                              f"{float(r2['close']):.1f} < low C1 {entry:.1f} (downtrend)."),
                   "thesis_breaker": f"Close H1 di atas SL {sl:.1f}."}
    if out is None and (r1["low"] < r0["low"] and r1["low"] < r2["low"]
                        and r2["close"] > r1["high"]):
        if ema_f < ema_s and funding <= p["funding_long_max"]:
            entry, sl = float(r1["high"]), float(r1["low"])
            tp = entry + (entry - sl) * p["tp_mult"]
            out = {"action": "LONG", "setup": "STEROID_3C", "entry": entry,
                   "sl": sl, "tp": tp, "confidence": 0.6,
                   "thesis": (f"Steroid bullish: C1 new-low {sl:.1f}, C2 close "
                              f"{float(r2['close']):.1f} > high C1 {entry:.1f} (uptrend)."),
                   "thesis_breaker": f"Close H1 di bawah SL {sl:.1f}."}
    if out is None:
        return None
    out["funding_rate"] = funding
    out["reason"] = "ok"
    return out
