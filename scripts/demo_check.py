"""Cek koneksi demo/testnet Binance (read-only, aman): balance + posisi BTCUSDT."""
from src.execution.binance_client import BinanceClient, SYMBOL

client = BinanceClient(dry_run=False)
print("dry_run=", client.dry_run)
bal = client._call_with_retry("fetch_balance")
usdt = (bal or {}).get("USDT", {})
print("USDT free=", usdt.get("free"), "total=", usdt.get("total"))
pos = client.get_position(SYMBOL)
print("position contracts=", pos.get("contracts"), "entry=", pos.get("entryPrice"))
print("DEMO_CHECK_OK")
