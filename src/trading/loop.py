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
import math
import os
import time
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


def _finite(value, default: float = 0.0) -> float:
    """Float aman: non-numerik / NaN / inf -> ``default``.

    NaN tak boleh bocor ke notif ("PnL nan USDT") atau ke jurnal: data rusak
    diperlakukan sama seperti data tidak ada.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _finite_or_none(value) -> float | None:
    """float finite, atau ``None`` bila data absen/tak valid (NaN, inf, teks)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _resolve_exit(client, closed: dict, ex_pos: dict) -> dict:
    """Exit price + PnL riil untuk posisi yang tertutup di luar STATE.

    Prioritas (fallback beruntun, semua di-try/except):
      1. income history exchange -> realized_pnl/fee/funding (sumber kebenaran)
      2. fetch_my_trades -> exit price eksekusi nyata
      3. markPrice di ex_pos (jarang ada: posisi sudah flat)
      4. hitung dari (entry, sl, tp) -> exit = sl atau tp mana yang realistis

    Return dict {exit_price, pnl, fee, funding, estimated, reason}.
    estimated=False bila berasal dari data exchange nyata. Data NaN/inf/teks
    diperlakukan sebagai "tak ada data" (tak pernah jadi PnL NaN di notif).
    """
    side = str((closed or {}).get("side", "")).upper()
    sign = 1.0 if side == "LONG" else -1.0
    entry_px = _finite((closed or {}).get("entry"))
    qty_c = _finite((closed or {}).get("qty"))
    sl = _finite((closed or {}).get("sl"))
    tp = _finite((closed or {}).get("tp"))
    out = {"exit_price": 0.0, "pnl": 0.0, "fee": 0.0, "funding": 0.0,
           "estimated": True, "reason": "unknown"}

    # 1+2. exchange: realized pnl + exit price nyata.
    # Tanpa opened_ts TIDAK boleh dijumlahkan: default window Binance 7 hari
    # akan mencampur PnL trade lain lalu diklaim "riil" (bug H3).
    _since = _opened_ms(closed)
    try:
        rz = client.fetch_realized(since_ms=_since) if _since else {}
    except Exception:  # noqa: BLE001
        rz = {}
    if not isinstance(rz, dict):
        rz = {}
    rz_pnl = _finite_or_none(rz.get("realized_pnl"))
    if rz_pnl is not None:
        out["pnl"] = rz_pnl
        out["fee"] = abs(_finite(rz.get("fee")))
        out["funding"] = _finite(rz.get("funding"))  # +ekspsi, -dibayar
        out["estimated"] = False
        out["reason"] = "realized"
        ep = _finite(rz.get("exit_price"))
        if ep > 0:
            out["exit_price"] = ep
            out["reason"] = _classify_exit(side, ep, entry_px, sl, tp)
            return out

    # 3. markPrice (fallback)
    exit_px = 0.0
    if isinstance(ex_pos, dict):
        for key in ("markPrice", "mark_price", "mark", "lastPrice", "last_price"):
            v = _finite(ex_pos.get(key), default=-1.0)
            if v > 0:
                exit_px = v
                break
    if exit_px <= 0:
        # 4. exit_price tak diketahui -> pakai SL atau TP yang tercatat
        exit_px = sl if sl > 0 else tp
        # estimated melacak sumber ANGKA PnL: PnL dari income history exchange
        # bukan estimasi walau exit price-nya cuma tebakan level STATE.
        out["estimated"] = rz_pnl is None
    if exit_px <= 0:
        exit_px = entry_px
    out["exit_price"] = exit_px
    if rz_pnl is None:
        out["reason"] = _classify_exit(side, exit_px, entry_px, sl, tp)
        out["pnl"] = (exit_px - entry_px) * qty_c * sign
    # rz_pnl riil tapi exit price hanya tebakan level STATE: reason tetap
    # "realized" -> tak mengklaim SL/TP pasti dari harga yang dikarang.
    return out


def _classify_exit(side: str, exit_px: float, entry_px: float,
                   sl: float, tp: float) -> str:
    """Tentukan SL / TP / unknown dari harga exit vs level di STATE."""
    px = _finite_or_none(exit_px)
    if px is None or px <= 0:
        return "unknown"
    entry = _finite(entry_px)
    sl_px, tp_px = _finite(sl), _finite(tp)
    tol = max(entry * 0.0005, 1e-9)
    if sl_px > 0 and (abs(px - sl_px) <= tol or
                      (side == "LONG" and px <= sl_px) or
                      (side == "SHORT" and px >= sl_px)):
        return "stop-loss"
    if tp_px > 0 and (abs(px - tp_px) <= tol or
                      (side == "LONG" and px >= tp_px) or
                      (side == "SHORT" and px <= tp_px)):
        return "take-profit"
    return "manual/unknown"


def _opened_ms(closed: dict) -> int | None:
    """Timestamp ms buka posisi dari STATE (dipakai fetch_realized since)."""
    try:
        ts = (closed or {}).get("opened_ts")
        return int(ts) if ts else None
    except (TypeError, ValueError):
        return None


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
        _entry_ex = _finite(ex_pos.get("entryPrice"))
        # trace_id unik: "adopted-exchange" konstan menimpa open sebelumnya
        # di dict journal.summarize() sehingga PnL trade salah (bug H4).
        _adopt_id = "adopted-%d-%s" % (int(time.time() * 1000),
                                       int(_entry_ex) if _entry_ex else 0)
        st["open_position"] = {"side": action_side, "qty": float(ex_qty),
                               "entry": _entry_ex if _entry_ex else 0.0,
                               "sl": None, "tp": None,
                               "trace_id": _adopt_id,
                               "adopted": True,
                               "opened_ts": int(time.time() * 1000)}
        _save_state(st, state_file)
        log.info("[%s] ADOPT posisi exchange %s qty=%s, entry baru diblokir",
                 trace_id, action_side, ex_qty)
        try:  # jurnal tak boleh menggagalkan loop
            _journal_open(trace_id=_adopt_id, side=action_side,
                          qty=float(ex_qty),
                          entry=_entry_ex if _entry_ex else 0.0,
                          sl=None, tp=None, source="adopted-exchange")
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] journal adopt gagal (%s)", trace_id, exc)
        _notify("ADOPT [%s] posisi exchange %s qty=%s entry=%s (entry baru diblokir)" % (
            _mode_label(), action_side, ex_qty,
            _entry_ex if _entry_ex else "?"), trace_id, log)
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": "adopted exchange position", "qty": 0.0}
    if ex_qty == 0 and st.get("open_position"):
        closed = dict(st["open_position"])
        log.info("[%s] CLOSED_EXTERNALLY posisi %s hilang (SL/TP?), STATE di-clear",
                 trace_id, closed.get("side"))
        st["open_position"] = None
        _save_state(st, state_file)
        try:  # exit + PnL riil dari exchange; semua fallback di-_resolve_exit
            ex = _resolve_exit(client, closed, ex_pos)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] resolve exit gagal (%s), fallback STATE", trace_id, exc)
            ex = {"exit_price": 0.0, "pnl": 0.0, "fee": 0.0, "funding": 0.0,
                  "estimated": True, "reason": "unknown"}
        exit_px = float(ex.get("exit_price") or 0.0)
        pnl = float(ex.get("pnl") or 0.0)
        fee = float(ex.get("fee") or 0.0)
        funding = float(ex.get("funding") or 0.0)
        estimated = bool(ex.get("estimated", True))
        reason = str(ex.get("reason") or "unknown")
        try:  # jurnal tak boleh menggagalkan loop
            _journal_close(trace_id=str(closed.get("trace_id") or trace_id),
                           exit_price=exit_px, reason=reason,
                           fee_paid=fee, funding_paid=funding,
                           estimated=estimated)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] journal close gagal (%s)", trace_id, exc)
        net = pnl - fee + funding
        _notify(
            "CLOSE [%s] %s qty=%s entry=%s exit=%s%s | PnL %+.2f USDT "
            "(gross %+.2f fee %.2f funding %+.2f net %+.2f) | %s"
            % (_mode_label(), closed.get("side"), closed.get("qty"),
               closed.get("entry"), exit_px, " (est)" if estimated else "",
               pnl, pnl, fee, funding, net, reason), trace_id, log)
        log.info("[%s] CLOSE realized pnl=%.4f fee=%.4f funding=%.4f net=%.4f exit=%.2f reason=%s",
                 trace_id, pnl, fee, funding, net, exit_px, reason)

    # daily pnl: eksplisit (tests) atau dari client.
    # None = "tidak tahu" -> FAIL-CLOSED: rem daily loss tak boleh buta.
    pnl_unknown = False
    if daily_pnl is None:
        try:
            _raw = client.get_daily_pnl()
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] get_daily_pnl gagal (%s)", trace_id, type(exc).__name__)
            _raw = None
        if _raw is None:
            pnl_unknown = True
            daily_pnl = 0.0
        else:
            daily_pnl = float(_raw)
    st["daily_pnl"] = None if pnl_unknown else float(daily_pnl)

    if pnl_unknown:
        _save_state(st, state_file)
        log.error("[%s] BLOCK daily PnL tak terbaca (fail-closed), tanpa order",
                  trace_id)
        return {"trace_id": trace_id, "action": "BLOCKED", "ordered": False,
                "reason": "daily_pnl_unknown: rem daily loss buta, order diblokir",
                "qty": 0.0}

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

    # Arbiter konflik: LONG vs SHORT bersamaan -> Fin memilih satu sisi
    # (atau menolak keduanya). Dedup per bar agar tak panggil LLM tiap 5 detik.
    # LLM OFF / gagal / NEITHER -> tetap NO_TRADE seperti sebelumnya.
    if action not in ("LONG", "SHORT"):
        _cands = sig.get("candidates") or []
        _sides: dict = {}
        for _c in _cands:
            _a = str((_c or {}).get("action", "")).upper()
            if _a in ("LONG", "SHORT") and _a not in _sides:
                _sides[_a] = _c
        if (len(_sides) == 2 and os.getenv("LLM_REVIEW", "false").lower()
                in ("1", "true", "yes")):
            try:
                _bar_ts = int(gold.iloc[-1]["open_time"])
            except (TypeError, ValueError, KeyError, IndexError):
                _bar_ts = 0
            _arb_cache = st.get("arbitration") or {}
            if _arb_cache.get("bar") == _bar_ts and _arb_cache.get("pick") in (
                    "LONG", "SHORT", "NEITHER"):
                _cached = _arb_cache.get("pick")
                if _cached == "NEITHER":
                    log.info("[%s] ARBITER cache bar=%s NEITHER (tanpa HTTP), tanpa order",
                             trace_id, _bar_ts)
                    return {"trace_id": trace_id, "action": "NO_TRADE",
                            "ordered": False,
                            "reason": "arbiter-neither: %s" % (_arb_cache.get("reason") or ""),
                            "signal": sig, "qty": 0.0}
                sig = dict(_arb_cache["signal"])
                action = _cached
                log.info("[%s] ARBITER cache bar=%s pick=%s (tanpa HTTP)",
                         trace_id, _bar_ts, action)
            else:
                try:
                    try:
                        from src.strategy.llm_review import arbitrate as _arbitrate
                    except ImportError:
                        from strategy.llm_review import arbitrate as _arbitrate  # type: ignore[no-redef]
                    _mkt_last = gold.iloc[-1]
                    _mkt = {"symbol": symbol,
                            "atr_pct": (float(_mkt_last.get("atr14") or 0.0)
                                        / float(_mkt_last.get("close") or 0.0))
                            if float(_mkt_last.get("close") or 0.0) > 0 else 0.0,
                            "funding": float(_mkt_last.get("funding_rate") or 0.0),
                            "rsi": float(_mkt_last.get("rsi14") or 0.0),
                            "closes": [float(x) for x in gold["close"].iloc[-5:].tolist()]}
                    _res = _arbitrate(_sides["LONG"], _sides["SHORT"], _mkt)
                    if not isinstance(_res, dict):
                        raise ValueError("arbitrate non-dict")
                    _pick = str(_res.get("pick", "NEITHER")).upper()
                    if _pick in ("LONG", "SHORT"):
                        sig = dict(_sides[_pick])
                        try:
                            _gl = gold.iloc[-1]
                            sig["atr"] = float(_gl.get("atr14") or 0.0)
                            _fr = _gl.get("funding_rate", None)
                            sig["funding_rate"] = float(_fr) if _fr is not None else 0.0
                        except (TypeError, ValueError, KeyError, IndexError):
                            sig["atr"] = sig.get("atr") or 0.0
                            sig["funding_rate"] = sig.get("funding_rate") or 0.0
                        try:
                            _m0 = float(sig.get("confidence") or 0.0)
                        except (TypeError, ValueError):
                            _m0 = 0.0
                        try:
                            _mm = min(max(float(_res.get("confidence_mult", 1.0)), 0.0), 1.0)
                        except (TypeError, ValueError):
                            _mm = 1.0
                        sig["confidence"] = min(_m0, _m0 * _mm)
                        action = _pick
                        st["arbitration"] = {"bar": _bar_ts, "pick": _pick,
                                             "signal": sig,
                                             "reason": str(_res.get("reason") or "")}
                        _save_state(st, state_file)
                        log.info("[%s] ARBITER bar=%s pick=%s (%s)",
                                 trace_id, _bar_ts, _pick, _res.get("reason"))
                        _notify("ARBITER [%s] konflik diputus: %s (%s)" % (
                            _mode_label(), _pick, _res.get("reason")), trace_id, log)
                    else:
                        st["arbitration"] = {"bar": _bar_ts, "pick": "NEITHER",
                                             "reason": str(_res.get("reason") or "")}
                        _save_state(st, state_file)
                        log.info("[%s] ARBITER bar=%s NEITHER (%s), tanpa order",
                                 trace_id, _bar_ts, _res.get("reason"))
                        return {"trace_id": trace_id, "action": "NO_TRADE",
                                "ordered": False,
                                "reason": "arbiter-neither: %s" % (_res.get("reason") or ""),
                                "signal": sig, "qty": 0.0}
                except Exception as exc:  # noqa: BLE001 - fail-closed: NO_TRADE
                    log.warning("[%s] arbiter gagal (%s), tetap NO_TRADE",
                                trace_id, exc)
                    return {"trace_id": trace_id, "action": "NO_TRADE",
                            "ordered": False,
                            "reason": str(sig.get("reason", "no-trade")),
                            "signal": sig, "qty": 0.0}
        if action not in ("LONG", "SHORT"):
            _save_state(st, state_file)
            log.info("[%s] NO_TRADE (%s: %s), tanpa order", trace_id,
                     action, sig.get("reason", sig.get("thesis", "")))
            return {"trace_id": trace_id, "action": action, "ordered": False,
                    "reason": str(sig.get("reason", "no-trade")), "signal": sig, "qty": 0.0}

    # LLM second-opinion: VETO ONLY, fail-closed, default OFF.
    # OFF -> nol network call, perilaku identik seperti sebelumnya.
    # Dedup per bar+sinyal: 1 HTTP call per sinyal unik, siklus berikutnya
    # pakai verdict cache (poll 5 detik tak boleh men-spam LLM).
    if os.getenv("LLM_REVIEW", "false").lower() in ("1", "true", "yes"):
        try:
            _rbar = int(gold.iloc[-1]["open_time"])
        except (TypeError, ValueError, KeyError, IndexError, AttributeError):
            _rbar = 0
        _rkey = "%s:%s:%s" % (action, sig.get("entry"), sig.get("sl"))
        _rc = st.get("review_cache") or {}
        _rev = None
        _from_cache = False
        if (_rc.get("bar") == _rbar and _rc.get("key") == _rkey
                and _rc.get("verdict") in ("VETO", "CONFIRM")):
            _from_cache = True
            _rev = {"verdict": _rc["verdict"],
                    "confidence_mult": _rc.get("mult", 1.0),
                    "reason": _rc.get("reason", "")}
            log.info("[%s] REVIEW cache bar=%s %s (tanpa HTTP)",
                     trace_id, _rbar, _rev["verdict"])
        else:
            _rev = None
            try:
                try:
                    from src.strategy.llm_review import review as _llm_review
                except ImportError:
                    from strategy.llm_review import review as _llm_review  # type: ignore[no-redef]
                _mkt_last = gold.iloc[-1]
                _mkt = {"symbol": symbol,
                        "atr_pct": (float(sig.get("atr") or 0.0)
                                    / float(sig.get("entry") or 0.0))
                        if float(sig.get("entry") or 0.0) > 0 else 0.0,
                        "funding": float(sig.get("funding_rate") or 0.0),
                        "rsi": float(_mkt_last.get("rsi14") or 0.0),
                        "closes": [float(x) for x in gold["close"].iloc[-5:].tolist()]}
                _fresh = _llm_review(sig, _mkt)
                if not isinstance(_fresh, dict):
                    raise ValueError("review non-dict")
                _raw_mult = _fresh.get("confidence_mult", 1.0)
                _mult = min(max(float(_raw_mult if _raw_mult is not None else 1.0),
                                0.0), 1.0)
                _rev = {"verdict": str(_fresh.get("verdict", "CONFIRM")).upper(),
                        "confidence_mult": _mult,
                        "reason": str(_fresh.get("reason") or "")}
                st["review_cache"] = {"bar": _rbar, "key": _rkey,
                                      "verdict": _rev["verdict"], "mult": _mult,
                                      "reason": _rev["reason"]}
                _save_state(st, state_file)
            except Exception as exc:  # noqa: BLE001 - fail-closed: rule-based jalan
                log.warning("[%s] llm-review gagal (%s), lanjut rule-based",
                            trace_id, exc)
                _rev = None
        if _rev is not None and _rev.get("verdict") == "VETO":
            _save_state(st, state_file)
            _reason = "llm-veto: %s" % (_rev.get("reason") or "vetoed")
            log.info("[%s] VETO %s %s (%s), tanpa order",
                     trace_id, symbol, action, _reason)
            if not _from_cache:  # notif hanya sekali per sinyal (anti-spam)
                _notify("VETO [%s] %s %s dibatalkan (%s)" % (
                    _mode_label(), symbol, action, _reason), trace_id, log)
            return {"trace_id": trace_id, "action": "NO_TRADE",
                    "ordered": False, "reason": _reason,
                    "signal": sig, "qty": 0.0}
        if _rev is not None:
            _mult = float(_rev.get("confidence_mult", 1.0))
            if _mult < 1.0:  # hanya boleh menurunkan, tak pernah menaikkan
                try:
                    _conf = float(sig.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    _conf = 0.0
                sig = dict(sig, confidence=min(_conf, _conf * _mult))

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

    _unprotected = False
    res = None
    try:
        res = client.place_entry(side=side, qty=qty, trace_id=trace_id,
                                 sl_price=float(sl),
                                 tp_price=float(tp) if tp else 0.0,
                                 signal_entry=float(entry))
    except Exception as exc:  # noqa: BLE001
        # EntryUnprotectedError = entry SUDAH fill tapi SL/TP gagal attach.
        # Posisi tetap harus dicatat supaya reconcile bisa menutupnya.
        if getattr(exc, "entry_filled", False):
            _unprotected = True
            log.error("[%s] UNPROTECTED %s %s: %s -> STATE dicatat agar exit jalan",
                      trace_id, side, qty, exc)
            _notify("UNPROTECTED [%s] %s %s qty=%s attach SL/TP gagal (%s) - "
                    "PERIKSA MANUAL" % (_mode_label(), symbol, side, qty,
                                        type(exc).__name__), trace_id, log)
        else:
            log.warning("[%s] ORDER_ERROR %s %s (%s)", trace_id, side, qty,
                        type(exc).__name__)
            _save_state(st, state_file)
            return {"trace_id": trace_id, "action": action, "ordered": False,
                    "reason": f"order error: {type(exc).__name__}",
                    "signal": sig, "qty": qty}
    # Level final = yang BENAR-BENAR terpasang di exchange (bisa berbeda dari
    # hasil re-anchor karena harga bergerak lagi antara fill dan attach).
    _fill = _finite((res or {}).get("filled_price")) if isinstance(res, dict) else 0.0
    _sl_final = _finite((res or {}).get("sl_price")) if isinstance(res, dict) else 0.0
    _tp_final = _finite((res or {}).get("tp_price")) if isinstance(res, dict) else 0.0
    if _sl_final <= 0:
        _sl_final = float(sl)
    if _tp_final <= 0:
        _tp_final = float(tp) if tp else 0.0
    _entry_final = _fill if _fill > 0 else float(entry)

    st["open_position"] = {"side": action, "qty": qty, "entry": _entry_final,
                           "sl": _sl_final, "tp": _tp_final or None,
                           "trace_id": trace_id,
                           "opened_ts": int(time.time() * 1000)}
    if _unprotected:
        st["open_position"]["unprotected"] = True
    _save_state(st, state_file)
    try:  # jurnal tak boleh menggagalkan loop
        setup = sig.get("setup") if isinstance(sig, dict) else None
        _journal_open(trace_id=trace_id, side=action, qty=qty,
                      entry=_entry_final, sl=_sl_final,
                      tp=_tp_final or None,
                      source=str(setup or "unknown"))
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] journal open gagal (%s)", trace_id, exc)
    log.info("[%s] ORDER %s %s qty=%s entry=%s sl=%s tp=%s clientOrderId=%s -> %s",
             trace_id, symbol, side, qty, _entry_final, _sl_final, _tp_final,
             trace_id, res)
    setup = sig.get("setup") if isinstance(sig, dict) else None
    conf = sig.get("confidence") if isinstance(sig, dict) else None
    breaker = sig.get("thesis_breaker") if isinstance(sig, dict) else None
    _notify("ENTRY [%s] %s %s qty=%s entry=%s sl=%s tp=%s setup=%s conf=%s breaker=%s" % (
        _mode_label(), symbol, side, qty, _entry_final, _sl_final, _tp_final,
        setup, conf, breaker), trace_id, log)
    return {"trace_id": trace_id, "action": action, "ordered": True,
            "reason": "filled-but-unprotected" if _unprotected else "ok",
            "signal": sig, "qty": qty, "side": side,
            "unprotected": _unprotected, "order": res}
