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
    from src.trading.journal import record_close as _journal_close
    from src.trading.journal import record_open as _journal_open
except ImportError:  # dijalankan dengan src/ di sys.path (pytest tests/)
    from data.pipeline import GOLD_DIR as _PIPELINE_GOLD_DIR  # type: ignore[no-redef]
    from execution.order_guard import validate_order  # type: ignore[no-redef]
    from orchestrator.killswitch import should_halt  # type: ignore[no-redef]
    from strategy.setups import evaluate  # type: ignore[no-redef]
    from strategy.sizing import calc_qty  # type: ignore[no-redef]
    from trading.state import load as _load_state  # type: ignore[no-redef]
    from trading.state import save as _save_state  # type: ignore[no-redef]
    from trading.state import trip_halt as _trip_halt  # type: ignore[no-redef]
    from trading.journal import record_close as _journal_close  # type: ignore[no-redef]
    from trading.journal import record_open as _journal_open  # type: ignore[no-redef]

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


def _journal_exit_price(ex_pos: dict, closed: dict) -> tuple[float, bool]:
    """Exit price posisi yang tertutup di luar STATE.

    Prioritas: markPrice (estimated=True) -> entryPrice exchange
    (estimated=False) -> entry STATE (estimated=True, fallback basi).
    """
    if isinstance(ex_pos, dict):
        for key in ("markPrice", "mark_price", "mark",
                    "lastPrice", "last_price"):
            try:
                v = float(ex_pos.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v, True
        for key in ("entryPrice", "entry_price"):
            try:
                v = float(ex_pos.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v, False
    try:
        return float((closed or {}).get("entry") or 0.0), True
    except (TypeError, ValueError):
        return 0.0, True


def _mode_label() -> str:
    """Label mode untuk notif: DRY_RUN / DEMO / TESTNET / LIVE."""
    import os as _os

    if _os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes"):
        return "DRY_RUN"
    if _os.getenv("BINANCE_DEMO", "true").lower() in ("1", "true", "yes"):
        return "DEMO"
    if _os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes"):
        return "TESTNET"
    return "LIVE"


def _notify(message: str, trace_id: str, log) -> None:
    """Kirim Telegram; gagal kirim tak boleh mengganggu loop."""
    try:
        try:
            from src.ops.alerts import send_alert
        except ImportError:
            from ops.alerts import send_alert  # type: ignore[no-redef]
        send_alert(message)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] telegram gagal (%s)", trace_id, exc)


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
        try:  # jurnal tak boleh menggagalkan loop
            _journal_open(trace_id="adopted-exchange", side=action_side,
                          qty=float(ex_qty),
                          entry=float(ex_pos.get("entryPrice") or 0.0),
                          sl=None, tp=None, source="adopted-exchange")
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] journal adopt gagal (%s)", trace_id, exc)
        _notify("ADOPT [%s] posisi exchange %s qty=%s entry=%s (entry baru diblokir)" % (
            _mode_label(), action_side, ex_qty,
            ex_pos.get("entryPrice") if isinstance(ex_pos, dict) else "?"),
            trace_id, log)
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": "adopted exchange position", "qty": 0.0}
    if ex_qty == 0 and st.get("open_position"):
        closed = dict(st["open_position"])
        log.info("[%s] CLOSED_EXTERNALLY posisi %s hilang (SL/TP?), STATE di-clear",
                 trace_id, closed.get("side"))
        st["open_position"] = None
        _save_state(st, state_file)
        exit_px, estimated = 0.0, True
        pnl_est = 0.0
        try:  # jurnal tak boleh menggagalkan loop
            exit_px, estimated = _journal_exit_price(ex_pos, closed)
            _journal_close(trace_id=str(closed.get("trace_id") or trace_id),
                           exit_price=exit_px, reason="sl_or_tp_unknown",
                           fee_paid=0.0, funding_paid=0.0,
                           estimated=estimated)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] journal close gagal (%s)", trace_id, exc)
        try:
            entry_px = float(closed.get("entry") or 0.0)
            qty_c = float(closed.get("qty") or 0.0)
            sign = 1.0 if str(closed.get("side", "")).upper() == "LONG" else -1.0
            pnl_est = (exit_px - entry_px) * qty_c * sign
        except (TypeError, ValueError):
            pnl_est = 0.0
        _notify("CLOSE [%s] %s qty=%s entry=%s exit=%s%s pnl_est=%+.2f USDT reason=SL/TP di exchange" % (
            _mode_label(), closed.get("side"), closed.get("qty"),
            closed.get("entry"), exit_px,
            " (est)" if estimated else "", pnl_est), trace_id, log)

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
        _notify("HALT [%s] %s trading dihentikan, resume manual" % (
            _mode_label(), reason), trace_id, log)
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
    try:  # jurnal tak boleh menggagalkan loop
        setup = sig.get("setup") if isinstance(sig, dict) else None
        _journal_open(trace_id=trace_id, side=action, qty=qty,
                      entry=float(entry), sl=float(sl),
                      tp=float(tp) if tp else None,
                      source=str(setup or "unknown"))
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] journal open gagal (%s)", trace_id, exc)
    log.info("[%s] ORDER %s %s entry=%s sl=%s tp=%s clientOrderId=%s -> %s",
             trace_id, symbol, side, entry, sl, tp, trace_id, res)
    setup = sig.get("setup") if isinstance(sig, dict) else None
    conf = sig.get("confidence") if isinstance(sig, dict) else None
    breaker = sig.get("thesis_breaker") if isinstance(sig, dict) else None
    _notify("ENTRY [%s] %s %s qty=%s entry=%s sl=%s tp=%s setup=%s conf=%s breaker=%s" % (
        _mode_label(), symbol, side, qty, entry, sl, tp, setup, conf, breaker),
        trace_id, log)
    return {"trace_id": trace_id, "action": action, "ordered": True,
            "reason": "ok", "signal": sig, "qty": qty, "side": side,
            "order": res}
