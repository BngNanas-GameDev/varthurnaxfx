"""Daemon killswitch harus tahan terhadap None (PnL tak terbaca).

Regresi nyata: get_daily_pnl() kini mengembalikan None saat tak terbaca
(fail-closed), tapi daemon membandingkan None <= -0.05 -> TypeError ->
container crash-loop, bukan halt.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops.daemon import check_killswitch  # noqa: E402


@pytest.mark.parametrize("pnl", [None, "bukan-angka", object()])
def test_unknown_pnl_halts(pnl):
    """Tak terbaca = halt. Rem daily loss tak boleh buta."""
    assert check_killswitch(pnl, False) is True


def test_zero_pnl_does_not_halt():
    assert check_killswitch(0.0, False) is False


def test_above_threshold_does_not_halt():
    assert check_killswitch(-0.049, False) is False


def test_below_threshold_halts():
    assert check_killswitch(-0.06, False) is True


def test_latched_always_halts():
    assert check_killswitch(0.0, True) is True


def test_daemon_loop_survives_unknown_pnl(tmp_path, monkeypatch):
    """Loop harus jalan (halt), tidak crash saat daily PnL None."""
    from ops import daemon as d

    calls = {"n": 0}

    class Client:
        dry_run = True

        def get_daily_pnl(self):
            return None

        def get_position(self, *a, **kw):
            return {"contracts": 0.0}

        def cancel_all(self, *a, **kw):
            calls["cancel"] = calls.get("cancel", 0) + 1
            return {}

    monkeypatch.setattr(d, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr("src.execution.binance_client.BinanceClient", Client)
    monkeypatch.setattr("src.ops.alerts.send_alert",
                        lambda m: calls.setdefault("alerts", []).append(m))
    d.run_loop(poll_interval=0.0, max_iters=2)          # tak boleh lempar
    assert calls.get("cancel", 0) >= 1                  # order dibatalkan saat halt
    assert any("daily_pnl_unknown" in a for a in calls.get("alerts", []))
