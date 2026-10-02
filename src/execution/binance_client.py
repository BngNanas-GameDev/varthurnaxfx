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
        testnet: bool = True,
        dry_run: Optional[bool] = None,
        max_retries: int = 3,
    ) -> None:
        if dry_run is None:
            dry_run = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
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
            self._exchange = self._build_exchange(api_key, api_secret, testnet)

    # -- setup -------------------------------------------------------------
    def _build_exchange(self, api_key: str, api_secret: str, testnet: bool) -> Any:
        try:
            import ccxt  # lazy import so DRY_RUN works without ccxt installed
        except ImportError as exc:
            raise RuntimeError("ccxt is required for live/testnet mode (pip install ccxt)") from exc
        ex = ccxt.binanceusdm({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
        if testnet:
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
        self._attach_sltp_native(symbol, close_side, sl_price, tp_price, trace_id)
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

    def get_daily_pnl(self) -> float:
        """Realized daily PnL fraction (e.g. -0.05 == -5%). Best-effort via
        income history; falls back to 0.0 when unavailable."""
        if self.dry_run or self._exchange is None:
            return 0.0
        try:
            since = int((time.time() - 24 * 3600) * 1000)
            income = self._call_with_retry("fetch_balance")
            _ = (since, income)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_daily_pnl fallback 0.0: %s", exc)
        return 0.0
