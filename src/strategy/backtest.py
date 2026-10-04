"""Backtest sederhana: sinyal setups.py + sizing.py, PnL net of fee+slippage.

Cara pakai:
    python src/strategy/backtest.py                      # sintetis default
    python src/strategy/backtest.py --synthetic 500 --seed 7
    python src/strategy/backtest.py --csv data/gold/x.csv --equity 10000 --leverage 10

Asumsi simulasi (konservatif & eksplisit):
- Sinyal dihitung walk-forward: bar i memakai df[:i+1] (tanpa lookahead).
- Entry di close bar sinyal; keluar saat SL/TP tersentuh bar berikutnya
  (cek SL dulu bila keduanya tersentuh dalam 1 bar = worst case),
  atau max_holding_bars tercapai -> exit di close (time-stop).
- Biaya: fee_taker 0.05% dari notional entry DAN exit + slippage 5bps per sisi.
- Output metrics {total_trades, winrate, sharpe, sortino, max_drawdown, profit_factor}.

Dependensi lokal (catat, milik worker lain bila sudah ada):
- src/common/schemas, src/orchestrator -> backtest ini standalone agar bisa run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from src.data.pipeline import build_features  # noqa: E402
except Exception:  # noqa: BLE001 - fallback bila struktur beda
    build_features = None  # type: ignore
try:
    from src.strategy.setups import evaluate  # noqa: E402
except ImportError:  # run sebagai file langsung
    from setups import evaluate  # type: ignore
try:
    from src.strategy.sizing import calc_qty  # noqa: E402
except ImportError:
    from sizing import calc_qty  # type: ignore

FEE_TAKER = 0.0005
SLIPPAGE_BPS = 5
SLIPPAGE_RATE = SLIPPAGE_BPS / 10_000.0
WARMUP = 60


def make_synthetic(n: int = 500, seed: int = 42, start: float = 60000.0) -> pd.DataFrame:
    """OHLCV H1 sintetis: random-walk + regime trend agar kedua setup kadang fire."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.004, n)
    # sisipkan trend naik di tengah agar breakout terpicu
    rets[n // 3: n // 3 + 40] += 0.004
    rets[2 * n // 3: 2 * n // 3 + 30] -= 0.005
    close = start * np.exp(np.cumsum(rets))
    spread = np.abs(rng.normal(0.0015, 0.0008, n))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + spread / 2)
    low = np.minimum(open_, close) * (1 - spread / 2)
    vol = np.abs(rng.normal(50, 15, n)) + 5
    base_ms = 1_700_000_000_000
    df = pd.DataFrame({
        "open_time": base_ms + np.arange(n) * 3_600_000,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": vol, "close_time": base_ms + np.arange(n) * 3_600_000 + 3_599_999,
        "trades": rng.integers(100, 2000, n),
        "is_gap": False,
        "funding_rate": rng.normal(0.00005, 0.00008, n),
    })
    if build_features is not None:
        return build_features(df)
    return df


def _exit_price(side: str, entry: float, ref: float, is_sl: bool) -> float:
    """Harga exit riil + slippage merugikan trader."""
    slip = SLIPPAGE_RATE * ref
    if side == "LONG":
        return (ref - slip) if True else ref
    return ref + slip


def run_backtest(df: pd.DataFrame, equity0: float = 10000.0,
                 leverage: int = 10, max_holding_bars: int = 24,
                 signal_fn=None) -> tuple[list, dict, list]:
    """Loop bar-per-bar. signal_fn(slice_df)->signal dict; default evaluate().
    Return (trades, metrics, equity_curve)."""
    if signal_fn is None:
        signal_fn = evaluate
    if df is None or len(df) < WARMUP + 5:
        return [], compute_metrics([], [equity0]), [equity0]
    d = df.sort_values("open_time").reset_index(drop=True)
    equity = equity0
    equity_curve = [equity0]
    trades: list[dict] = []
    i = WARMUP
    while i < len(d) - 1:
        sig = signal_fn(d.iloc[: i + 1])
        if sig["action"] == "NO_TRADE":
            equity_curve.append(equity)
            i += 1
            continue
        entry, sl, tp = float(sig["entry"]), float(sig["sl"]), float(sig["tp"])
        pos = calc_qty(equity, entry, sl, leverage)
        if pos["qty"] <= 0:
            equity_curve.append(equity)
            i += 1
            continue
        qty, side = pos["qty"], sig["action"]
        entry_px = _exit_price(side, entry, entry, False)
        fee_entry = entry_px * qty * FEE_TAKER
        exit_px, outcome, exit_i = entry_px, "timeout", min(i + max_holding_bars, len(d) - 1)
        for j in range(i + 1, min(i + max_holding_bars + 1, len(d))):
            hi, lo, cl = float(d.loc[j, "high"]), float(d.loc[j, "low"]), float(d.loc[j, "close"])
            if side == "LONG":
                hit_sl, hit_tp = lo <= sl, hi >= tp
                if hit_sl and hit_tp:
                    exit_px, outcome, exit_i = _exit_price(side, entry, sl, True), "sl", j
                    break
                if hit_sl:
                    exit_px, outcome, exit_i = _exit_price(side, entry, sl, True), "sl", j
                    break
                if hit_tp:
                    exit_px, outcome, exit_i = _exit_price(side, entry, tp, False), "tp", j
                    break
            else:
                hit_sl, hit_tp = hi >= sl, lo <= tp
                if hit_sl and hit_tp:
                    exit_px, outcome, exit_i = _exit_price(side, entry, sl, True), "sl", j
                    break
                if hit_sl:
                    exit_px, outcome, exit_i = _exit_price(side, entry, sl, True), "sl", j
                    break
                if hit_tp:
                    exit_px, outcome, exit_i = _exit_price(side, entry, tp, False), "tp", j
                    break
            if j == min(i + max_holding_bars, len(d) - 1):
                exit_px, outcome, exit_i = _exit_price(side, entry, cl, False), "timeout", j
        direction = 1 if side == "LONG" else -1
        gross = (exit_px - entry_px) * qty * direction
        fee_exit = exit_px * qty * FEE_TAKER
        pnl = gross - fee_entry - fee_exit
        ret = pnl / equity if equity > 0 else 0.0
        equity += pnl
        trades.append({"idx_in": i, "idx_out": exit_i, "side": side,
                       "setup": sig["setup"], "entry": round(entry_px, 2),
                       "exit": round(exit_px, 2), "sl": sl, "tp": tp,
                       "qty": qty, "outcome": outcome, "pnl": round(pnl, 2),
                       "ret": ret, "equity": round(equity, 2)})
        equity_curve.append(equity)
        i = exit_i + 1  # flat antar trade (tanpa posisi overlapping)
    return trades, compute_metrics(trades, equity_curve), equity_curve


def compute_metrics(trades: list, equity_curve: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"total_trades": 0, "winrate": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "max_drawdown": 0.0, "profit_factor": 0.0, "net_pnl": 0.0,
                "final_equity": equity_curve[-1] if equity_curve else 0.0}
    rets = np.array([t["ret"] for t in trades], dtype=float)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    std = rets.std(ddof=1) if n > 1 else 0.0
    sharpe = float(rets.mean() / std * np.sqrt(n)) if std > 0 else 0.0
    downside = rets[rets < 0]
    dstd = downside.std(ddof=1) if len(downside) > 1 else (abs(downside[0]) if len(downside) == 1 else 0.0)
    sortino = float(rets.mean() / dstd * np.sqrt(n)) if dstd and dstd > 0 else 0.0
    curve = np.array(equity_curve, dtype=float)
    peak = np.maximum.accumulate(curve)
    dd = np.where(peak > 0, (curve - peak) / peak, 0.0)
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return {"total_trades": n, "winrate": round(wins / n, 4),
            "sharpe": round(sharpe, 4), "sortino": round(sortino, 4),
            "max_drawdown": round(float(dd.min()), 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
            "net_pnl": round(sum(t["pnl"] for t in trades), 2),
            "final_equity": round(float(curve[-1]), 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest standalone (sintetis bila tanpa --csv)")
    ap.add_argument("--csv", default="")
    ap.add_argument("--synthetic", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--max-holding", type=int, default=24)
    a = ap.parse_args(argv)
    if a.csv:
        df = pd.read_csv(a.csv)
        if build_features is not None and "ema20" not in df.columns:
            df = build_features(df)
    else:
        df = make_synthetic(a.synthetic, a.seed)
    trades, metrics, _ = run_backtest(df, a.equity, a.leverage, a.max_holding)
    print(f"trades={len(trades)} metrics={metrics}")
    for t in trades[:10]:
        print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
