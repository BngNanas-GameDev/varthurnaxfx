"""Mainnet preflight: 100% READ-ONLY audit gate (no orders, no network, no writes).

Checks:
  (a) .env exists & required vars filled (secrets shown as length ONLY, never values)
  (b) BINANCE_DEMO / BINANCE_TESTNET / DRY_RUN / ALLOW_LIVE consistent
      FAIL when DRY_RUN=false + DEMO=false + TESTNET=false + ALLOW_LIVE unset
      (live without explicit permission)
  (c) configs/risk.json valid & matches code constants imported from modules
      (loop.LEVERAGE, order_guard/loop DAILY_HALT, sizing DEFAULT_RISK_PCT)
  (d) order_guard rejects orders without SL + leverage > 10
  (e) killswitch halts at -5% (pure check_killswitch + order_guard, no STATE writes)

Exit code: 0 = all PASS, 1 = any FAIL (fail-closed).
Read-only: only file/env reads + pure-function calls. Never instantiates
BinanceClient, never calls place_entry/cancel_all, never writes STATE,
never touches the network. Never prints secret material.

Usage:
    python scripts/preflight.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Import constants from modules (never duplicate them here).
from src.execution.order_guard import (  # noqa: E402
    DAILY_HALT_PNL as GUARD_HALT,
)
from src.execution.order_guard import MAX_LEVERAGE as GUARD_MAX_LEV
from src.execution.order_guard import validate_order
from src.ops.daemon import WS_STALE_S  # noqa: E402
from src.ops.daemon import check_killswitch as daemon_killswitch
from src.strategy.sizing import DEFAULT_LEVERAGE as SIZING_LEV  # noqa: E402
from src.strategy.sizing import DEFAULT_RISK_PCT as SIZING_RISK
from src.trading.loop import DAILY_HALT_PNL as LOOP_HALT  # noqa: E402
from src.trading.loop import LEVERAGE as LOOP_LEV
from src.trading.loop import _live_trading_allowed

ENV_FILE = PROJECT_ROOT / ".env"
RISK_FILE = PROJECT_ROOT / "configs" / "risk.json"

SECRET_KEYS = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "OPENROUTER_API_KEY")


def _is_true(v: str | None, default: bool) -> bool:
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes")


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal read-only .env parser (KEY=VALUE, # comments, quotes, export)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")  # -sig: strip BOM Windows
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"').strip()
        if k:
            out[k] = v
    return out


def _describe(name: str, value: str) -> str:
    """Safe display: secrets as length only, flags as values (not secrets)."""
    if name in SECRET_KEYS:
        return "present(len=%d)" % len(value) if value else "(missing)"
    return repr(value) if value else "(unset)"


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ": " + detail)


def check_env_file() -> dict[str, str]:
    print("--- (a) .env presence & required vars (secrets: length only) ---")
    if not ENV_FILE.exists():
        check("env-file", False, ".env NOT FOUND at repo root (copy from .env.example)")
        return {}
    file_vars = _parse_dotenv(ENV_FILE)
    check("env-file", True, ".env found, %d entries parsed" % len(file_vars))
    for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
        check("env-%s" % key, bool(file_vars.get(key)),
              _describe(key, file_vars.get(key, "")))
    for key, default in (("BINANCE_TESTNET", "true"), ("BINANCE_DEMO", "true"),
                         ("DRY_RUN", "true")):
        v = file_vars.get(key, "")
        src = ".env" if key in file_vars else "default(%s)" % default
        print("  [info] %s=%s (%s)" % (key, _describe(key, v), src))
    wd = os.getenv("BINANCE_WITHDRAW_ENABLED", file_vars.get("BINANCE_WITHDRAW_ENABLED", ""))
    check("env-withdraw-disabled", wd.strip().lower() not in ("1", "true", "yes"),
          "BINANCE_WITHDRAW_ENABLED=%s (must stay unset/false, trade-only key)"
          % _describe("BINANCE_WITHDRAW_ENABLED", wd))
    return file_vars


def check_mode_consistency(file_vars: dict[str, str]) -> None:
    print("--- (b) mode consistency DEMO/TESTNET/DRY_RUN/ALLOW_LIVE ---")
    eff = lambda k, d: os.getenv(k, file_vars.get(k, d))  # noqa: E731
    dry = _is_true(eff("DRY_RUN", "true"), True)
    demo = _is_true(eff("BINANCE_DEMO", "true"), True)
    testnet = _is_true(eff("BINANCE_TESTNET", "true"), True)
    allow = _is_true(os.getenv("ALLOW_LIVE", file_vars.get("ALLOW_LIVE", "")), False)
    print("  [info] effective DRY_RUN=%s BINANCE_DEMO=%s BINANCE_TESTNET=%s "
          "ALLOW_LIVE=%s" % (dry, demo, testnet, eff("ALLOW_LIVE", "(unset)")))
    # Core fail-closed rule: live order path without explicit permission.
    live_without_consent = (not dry) and (not demo) and (not testnet) and (not allow)
    check("mode-no-silent-live", not live_without_consent,
          "FAIL-closed: DRY_RUN=false + DEMO=false + TESTNET=false + ALLOW_LIVE "
          "unset means LIVE WITHOUT EXPLICIT CONSENT" if live_without_consent
          else "no silent-live combo (dry=%s demo=%s testnet=%s allow_live=%s)"
          % (dry, demo, testnet, allow))
    # Mirror of _live_trading_allowed() in src/trading/loop.py.
    gate_ok, gate_reason = _live_trading_allowed()
    gate_blocks_live = (not testnet) and (not allow)
    check("mode-live-gate", (not gate_blocks_live) == gate_ok,
          "_live_trading_allowed()=%s (%s)" % (gate_ok, gate_reason))
    if not testnet and not allow:
        check("mode-testnet-or-allow", False,
              "BINANCE_TESTNET=false requires ALLOW_LIVE=true (live explicitly blocked)")
    else:
        check("mode-testnet-or-allow", True,
              "testnet=%s allow_live=%s (live path gated)" % (testnet, allow))


def check_risk_config() -> None:
    print("--- (c) configs/risk.json vs code constants (imported, not duplicated) ---")
    try:
        cfg = json.loads(RISK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        check("risk-valid-json", False, "cannot read/parse configs/risk.json: %s" % exc)
        return
    check("risk-valid-json", isinstance(cfg, dict), "configs/risk.json parses as object")
    if not isinstance(cfg, dict):
        return
    check("risk-leverage-default", cfg.get("leverage_default") == LOOP_LEV,
          "risk.leverage_default=%s vs loop.LEVERAGE=%s"
          % (cfg.get("leverage_default"), LOOP_LEV))
    check("risk-leverage-max-cap", isinstance(cfg.get("leverage_max"), (int, float))
          and cfg["leverage_max"] <= GUARD_MAX_LEV,
          "risk.leverage_max=%s vs order_guard.MAX_LEVERAGE=%s (burn-in cap; "
          "tighten-only: lower risk.json or get risk sign-off)"
          % (cfg.get("leverage_max"), GUARD_MAX_LEV))
    expect_stop = round(abs(LOOP_HALT) * 100, 6)
    check("risk-daily-stop", cfg.get("daily_stop_pct") == expect_stop
          and abs(GUARD_HALT) == abs(LOOP_HALT),
          "risk.daily_stop_pct=%s vs |loop.DAILY_HALT_PNL|*100=%s "
          "(guard=%s loop=%s)" % (cfg.get("daily_stop_pct"), expect_stop,
                                  GUARD_HALT, LOOP_HALT))
    expect_risk = round(SIZING_RISK * 100, 6)
    check("risk-per-trade", cfg.get("risk_per_trade_pct") == expect_risk,
          "risk.risk_per_trade_pct=%s vs sizing.DEFAULT_RISK_PCT*100=%s"
          % (cfg.get("risk_per_trade_pct"), expect_risk))
    check("risk-margin-isolated", str(cfg.get("margin", "")).lower() == "isolated",
          "risk.margin=%r (must be isolated)" % cfg.get("margin"))
    check("risk-single-position", cfg.get("max_positions") == 1,
          "risk.max_positions=%s (must be 1: 1 position 1 direction)"
          % cfg.get("max_positions"))
    check("risk-ws-stale", cfg.get("ws_stale_sec") == WS_STALE_S,
          "risk.ws_stale_sec=%s vs daemon.WS_STALE_S=%s"
          % (cfg.get("ws_stale_sec"), WS_STALE_S))
    check("risk-sizing-lev", SIZING_LEV == LOOP_LEV == cfg.get("leverage_default"),
          "sizing.DEFAULT_LEVERAGE=%s loop.LEVERAGE=%s risk.leverage_default=%s"
          % (SIZING_LEV, LOOP_LEV, cfg.get("leverage_default")))


def check_order_guard() -> None:
    print("--- (d) order_guard: SL mandatory + leverage cap ---")
    base = dict(side="BUY", qty=0.01, price=60000.0, equity=1000.0,
                leverage=LOOP_LEV, daily_pnl=0.0)
    ok, reason = validate_order(sl_price=59000.0, **base)
    check("guard-valid-passes", ok, "sane LONG passes (%s)" % reason)
    ok, reason = validate_order(sl_price=None, **base)
    check("guard-no-sl-rejected", (not ok) and ("SL" in reason),
          "sl=None -> rejected (%s)" % reason)
    ok, reason = validate_order(sl_price=0.0, **base)
    check("guard-zero-sl-rejected", (not ok) and ("SL" in reason),
          "sl=0 -> rejected (%s)" % reason)
    for lev in (GUARD_MAX_LEV + 1, 20):
        args = dict(base, leverage=lev)
        ok, reason = validate_order(sl_price=59000.0, **args)
        check("guard-lev-%d-rejected" % lev, (not ok) and ("leverage" in reason.lower()),
              "leverage=%d (cap %s) -> rejected (%s)" % (lev, GUARD_MAX_LEV, reason))


def check_killswitch_section() -> None:
    print("--- (e) killswitch halts at -5%% (pure, no STATE writes) ---")
    at = LOOP_HALT  # -0.05 imported from loop; no duplicated literal
    check("halt-threshold-equal", GUARD_HALT == LOOP_HALT,
          "order_guard.DAILY_HALT_PNL=%s loop.DAILY_HALT_PNL=%s"
          % (GUARD_HALT, LOOP_HALT))
    check("halt-at-minus-5", daemon_killswitch(at, False) is True,
          "check_killswitch(%.4f, latched=False) is True" % at)
    check("halt-below-minus-5", daemon_killswitch(at - 0.01, False) is True,
          "check_killswitch(%.4f, latched=False) is True" % (at - 0.01))
    check("halt-above-minus-5", daemon_killswitch(at + 0.001, False) is False,
          "check_killswitch(%.4f, latched=False) is False (no false positive)"
          % (at + 0.001))
    check("halt-latched-blocks", daemon_killswitch(0.0, True) is True,
          "check_killswitch(0.0, latched=True) is True (latch persists)")
    ok, reason = validate_order(side="BUY", qty=0.01, price=60000.0,
                                equity=1000.0, leverage=LOOP_LEV,
                                sl_price=59000.0, daily_pnl=at)
    check("halt-guard-at-minus-5", (not ok) and ("halt" in reason.lower()),
          "validate_order(daily_pnl=%s) -> rejected (%s)" % (at, reason))


def main() -> int:
    print("=== PREFLIGHT mainnet readiness (READ-ONLY, no orders/network/writes) ===")
    print("code constants: loop.LEVERAGE=%s guard.MAX_LEVERAGE=%s "
          "halt=%s sizing.risk=%s" % (LOOP_LEV, GUARD_MAX_LEV, LOOP_HALT, SIZING_RISK))
    file_vars = check_env_file()
    check_mode_consistency(file_vars)
    check_risk_config()
    check_order_guard()
    check_killswitch_section()
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print("--- CHECKLIST: %d/%d PASS, %d FAIL ---"
          % (len(results) - n_fail, len(results), n_fail))
    if n_fail:
        print("PREFLIGHT FAIL (%d item): fix above before any mainnet step. "
              "See runbooks/GO_LIVE.md section 5." % n_fail)
        return 1
    print("PREFLIGHT PASS: safe to proceed to runbooks/GO_LIVE.md section 5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
