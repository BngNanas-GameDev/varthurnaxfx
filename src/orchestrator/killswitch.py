"""Kill-switch: daily stop, WS staleness, rate-limit. Lock file STATE until manual resume."""

from __future__ import annotations

import json
import os
import time

STATE_FILE = os.getenv("KILLSWITCH_STATE_FILE", "STATE")
RISK_CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "risk.json")


def _risk_defaults() -> dict:
    try:
        with open(os.path.normpath(RISK_CONFIG), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"daily_stop_pct": 5, "ws_stale_sec": 10}


def is_locked(state_file: str = STATE_FILE) -> tuple[bool, str]:
    """Return (locked, reason). Lock persists until manual resume (delete STATE file)."""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as fh:
                reason = fh.read().strip() or "locked"
        except OSError:
            reason = "locked"
        return True, reason
    return False, ""


def trip(reason: str, state_file: str = STATE_FILE) -> None:
    with open(state_file, "w", encoding="utf-8") as fh:
        fh.write(reason)


def resume(state_file: str = STATE_FILE) -> None:
    """Manual resume only: operator deletes STATE file after review."""
    if os.path.exists(state_file):
        os.remove(state_file)


def should_halt(
    daily_pnl_pct: float = 0.0,
    ws_last_update_ts: float | None = None,
    rate_limited: bool = False,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    """Evaluate halt conditions. Returns (halt, reason).

    - locked STATE file -> halt (requires manual resume)
    - daily_pnl_pct <= -daily_stop_pct (default -5%) -> trip + halt
    - ws stale: now - ws_last_update_ts > ws_stale_sec (default 10s) -> halt (no lock)
    - rate-limit flag (e.g. Binance -1003) -> halt (no lock, caller backs off)
    """
    cfg = _risk_defaults()
    daily_stop = float(cfg.get("daily_stop_pct", 5))
    stale_sec = float(cfg.get("ws_stale_sec", 10))

    locked, reason = is_locked()
    if locked:
        return True, f"locked:{reason}"

    if daily_pnl_pct <= -abs(daily_stop):
        r = f"daily_stop:{daily_pnl_pct:.2f}%<=-{daily_stop}%"
        try:
            trip(r)
        except OSError:
            pass
        return True, r

    if ws_last_update_ts is not None:
        now = now_ts if now_ts is not None else time.time()
        if now - ws_last_update_ts > stale_sec:
            return True, f"ws_stale:{now - ws_last_update_ts:.1f}s>{stale_sec}s"

    if rate_limited:
        return True, "rate_limited:-1003_backoff"

    return False, ""
