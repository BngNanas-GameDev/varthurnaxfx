"""Debug mentah 1 call OpenRouter (tampilkan respons apa adanya)."""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import llm_review as L

model = os.getenv("LLM_MODEL", "") or os.getenv("OPENROUTER_PRIMARY", "")
key = os.getenv("OPENROUTER_API_KEY", "")
print("model=", model, "key_len=", len(key))
msgs = L._messages("Tes. Balas HANYA JSON {verdict, confidence_mult, reason}.", False)
for use_rf in (True, False):
    try:
        text = L._post(model, key, msgs, use_rf, 25.0)
        print("response_format=", use_rf, "len=", len(text))
        print(text[:600])
        print("parse->", L._strict_parse(text))
        break
    except Exception as exc:  # noqa: BLE001
        print("response_format=", use_rf, "GAGAL", type(exc).__name__, str(exc)[:300])
