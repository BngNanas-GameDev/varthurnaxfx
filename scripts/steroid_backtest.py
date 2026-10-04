"""Backtest pola 'Steroid 3-Candle Reversal' (TradingView desc, terkuantifikasi):

Bullish: low C1 < low C0 DAN low C1 < low C2 (new low tengah), DAN
         close C2 > high C1. Entry = high C2 (opsi konservatif), SL = low C1,
         TP = entry + 1.5*(entry-SL). Mirror untuk bearish.
Filter konteks SAMA seperti live (ema trend + funding) agar apel-ke-apel.

Lolos bila OOS: net>0 DAN trades>=10 DAN dd>-0.08. Gagal -> lapor saja.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.pipeline import build_features, load_bronze_klines, to_silver  # noqa: E402
from src.strategy.backtest import run_backtest  # noqa: E402

TP_MULT = 1.5


def _sig(df, i: int):
    r0, r1, r2 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    f = float(r2.get("funding_rate", 0.0) or 0.0)
    ema_f, ema_s = float(r2["ema20"]), float(r2["ema50"])
    # Bearish: C1 new high + C2 close di bawah low C1, dari uptrend
    if r1["high"] > r0["high"] and r1["high"] > r2["high"] and r2["close"] < r1["low"]:
        if ema_f > ema_s and f >= -0.0001:
            entry = float(r1["low"])
            sl = float(r1["high"])
            tp = entry - (sl - entry) * TP_MULT
            return {"action": "SHORT", "setup": "STEROID_3C", "entry": entry,
                    "sl": sl, "tp": tp, "confidence": 0.6,
                    "thesis": "steroid bearish", "thesis_breaker": f"close>{sl:.1f}",
                    "reason": "ok"}
    # Bullish: C1 new low + C2 close di atas high C1, dari downtrend
    if r1["low"] < r0["low"] and r1["low"] < r2["low"] and r2["close"] > r1["high"]:
        if ema_f < ema_s and f <= 0.0001:
            entry = float(r1["high"])
            sl = float(r1["low"])
            tp = entry + (entry - sl) * TP_MULT
            return {"action": "LONG", "setup": "STEROID_3C", "entry": entry,
                    "sl": sl, "tp": tp, "confidence": 0.6,
                    "thesis": "steroid bullish", "thesis_breaker": f"close<{sl:.1f}",
                    "reason": "ok"}
    return {"action": "NO_TRADE", "setup": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bronze", default="data/bronze/tune_1h_1000.jsonl")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--leverage", type=int, default=10)
    a = ap.parse_args(argv)
    gold = build_features(to_silver(
        load_bronze_klines(PROJECT_ROOT / a.bronze), a.interval))
    cut = int(len(gold) * 0.7)
    ok = True
    for name, sub in (("IS", gold.iloc[:cut]), ("OOS", gold.iloc[cut:])):
        sub = sub.reset_index(drop=True)
        trades, m, _ = run_backtest(
            sub, a.equity, a.leverage,
            signal_fn=lambda d: _sig(d, len(d) - 1))
        print(f"{name}: bars={len(sub)} trades={m['total_trades']} "
              f"winrate={m['winrate']} max_dd={m['max_drawdown']} net={m['net_pnl']}")
        for t in trades[:5]:
            print("   ", {k: t[k] for k in ("side", "entry", "exit", "outcome", "pnl")})
        if name == "OOS":
            ok = (m["net_pnl"] > 0 and m["total_trades"] >= 10
                  and m["max_drawdown"] > -0.08)
    print("STEROID_ACCEPT" if ok else "STEROID_REJECT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
