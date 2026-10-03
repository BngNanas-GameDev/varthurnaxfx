"""Mini WS feed publik Binance Futures (tanpa API key) -> mark price realtime.

Stream gabungan (combined):
    wss://fstream.binance.com/stream?streams=btcusdt@miniTicker/btcusdt@kline_1m

Kontrak:
- MiniFeed: thread background, auto-reconnect backoff, fail-closed.
- on message: update ``last_msg_ts`` + ``last_mark_price``.
- healthy(stale_s=10) -> bool (error apapun -> False agar daemon pakai REST).
- get_mark() -> float | None (default None sebelum ada pesan valid).

Dependensi: ``websocket-client`` (>=1.0, ringan, threaded WebSocketApp).
Import dilakukan lazy di dalam loop sehingga modul tetap importable
offline / tanpa lib (healthy() -> False = REST fallback).
"""

from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger("ws_feed")

WS_URL = ("wss://fstream.binance.com/stream"
          "?streams=btcusdt@miniTicker/btcusdt@kline_1m")

BACKOFF_STEPS: tuple[float, ...] = (2.0, 5.0, 15.0)
BACKOFF_MAX: float = 60.0


def backoff_delay(attempt: int) -> float:
    """Delay reconnect untuk percobaan ke-``attempt`` (0-based).

    Urutan: 2s, 5s, 15s, lalu 60s (max) untuk percobaan berikutnya.
    """
    try:
        i = int(attempt)
    except (TypeError, ValueError):
        return BACKOFF_STEPS[0]
    if i < 0:
        i = 0
    if i < len(BACKOFF_STEPS):
        return BACKOFF_STEPS[i]
    return BACKOFF_MAX


def extract_mark_price(data: dict) -> float | None:
    """Ambil harga mark/close dari payload WS (combined atau single stream).

    Combined: {"stream": ..., "data": {...}} -> gali ke "data".
    - miniTicker: data["c"] (close 24h, proxy mark).
    - kline: data["k"]["c"] (close kline 1m).
    Return float atau None bila tak ada harga valid.
    """
    try:
        if not isinstance(data, dict):
            return None
        d = data.get("data", data) if "stream" in data or "data" in data else data
        if not isinstance(d, dict):
            return None
        k = d.get("k")
        if isinstance(k, dict) and k.get("c") not in (None, ""):
            return float(k["c"])
        if d.get("c") not in (None, ""):
            return float(d["c"])
        return None
    except (TypeError, ValueError):
        return None


class MiniFeed:
    """Feed WS minimal: miniTicker + kline_1m BTCUSDT futures (public, no key).

    Fail-closed: error apapun (no lib, network blokir, parse gagal)
    -> healthy() False agar daemon fallback ke REST polling.
    """

    def __init__(self, url: str = WS_URL) -> None:
        self.url = url
        self.last_msg_ts: float = 0.0
        self.last_mark_price: float | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._ws = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> "MiniFeed":
        """Mulai thread background WS. Idempotent, tak pernah raise."""
        try:
            if self._running and self._thread is not None and self._thread.is_alive():
                return self
            self._running = True
            t = threading.Thread(target=self._run_loop, name="minifeed-ws", daemon=True)
            self._thread = t
            t.start()
        except Exception as exc:  # noqa: BLE001 - fail-closed
            logger.warning("MiniFeed connect gagal (%s), REST fallback", exc)
            self._running = False
        return self

    # alias agar pemakaian fleksibel (daemon/tests)
    start = connect

    def stop(self) -> None:
        """Hentikan loop reconnect + tutup socket (best-effort)."""
        try:
            self._running = False
            ws, self._ws = self._ws, None
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        except Exception:  # noqa: BLE001 - stop tak boleh raise
            pass

    close = stop

    # ------------------------------------------------------------- callbacks
    def on_message(self, ws, message: str) -> None:  # noqa: ARG002
        """Handler pesan WS (signature websocket-client: (ws, message)).

        Boleh dipanggil langsung dengan string JSON (dipakai tests).
        Tak pernah raise: parse gagal -> abaikan pesan.
        """
        try:
            data = json.loads(message) if isinstance(message, str) else message
            px = extract_mark_price(data)
            with self._lock:
                self.last_msg_ts = time.time()
                if px is not None:
                    self.last_mark_price = px
        except Exception as exc:  # noqa: BLE001
            logger.debug("MiniFeed on_message abaikan (%s)", exc)

    def on_error(self, ws, error) -> None:  # noqa: ARG002
        logger.debug("MiniFeed ws error: %s", error)

    def on_close(self, ws, *args) -> None:  # noqa: ARG002
        logger.debug("MiniFeed ws closed")

    # ------------------------------------------------------------- queries
    def healthy(self, stale_s: float = 10.0) -> bool:
        """True bila ada pesan WS dalam ``stale_s`` detik terakhir.

        Fail-closed: error apapun (termasuk clock mock aneh) -> False.
        """
        try:
            ts = float(self.last_msg_ts)
            if ts <= 0:
                return False
            return (time.time() - ts) <= float(stale_s)
        except Exception:  # noqa: BLE001
            return False

    def get_mark(self) -> float | None:
        """Harga mark terakhir, atau None bila belum ada pesan valid."""
        try:
            return self.last_mark_price
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- internals
    def _run_loop(self) -> None:
        """Loop background: connect -> run_forever -> backoff -> retry."""
        try:
            import websocket  # type: ignore  # noqa: WPS433 - lazy, opsional
        except ImportError:
            logger.warning("MiniFeed: 'websocket-client' tak ada -> REST fallback")
            self._running = False
            return
        attempt = 0
        while self._running:
            try:
                app = websocket.WebSocketApp(
                    self.url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                self._ws = app
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 - putus -> backoff + retry
                logger.debug("MiniFeed loop error (%s)", exc)
            finally:
                self._ws = None
            if not self._running:
                break
            delay = backoff_delay(attempt)
            attempt += 1
            try:
                time.sleep(delay)
            except Exception:  # noqa: BLE001
                break
