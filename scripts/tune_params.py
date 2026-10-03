"""Grid-search TERBATAS (±12 kombinasi) parameter strategy di data real tune_1h_1000.

Kontrak terkunci (tidak diubah di sini): risk 1%, halt -5%, leverage 10,
fee 0.05%/sisi + slippage 5bps (milik backtest.py), sizing/order_guard/
killswitch/loop. Script ini HANYA memvariasikan nilai param setups.py
sementara (in-place, restore tiap iterasi) lalu backtest IS/OOS.

Pakai:
    python scripts/tune_params.py
    python scripts/tune_params.py --data data/bronze/tune_1h_1000.jsonl

Kriteria pemenang (OOS): net_pnl > 0 DAN total_trades >= 10 DAN max_dd > -0.08.
Pemenang = net_pnl OOS tertinggi di antara yang lolos (tie-break: sharpe OOS).
Bila tidak ada yang lolos -> exit code 1 + pesan NEGATIF apa adanya (no overfit).
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.data.pipeline import build_features, load_bronze_klines, to_silver
from src.strategy import setups
from src.strategy.backtest import run_backtest

BASE_BREAKOUT = copy.deepcopy(setups.BREAKOUT_PARAMS)
BASE_MR = copy.deepcopy(setups.MR_PARAMS)

# 12 kombinasi: baseline (#0) + 11 variasi satu-faktor & 2 combo gabungan.
# atr_mult_sl/tp_mult di bawah = param BREAKOUT (MR sl/tp dikunci 1.0/1.5).
COMBOS: list[dict] = [
    {"name": "baseline", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "bo_n14", "breakout_n": 14, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "bo_n28", "breakout_n": 28, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "sl_tight12", "breakout_n": 20, "atr_mult_sl": 1.2, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "sl_wide20", "breakout_n": 20, "atr_mult_sl": 2.0, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "tp_15", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 1.5,
     "z": 2.0, "min_atr_pct": 0.0008},
    {"name": "mr_z15", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 1.5, "min_atr_pct": 0.0008},
    {"name": "mr_z25", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.5, "min_atr_pct": 0.0008},
    {"name": "atr_loose", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0005},
    {"name": "atr_strict", "breakout_n": 20, "atr_mult_sl": 1.5, "tp_mult": 2.0,
     "z": 2.0, "min_atr_pct": 0.0012},
    {"name": "aggr", "breakout_n": 14, "atr_mult_sl": 1.2, "tp_mult": 1.5,
     "z": 1.5, "min_atr_pct": 0.0005},
    {"name": "consv", "breakout_n": 28, "atr_mult_sl": 2.0, "tp_mult": 2.0,
     "z": 2.5, "min_atr_pct": 0.0012},
]


def apply_combo(c: dict) -> None:
    """Patch in-place (default-arg di setups mengikat objek dict yang sama)."""
    setups.BREAKOUT_PARAMS.update({
        "breakout_n": c["breakout_n"],
        "atr_mult_sl": c["atr_mult_sl"],
        "tp_mult": c["tp_mult"],
        "min_atr_pct": c["min_atr_pct"],
    })
    setups.MR_PARAMS.update({"zscore_threshold": c["z"]})


def restore() -> None:
    setups.BREAKOUT_PARAMS.clear()
    setups.BREAKOUT_PARAMS.update(copy.deepcopy(BASE_BREAKOUT))
    setups.MR_PARAMS.clear()
    setups.MR_PARAMS.update(copy.deepcopy(BASE_MR))


def eval_combo(c: dict, df_is: pd.DataFrame, df_oos: pd.DataFrame,
               equity: float, leverage: int, max_holding: int) -> dict:
    apply_combo(c)
    try:
        _, m_is, _ = run_backtest(df_is, equity, leverage, max_holding)
        _, m_oos, _ = run_backtest(df_oos, equity, leverage, max_holding)
    finally:
        restore()
    return {"combo": c, "is": m_is, "oos": m_oos}


def passes_oos(m_oos: dict) -> bool:
    return (m_oos["net_pnl"] > 0
            and m_oos["total_trades"] >= 10
            and m_oos["max_drawdown"] > -0.08)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tuning terbatas 12 kombinasi (IS/OOS 70/30).")
    ap.add_argument("--data", default="data/bronze/tune_1h_1000.jsonl")
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--max-holding", type=int, default=24)
    a = ap.parse_args(argv)

    raw = load_bronze_klines(a.data)
    if len(raw) == 0:
        print(f"ERROR: tidak ada bar valid dari {a.data}")
        return 2
    gold = build_features(to_silver(raw, "1h"))
    n = len(gold)
    cut = int(n * 0.7)
    df_is = gold.iloc[:cut].reset_index(drop=True)
    df_oos = gold.iloc[cut:].reset_index(drop=True)
    print(f"bars total={n} IS={len(df_is)} OOS={len(df_oos)} (split 70/30 kronologis)")

    rows: list[dict] = []
    for c in COMBOS:
        r = eval_combo(c, df_is, df_oos, a.equity, a.leverage, a.max_holding)
        rows.append(r)
        mi, mo = r["is"], r["oos"]
        flag = "PASS" if passes_oos(mo) else "fail"
        print(f"[{flag}] {c['name']:10s} "
              f"IS trades={mi['total_trades']:3d} net={mi['net_pnl']:9.2f} dd={mi['max_drawdown']:.4f} | "
              f"OOS trades={mo['total_trades']:3d} net={mo['net_pnl']:9.2f} "
              f"wr={mo['winrate']:.3f} sh={mo['sharpe']:.3f} dd={mo['max_drawdown']:.4f}")

    winners = [r for r in rows if passes_oos(r["oos"])]
    if not winners:
        print("HASIL NEGATIF: tidak ada kombinasi yang lolos kriteria OOS "
              "(net_pnl>0 & >=10 trade & max_dd>-0.08). setups.py TIDAK diubah.")
        return 1
    winners.sort(key=lambda r: (r["oos"]["net_pnl"], r["oos"]["sharpe"]), reverse=True)
    w = winners[0]
    print(f"PEMENANG: {w['combo']['name']} {w['combo']} OOS={w['oos']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
