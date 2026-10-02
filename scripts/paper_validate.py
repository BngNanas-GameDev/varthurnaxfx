"""Validasi paper end-to-end KERING (tanpa API key).

Alur:
  (a) fetch klines 1h via src.data.live_feed
  (b) to_silver + build_features (src.data.pipeline)
  (c) evaluate() -> sinyal + thesis_breaker (src.strategy.setups)
  (d) calc_qty equity=1000 leverage=10 -> qty/notional (src.strategy.sizing)
  (e) backtest metrics di data itu (src.strategy.backtest.run_backtest)

Print ringkasan manusiawi. Exit 0 bila semua langkah jalan
(walau sinyal FLAT/NO-TRADE — itu hasil valid).
Bila network gagal: fallback sintetis (--synthetic paksa) dan catat.

Contoh:
    python scripts/paper_validate.py
    python scripts/paper_validate.py --synthetic
    python scripts/paper_validate.py --limit 200 --equity 1000 --leverage 10 --save
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.live_feed import fetch_klines, save_bronze, to_dataframe  # noqa: E402
from src.data.pipeline import build_features, to_silver  # noqa: E402
from src.strategy.backtest import run_backtest  # noqa: E402
from src.strategy.setups import evaluate  # noqa: E402
from src.strategy.sizing import calc_qty  # noqa: E402


def _synthetic_df(n: int = 200):
    from src.strategy.backtest import make_synthetic

    return make_synthetic(n, seed=7), "synthetic-fallback"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Paper validate kering (no key)")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--save", action="store_true", help="simpan bronze mentah")
    ap.add_argument("--synthetic", action="store_true", help="paksa data sintetis (tanpa network)")
    a = ap.parse_args(argv)

    print("=== PAPER VALIDATE (kering, tanpa key) ===")
    source = "live"
    df_raw = None
    if a.synthetic:
        gold_probe, source = _synthetic_df(a.limit)
        print(f"[a] fetch: SKIPPED (--synthetic), pakai sintetis n={len(gold_probe)}")
        silver = gold_probe  # make_synthetic sudah gold; to_silver/build ulang di bawah
        # rekonstruksi jalur pipeline dari OHLCV mentah agar (b) tetap diuji:
        raw_cols = [c for c in ["open_time", "open", "high", "low", "close",
                                "volume", "close_time", "trades", "is_gap", "funding_rate"]
                    if c in gold_probe.columns]
        silver = to_silver(gold_probe[raw_cols], a.interval)
        gold = build_features(silver)
    else:
        try:
            rows = fetch_klines(a.interval, a.limit)
            df_raw = to_dataframe(rows)
            print(f"[a] fetch live: OK bars={len(df_raw)} interval={a.interval}")
            if a.save:
                p = save_bronze(rows, a.interval)
                print(f"[a] bronze saved: {p}")
        except Exception as e:  # noqa: BLE001 - network gagal -> sintetis, tetap exit 0
            print(f"[a] fetch live GAGAL ({e}); fallback DATA SINTETIS (dicatat).")
            gold_probe, source = _synthetic_df(a.limit)
            print(f"[a] fallback sintetis n={len(gold_probe)} seed=7")
            raw_cols = [c for c in ["open_time", "open", "high", "low", "close",
                                    "volume", "close_time", "trades", "is_gap", "funding_rate"]
                        if c in gold_probe.columns]
            silver = to_silver(gold_probe[raw_cols], a.interval)
            gold = build_features(silver)
        else:
            silver = to_silver(df_raw, a.interval)
            gold = build_features(silver)
    print(f"[b] pipeline: source={source} silver={len(silver)} gold={len(gold)} "
          f"cols_ok={all(c in gold.columns for c in ['ema20', 'ema50', 'atr14', 'rsi14'])}")

    sig = evaluate(gold)
    print(f"[c] sinyal: action={sig.get('action')} setup={sig.get('setup')} "
          f"entry={sig.get('entry')} sl={sig.get('sl')} tp={sig.get('tp')} "
          f"conf={sig.get('confidence')}")
    print(f"    thesis: {sig.get('thesis')}")
    print(f"    thesis_breaker: {sig.get('thesis_breaker')}")

    if sig.get("action") in ("LONG", "SHORT"):
        pos = calc_qty(a.equity, float(sig["entry"]), float(sig["sl"]), a.leverage)
        print(f"[d] sizing: equity={a.equity} lev={a.leverage} -> "
              f"qty={pos['qty']} notional={pos['notional']} risk={pos['risk_amount']} "
              f"capped={pos['capped']} reason={pos['reason']}")
        risk_pct = (pos["risk_amount"] / a.equity) if a.equity else 0
        print(f"    risk_check: {risk_pct:.2%} per-trade (batas 1%) "
              f"-> {'OK' if risk_pct <= 0.010001 else 'LEWAT BATAS'}")
    else:
        # tetap uji sizing (smoke test) pakai bar terakhir + SL ATR-based
        last = gold.iloc[-1]
        entry_demo = float(last["close"])
        atr = float(last["atr14"])
        sl_demo = entry_demo - atr * 1.5
        pos = calc_qty(a.equity, entry_demo, sl_demo, a.leverage)
        print(f"[d] sizing (demo, sinyal NO-TRADE): entry={entry_demo:.1f} sl={sl_demo:.1f} "
              f"-> qty={pos['qty']} notional={pos['notional']} reason={pos['reason']}")

    trades, metrics, _curve = run_backtest(gold, a.equity, a.leverage)
    print(f"[e] backtest di data ini: trades={metrics.get('total_trades')} "
          f"winrate={metrics.get('winrate')} sharpe={metrics.get('sharpe')} "
          f"max_dd={metrics.get('max_drawdown')} pf={metrics.get('profit_factor')} "
          f"net_pnl={metrics.get('net_pnl')} final_eq={metrics.get('final_equity')}")
    for t in trades[:5]:
        print(f"    trade: {t}")

    print("--- RINGKASAN ---")
    print(f"source={source} bars={len(gold)} sinyal={sig.get('action')}/{sig.get('setup')} "
          f"trades={metrics.get('total_trades')} net={metrics.get('net_pnl')}")
    print("paper_validate: SEMUA LANGKAH JALAN (valid).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
