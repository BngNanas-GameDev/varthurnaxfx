"""Smoke test LLM review Fin: 1 sinyal LONG sintetis -> CONFIRM/VETO + model."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("LLM_REVIEW", "true")
from src.strategy.llm_review import review

sig = {"action": "LONG", "entry": 84500.0, "sl": 83800.0, "tp": 85900.0,
       "confidence": 0.62, "setup": "TREND_BREAKOUT_H1",
       "thesis": "smoke test", "thesis_breaker": "close < sl"}
market = {"symbol": "BTCUSDT", "atr_pct": 0.005, "funding": 0.00005,
          "rsi": 58.0, "closes": [84000.0, 84100.0, 84250.0, 84380.0, 84500.0]}
out = review(sig, market)
print("verdict=", out.get("verdict"), "mult=", out.get("confidence_mult"))
print("model=", out.get("model"))
print("reason=", out.get("reason"))
print("LLM_SMOKE_OK")
