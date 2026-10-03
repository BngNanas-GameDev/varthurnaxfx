"""Refresh Gold berkala dari klines live (public, tanpa key).

Dipanggil daemon tiap REFRESH_S (default 300s). Gagal network ->
kembalikan None agar loop pakai Gold lama (fail-open baca, fail-closed order).
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    from src.data.live_feed import fetch_klines, save_bronze, to_dataframe
    from src.data.pipeline import GOLD_DIR as _GOLD_DIR
    from src.data.pipeline import build_features, to_silver
except ImportError:
    from data.live_feed import fetch_klines, save_bronze, to_dataframe  # type: ignore[no-redef]
    from data.pipeline import GOLD_DIR as _GOLD_DIR  # type: ignore[no-redef]
    from data.pipeline import build_features, to_silver  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

GOLD_DIR = Path(_GOLD_DIR)


def refresh_gold(interval: str = "1h", limit: int = 200):
    """Fetch klines -> Bronze -> Silver -> Gold CSV terbaru. Return df/None."""
    rows = fetch_klines(interval=interval, limit=limit)
    if not rows:
        logger.warning("refresh_gold: fetch kosong, Gold lama dipakai")
        return None
    save_bronze(rows, interval)
    silver = to_silver(to_dataframe(rows), interval)
    gold = build_features(silver)
    out = GOLD_DIR / f"gold_{interval}_latest.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    gold.to_csv(out, index=False)
    logger.info("refresh_gold: %s bars=%d -> %s", interval, len(gold), out.name)
    return gold
