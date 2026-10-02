"""Live feed publik BTCUSDT-PERP (tanpa API key) -> Bronze JSONL.

Sumber utama: ccxt ``binanceusdm`` public REST (tanpa key).
Fallback: GET langsung ke https://fapi.binance.com/fapi/v1/klines
  (pakai httpx bila ada, sonst urllib stdlib — tanpa dependensi baru).

Kontrak:
- fetch_klines(interval='1h', limit=200) -> list[rows mentah]
- to_dataframe(rows) -> DataFrame kolom
  [open_time, open, high, low, close, volume, close_time]
- save_bronze(rows, interval, ...) -> Path data/bronze/klines_<interval>_<ts>.jsonl (append)

CLI:
    python -m src.data.live_feed --interval 1h --limit 200 --save
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRONZE_DIR = PROJECT_ROOT / "data" / "bronze"

SYMBOL_SPOT = "BTCUSDT"  # simbol futures USDT-M di REST fapi
SYMBOL_CCXT = "BTC/USDT:USDT"  # unified ccxt binanceusdm
FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
DF_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def _fetch_via_ccxt(interval: str = "1h", limit: int = 200) -> list:
    import ccxt  # noqa: WPS433 - import lokal agar modul tetap importable tanpa ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    rows = ex.fetch_ohlcv(SYMBOL_CCXT, timeframe=interval, limit=limit)
    return [list(r) for r in rows]


def _fetch_via_http(interval: str = "1h", limit: int = 200) -> list:
    """Fallback langsung ke fapi REST. Coba httpx, lalu urllib stdlib."""
    params = {"symbol": SYMBOL_SPOT, "interval": interval, "limit": limit}
    try:
        import httpx  # type: ignore

        r = httpx.get(FAPI_KLINES_URL, params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except ImportError:
        pass  # jatuh ke urllib di bawah
    except Exception:
        pass  # httpx ada tapi gagal (blokir/geo) -> coba urllib sebelum menyerah
    # --- stdlib fallback (tanpa dependensi baru) ---
    import urllib.parse
    import urllib.request

    url = FAPI_KLINES_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "TradeAgent/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - endpoint publik baca saja
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(interval: str = "1h", limit: int = 200) -> list:
    """Ambil klines mentah publik (tanpa key).

    Urutan: ccxt binanceusdm -> fallback HTTP langsung ke fapi.
    Return: list rows (ccxt 6-kolom atau fapi 12-kolom, apa adanya).
    Raise: RuntimeError bila semua sumber gagal (caller yang fallback sintetis).
    """
    try:
        rows = _fetch_via_ccxt(interval, limit)
        if rows:
            return rows
    except Exception as e_ccxt:  # noqa: BLE001 - catat lalu fallback
        ccxt_err = e_ccxt
    else:
        ccxt_err = None
    try:
        rows = _fetch_via_http(interval, limit)
        if rows:
            return rows
    except Exception as e_http:  # noqa: BLE001
        raise RuntimeError(f"live_feed gagal: ccxt err={ccxt_err!r}, http err={e_http!r}")
    raise RuntimeError(f"live_feed gagal: ccxt err={ccxt_err!r}, http kosong")


def to_dataframe(rows: list) -> "pd.DataFrame":
    """Mapping rows mentah -> DataFrame kolom DF_COLUMNS.

    Terima 2 bentuk:
    - ccxt: [open_time, open, high, low, close, volume] (+opsional close_time idx 6)
    - fapi: [open_time, open, high, low, close, volume, close_time, ...]
    """
    if pd is None:  # pragma: no cover
        raise ImportError("pandas dibutuhkan untuk to_dataframe")
    recs: list[dict] = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        try:
            recs.append({
                "open_time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "close_time": int(r[6]) if len(r) > 6 and r[6] not in (None, "") else None,
            })
        except (ValueError, TypeError):
            continue
    df = pd.DataFrame(recs, columns=DF_COLUMNS)
    return df


def save_bronze(rows: list, interval: str = "1h",
                out_dir: str | Path | None = None,
                ts: int | None = None) -> Path:
    """Append rows mentah ke data/bronze/klines_<interval>_<ts>.jsonl.

    Satu baris = {"candle": [...]} (kompatibel dgn pipeline.load_bronze_klines).
    Tidak pernah overwrite file lain; selalu file baru per-ts (lalu append).
    Return Path file yang ditulis.
    """
    d = Path(out_dir) if out_dir else BRONZE_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = ts if ts is not None else int(time.time())
    path = d / f"klines_{interval}_{stamp}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for r in rows or []:
            rec = list(r) if isinstance(r, (list, tuple)) else r
            f.write(json.dumps({"candle": rec}, ensure_ascii=False) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live feed publik BTCUSDT klines -> Bronze (no key)")
    ap.add_argument("--interval", default="1h", choices=["1m", "5m", "1h"])
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--save", action="store_true", help="simpan mentah ke data/bronze/")
    ap.add_argument("--out-dir", default=str(BRONZE_DIR))
    a = ap.parse_args(argv)
    rows = fetch_klines(a.interval, a.limit)
    df = to_dataframe(rows)
    print(f"fetched={len(rows)} bars={len(df)} interval={a.interval}")
    if len(df):
        last = df.iloc[-1].to_dict()
        print(f"last_close={last['close']} last_open_time={last['open_time']} "
              f"({datetime.fromtimestamp(last['open_time'] / 1000, tz=timezone.utc).isoformat()})")
        print(df.tail(3).to_string(index=False))
    if a.save:
        p = save_bronze(rows, a.interval, a.out_dir)
        print(f"bronze: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
