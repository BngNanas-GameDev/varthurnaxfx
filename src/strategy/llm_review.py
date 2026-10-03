"""LLM second-opinion: VETO ONLY atas sinyal LONG/SHORT (worker-3).

Contract (locked, fail-closed):
- Env LLM_REVIEW=false default (OFF). OFF -> review() -> CONFIRM tanpa network.
- Hanya boleh membatalkan/melemahkan: VETO -> loop ubah jadi NO_TRADE;
  confidence_mult hanya menurunkan (loop: min(conf, conf*mult)).
- review() tak pernah membuat order / menaikkan size / raise: semua
  exception, timeout, JSON tak valid -> CONFIRM (rule-based tetap jalan).
- Tanpa retry: 1x L1, 1x fallback, lalu CONFIRM. Total budget 25 detik.
- Payload minimal (tanpa secret). Tidak ada log berisi key / URL berkey.

Tidak memakai orchestrator.router (ia butuh httpx + retry 2x yang melanggar
budget veto); HTTP langsung via stdlib urllib agar tanpa dependensi baru.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PER_ATTEMPT_TIMEOUT_S = 20.0
TOTAL_BUDGET_S = 25.0
DEFAULT_PRIMARY = "inclusionai/ling-3.0-flash"
DEFAULT_FALLBACK = "inclusionai/ling-3.1-flash"

_ON = ("1", "true", "yes")

_SYSTEM = (
    "Kamu filter risiko second-opinion (VETO ONLY) untuk sinyal futures "
    "BTCUSDT-PERP. Tugasmu HANYA membatalkan sinyal berbahaya atau menurunkan "
    "confidence. Balas HANYA satu objek JSON valid: "
    '{"verdict": "CONFIRM"|"VETO", "confidence_mult": <0..1>, '
    '"reason": "<singkat>"}. VETO hanya bila data jelas bertentangan '
    "(mis. funding ekstrem melawan arah, risk/reward rusak, volatilitas mati). "
    "Bila ragu -> CONFIRM dengan confidence_mult 1.0."
)


def is_enabled() -> bool:
    """True hanya bila env LLM_REVIEW eksplisit on. Default OFF."""
    return os.getenv("LLM_REVIEW", "false").lower() in _ON


def _resolve_models() -> tuple[str, str]:
    primary = (os.getenv("LLM_MODEL", "").strip()
               or os.getenv("OPENROUTER_PRIMARY", "").strip()
               or DEFAULT_PRIMARY)
    fallback = (os.getenv("OPENROUTER_FALLBACK", "").strip()
                or DEFAULT_FALLBACK)
    return primary, fallback


def _out(verdict: str, mult: float, reason: str, model: str) -> dict:
    return {"verdict": verdict, "confidence_mult": mult,
            "reason": reason, "model": model}


def _confirm(reason: str, model: str) -> dict:
    return _out("CONFIRM", 1.0, reason, model)


def _num(x, default: float = 0.0) -> float:
    """Float aman: non-numerik / NaN / inf -> default (payload tak pernah NaN)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _compact(signal: dict, market: dict) -> dict:
    """Payload minimal untuk LLM. Tak pernah berisi key/secret."""
    s, m = signal or {}, market or {}
    try:
        closes = [round(_num(c), 1) for c in list(m.get("closes") or [])[-5:]]
    except (TypeError, ValueError):
        closes = []
    entry = _num(s.get("entry"))
    return {
        "symbol": str(m.get("symbol") or "BTCUSDT-PERP"),
        "action": str(s.get("action") or ""),
        "entry": entry,
        "sl": _num(s.get("sl")),
        "tp": _num(s.get("tp")),
        "atr_pct": _num(m.get("atr_pct")),
        "funding": _num(m.get("funding")),
        "rsi": _num(m.get("rsi")),
        "closes": closes,
    }


def _messages(prompt: str, force_json: bool) -> list:
    user = prompt if not force_json else prompt + "\nBalas HANYA objek JSON valid."
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]


def _post(model: str, api_key: str, messages: list,
          use_response_format: bool, timeout_s: float) -> str:
    """Satu HTTP call ke OpenRouter. Raise untuk semua kegagalan."""
    body: dict = {"model": model, "messages": messages}
    if use_response_format:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty content")
    return content


def _strict_parse(text: str) -> dict:
    """Parse ketat: apapun yang tak valid -> ValueError -> CONFIRM.

    confidence_mult dijepit ke [0, 1] agar tak pernah bisa menaikkan size.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON harus objek")
    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in ("CONFIRM", "VETO"):
        raise ValueError(f"verdict tak dikenal: {verdict!r}")
    raw = data.get("confidence_mult", 1.0)
    mult = 1.0 if raw is None else float(raw)  # float() raise -> invalid
    if not math.isfinite(mult):
        raise ValueError("confidence_mult tak finite")
    mult = min(max(mult, 0.0), 1.0)
    return _out(verdict, mult, str(data.get("reason") or "")[:500], "")


def _timeout(deadline: float) -> float:
    return max(0.5, min(PER_ATTEMPT_TIMEOUT_S, deadline - time.monotonic()))


def review(signal: dict, market: dict) -> dict:
    """Second-opinion VETO-ONLY. Tak pernah raise; gagal -> CONFIRM.

    Return: {verdict: CONFIRM|VETO, confidence_mult: 0..1, reason, model}.
    """
    primary, fallback = _resolve_models()
    if not is_enabled():
        return _confirm("llm-review-off", "off")
    try:
        action = str((signal or {}).get("action", "")).upper()
    except Exception:  # noqa: BLE001
        action = ""
    if action not in ("LONG", "SHORT"):
        return _confirm("not-applicable", primary)
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return _confirm("no-api-key", primary)
    try:
        prompt = ("Sinyal rule-based (JSON): " + json.dumps(_compact(signal, market))
                  + ". Balas HANYA JSON {verdict, confidence_mult, reason}.")
    except Exception:  # noqa: BLE001
        return _confirm("bad-input", primary)

    deadline = time.monotonic() + TOTAL_BUDGET_S
    # L1: model utama, structured output.
    try:
        text = _post(primary, api_key, _messages(prompt, False),
                     True, _timeout(deadline))
        res = _strict_parse(text)
        res["model"] = primary
        return res
    except Exception as exc:  # noqa: BLE001 - fall through ke L2
        logger.warning("llm-review L1 gagal (%s)", type(exc).__name__)
    # L2: fallback sekali, tanpa response_format.
    if deadline - time.monotonic() <= 0:
        return _confirm("budget-exceeded", fallback)
    try:
        text = _post(fallback, api_key, _messages(prompt, True),
                     False, _timeout(deadline))
        res = _strict_parse(text)
        res["model"] = fallback
        return res
    except Exception as exc:  # noqa: BLE001 - fail-closed
        logger.warning("llm-review L2 gagal (%s)", type(exc).__name__)
        return _confirm("all-tiers-failed", fallback)
