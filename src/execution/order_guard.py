"""Pre-send order guard: validate before any order reaches the exchange.

Returns (ok: bool, reason: str). All rejections must be logged by caller
and must never place an order.
"""

from __future__ import annotations

import math

MAX_LEVERAGE = 10
QTY_STEP = 0.001
DAILY_HALT_PNL = -0.05  # -5% daily halt


def _is_step_valid(qty: float, step: float = QTY_STEP) -> bool:
    # float-safe step check: qty must be a whole multiple of step
    ratio = qty / step
    return qty > 0 and math.isclose(ratio, round(ratio), rel_tol=1e-9, abs_tol=1e-9)


def validate_order(*, side: str, qty: float, price: float, equity: float,
                   leverage: int, sl_price: float | None,
                   daily_pnl: float, halt_triggered: bool = False) -> tuple[bool, str]:
    """Validate a prospective order.

    Args:
        side: BUY or SELL.
        qty: order quantity in BTC.
        price: reference price (mark/limit) in USDT.
        equity: account equity in USDT.
        leverage: requested leverage (burn-in cap 10).
        sl_price: stop-loss price or None (None/<=0 is rejected).
        daily_pnl: realized daily PnL fraction, e.g. -0.05 == -5%.
        halt_triggered: latched killswitch state from STATE.
    """
    s = (side or "").upper()
    if s not in ("BUY", "SELL"):
        return False, f"invalid side {side!r}"
    if halt_triggered:
        return False, "killswitch latched (STATE halt)"
    if daily_pnl <= DAILY_HALT_PNL:
        return False, f"daily halt: pnl {daily_pnl:.4f} <= -5%"
    if leverage > MAX_LEVERAGE:
        return False, f"leverage {leverage} exceeds burn-in max {MAX_LEVERAGE}"
    if leverage < 1:
        return False, f"leverage {leverage} invalid"
    if equity <= 0:
        return False, "equity must be > 0"
    if price <= 0:
        return False, "price must be > 0"
    if not _is_step_valid(qty):
        return False, f"qty {qty} violates step {QTY_STEP}"
    notional = qty * price
    max_notional = equity * leverage
    if notional > max_notional:
        return False, f"notional {notional:.2f} > equity*lev {max_notional:.2f} (over-risk)"
    if not sl_price or sl_price <= 0:
        return False, "SL missing (SL native wajib)"
    # SL must be on protective side of price
    if s == "BUY" and sl_price >= price:
        return False, "SL must be below entry for BUY"
    if s == "SELL" and sl_price <= price:
        return False, "SL must be above entry for SELL"
    return True, "ok"
