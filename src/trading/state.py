"""Paper-trading STATE (JSON) untuk worker-1 loop.

Schema:
{
  "open_position": null | {"side": "LONG"|"SHORT", "qty": float, "entry": float,
                           "sl": float, "tp": float|None, "trace_id": str},
  "daily_pnl": float,          # fraksi, mis. -0.05 == -5%
  "halt_latched": bool,        # True -> loop dilarang order sampai resume manual
  "halt_reason": str,
  "last_signal_ts": str|None,  # ISO-8601 evaluasi sinyal terakhir
  "arbitration": dict|None,   # cache putusan arbiter per bar {bar, pick, ...}
  "review_cache": dict|None,  # cache verdict review per bar+sinyal
  "updated_at": str|None,
}

File default: <repo>/data/trading_state.json (bukan file lock "STATE" milik
killswitch). Override via env TRADING_STATE_FILE. Manual resume: panggil
resume() atau hapus file STATE JSON (load() menganggap file hilang = fresh).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "trading_state.json"


def state_path(state_file: str | Path | None = None) -> Path:
    if state_file:
        return Path(state_file)
    return Path(os.getenv("TRADING_STATE_FILE", str(DEFAULT_STATE_FILE)))


def default_state() -> dict:
    return {
        "open_position": None,
        "daily_pnl": 0.0,
        "halt_latched": False,
        "halt_reason": "",
        "last_signal_ts": None,
        "arbitration": None,
        "review_cache": None,
        "updated_at": None,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(state_file: str | Path | None = None) -> dict:
    """Baca STATE JSON. File hilang/korup -> default fresh (log warning)."""
    path = state_path(state_file)
    st = default_state()
    if not path.exists():
        return st
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("STATE root bukan object")
        st.update({k: data[k] for k in st if k in data})
        # validasi ringan open_position
        pos = st.get("open_position")
        if pos is not None and not isinstance(pos, dict):
            logger.warning("STATE %s: open_position invalid, reset ke None", path)
            st["open_position"] = None
        return st
    except (OSError, ValueError) as exc:
        logger.warning("STATE %s korup/hilang (%s), pakai default fresh", path, exc)
        return default_state()


def save(state: dict, state_file: str | Path | None = None) -> Path:
    """Tulis STATE atomically: write tmp + os.replace (rename)."""
    path = state_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["updated_at"] = _now_iso()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def trip_halt(reason: str, state_file: str | Path | None = None) -> dict:
    """Latch halt: set halt_latched=True + reason, persist, return state."""
    st = load(state_file)
    st["halt_latched"] = True
    st["halt_reason"] = reason or "manual trip"
    save(st, state_file)
    logger.error("HALT latched: %s", st["halt_reason"])
    return st


def resume(state_file: str | Path | None = None) -> dict:
    """Resume manual oleh operator: hapus latch (halt_latched=False).

    Alternatif setara: hapus file STATE JSON; load() berikutnya fresh.
    """
    st = load(state_file)
    st["halt_latched"] = False
    st["halt_reason"] = ""
    save(st, state_file)
    logger.info("HALT latch dihapus operator (manual resume)")
    return st


def set_position(position: dict, state_file: str | Path | None = None) -> dict:
    """Catat posisi open baru, persist, return state."""
    st = load(state_file)
    st["open_position"] = dict(position)
    save(st, state_file)
    return st


def clear_position(state_file: str | Path | None = None) -> dict:
    """Hapus catatan posisi open (mis. setelah exit/TP/SL), persist."""
    st = load(state_file)
    st["open_position"] = None
    save(st, state_file)
    return st
