"""24/7 daemon loop: poll interval, WS-stale 10s check, killswitch hook.

Auto-restart is provided by docker-compose `restart: unless-stopped`
(see docker-compose.yml). This loop additionally exits non-zero on
unrecoverable errors so the container restarts cleanly.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("daemon")

POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "5"))
WS_STALE_S = 10.0  # WS-stale check threshold (locked spec)


def check_killswitch(daily_pnl: float, halt_latched: bool) -> bool:
    """Return True when trading must halt: daily -5% or latched STATE."""
    if halt_latched:
        return True
    return daily_pnl <= -0.05


def run_loop(poll_interval: float = POLL_INTERVAL_S,
             max_iters: int | None = None) -> None:
    """Main loop. max_iters is for tests only (None = run forever)."""
    from src.execution.binance_client import BinanceClient  # lazy import
    from src.ops.alerts import send_alert

    client = BinanceClient()  # reads DRY_RUN / testnet env internally
    last_ws_msg_ts = time.time()
    ws_connected = False  # set True once a real WS callback delivers messages
    rest_mode_logged = False
    halt_latched = False
    last_refresh_ts = 0.0
    try:
        refresh_every = float(os.getenv("REFRESH_S", "300"))
    except ValueError:
        refresh_every = 300.0
    it = 0
    logger.info("daemon start poll=%.1fs ws_stale=%.0fs dry_run=%s",
                poll_interval, WS_STALE_S, client.dry_run)
    while True:
        try:
            daily_pnl = client.get_daily_pnl()
            # WS-stale check: in full implementation last_ws_msg_ts is updated
            # by the websocket callback; here we treat >10s without update
            # as stale and force a REST fallback read.
            stale_for = time.time() - last_ws_msg_ts
            if not ws_connected:
                # No WS feed attached yet (Phase-1 stub): REST polling is the
                # primary feed, not a fallback. Log once, keep-alive quietly.
                if not rest_mode_logged:
                    logger.info("WS not connected, using REST polling as primary feed")
                    rest_mode_logged = True
                client.get_position()  # keep-alive REST read
                last_ws_msg_ts = time.time()
                logger.debug("REST keep-alive read")
            elif stale_for > WS_STALE_S:
                logger.warning("WS stale %.1fs > %.0fs, REST fallback read", stale_for, WS_STALE_S)
                client.get_position()  # keep-alive REST read
                last_ws_msg_ts = time.time()
            if check_killswitch(daily_pnl, halt_latched):
                halt_latched = True
                logger.error("KILLSWITCH: halt pnl=%s", daily_pnl)
                send_alert(f"KILLSWITCH halt latched (daily_pnl={daily_pnl})")
                client.cancel_all()
            if os.getenv("ENABLE_STRATEGY", "false").lower() in ("1", "true", "yes"):
                # Strategy paper-trading (testnet). Error di sini tak boleh
                # mematikan daemon: tangkap, log, lanjut iterasi.
                try:
                    if time.time() - last_refresh_ts >= refresh_every:
                        try:
                            try:
                                from src.data.refresh import refresh_gold
                            except ImportError:
                                from data.refresh import refresh_gold  # noqa: E402
                            if refresh_gold() is not None:
                                last_refresh_ts = time.time()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("refresh Gold gagal (%s), pakai Gold lama", exc)
                    try:
                        from src.trading.loop import run_cycle
                    except ImportError:
                        from trading.loop import run_cycle  # noqa: E402
                    try:
                        equity = float(os.getenv("PAPER_EQUITY", "1000"))
                    except ValueError:
                        equity = 1000.0
                    result = run_cycle(client, equity)
                    logger.info("strategy cycle: action=%s ordered=%s reason=%s",
                                result.get("action"), result.get("ordered"),
                                result.get("reason"))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("strategy cycle error (continuing): %s", exc)
            it += 1
            if max_iters is not None and it >= max_iters:
                break
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("daemon stopped by operator")
            break
        except Exception as exc:  # noqa: BLE001 -> exit so docker restarts us
            logger.exception("daemon fatal, exiting for auto-restart: %s", exc)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_loop()
