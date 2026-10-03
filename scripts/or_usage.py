"""Cek pemakaian OpenRouter (hanya angka agregat, tanpa secret)."""
import json
import os
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

key = os.getenv("OPENROUTER_API_KEY", "")
if not key:
    print("no-api-key")
    sys.exit(2)


def get(path: str):
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1" + path,
        headers={"Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


info = get("/auth/key").get("data", {})
print("label=", info.get("label"))
print("usage_credits=", info.get("usage"), "limit_credits=", info.get("limit"))
print("rate_limit=", json.dumps(info.get("rate_limit")))
try:
    act = get("/generation/activity?daily=true")
    days = act.get("data", [])
    for d in days[-7:]:
        print("day=", d.get("date"), "requests=", d.get("requests"),
              "tokens=", d.get("tokens"), "credits=", d.get("credits"))
except Exception as exc:  # noqa: BLE001
    print("activity-unavailable", type(exc).__name__)
print("OR_USAGE_OK")
