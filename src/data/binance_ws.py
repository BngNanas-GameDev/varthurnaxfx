"""Binance Futures (USDT-M) ingest: kline / trades / depth -> Bronze JSONL.

- Public-only: bisa run tanpa API key (paper mode). REST via ccxt,
  websocket via public stream wss://fstream.binance.com (no key).
- Bronze: append-only JSONL di data/bronze/, satu baris = satu event mentah
  + envelope {recv_ts, stream, symbol, payload}. Tidak pernah overwrite.
- Websocket adalah stub yang aman: jika lib `websockets` tidak ada atau
  koneksi gagal, otomatis fallback ke polling REST (poll_loop).

Dependensi eksternal: ccxt (wajib), websockets (opsional), pandas (opsional,
hanya untuk helper snapshot -> DataFrame).

Contoh:
    python -m src.data.binance_ws --once
    python -m src.data.binance_ws --poll-only --symbol BTCUSDT --timeframes 1m,1h
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BRONZE_DIR = PROJECT_ROOT / "data" / "bronze"

FAPI_WS_BASE = "wss://fstream.binance.com/stream"
# ccxt timeframe -> binance ws interval (sama untuk USDT-M)
CCXT_TO_WS_INTERVAL = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_symbol(symbol: str) -> str:
    """'BTCUSDT-PERP' / 'BTCUSDT' -> ccxt USDT-M 'BTC/USDT:USDT'."""
    s = symbol.strip().upper().replace("-", "").replace("/", "").replace(" ", "")
    s = s.replace(":USDT", "")
    if s.endswith("PERP"):
        s = s[: -len("PERP")]
    if s.endswith("USDT") and "/" not in s:
        base = s[: -len("USDT")]
        return f"{base}/USDT:USDT"
    return s  # sudah format ccxt, pass-through


def ws_market_symbol(symbol: str) -> str:
    """'BTCUSDT-PERP' -> 'btcusdt' untuk websocket stream."""
    s = symbol.strip().lower().replace("-", "").replace("/", "").replace(":usdt", "")
    if s.endswith("perp"):
        s = s[: -len("perp")]
    return s


def get_exchange():
    """Public-only ccxt exchange (tanpa key)."""
    import ccxt  # noqa: WPS433

    return ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})


# ------------------------------------------------------------- bronze writer
class BronzeWriter:
    """Append-only JSONL writer. Tidak pernah truncate/overwrite."""

    def __init__(self, out_dir: Path | str = DEFAULT_BRONZE_DIR):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, symbol: str, timeframe: str = "") -> Path:
        safe = symbol.strip().upper().replace("/", "").replace(":", "").replace("-", "")
        suffix = f"-{timeframe}" if timeframe else ""
        return self.out_dir / f"{kind}-{safe}{suffix}.jsonl"

    def append(self, kind: str, symbol: str, payload: dict, timeframe: str = "") -> Path:
        rec = {
            "recv_ts": utc_now_iso(),
            "recv_ts_ms": int(time.time() * 1000),
            "kind": kind,
            "symbol": symbol,
            "timeframe": timeframe,
            "payload": payload,
        }
        path = self._path(kind, symbol, timeframe)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path

    # convenience
    def write_klines(self, symbol, timeframe, candles: list) -> Path:
        last: Path | None = None
        for c in candles:
            last = self.append("klines", symbol, {"candle": c}, timeframe)
        return last if last else self._path("klines", symbol, timeframe)

    def write_trades(self, symbol, trades: list) -> Path:
        last: Path | None = None
        for t in trades:
            last = self.append("trades", symbol, {"trade": t}, "")
        return last if last else self._path("trades", symbol)

    def write_depth(self, symbol, orderbook: dict) -> Path:
        return self.append("depth", symbol, {"depth": orderbook}, "")


# ------------------------------------------------------------- REST snapshot
def fetch_klines_snapshot(symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
    """Ambil OHLCV publik via ccxt (tanpa key). Return list candle ccxt."""
    ex = get_exchange()
    unified = normalize_symbol(symbol)
    return ex.fetch_ohlcv(unified, timeframe=timeframe, limit=limit)


def fetch_trades_snapshot(symbol: str, limit: int = 50) -> list:
    ex = get_exchange()
    return ex.fetch_trades(normalize_symbol(symbol), limit=limit)


def fetch_depth_snapshot(symbol: str, limit: int = 20) -> dict:
    ex = get_exchange()
    return ex.fetch_order_book(normalize_symbol(symbol), limit=limit)


def snapshot_once(symbol="BTCUSDT", timeframes=("1m", "5m", "1h"),
                  out_dir=DEFAULT_BRONZE_DIR, kline_limit=200) -> dict:
    """Satu kali snapshot REST -> Bronze. Dipakai paper/bootstrap & fallback."""
    w = BronzeWriter(out_dir)
    out: dict = {"klines": {}, "trades": None, "depth": None}
    for tf in timeframes:
        try:
            candles = fetch_klines_snapshot(symbol, tf, kline_limit)
            out["klines"][tf] = str(w.write_klines(symbol, tf, candles))
        except Exception as e:  # noqa: BLE001 - ingest harus tahan gagal per-stream
            out["klines"][tf] = f"ERROR: {e}"
    try:
        out["trades"] = str(w.write_trades(symbol, fetch_trades_snapshot(symbol)))
    except Exception as e:  # noqa: BLE001
        out["trades"] = f"ERROR: {e}"
    try:
        out["depth"] = str(w.write_depth(symbol, fetch_depth_snapshot(symbol)))
    except Exception as e:  # noqa: BLE001
        out["depth"] = f"ERROR: {e}"
    return out


def poll_loop(symbol="BTCUSDT", timeframes=("1h",), interval_sec=60,
              out_dir=DEFAULT_BRONZE_DIR, kline_limit=2, stop_after: int | None = None) -> None:
    """Fallback ingest: polling REST berkala (tanpa websocket, tanpa key)."""
    w = BronzeWriter(out_dir)
    n = 0
    print(f"[poll] {symbol} {list(timeframes)} tiap {interval_sec}s -> {w.out_dir}", flush=True)
    while True:
        for tf in timeframes:
            try:
                candles = fetch_klines_snapshot(symbol, tf, kline_limit)
                # hanya candle terakhir (delta kecil, tetap append-only)
                w.write_klines(symbol, tf, candles[-1:])
                print(f"[poll] {utc_now_iso()} {tf} +{len(candles)} ok", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[poll] {tf} ERROR: {e}", flush=True)
        n += 1
        if stop_after is not None and n >= stop_after:
            return
        time.sleep(interval_sec)


# ------------------------------------------------------------- websocket stub
def build_stream_names(symbol: str, timeframes=("1h",), with_trades=True, with_depth=True) -> list[str]:
    m = ws_market_symbol(symbol)
    streams: list[str] = []
    for tf in timeframes:
        streams.append(f"{m}@kline_{CCXT_TO_WS_INTERVAL.get(tf, tf)}")
    if with_trades:
        streams.append(f"{m}@aggTrade")
    if with_depth:
        streams.append(f"{m}@depth10@1000ms")
    return streams


class WsIngest:
    """Websocket ingest stub (public stream, tanpa key).

    - Paksa aman: jika `websockets` tidak terinstal / koneksi gagal,
      otomatis fallback ke poll_loop (REST).
    - Setiap message langsung append ke Bronze (raw, tanpa parsing merusak).
    """

    def __init__(self, symbol="BTCUSDT", timeframes=("1h",),
                 out_dir=DEFAULT_BRONZE_DIR, with_trades=True, with_depth=True):
        self.symbol = symbol
        self.timeframes = tuple(timeframes)
        self.writer = BronzeWriter(out_dir)
        self.with_trades = with_trades
        self.with_depth = with_depth

    async def _run_ws(self, max_messages: int | None = None) -> None:
        try:
            import websockets  # type: ignore  # noqa: WPS433
        except ImportError:
            print("[ws] lib 'websockets' tidak ada -> fallback polling REST", flush=True)
            poll_loop(self.symbol, self.timeframes, interval_sec=10,
                      out_dir=self.writer.out_dir,
                      stop_after=max_messages if max_messages else 3)
            return
        streams = build_stream_names(self.symbol, self.timeframes,
                                     self.with_trades, self.with_depth)
        url = FAPI_WS_BASE + "?streams=" + "/".join(streams)
        print(f"[ws] connect {url}", flush=True)
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                count = 0
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    stream = data.get("stream", "")
                    payload = data.get("data", data)
                    kind = "klines" if "kline" in stream else (
                        "trades" if "aggTrade" in stream else "depth")
                    tf = ""
                    if kind == "klines":
                        tf = payload.get("k", {}).get("i", self.timeframes[0])
                    self.writer.append(kind, self.symbol, payload, tf)
                    count += 1
                    if max_messages and count >= max_messages:
                        return
        except Exception as e:  # noqa: BLE001 - ws putus -> fallback polling
            print(f"[ws] ERROR {e} -> fallback polling sekali", flush=True)
            try:
                snapshot_once(self.symbol, self.timeframes, self.writer.out_dir)
            except Exception as e2:  # noqa: BLE001
                print(f"[ws fallback] ERROR {e2}", flush=True)

    def run(self, max_messages: int | None = None) -> None:
        asyncio.run(self._run_ws(max_messages))


# ------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Binance USDT-M ingest -> Bronze JSONL (no key)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default="1m,5m,1h")
    ap.add_argument("--out-dir", default=str(DEFAULT_BRONZE_DIR))
    ap.add_argument("--once", action="store_true", help="satu snapshot REST lalu keluar")
    ap.add_argument("--poll-only", action="store_true", help="paksa polling REST (tanpa ws)")
    ap.add_argument("--poll-interval", type=int, default=60)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-messages", type=int, default=0)
    args = ap.parse_args(argv)
    tfs = tuple(t.strip() for t in args.timeframes.split(",") if t.strip())
    out = Path(args.out_dir)
    if args.once:
        res = snapshot_once(args.symbol, tfs, out, args.limit)
        print(json.dumps(res, indent=2))
        return 0
    if args.poll_only:
        poll_loop(args.symbol, tfs, args.poll_interval, out, kline_limit=2)
        return 0
    WsIngest(args.symbol, tfs, out).run(
        max_messages=args.max_messages or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
