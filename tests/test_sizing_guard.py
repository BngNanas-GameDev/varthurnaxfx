"""Sizing / order-guard tests: never over-risk, SL mandatory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution.order_guard import validate_order  # noqa: E402

PRICE = 60000.0
EQUITY = 1000.0


def test_valid_long_passes():
    ok, reason = validate_order(side="BUY", qty=0.01, price=PRICE, equity=EQUITY,
                                leverage=10, sl_price=59000.0, daily_pnl=0.0)
    assert ok, reason


def test_over_risk_blocked():
    # notional = 1.0 * 60000 = 60000 > equity*lev = 10000
    ok, reason = validate_order(side="BUY", qty=1.0, price=PRICE, equity=EQUITY,
                                leverage=10, sl_price=59000.0, daily_pnl=0.0)
    assert not ok and "over-risk" in reason


def test_leverage_above_burnin_blocked():
    ok, reason = validate_order(side="BUY", qty=0.01, price=PRICE, equity=EQUITY,
                                leverage=20, sl_price=59000.0, daily_pnl=0.0)
    assert not ok and "leverage" in reason


def test_qty_step_enforced():
    ok, reason = validate_order(side="BUY", qty=0.0105, price=PRICE, equity=EQUITY,
                                leverage=10, sl_price=59000.0, daily_pnl=0.0)
    assert not ok and "step" in reason


def test_missing_sl_blocked():
    ok, reason = validate_order(side="BUY", qty=0.01, price=PRICE, equity=EQUITY,
                                leverage=10, sl_price=None, daily_pnl=0.0)
    assert not ok and "SL" in reason
