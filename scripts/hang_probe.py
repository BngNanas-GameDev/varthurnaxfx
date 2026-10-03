"""Probe tiap langkah iterasi daemon (flush tiap baris)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

print("1 import client", flush=True)
from src.execution.binance_client import BinanceClient

print("2 construct", flush=True)
c = BinanceClient(dry_run=False)
print("3 dry_run=%s" % c.dry_run, flush=True)
print("4 get_daily_pnl...", flush=True)
print("pnl=%s" % c.get_daily_pnl(), flush=True)
print("5 get_position...", flush=True)
print("pos=%s" % c.get_position(), flush=True)
print("6 run_cycle...", flush=True)
from src.trading.loop import run_cycle

print("cycle=%s" % run_cycle(c, 1000.0), flush=True)
print("PROBE_DONE", flush=True)
