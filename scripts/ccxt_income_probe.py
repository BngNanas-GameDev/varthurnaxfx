"""Probe kapabilitas ccxt untuk income history (bukti klaim review)."""
import json

import ccxt

e = ccxt.binanceusdm()
names = ["fetch_income", "parse_income", "fetch_my_trades", "fapiPrivateGetIncome"]
print(json.dumps({n: hasattr(e, n) for n in names}, indent=2))
print("has:", [m for m in dir(e) if "income" in m.lower() or "Income" in m])
print("ENDPOINT:", "fapiPrivate" in e.urls["api"], e.urls["api"].get("fapiPrivate"))
