"""Tarik N bar klines mundur (paginasi endTime) -> 1 file bronze.

Pakai di VPS (network Binance OK):
    python scripts/fetch_history.py --interval 5m --bars 5000
Hasil: data/bronze/hist_<interval>_<n>.jsonl (format candle, kompatibel load_bronze_klines).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.live_feed import _interval_ms, fetch_klines  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--batch", type=int, default=1000)
    a = ap.parse_args(argv)
    step = _interval_ms(a.interval)
    end_ms = int(time.time() * 1000)
    all_rows: list = []
    pages = (a.bars + a.batch - 1) // a.batch
    for p in range(pages):
        rows = fetch_klines(a.interval, min(a.batch, a.bars - len(all_rows)), end_ms)
        if not rows:
            break
        all_rows = rows + all_rows
        end_ms = int(rows[0][0]) - 1
        print(f"page {p + 1}/{pages}: got {len(rows)}, total {len(all_rows)}", flush=True)
        if len(all_rows) >= a.bars:
            break
        time.sleep(0.3)
    out = (PROJECT_ROOT / "data/bronze"
           / f"hist_{a.interval}_{len(all_rows)}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for r in all_rows[-a.bars:]:
            f.write(json.dumps({"candle": list(r)}) + "\n")
    print(f"saved {out} rows={min(len(all_rows), a.bars)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
