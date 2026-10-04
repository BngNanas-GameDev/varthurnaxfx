"""Backtest setup SEQ_REVERSAL_H1 di data real, split 70/30 kronologis.

Lolos bila OOS: net_pnl>0 DAN trades>=10 DAN max_drawdown>-0.08.
Gagal -> laporkan apa adanya, JANGAN wire ke live.

Pakai: python scripts/seq_backtest.py [--bronze data/bronze/tune_1h_1000.jsonl]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.pipeline import build_features, load_bronze_klines, to_silver  # noqa: E402
from src.strategy.backtest import run_backtest  # noqa: E402
from src.strategy.seq import evaluate_seq  # noqa: E402


def _seq_or_flat(df):
    sig = evaluate_seq(df)
    if sig is None:
        return {"action": "NO_TRADE", "setup": None}
    return sig


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bronze", default="data/bronze/tune_1h_1000.jsonl")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--leverage", type=int, default=10)
    a = ap.parse_args(argv)
    raw = load_bronze_klines(PROJECT_ROOT / a.bronze)
    gold = build_features(to_silver(raw, a.interval))
    cut = int(len(gold) * 0.7)
    ok = True
    for name, sub in (("IS", gold.iloc[:cut]), ("OOS", gold.iloc[cut:])):
        trades, m, _ = run_backtest(sub, a.equity, a.leverage, signal_fn=_seq_or_flat)
        print(f"{name}: bars={len(sub)} trades={m['total_trades']} "
              f"winrate={m['winrate']} sharpe={m['sharpe']} "
              f"max_dd={m['max_drawdown']} net={m['net_pnl']}")
        for t in trades[:5]:
            print("   ", {k: t[k] for k in ("side", "setup", "entry", "exit", "outcome", "pnl")})
        if name == "OOS":
            ok = (m["net_pnl"] > 0 and m["total_trades"] >= 10
                  and m["max_drawdown"] > -0.08)
    print("SEQ_ACCEPT" if ok else "SEQ_REJECT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
