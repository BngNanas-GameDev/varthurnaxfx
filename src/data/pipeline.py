"""Bronze -> Silver -> Gold pipeline.

Layer:
- Bronze: raw append-only JSONL (tulis oleh src/data/binance_ws.py). Schema bebas.
- Silver: OHLCV bersih. Dedup by open_time (keep last = data paling telat),
  sort ascending, coerce numerik, drop bar invalid, tandai gap via `is_gap`.
- Gold: Silver + features EMA20/50, ATR14, RSI14, VWAP(20), funding_rate.

Kontrak schema eksplisit (lihat SILVER_COLUMNS / GOLD_COLUMNS).
Semua fungsi idempotent: input sama -> output sama; `build_features` menghapus
kolom fitur lama lalu hitung ulang sehingga aman dijalankan berulang.

Dependensi lokal yang dibutuhkan worker lain (catat, JANGAN buat di sini):
- src/common/schemas (kontrak global) | src/orchestrator (penjadwal pipeline)
  -> pipeline ini definisikan kontrak lokal SILVER_COLUMNS/GOLD_COLUMNS agar
  standalone; sinkronkan ke src/common/schemas saat tersedia.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRONZE_DIR = PROJECT_ROOT / "data" / "bronze"
SILVER_DIR = PROJECT_ROOT / "data" / "silver"
GOLD_DIR = PROJECT_ROOT / "data" / "gold"

# ------------------------------------------------------------- schema contract
SILVER_COLUMNS = ["open_time", "open", "high", "low", "close", "volume",
                  "close_time", "trades", "is_gap"]
GOLD_COLUMNS = SILVER_COLUMNS + ["ema20", "ema50", "atr14", "rsi14",
                                 "vwap", "funding_rate"]
FEATURE_COLUMNS = ["ema20", "ema50", "atr14", "rsi14", "vwap", "funding_rate"]

EXPECTED_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}

EMA_FAST, EMA_SLOW, ATR_PERIOD, RSI_PERIOD, VWAP_WINDOW = 20, 50, 14, 14, 20


# ------------------------------------------------------------- bronze -> silver
def _parse_bronze_candle(rec: dict) -> dict | None:
    """Terima 2 bentuk: ccxt [ts,o,h,l,c,v] atau dict payload ws/ccxt."""
    p = rec.get("payload", rec)
    c = p.get("candle", p.get("k", p))
    try:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            ts, o, h, l, cl, v = c[0], c[1], c[2], c[3], c[4], c[5]
            return {"open_time": int(ts), "open": float(o), "high": float(h),
                    "low": float(l), "close": float(cl), "volume": float(v),
                    "close_time": int(c[6]) if len(c) > 6 else None,
                    "trades": int(c[8]) if len(c) > 8 and c[8] else 0}
        if isinstance(c, dict):
            # format ws binance kline
            if "t" in c and "o" in c:
                return {"open_time": int(c["t"]), "open": float(c["o"]),
                        "high": float(c["h"]), "low": float(c["l"]),
                        "close": float(c["c"]), "volume": float(c["v"]),
                        "close_time": int(c.get("T", 0)) or None,
                        "trades": int(c.get("n", 0))}
            if "open_time" in c and "close" in c:
                return {"open_time": int(c["open_time"]), "open": float(c["open"]),
                        "high": float(c["high"]), "low": float(c["low"]),
                        "close": float(c["close"]), "volume": float(c.get("volume", 0)),
                        "close_time": c.get("close_time"),
                        "trades": int(c.get("trades", 0))}
    except (ValueError, TypeError, KeyError):
        return None
    return None


def load_bronze_klines(path: str | Path) -> pd.DataFrame:
    """Baca Bronze JSONL -> DataFrame mentah (satu baris per record valid)."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = _parse_bronze_candle(rec)
            if row is not None:
                rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["open_time", "open", "high", "low",
                                     "close", "volume", "close_time", "trades"])
    return pd.DataFrame(rows)


def to_silver(df: pd.DataFrame, timeframe: str = "1h") -> pd.DataFrame:
    """Bersihkan OHLCV mentah -> Silver.

    - coerce numerik, drop bar dengan open_time/close <= 0 atau high<low
    - dedup open_time keep='last' (data telat menimpa = handling late)
    - sort open_time, tandai gap (selisih > 1.5x interval) di `is_gap`
      (handling gap: flag, TIDAK mengarang bar; forward-fill hanya untuk fitur)
    Idempotent: sudah-silver -> hasil identik.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=SILVER_COLUMNS)
    d = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["open_time"] = pd.to_numeric(d["open_time"], errors="coerce")
    d = d.dropna(subset=["open_time", "open", "high", "low", "close"])
    d = d[(d["close"] > 0) & (d["high"] >= d["low"])]
    d["open_time"] = d["open_time"].astype("int64")
    if "close_time" not in d.columns:
        d["close_time"] = pd.NA
    if "trades" not in d.columns:
        d["trades"] = 0
    d["trades"] = pd.to_numeric(d["trades"], errors="coerce").fillna(0).astype("int64")
    # dedup late-data: keep last arrival
    d = d.drop_duplicates(subset=["open_time"], keep="last")
    d = d.sort_values("open_time").reset_index(drop=True)
    step = EXPECTED_INTERVAL_MS.get(timeframe, 3_600_000)
    diff = d["open_time"].diff()
    d["is_gap"] = (diff > step * 1.5).fillna(False).astype(bool)
    d["volume"] = d["volume"].fillna(0.0)
    cols = [c for c in SILVER_COLUMNS if c in d.columns]
    out = d[cols].copy()
    for c in SILVER_COLUMNS:  # pastikan semua kolom kontrak ada
        if c not in out.columns:
            out[c] = False if c == "is_gap" else pd.NA
    return out[SILVER_COLUMNS].reset_index(drop=True)


# ------------------------------------------------------------- silver -> gold
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=1).mean()


def _atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # edge: loss 0 -> RSI 100; awal NaN -> 50 netral
    rsi = rsi.where(avg_loss != 0, 100.0).fillna(50.0)
    return rsi


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Silver -> Gold (tambah fitur). Idempotent, murni fungsi.

    Input: DataFrame kolom Silver (wajib open/high/low/close/volume).
    Opsional: kolom `funding_rate` (dipass-through + ffill, default 0.0).
    Output: DataFrame kolom GOLD_COLUMNS. Baris/input tidak dimutasi.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=GOLD_COLUMNS)
    missing = [c for c in ["open_time", "open", "high", "low", "close", "volume"]
               if c not in df.columns]
    if missing:
        raise ValueError(f"build_features: kolom hilang {missing}")
    d = df.copy()
    d = d.sort_values("open_time").drop_duplicates(subset=["open_time"],
                                                   keep="last").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    # hapus fitur lama agar idempotent (recompute penuh)
    d = d.drop(columns=[c for c in FEATURE_COLUMNS if c in d.columns])
    close, high, low, vol = d["close"], d["high"], d["low"], d["volume"].fillna(0.0)
    d["ema20"] = _ema(close, EMA_FAST)
    d["ema50"] = _ema(close, EMA_SLOW)
    d["atr14"] = _atr_wilder(high, low, close, ATR_PERIOD)
    d["rsi14"] = _rsi_wilder(close, RSI_PERIOD)
    tp = (high + low + close) / 3.0
    vol_sum = vol.rolling(VWAP_WINDOW, min_periods=1).sum().replace(0, np.nan)
    d["vwap"] = (tp * vol).rolling(VWAP_WINDOW, min_periods=1).sum() / vol_sum
    d["vwap"] = d["vwap"].fillna(tp)
    if "funding_rate" in df.columns:
        d["funding_rate"] = (pd.to_numeric(df["funding_rate"], errors="coerce")
                             .ffill().fillna(0.0).values)
    else:
        d["funding_rate"] = 0.0
    for c in SILVER_COLUMNS:  # jaga kolom kontrak silver
        if c not in d.columns:
            d[c] = False if c == "is_gap" else pd.NA
    return d[GOLD_COLUMNS].reset_index(drop=True)


# ------------------------------------------------------------- file helpers
def bronze_to_silver(bronze_path: str | Path, silver_path: str | Path | None = None,
                     timeframe: str = "1h") -> Path:
    raw = load_bronze_klines(bronze_path)
    silver = to_silver(raw, timeframe)
    out = Path(silver_path) if silver_path else SILVER_DIR / (Path(bronze_path).stem + ".parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".csv" or len(silver) == 0:
        out = out.with_suffix(".csv")
        silver.to_csv(out, index=False)
    else:
        try:
            silver.to_parquet(out, index=False)
        except (ImportError, ValueError):
            out = out.with_suffix(".csv")
            silver.to_csv(out, index=False)
    return out


def silver_to_gold(silver_path: str | Path, gold_path: str | Path | None = None) -> Path:
    p = Path(silver_path)
    silver = pd.read_csv(p) if p.suffix == ".csv" else pd.read_parquet(p)
    gold = build_features(silver)
    out = Path(gold_path) if gold_path else GOLD_DIR / (p.stem + ".parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".csv":
        gold.to_csv(out, index=False)
    else:
        try:
            gold.to_parquet(out, index=False)
        except (ImportError, ValueError):
            out = out.with_suffix(".csv")
            gold.to_csv(out, index=False)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Bronze->Silver->Gold")
    ap.add_argument("--bronze", default=str(BRONZE_DIR / "klines-BTCUSDT-1h.jsonl"))
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--silver-out", default="")
    ap.add_argument("--gold-out", default="")
    a = ap.parse_args()
    sp = bronze_to_silver(a.bronze, a.silver_out or None, a.timeframe)
    print(f"silver: {sp}")
    try:
        gp = silver_to_gold(sp, a.gold_out or None)
        print(f"gold: {gp}")
    except Exception as e:  # noqa: BLE001
        print(f"gold SKIP: {e}")
