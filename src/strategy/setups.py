"""Dua setup strategi BTCUSDT-PERP (H1 utama). Aturan eksplisit, NO-TRADE bila ragu.

Setup 1 — TREND_BREAKOUT_H1 (breakout + filter EMA & ATR):
  LONG  jika close[-1] > max(high[-N-1:-1]) DAN ema20 > ema50 DAN atr/close >= MIN_ATR_PCT
  SHORT jika close[-1] < min(low[-N-1:-1])  DAN ema20 < ema50 DAN atr/close >= MIN_ATR_PCT
  SL = entry ∓ atr*ATR_MULT_SL | TP = entry ± |entry-SL|*TP_MULT (RR 1:2)

Setup 2 — MEAN_REVERSION (Bollinger + RSI z-score + filter funding):
  LONG  jika close < lower_band DAN (rsi < OVERSOLD ATAU z <= -Z) DAN funding <= FUND_LONG_MAX
  SHORT jika close > upper_band DAN (rsi > OVERBOUGHT ATAU z >= +Z) DAN funding >= FUND_SHORT_MIN
  SL = entry ∓ atr*ATR_MULT_SL_MR | TP = entry ± |entry-SL|*TP_MULT_MR (RR 1:1.5)

Prioritas/konflik: bila kedua setup valid tapi beda arah -> NO_TRADE.
Bila fitur NaN / data < MIN_BARS / ATR terlalu kecil / funding memblokir -> NO-TRADE.
Setiap sinyal membawa `thesis` + `thesis_breaker` (falsifikasi).
"""

from __future__ import annotations

import os

import pandas as pd

from src.strategy.seq import SEQ_PARAMS, evaluate_seq
from src.strategy.steroid import evaluate_steroid

# ------------------------------------------------------------- parameter eksplisit
BREAKOUT_PARAMS = {
    "timeframe": "1h",
    "breakout_n": 20,       # Donchian N
    "ema_fast": 20,
    "ema_slow": 50,
    "atr_period": 14,
    "min_atr_pct": 0.0008,  # 0.08%: tolak pasar mati
    "atr_mult_sl": 1.5,
    "tp_mult": 2.0,         # RR 1:2
    "min_bars": 60,
}

MR_PARAMS = {
    "timeframe": "1h",
    "bb_period": 20,
    "bb_std": 2.0,
    "rsi_period": 14,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "zscore_threshold": 2.0,
    "atr_mult_sl": 1.0,
    "tp_mult": 1.5,         # RR 1:1.5
    "funding_long_max": 0.0001,   # funding > ini -> LONG diblokir (crowded long)
    "funding_short_min": -0.0001,  # funding < ini -> SHORT diblokir (crowded short)
    "min_bars": 60,
}

MIN_CONFIDENCE = 0.5
NO_TRADE = "NO_TRADE"


def _no_trade(reason: str) -> dict:
    return {"action": NO_TRADE, "setup": None, "entry": None, "sl": None,
            "tp": None, "confidence": 0.0, "thesis": f"NO-TRADE: {reason}",
            "thesis_breaker": "N/A - tidak ada posisi.", "reason": reason}


def _breakout_signal(d: pd.DataFrame, last: pd.Series, p: dict = BREAKOUT_PARAMS) -> dict | None:
    n = p["breakout_n"]
    if len(d) < n + 2:
        return None
    window = d.iloc[-(n + 1):-1]
    hh, ll = float(window["high"].max()), float(window["low"].min())
    close, ema_f = float(last["close"]), float(last["ema20"])
    ema_s, atr = float(last["ema50"]), float(last["atr14"])
    if not all(pd.notna([close, ema_f, ema_s, atr])) or close <= 0:
        return None
    if atr / close < p["min_atr_pct"]:
        return None  # pasar mati
    atr = max(atr, 1e-9)
    if close > hh and ema_f > ema_s:  # LONG breakout
        entry = close
        sl = entry - atr * p["atr_mult_sl"]
        tp = entry + (entry - sl) * p["tp_mult"]
        strength = min((close - hh) / atr, 2.0) / 2.0  # 0..1 seberapa kuat break
        conf = round(0.55 + 0.25 * strength, 3)
        return {"action": "LONG", "setup": "TREND_BREAKOUT_H1", "entry": entry,
                "sl": sl, "tp": tp, "confidence": conf,
                "thesis": (f"Breakout H1: close {close:.1f} > Donchian-HH({n}) {hh:.1f}, "
                           f"EMA{p['ema_fast']} {ema_f:.1f} > EMA{p['ema_slow']} {ema_s:.1f} "
                           f"(uptrend), ATR {atr:.1f} lolos filter volatilitas."),
                "thesis_breaker": f"Close H1 kembali di bawah {hh:.1f} (failed breakout) "
                                  f"atau EMA{ p['ema_fast']} memotong ke bawah EMA{p['ema_slow']}."}
    if close < ll and ema_f < ema_s:  # SHORT breakdown
        entry = close
        sl = entry + atr * p["atr_mult_sl"]
        tp = entry - (sl - entry) * p["tp_mult"]
        strength = min((ll - close) / atr, 2.0) / 2.0
        conf = round(0.55 + 0.25 * strength, 3)
        return {"action": "SHORT", "setup": "TREND_BREAKOUT_H1", "entry": entry,
                "sl": sl, "tp": tp, "confidence": conf,
                "thesis": (f"Breakdown H1: close {close:.1f} < Donchian-LL({n}) {ll:.1f}, "
                           f"EMA{p['ema_fast']} {ema_f:.1f} < EMA{p['ema_slow']} {ema_s:.1f} "
                           f"(downtrend), ATR {atr:.1f} lolos filter."),
                "thesis_breaker": f"Close H1 kembali di atas {ll:.1f} (failed breakdown) "
                                  f"atau EMA{p['ema_fast']} memotong ke atas EMA{p['ema_slow']}."}
    return None


def _mr_signal(d: pd.DataFrame, last: pd.Series, funding: float, p: dict = MR_PARAMS) -> dict | None:
    n = p["bb_period"]
    if len(d) < n + 2:
        return None
    closes = d["close"].iloc[-n:]
    sma, std = float(closes.mean()), float(closes.std(ddof=0))
    if std <= 0 or pd.isna(sma):
        return None
    upper, lower = sma + p["bb_std"] * std, sma - p["bb_std"] * std
    close, rsi, atr = float(last["close"]), float(last["rsi14"]), float(last["atr14"])
    if not all(pd.notna([close, rsi, atr])):
        return None
    atr = max(atr, 1e-9)
    z = (close - sma) / std
    # --- LONG: oversold ekstrem
    if close < lower and (rsi < p["rsi_oversold"] or z <= -p["zscore_threshold"]):
        if funding > p["funding_long_max"]:
            return None  # funding filter: long crowded, diblokir
        entry = close
        sl = entry - atr * p["atr_mult_sl"]
        tp = entry + (entry - sl) * p["tp_mult"]
        conf = round(0.5 + min(abs(z) - p["zscore_threshold"] + 1.0, 2.0) * 0.1, 3)
        return {"action": "LONG", "setup": "MEAN_REVERSION", "entry": entry,
                "sl": sl, "tp": tp, "confidence": min(conf, 0.85),
                "thesis": (f"Mean-reversion: close {close:.1f} < BB-lower {lower:.1f} "
                           f"(z={z:.2f}), RSI {rsi:.1f} oversold, funding {funding:.6f} "
                           f"<= {p['funding_long_max']} (tidak crowded long). Target balik ke SMA {sma:.1f}."),
                "thesis_breaker": f"Close H1 di bawah SL {sl:.1f} (trend, bukan noise) "
                                  f"atau funding melonjak > {p['funding_long_max']}."}
    # --- SHORT: overbought ekstrem
    if close > upper and (rsi > p["rsi_overbought"] or z >= p["zscore_threshold"]):
        if funding < p["funding_short_min"]:
            return None  # funding filter: short crowded, diblokir
        entry = close
        sl = entry + atr * p["atr_mult_sl"]
        tp = entry - (sl - entry) * p["tp_mult"]
        conf = round(0.5 + min(abs(z) - p["zscore_threshold"] + 1.0, 2.0) * 0.1, 3)
        return {"action": "SHORT", "setup": "MEAN_REVERSION", "entry": entry,
                "sl": sl, "tp": tp, "confidence": min(conf, 0.85),
                "thesis": (f"Mean-reversion: close {close:.1f} > BB-upper {upper:.1f} "
                           f"(z={z:.2f}), RSI {rsi:.1f} overbought, funding {funding:.6f} "
                           f">= {p['funding_short_min']}. Target balik ke SMA {sma:.1f}."),
                "thesis_breaker": f"Close H1 di atas SL {sl:.1f} (breakout trend) "
                                  f"atau funding anjlok < {p['funding_short_min']}."}
    return None


def evaluate(df: pd.DataFrame, funding_rate: float | None = None) -> dict:
    """Evaluasi bar terakhir (yang diasumsikan sudah close) -> Signal dict.

    Return: {action, setup, entry, sl, tp, confidence, thesis, thesis_breaker, ...}
    action ∈ {LONG, SHORT, NO_TRADE}. NO-TRADE bila konflik/ragu/missing.
    """
    if df is None or len(df) < max(BREAKOUT_PARAMS["min_bars"], MR_PARAMS["min_bars"]):
        return _no_trade(f"data kurang (<{max(BREAKOUT_PARAMS['min_bars'], MR_PARAMS['min_bars'])} bar closed)")
    need = ["open_time", "open", "high", "low", "close", "ema20", "ema50", "atr14", "rsi14"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        return _no_trade(f"kolom Gold hilang: {missing}")
    d = df.sort_values("open_time").reset_index(drop=True)
    last = d.iloc[-1]
    if last[["close", "ema20", "ema50", "atr14", "rsi14"]].isna().any():
        return _no_trade("indikator terakhir NaN (warmup belum cukup)")
    if "funding_rate" in d.columns and funding_rate is None:
        try:
            funding_rate = float(last["funding_rate"])
        except (ValueError, TypeError):
            funding_rate = 0.0
    funding = float(funding_rate) if funding_rate is not None else 0.0

    sig_break = _breakout_signal(d, last)
    sig_mr = _mr_signal(d, last, funding)
    sig_seq = None
    if os.getenv("SEQ_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            sig_seq = evaluate_seq(d, funding)
        except Exception:  # noqa: BLE001 - SEQ tak boleh mematikan evaluate
            sig_seq = None
    sig_steroid = None
    if os.getenv("STEROID_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            sig_steroid = evaluate_steroid(d, funding)
        except Exception:  # noqa: BLE001
            sig_steroid = None

    cands = [s for s in (sig_break, sig_mr, sig_seq, sig_steroid) if s]
    if not cands:
        return _no_trade("tidak ada setup valid (filter EMA/ATR/BB/RSI/funding tidak terpenuhi)")
    acts = {s["action"] for s in cands}
    if len(acts) > 1:
        out = _no_trade("konflik: " + " vs ".join(f"{s['setup']}={s['action']}" for s in cands))
        out["candidates"] = cands  # arbiter Fin boleh memilih satu sisi
        return out
    # arah sama -> pilih confidence tertinggi (breakout diutamakan bila seri)
    best = max(cands, key=lambda s: (s["confidence"], s["setup"] == "TREND_BREAKOUT_H1"))
    if best["confidence"] < MIN_CONFIDENCE:
        return _no_trade(f"confidence {best['confidence']} < {MIN_CONFIDENCE}")
    if not (best["sl"] and best["tp"] and best["entry"] != best["sl"]):
        return _no_trade("SL/TP invalid")
    best = dict(best)
    best["funding_rate"] = funding
    best["atr"] = float(last["atr14"])
    best["reason"] = "ok"
    return best
