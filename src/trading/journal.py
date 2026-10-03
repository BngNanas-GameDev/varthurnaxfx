"""Jurnal trade JSONL (worker-2): siklus hidup penuh tiap posisi.

Dua event per posisi, di-join via ``trace_id``:
- ``open``  via :func:`record_open`  (ORDER sukses / adopsi posisi exchange).
- ``close`` via :func:`record_close` (posisi hilang di luar STATE / SL-TP native).

File default: ``<repo>/data/journal.jsonl`` (runtime, git-ignored).
Override via env ``JOURNAL_FILE`` (tests) atau arg ``journal_file``.
Tanpa network, tanpa key. Tak pernah raise ke loop (caller membungkus
try/except; fungsi di sini pun menelan OSError tulis -> warning).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOURNAL_FILE = PROJECT_ROOT / "data" / "journal.jsonl"


def journal_path(journal_file: str | Path | None = None) -> Path:
    """Path file JSONL: arg eksplisit > env JOURNAL_FILE > default repo."""
    if journal_file:
        return Path(journal_file)
    return Path(os.getenv("JOURNAL_FILE", str(DEFAULT_JOURNAL_FILE)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(record: dict, journal_file: str | Path | None = None) -> dict:
    path = journal_path(journal_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("jurnal %s gagal ditulis (%s)", path, exc)
    return record


def side_sign(side: str) -> int:
    """+1 untuk LONG/BUY, -1 untuk SHORT/SELL."""
    s = str(side or "").upper()
    if s in ("SHORT", "SELL"):
        return -1
    return 1


def record_open(trace_id, side, qty, entry, sl=None, tp=None,
                source="unknown", journal_file=None) -> dict:
    """Catat pembukaan posisi. ``source`` = nama setup / adopted-exchange."""
    rec = {"event": "open", "trace_id": str(trace_id), "side": str(side),
           "qty": float(qty), "entry": float(entry),
           "sl": None if sl is None else float(sl),
           "tp": None if tp is None else float(tp),
           "source": str(source or "unknown"), "ts": _now_iso()}
    return _append(rec, journal_file)


def record_close(trace_id, exit_price, reason, fee_paid=0.0,
                 funding_paid=0.0, estimated=False, journal_file=None) -> dict:
    """Catat penutupan posisi. ``estimated`` True bila exit dari markPrice."""
    rec = {"event": "close", "trace_id": str(trace_id),
           "exit_price": float(exit_price), "reason": str(reason),
           "fee_paid": float(fee_paid or 0.0),
           "funding_paid": float(funding_paid or 0.0),
           "estimated": bool(estimated), "ts": _now_iso()}
    return _append(rec, journal_file)


def realized_pnl(side: str, qty: float, entry: float, exit_price: float,
                 fee_paid: float = 0.0, funding_paid: float = 0.0) -> float:
    """PnL terealisasi = (exit-entry)*qty*side_sign - fee - funding."""
    gross = (float(exit_price) - float(entry)) * float(qty) * side_sign(side)
    return gross - float(fee_paid or 0.0) - float(funding_paid or 0.0)


def summarize(journal_file=None) -> dict:
    """Agregat trade tuntas (open+close berpasangan via trace_id).

    Return {trades, wins, total_pnl, total_fee, by_setup} dengan
    total_fee = fee_paid + funding_paid, dan
    by_setup = {setup: {trades, pnl}}.
    Close tanpa open pasangan dilewati (tak bisa hitung gross).
    """
    path = journal_path(journal_file)
    opens: dict[str, dict] = {}
    closes: list[dict] = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("event") == "open":
                        opens[str(rec.get("trace_id"))] = rec
                    elif rec.get("event") == "close":
                        closes.append(rec)
        except OSError as exc:
            logger.warning("jurnal %s gagal dibaca (%s)", path, exc)

    trades = 0
    wins = 0
    total_pnl = 0.0
    total_fee = 0.0
    by_setup: dict[str, dict] = {}
    for cl in closes:
        op = opens.get(str(cl.get("trace_id")))
        if op is None:
            continue
        try:
            pnl = realized_pnl(op.get("side", "LONG"), op.get("qty", 0.0),
                               op.get("entry", 0.0), cl.get("exit_price", 0.0),
                               cl.get("fee_paid", 0.0),
                               cl.get("funding_paid", 0.0))
        except (TypeError, ValueError):
            continue
        fee = float(cl.get("fee_paid") or 0.0) + float(cl.get("funding_paid") or 0.0)
        trades += 1
        if pnl > 0:
            wins += 1
        total_pnl += pnl
        total_fee += fee
        setup = str(op.get("source") or "unknown")
        agg = by_setup.setdefault(setup, {"trades": 0, "pnl": 0.0})
        agg["trades"] += 1
        agg["pnl"] = round(agg["pnl"] + pnl, 2)

    return {"trades": trades, "wins": wins,
            "total_pnl": round(total_pnl, 2),
            "total_fee": round(total_fee, 2), "by_setup": by_setup}
