"""Cek koneksi demo/testnet Binance (read-only, aman): balance + posisi BTCUSDT."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.binance_client import BinanceClient, SYMBOL

client = BinanceClient(dry_run=False)
print("dry_run=", client.dry_run)
bal = client._call_with_retry("fetch_balance")
usdt = (bal or {}).get("USDT", {})
print("USDT free=", usdt.get("free"), "total=", usdt.get("total"))
pos = client.get_position(SYMBOL)
print("position contracts=", pos.get("contracts"), "entry=", pos.get("entryPrice"))
print("DEMO_CHECK_OK")
