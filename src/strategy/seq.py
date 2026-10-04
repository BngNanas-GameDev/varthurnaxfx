"""Setup 3 — SEQ_REVERSAL_H1: sequence exhaustion -> indecision -> confirmation.

Kuantifikasi artikel 3-candle safety check (reversal only, H1):
  SHORT (top): C0 shooting-star (upper_wick>=0.6R, body<=0.35R, close di 40%
    bawah range) + C1 doji (|O-C|<=0.12R) atau inside-bar + C2 bearish engulf
    yang menutup di bawah min(low C0,C1), body>=0.5R, volume>=1.3x median-20.
    Konteks: ema20>ema50 (reversal dari uptrend) + funding tidak crowded short.
  LONG: mirror (hammer + doji/inside + bullish engulf breakthrough + ema20<ema50).

SL = ekstrem sequence +/- 0.3*ATR | TP = 1.5R. Semua threshold di SEQ_PARAMS.
Return None bila tak ada sequence (bukan NO_TRADE dict — wrapper yang menilai).
"""

from __future__ import annotations

import pandas as pd

SEQ_PARAMS = {
    "timeframe": "1h",
    "wick_min": 0.6,        # wick penolakan >= 60% range C0
    "body_max_c0": 0.35,    # body C0 <= 35% range
    "close_edge": 0.4,      # close di 40% tepi range (bawah utk SHORT)
    "doji_max": 0.12,       # |O-C| <= 12% range -> doji
    "body_min_c2": 0.5,     # body konfirmasi >= 50% range
    "vol_mult": 1.0,        # volume C2 >= median-20 (di atas rata-rata)
    "vol_lookback": 20,
    "sl_buf_atr": 0.3,
    "tp_mult": 1.5,         # RR 1:1.5
    "funding_long_max": 0.0001,
    "funding_short_min": -0.0001,
    "min_bars": 60,
    "min_confidence": 0.5,
}


def _m(r) -> dict | None:
    try:
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
    except (TypeError, ValueError):
        return None
    rng = h - l
    if not rng > 0 or pd.isna(rng):
        return None
    body = abs(c - o)
    return {"o": o, "h": h, "l": l, "c": c, "rng": rng, "body": body,
            "upper": h - max(o, c), "lower": min(o, c) - l,
            "pos": (c - l) / rng}


def _seq_short(c0: dict, c1: dict, c2: dict, vol_ok: bool,
               ctx_ok: bool, atr: float, p: dict) -> dict | None:
    if not (c0["upper"] >= p["wick_min"] * c0["rng"] and
            c0["body"] <= p["body_max_c0"] * c0["rng"] and
            c0["pos"] <= p["close_edge"]):
        return None  # bukan shooting star
    doji = c1["body"] <= p["doji_max"] * c1["rng"]
    inside = c1["h"] <= c0["h"] and c1["l"] >= c0["l"]
    if not (doji or inside):
        return None
    if not (c2["c"] < c2["o"] and c2["body"] >= p["body_min_c2"] * c2["rng"]):
        return None
    if not (min(c2["o"], c2["c"]) <= min(c1["o"], c1["c"]) and
            max(c2["o"], c2["c"]) >= max(c1["o"], c1["c"])):
        return None  # body tak meng-engulf C1
    if not c2["c"] < min(c0["l"], c1["l"]):
        return None  # tak breakthrough
    if not (vol_ok and ctx_ok):
        return None
    entry = c2["c"]
    sl = max(c0["h"], c1["h"], c2["h"]) + p["sl_buf_atr"] * atr
    tp = entry - (sl - entry) * p["tp_mult"]
    wick_s = min(c0["upper"] / c0["rng"], 1.0)
    depth = min((min(c0["l"], c1["l"]) - c2["c"]) / atr, 2.0) / 2.0
    conf = round(min(0.55 + 0.1 * wick_s + 0.1 * depth, 0.85), 3)
    if conf < p["min_confidence"]:
        return None
    return {"action": "SHORT", "setup": "SEQ_REVERSAL_H1", "entry": entry,
            "sl": sl, "tp": tp, "confidence": conf,
            "thesis": (f"SEQ short: shooting-star (wick {c0['upper']:.1f}) + "
                       f"{'doji' if doji else 'inside-bar'} + bearish engulf break "
                       f"{c2['c']:.1f} < {min(c0['l'], c1['l']):.1f}, vol ok."),
            "thesis_breaker": f"Close H1 kembali di atas SL {sl:.1f} (reversal gagal)."}


def _seq_long(c0: dict, c1: dict, c2: dict, vol_ok: bool,
              ctx_ok: bool, atr: float, p: dict) -> dict | None:
    if not (c0["lower"] >= p["wick_min"] * c0["rng"] and
            c0["body"] <= p["body_max_c0"] * c0["rng"] and
            c0["pos"] >= 1.0 - p["close_edge"]):
        return None  # bukan hammer
    doji = c1["body"] <= p["doji_max"] * c1["rng"]
    inside = c1["h"] <= c0["h"] and c1["l"] >= c0["l"]
    if not (doji or inside):
        return None
    if not (c2["c"] > c2["o"] and c2["body"] >= p["body_min_c2"] * c2["rng"]):
        return None
    if not (min(c2["o"], c2["c"]) <= min(c1["o"], c1["c"]) and
            max(c2["o"], c2["c"]) >= max(c1["o"], c1["c"])):
        return None
    if not c2["c"] > max(c0["h"], c1["h"]):
        return None
    if not (vol_ok and ctx_ok):
        return None
    entry = c2["c"]
    sl = min(c0["l"], c1["l"], c2["l"]) - p["sl_buf_atr"] * atr
    tp = entry + (entry - sl) * p["tp_mult"]
    wick_s = min(c0["lower"] / c0["rng"], 1.0)
    depth = min((c2["c"] - max(c0["h"], c1["h"])) / atr, 2.0) / 2.0
    conf = round(min(0.55 + 0.1 * wick_s + 0.1 * depth, 0.85), 3)
    if conf < p["min_confidence"]:
        return None
    return {"action": "LONG", "setup": "SEQ_REVERSAL_H1", "entry": entry,
            "sl": sl, "tp": tp, "confidence": conf,
            "thesis": (f"SEQ long: hammer (wick {c0['lower']:.1f}) + "
                       f"{'doji' if doji else 'inside-bar'} + bullish engulf break "
                       f"{c2['c']:.1f} > {max(c0['h'], c1['h']):.1f}, vol ok."),
            "thesis_breaker": f"Close H1 kembali di bawah SL {sl:.1f} (reversal gagal)."}


def evaluate_seq(df: pd.DataFrame, funding_rate: float | None = None,
                 p: dict = SEQ_PARAMS) -> dict | None:
    """Sequence 3 bar terakhir (diasumsikan sudah close) -> sinyal / None."""
    if df is None or len(df) < max(p["min_bars"], p["vol_lookback"] + 5):
        return None
    need = ["open_time", "open", "high", "low", "close", "volume", "ema20", "ema50", "atr14"]
    if any(c not in df.columns for c in need):
        return None
    d = df.sort_values("open_time").reset_index(drop=True)
    last = d.iloc[-1]
    try:
        atr = float(last["atr14"])
        ema_f, ema_s = float(last["ema20"]), float(last["ema50"])
    except (TypeError, ValueError):
        return None
    if pd.isna(atr) or atr <= 0 or pd.isna(ema_f) or pd.isna(ema_s):
        return None
    bars = [_m(d.iloc[-3]), _m(d.iloc[-2]), _m(d.iloc[-1])]
    if any(b is None for b in bars):
        return None
    c0, c1, c2 = bars
    try:
        vols = d["volume"].iloc[-p["vol_lookback"]:].astype(float)
        vmed = float(vols.median())
        v2 = float(d.iloc[-1]["volume"])
    except (TypeError, ValueError):
        return None
    if pd.isna(vmed) or vmed <= 0 or pd.isna(v2):
        return None
    vol_ok = v2 >= p["vol_mult"] * vmed
    funding = 0.0
    if "funding_rate" in d.columns and funding_rate is None:
        try:
            funding = float(last["funding_rate"])
        except (TypeError, ValueError):
            funding = 0.0
    elif funding_rate is not None:
        funding = float(funding_rate)
    sig = None
    if ema_f > ema_s and funding >= p["funding_short_min"]:
        sig = _seq_short(c0, c1, c2, vol_ok, True, atr, p)
    if sig is None and ema_f < ema_s and funding <= p["funding_long_max"]:
        sig = _seq_long(c0, c1, c2, vol_ok, True, atr, p)
    if sig is None:
        return None
    sig["funding_rate"] = funding
    sig["atr"] = atr
    sig["reason"] = "ok"
    return sig
