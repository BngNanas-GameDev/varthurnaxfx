"""Killswitch tests: daily -5% halt latches, blocks new orders."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution.order_guard import validate_order  # noqa: E402
from ops.daemon import check_killswitch  # noqa: E402


def _base(**kw):
    args = dict(side="BUY", qty=0.01, price=60000.0, equity=1000.0,
                leverage=10, sl_price=59000.0, daily_pnl=0.0)
    args.update(kw)
    return args


def test_halt_at_minus_five_percent():
    assert check_killswitch(-0.05, False) is True
    assert check_killswitch(-0.051, False) is True
    assert check_killswitch(-0.049, False) is False


def test_latched_halt_blocks_even_if_pnl_recovers():
    assert check_killswitch(0.0, True) is True
    ok, reason = validate_order(**_base(daily_pnl=0.0, halt_triggered=True))
    assert not ok and "killswitch" in reason.lower()


def test_guard_blocks_without_sl():
    ok, reason = validate_order(**_base(sl_price=0.0))
    assert not ok and "SL" in reason


def test_guard_blocks_when_daily_pnl_below_threshold():
    ok, reason = validate_order(**_base(daily_pnl=-0.06))
    assert not ok and "halt" in reason.lower()
