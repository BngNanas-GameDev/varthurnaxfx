"""Paper-trading strategy cycle (worker-1).

run_cycle(client, equity) -> dict:
  1. load Gold terakhir dari data/gold/ (CSV/parquet terbaru; kosong -> NO_DATA).
  2. evaluate() -> LONG/SHORT/NO_TRADE.
  3. LONG/SHORT: calc_qty sizing -> order_guard.validate_order ->
     killswitch.should_halt -> client.place_entry + catat state.
  4. FLAT/NO-TRADE: tanpa order.

Blokir order bila: halt latched, daily pnl <= -5%, posisi sudah open
(1 posisi 1 arah), equity <= 0, atau live-guard (BINANCE_TESTNET=false
tanpa ALLOW_LIVE). Semua keputusan di-log dengan trace_id (uuid4).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

try:  # dijalankan dari repo-root (daemon: python -m src.ops.daemon)
    from src.data.pipeline import GOLD_DIR as _PIPELINE_GOLD_DIR
    from src.execution.order_guard import validate_order
    from src.orchestrator.killswitch import should_halt
    from src.strategy.setups import evaluate
    from src.strategy.sizing import calc_qty
    from src.trading.state import load as _load_state
    from src.trading.state import save as _save_state
    from src.trading.state import trip_halt as _trip_halt
except ImportError:  # dijalankan dengan src/ di sys.path (pytest tests/)
    from data.pipeline import GOLD_DIR as _PIPELINE_GOLD_DIR  # type: ignore[no-redef]
    from execution.order_guard import validate_order  # type: ignore[no-redef]
    from orchestrator.killswitch import should_halt  # type: ignore[no-redef]
    from strategy.setups import evaluate  # type: ignore[no-redef]
    from strategy.sizing import calc_qty  # type: ignore[no-redef]
    from trading.state import load as _load_state  # type: ignore[no-redef]
    from trading.state import save as _save_state  # type: ignore[no-redef]
    from trading.state import trip_halt as _trip_halt  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

GOLD_DIR = Path(_PIPELINE_GOLD_DIR)
SYMBOL = "BTCUSDT-PERP"
LEVERAGE = 10
DAILY_HALT_PNL = -0.05  # -5% equity


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_trading_allowed() -> tuple[bool, str]:
    """True bila boleh kirim order: testnet, atau live dengan ALLOW_LIVE."""
    testnet = os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes")
    allow_live = os.getenv("ALLOW_LIVE", "").lower() in ("1", "true", "yes")
    if testnet:
        return True, "testnet"
    if allow_live:
        return True, "live-allowed"
    return False, "live-blocked: BINANCE_TESTNET=false tanpa ALLOW_LIVE"


def load_latest_gold(gold_dir: str | Path | None = None):
    """Baca file Gold terbaru (parquet/csv) di data/gold/.

    Return DataFrame; kosong bila belum ada file / pandas tak tersedia.
    """
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas tak tersedia, Gold dianggap kosong")
        return None
    d = Path(gold_dir) if gold_dir else GOLD_DIR
    if not d.exists():
        return pd.DataFrame()
    cands = [p for p in d.iterdir()
             if p.is_file() and p.suffix.lower() in (".csv", ".parquet")]
    # abaikan placeholder .gitkeep
    cands = [p for p in cands if p.name != ".gitkeep"]
    if not cands:
        return pd.DataFrame()
    latest = max(cands, key=lambda p: p.stat().st_mtime)
    try:
        if latest.suffix.lower() == ".csv":
            return pd.read_csv(latest)
        return pd.read_parquet(latest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gagal baca Gold %s: %s", latest, exc)
        return pd.DataFrame()


def run_cycle(client, equity: float, df=None, symbol: str = SYMBOL,
              leverage: int = LEVERAGE, state_file=None,
              daily_pnl: float | None = None, gold_dir=None) -> dict:
    """Satu siklus strategy -> order. Tak pernah raise untuk kondisi trading
    normal (error exchange diteruskan sebagai reason ORDER_ERROR).

    Args:
        client: BinanceClient (atau mock dengan get_daily_pnl/place_entry).
        equity: ekuitas akun USDT (PAPER_EQUITY).
        df: DataFrame Gold injeksi (tests/smoke). None -> load dari data/gold/.
        state_file: override path STATE JSON (tests). None -> env/default.
    """
    trace_id = uuid4().hex
    log = logging.LoggerAdapter(logger, {"trace_id": trace_id})

    if equity is None or equity <= 0:
        log.info("[%s] SKIP equity<=0 (equity=%s), tanpa order", trace_id, equity)
        return {"trace_id": trace_id, "action": "SKIP", "ordered": False,
                "reason": "equity <= 0", "qty": 0.0}

    st = _load_state(state_file)

    # Rekonsiliasi dengan posisi exchange (anti double-entry):
    # - exchange ada posisi tapi STATE kosong -> adopsi, blokir entry baru.
    # - STATE ada posisi tapi exchange flat -> anggap SL/TP kena, clear.
    try:
        ex_pos = client.get_position()
        ex_qty = abs(float(ex_pos.get("contracts") or 0.0))
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] get_position gagal (%s), pakai STATE saja", trace_id, exc)
        ex_qty, ex_pos = 0.0, {}
    if ex_qty > 0 and not st.get("open_position"):
        side = str(ex_pos.get("side", "")).lower()
        action_side = "LONG" if side == "long" else "SHORT"
        st["open_position"] = {"side": action_side, "qty": float(ex_qty),
                               "entry": float(ex_pos.get("entryPrice") or 0.0),
                               "sl": None, "tp": None,
                               "trace_id": "adopted-exchange",
                               "adopted": True}
        _save_state(st, state_file)
        log.info("[%s] ADOPT posisi exchange %s qty=%s, entry baru diblokir",
                 trace_id, action_side, ex_qty)
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": "adopted exchange position", "qty": 0.0}
    if ex_qty == 0 and st.get("open_position"):
        log.info("[%s] CLOSED_EXTERNALLY posisi %s hilang (SL/TP?), STATE di-clear",
                 trace_id, st["open_position"].get("side"))
        st["open_position"] = None
        _save_state(st, state_file)

    # daily pnl: eksplisit (tests) atau best-effort dari client (0.0 bila gagal)
    if daily_pnl is None:
        try:
            daily_pnl = float(client.get_daily_pnl())
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] get_daily_pnl gagal (%s), pakai 0.0", trace_id, exc)
            daily_pnl = 0.0
    st["daily_pnl"] = float(daily_pnl)

    if st.get("halt_latched"):
        _save_state(st, state_file)
        log.info("[%s] BLOCK halt latched (%s), tanpa order",
                 trace_id, st.get("halt_reason", ""))
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": f"halt latched: {st.get('halt_reason', '')}", "qty": 0.0}

    if float(daily_pnl) <= DAILY_HALT_PNL:
        reason = f"daily_stop:{float(daily_pnl):.4f}<={DAILY_HALT_PNL}"
        _trip_halt(reason, state_file)
        log.info("[%s] BLOCK %s, halt di-latch, tanpa order", trace_id, reason)
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": reason, "qty": 0.0}

    gold = df if df is not None else load_latest_gold(gold_dir)
    if gold is None or len(gold) == 0:
        _save_state(st, state_file)
        log.info("[%s] NO_DATA: Gold kosong (data/gold/ belum ada), tanpa order", trace_id)
        return {"trace_id": trace_id, "action": "NO_DATA", "ordered": False,
                "reason": "gold empty", "qty": 0.0}

    try:
        sig = evaluate(gold)
    except Exception as exc:  # noqa: BLE001
        _save_state(st, state_file)
        log.warning("[%s] evaluate gagal (%s), tanpa order", trace_id, exc)
        return {"trace_id": trace_id, "action": "ERROR", "ordered": False,
                "reason": f"evaluate error: {exc}", "qty": 0.0}

    action = str(sig.get("action", "NO_TRADE")).upper()
    st["last_signal_ts"] = _now_iso()

    if action not in ("LONG", "SHORT"):
        _save_state(st, state_file)
        log.info("[%s] NO_TRADE (%s: %s), tanpa order", trace_id,
                 action, sig.get("reason", sig.get("thesis", "")))
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": str(sig.get("reason", "no-trade")), "signal": sig, "qty": 0.0}

    # 1 posisi 1 arah: posisi open apa pun memblokir entry baru
    pos = st.get("open_position")
    if pos:
        _save_state(st, state_file)
        same = str(pos.get("side", "")).upper() == action
        reason = ("already open same side" if same else "position open opposite side") \
            + f" ({pos.get('side')} qty={pos.get('qty')})"
        log.info("[%s] BLOCK %s untuk sinyal %s, tanpa order", trace_id, reason, action)
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": reason, "signal": sig, "qty": 0.0}

    entry, sl, tp = sig.get("entry"), sig.get("sl"), sig.get("tp")
    size = calc_qty(float(equity), entry, sl, leverage)
    qty = float(size.get("qty", 0.0))
    if qty <= 0:
        _save_state(st, state_file)
        log.info("[%s] SIZING_ZERO %s entry=%s sl=%s (%s), tanpa order",
                 trace_id, action, entry, sl, size.get("reason"))
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": f"sizing zero: {size.get('reason')}", "signal": sig, "qty": 0.0}

    side = "BUY" if action == "LONG" else "SELL"
    ok, guard_reason = validate_order(side=side, qty=qty, price=float(entry),
                                      equity=float(equity), leverage=leverage,
                                      sl_price=sl, daily_pnl=float(daily_pnl),
                                      halt_triggered=bool(st.get("halt_latched")))
    if not ok:
        _save_state(st, state_file)
        log.info("[%s] GUARD_REJECT %s qty=%s entry=%s (%s), tanpa order",
                 trace_id, side, qty, entry, guard_reason)
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": f"guard: {guard_reason}", "signal": sig, "qty": qty}

    halt, halt_reason = should_halt(daily_pnl_pct=float(daily_pnl) * 100.0)
    if halt:
        if halt_reason.startswith(("daily_stop", "locked")):
            _trip_halt(halt_reason, state_file)
        else:
            _save_state(st, state_file)
        log.info("[%s] KILLSWITCH %s, tanpa order", trace_id, halt_reason)
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": f"killswitch: {halt_reason}", "signal": sig, "qty": qty}

    live_ok, live_reason = _live_trading_allowed()
    if not live_ok:
        _save_state(st, state_file)
        log.info("[%s] BLOCK %s, tanpa order", trace_id, live_reason)
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": live_reason, "signal": sig, "qty": qty}

    try:
        res = client.place_entry(side=side, qty=qty, trace_id=trace_id,
                                 sl_price=float(sl),
                                 tp_price=float(tp) if tp else 0.0)
    except Exception as exc:  # noqa: BLE001
        _save_state(st, state_file)
        log.warning("[%s] ORDER_ERROR %s %s (%s)", trace_id, side, qty, exc)
        return {"trace_id": trace_id, "action": action, "ordered": False,
                "reason": f"order error: {exc}", "signal": sig, "qty": qty}

    st["open_position"] = {"side": action, "qty": qty, "entry": float(entry),
                           "sl": float(sl), "tp": float(tp) if tp else None,
                           "trace_id": trace_id}
    _save_state(st, state_file)
    log.info("[%s] ORDER %s %s entry=%s sl=%s tp=%s clientOrderId=%s -> %s",
             trace_id, symbol, side, entry, sl, tp, trace_id, res)
    return {"trace_id": trace_id, "action": action, "ordered": True,
            "reason": "ok", "signal": sig, "qty": qty, "side": side,
            "order": res}
