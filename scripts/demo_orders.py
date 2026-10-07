"""Cek order proteksi di exchange (read-only): SL/TP native benar-benar ada?"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.binance_client import BinanceClient, SYMBOL  # noqa: E402

c = BinanceClient(dry_run=False)
pos = c.get_position(SYMBOL)
print("POSITION contracts=%s entry=%s mark=%s liq=%s"
      % (pos.get("contracts"), pos.get("entryPrice"),
         pos.get("markPrice"), pos.get("liquidationPrice")))
print("POSITION stopLossPrice=%s takeProfitPrice=%s"
      % (pos.get("stopLossPrice"), pos.get("takeProfitPrice")))

orders = c._call_with_retry("fetch_open_orders", SYMBOL)
print("OPEN ORDERS: %d" % len(orders or []))
for o in orders or []:
    print("  %-18s %-16s %-8s amt=%s stop=%s px=%s reduceOnly=%s closePos=%s"
          % (o.get("clientOrderId"), o.get("type"), o.get("side"),
             o.get("amount"), (o.get("info") or {}).get("stopPrice"),
             o.get("price"),
             (o.get("info") or {}).get("reduceOnly"),
             (o.get("info") or {}).get("closePosition")))

closed = c._call_with_retry("fetch_closed_orders", SYMBOL)
print("CLOSED ORDERS (5 terakhir):")
for o in (closed or [])[-5:]:
    print("  %-18s %-16s %-8s status=%s avg=%s"
          % (o.get("clientOrderId"), o.get("type"), o.get("side"),
             o.get("status"), o.get("average")))
print("DEMO_ORDERS_OK")
