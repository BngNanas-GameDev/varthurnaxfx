"""Binance USDT-M futures client wrapper (ccxt + testnet + DRY_RUN paper mode).

Locked context: BTCUSDT-PERP, isolated 10x, SL+TP native mandatory
(STOP_MARKET + TAKE_PROFIT_MARKET closePosition=true),
idempotency clientOrderId=trace_id, 1 position 1 direction.
API key trade-only (no withdraw) + IP whitelist (enforced server-side,
validated in src/security/keys.py).

DRY_RUN mode: log order without sending. Enable via env DRY_RUN=true
or dry_run=True constructor arg.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

SYMBOL = "BTC/USDT:USDT"  # ccxt unified symbol for BTCUSDT-PERP
DEFAULT_LEVERAGE = 10

# Binance error codes / messages that are safe to retry with backoff.
_RETRYABLE_CODES = {"-1003", "-1015"}
_RETRYABLE_TEXT = ("timeout", "timed out", "connection", "rate limit", "418", "429", "5xx")


class EntryUnprotectedError(RuntimeError):
    """Entry ter-fill tetapi SL/TP gagal attach (posisi sempat telanjang).

    `entry_filled=True` -> pemanggil WAJIB tetap mencatat posisi supaya
    reconcile/exit tetap berjalan, walau order dicatat gagal.
    """

    entry_filled = True


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(t in msg for t in _RETRYABLE_TEXT):
        return True
    for code in _RETRYABLE_CODES:
        if code in str(exc):
            return True
    return False


class BinanceClient:
    """Thin wrapper over ccxt.binanceusdm with retry + idempotency + DRY_RUN."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        max_retries: int = 3,
    ) -> None:
        if dry_run is None:
            dry_run = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
        if testnet is None:
            # Default lama `testnet=True` diam-diam mengabaikan env
            # BINANCE_TESTNET=false, jadi gate ALLOW_LIVE tak pernah aktif.
            testnet = os.getenv("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes")
        self.dry_run = bool(dry_run)
        if not api_key:
            api_key = os.getenv("BINANCE_API_KEY", "")
        if not api_secret:
            api_secret = os.getenv("BINANCE_API_SECRET", "")
        self.testnet = bool(testnet)
        self.max_retries = max(1, int(max_retries))
        self._exchange: Any = None
        if not self.dry_run:
            if not api_key or not api_secret:
                raise RuntimeError("BINANCE_API_KEY/SECRET kosong (isi .env dulu)")
            # Gate live WAJIB di lapisan client, bukan hanya di loop: daemon
            # memakai client langsung (get_position/cancel_all) dan bisa
            # menyentuh mainnet tanpa pernah lewat loop._live_trading_allowed.
            demo = os.getenv("BINANCE_DEMO", "true").lower() in ("1", "true", "yes")
            allow_live = os.getenv("ALLOW_LIVE", "").lower() in ("1", "true", "yes")
            if not (testnet or demo) and not allow_live:
                raise RuntimeError(
                    "live trading diblokir: set ALLOW_LIVE=true eksplisit "
                    "(BINANCE_DEMO=false + BINANCE_TESTNET=false)")
            self._exchange = self._build_exchange(api_key, api_secret, testnet)

    # -- setup -------------------------------------------------------------
    def _build_exchange(self, api_key: str, api_secret: str, testnet: bool) -> Any:
        try:
            import ccxt  # lazy import so DRY_RUN works without ccxt installed
        except ImportError as exc:
            raise RuntimeError("ccxt is required for live/testnet mode (pip install ccxt)") from exc
        ex = ccxt.binanceusdm({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
        demo = os.getenv("BINANCE_DEMO", "true").lower() in ("1", "true", "yes")
        if demo or testnet:
            # Mode non-live: jangan pernah sentuh sapi/mainnet (fetchCurrencies
            # memanggil sapi signed). Pasar fapi di-override ke demo/test di bawah.
            ex.options["fetchCurrencies"] = False
        if demo:
            # Binance mengganti futures-testnet dengan demo trading;
            # tabel urls['demo'] bawaan ccxt (terverifikasi via scripts/ccxt_probe.py).
            demo_urls = (ex.urls or {}).get("demo") or {}
            if demo_urls:
                ex.urls["api"].update(demo_urls)
                return ex
        if testnet:
            # Jalur lama testnet.binancefuture.com (sebagian dibatasi ccxt).
            test_urls = (ex.urls or {}).get("test") or {}
            if test_urls:
                ex.urls["api"].update(test_urls)
            else:
                ex.set_sandbox_mode(True)
        return ex

    # -- retry helper -------------------------------------------------------
    def _call_with_retry(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call exchange method with exp backoff. DO NOT use for market entry
        re-submission without idempotency check (see place_entry)."""
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                fn = getattr(self._exchange, fn_name)
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.max_retries or not _is_retryable(exc):
                    raise
                sleep_s = 2 ** (attempt - 1)
                logger.warning("retry %s attempt %d/%d after %s: %s",
                               fn_name, attempt, self.max_retries, sleep_s, exc)
                time.sleep(sleep_s)
        assert last is not None
        raise last

    # -- leverage -------------------------------------------------------------
    def set_leverage_isolated(self, leverage: int = DEFAULT_LEVERAGE,
                              symbol: str = SYMBOL) -> dict:
        """Set isolated margin + leverage. Burn-in rule: leverage <= 10."""
        if leverage > 10:
            raise ValueError(f"leverage {leverage} exceeds burn-in max 10")
        if self.dry_run:
            logger.info("[DRY_RUN] set_leverage_isolated lev=%d symbol=%s margin=ISOLATED",
                        leverage, symbol)
            return {"dry_run": True, "leverage": leverage,
                    "marginMode": "ISOLATED", "symbol": symbol}
        # fapiPrivatePostLeverageType equivalent via ccxt options
        self._call_with_retry("set_margin_mode", "isolated", symbol)
        return self._call_with_retry("set_leverage", leverage, symbol)

    # -- orders ---------------------------------------------------------------
    def place_entry(self, side: str, qty: float, entry_type: str = "market",
                    trace_id: str = "", sl_price: float = 0.0,
                    tp_price: float = 0.0, symbol: str = SYMBOL) -> dict:
        """Place entry + attach native SL/TP (STOP_MARKET + TAKE_PROFIT_MARKET,
        closePosition=true). trace_id is used as clientOrderId (idempotency).

        Market-order retry policy: NEVER blindly re-send a market order.
        On retryable error we first check for an existing order/position with
        the same clientOrderId before re-submitting.
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if qty <= 0:
            raise ValueError("qty must be > 0")
        if not trace_id:
            raise ValueError("trace_id (clientOrderId) is required for idempotency")
        if not sl_price or sl_price <= 0:
            raise ValueError("sl_price is mandatory (SL+TP native wajib)")
        if entry_type not in ("market", "limit"):
            raise ValueError("entry_type must be market or limit")

        params = {"newClientOrderId": trace_id}
        close_side = "SELL" if side == "BUY" else "BUY"

        if self.dry_run:
            logger.info("[DRY_RUN] entry %s %s qty=%s type=%s clientOrderId=%s sl=%s tp=%s",
                        symbol, side, qty, entry_type, trace_id, sl_price, tp_price)
            logger.info("[DRY_RUN] protective STOP_MARKET %s closePosition=true stopPrice=%s",
                        close_side, sl_price)
            if tp_price:
                logger.info("[DRY_RUN] protective TAKE_PROFIT_MARKET %s closePosition=true stopPrice=%s",
                            close_side, tp_price)
            return {"dry_run": True, "clientOrderId": trace_id, "side": side,
                    "qty": qty, "sl_price": sl_price, "tp_price": tp_price}

        order_type = "market" if entry_type == "market" else "limit"
        attempt = 0
        while True:
            attempt += 1
            try:
                entry = self._exchange.create_order(symbol, order_type, side.lower(),
                                                    qty, None, params)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries or not _is_retryable(exc):
                    raise
                logger.warning("entry failed (attempt %d), idempotency check before retry: %s",
                               attempt, exc)
                existing = self.find_order_by_client_id(trace_id, symbol)
                if existing:
                    logger.info("idempotency hit: order %s already exists, skip resend",
                                trace_id)
                    entry = existing
                    break
                time.sleep(2 ** (attempt - 1))
                # loop retries the create_order once after idempotency check
        # Attach native protective orders (reduce-only via closePosition).
        # Entry SUDAH fill di titik ini: bila attach gagal, posisi telanjang
        # tanpa SL = risiko tak terbatas. Fail-closed: cancel semua order
        # proteksi yang sempat terpasang, flatten paksa, lalu naikkan error
        # dengan flag entry_filled supaya pemanggil tetap mencatat posisi.
        try:
            self._attach_sltp_native(symbol, close_side, sl_price, tp_price, trace_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("ATTACH SL/TP GAGAL setelah entry fill (%s) -> flatten paksa",
                         type(exc).__name__)
            try:
                self._call_with_retry("cancel_all_orders", symbol)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._exchange.create_order(symbol, "MARKET", close_side.lower(),
                                            None, None,
                                            {"reduceOnly": True,
                                             "newClientOrderId": f"{trace_id}-FLAT"})
                logger.error("posisi di-flatten paksa: %s", trace_id)
            except Exception as flat_exc:  # noqa: BLE001
                logger.error("flatten GAGAL (%s) - PERIKSA MANUAL! trace=%s",
                             type(flat_exc).__name__, trace_id)
            raise EntryUnprotectedError(
                f"entry filled tanpa proteksi, di-flatten: {exc}") from exc
        return entry

    def _attach_sltp_native(self, symbol: str, close_side: str,
                            sl_price: float, tp_price: float, trace_id: str) -> None:
        sl_params = {"stopPrice": sl_price, "closePosition": True,
                     "newClientOrderId": f"{trace_id}-SL"}
        self._call_with_retry("create_order", symbol, "STOP_MARKET",
                              close_side.lower(), None, None, sl_params)
        if tp_price and tp_price > 0:
            tp_params = {"stopPrice": tp_price, "closePosition": True,
                         "newClientOrderId": f"{trace_id}-TP"}
            self._call_with_retry("create_order", symbol, "TAKE_PROFIT_MARKET",
                                  close_side.lower(), None, None, tp_params)

    def find_order_by_client_id(self, client_id: str, symbol: str = SYMBOL) -> Optional[dict]:
        """Idempotency lookup: search open + recent orders for clientOrderId."""
        if self.dry_run or self._exchange is None:
            return None
        try:
            for fn in ("fetch_open_orders", "fetch_closed_orders"):
                try:
                    orders = self._call_with_retry(fn, symbol)
                except Exception:  # noqa: BLE001
                    continue
                for o in orders or []:
                    if o.get("clientOrderId") == client_id or o.get("info", {}).get("clientOrderId") == client_id:
                        return o
        except Exception as exc:  # noqa: BLE001
            logger.warning("idempotency lookup failed: %s", exc)
        return None

    def _fetch_income_rows(self, symbol: str, start_ms: int,
                           end_ms: int) -> list:
        """Income history Binance via endpoint implisit ccxt.

        PENTING: ccxt 4.5.x TIDAK punya `fetch_income` untuk Binance futures
        (diverifikasi scripts/ccxt_income_probe.py: hasattr == False).
        Yang tersedia: `fapiPrivateGetIncome` + `parse_income`.
        """
        ex = self._exchange
        raw = self._call_with_retry(
            "fapiPrivateGetIncome",
            {"symbol": symbol, "startTime": int(start_ms),
             "endTime": int(end_ms), "limit": 1000})
        items = []
        if isinstance(raw, dict):
            items = raw.get("income") or raw.get("rows") or []
        elif isinstance(raw, list):
            items = raw
        rows = []
        for it in items:
            try:
                rows.append(ex.parse_income(it))
            except Exception:  # noqa: BLE001 - item rusak -> skip
                continue
        return rows

    def fetch_realized(self, symbol: str = SYMBOL, since_ms: int | None = None) -> dict:
        """Realized PnL + fee + funding riil dari income history Binance.

        Sumber kebenaran untuk notif CLOSE (markPrice tak ada saat posisi sudah
        flat, sehingga exit price harus diambil dari eksekusi nyata).

        FAIL-CLOSED: tanpa `since_ms` TIDAK dijumlahkan — default window
        Binance adalah 7 hari sehingga PnL trade lain ikut tercampur dan
        angka itu akan diklaim "riil" padahal bukan milik posisi ini.
        """
        out = {"realized_pnl": None, "fee": None, "funding": None,
               "exit_price": None, "exit_ts": None}
        if self.dry_run or self._exchange is None:
            return out
        if not since_ms:
            logger.warning("fetch_realized tanpa since_ms -> ditolak "
                           "(menghindari pencampuran PnL trade lain)")
            return out
        now_ms = int(time.time() * 1000)
        try:
            rows = self._fetch_income_rows(symbol, int(since_ms), now_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_realized gagal: %s", type(exc).__name__)
            return out
        realized = fee = funding = 0.0
        for r in rows:
            info = r.get("info", {}) if isinstance(r, dict) else {}
            i_type = str(info.get("incomeType") or r.get("type") or "")
            amt = r.get("amount", info.get("income"))
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                continue
            if i_type == "REALIZED_PNL":
                realized += amt
            elif i_type == "COMMISSION":
                fee = abs(fee) + abs(amt)   # Binance tulis fee negatif
            elif i_type == "FUNDING_FEE":
                funding += amt             # funding: +ekspsi, -dibayar
        exit_price = None
        exit_ts = None
        # exit price: trade terakhir dari window (harga eksekusi nyata)
        try:
            trades = self._call_with_retry(
                "fetch_my_trades", symbol, int(since_ms)) or []
            if trades:
                last = trades[-1]
                exit_price = float(last.get("price") or 0.0) or None
                exit_ts = last.get("timestamp")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_my_trades gagal: %s", type(exc).__name__)
        out.update({"realized_pnl": realized, "fee": fee, "funding": funding,
                    "exit_price": exit_price, "exit_ts": exit_ts})
        return out

    def cancel_all(self, symbol: str = SYMBOL) -> dict:
        if self.dry_run:
            logger.info("[DRY_RUN] cancel_all %s", symbol)
            return {"dry_run": True, "symbol": symbol}
        return self._call_with_retry("cancel_all_orders", symbol)

    # -- reads ------------------------------------------------------------------
    def get_position(self, symbol: str = SYMBOL) -> dict:
        """Return current isolated position for symbol (qty signed, entryPx, lev)."""
        if self.dry_run:
            return {"dry_run": True, "symbol": symbol, "contracts": 0.0,
                    "entryPrice": 0.0, "leverage": DEFAULT_LEVERAGE}
        positions = self._call_with_retry("fetch_positions", [symbol])
        for p in positions or []:
            if p.get("symbol") == symbol:
                return p
        return {"symbol": symbol, "contracts": 0.0}

    def get_daily_pnl(self) -> Optional[float]:
        """Realized daily PnL sebagai FRAKSI equity (mis. -0.05 == -5%).

        Fail-closed: None bila tak bisa dihitung. Nilai 0.0 hanya berarti
        "belum rugi hari ini"; None berarti "tidak tahu" dan pemanggil
        WAJIB memblokir order (rem daily loss tak boleh buta).
        """
        if self.dry_run or self._exchange is None:
            return 0.0
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3600 * 1000
        realized = 0.0
        try:
            for r in self._fetch_income_rows(SYMBOL, start_ms, now_ms):
                info = r.get("info", {}) if isinstance(r, dict) else {}
                if str(info.get("incomeType") or r.get("type") or "") != "REALIZED_PNL":
                    continue
                try:
                    realized += float(r.get("amount", info.get("income")))
                except (TypeError, ValueError):
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_daily_pnl tak terbaca: %s", type(exc).__name__)
            return None
        try:
            bal = self._call_with_retry("fetch_balance") or {}
            usdt = bal.get("USDT") or bal.get("total") or {}
            equity = float(usdt.get("total") or 0.0)
            if equity <= 0:
                # fallback: total seluruh currency
                equity = float(bal.get("total") or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_daily_pnl balance gagal: %s", type(exc).__name__)
            return None
        if equity <= 0:
            logger.warning("get_daily_pnl equity 0 -> None")
            return None
        return realized / equity
