"""LLM router: Orchestrator.dispatch(role) with L1 -> L2 -> degraded fallback.

Contract (locked):
- L1: inclusionai/ling-3.0-flash (paid, response_format structured outputs)
- L2 fallback: inclusionai/ling-3.1-flash (25B active, tools but NO structured_outputs)
- L3 degraded: deterministic code path, no-trade
- Retry LLM max 2x, timeout 25s per attempt.
- Keys/models from env only, never hardcoded.
- Structured logging: trace_id, agent(role), latency, retry, model, status.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

import httpx

logger = logging.getLogger("orchestrator.router")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 25
MAX_RETRIES = 2  # per model tier; then fall through to next tier

ROLE_MODEL_MAP = {
    "strategy": "STRATEGY_MODEL",
    "risk": "RISK_MODEL",
}

DEGRADED_NO_TRADE = {
    "action": "FLAT",
    "reason": "degraded_no_trade",
    "confidence": 0.0,
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _resolve_models() -> tuple[str, str]:
    primary = _env("OPENROUTER_PRIMARY", "inclusionai/ling-3.0-flash")
    fallback = _env("OPENROUTER_FALLBACK", "inclusionai/ling-3.1-flash")
    return primary, fallback


def _log(trace_id: str, agent: str, latency_ms: int, retry: int, model: str, status: str) -> None:
    logger.info(
        json.dumps(
            {
                "trace_id": trace_id,
                "agent": agent,
                "latency_ms": latency_ms,
                "retry": retry,
                "model": model,
                "status": status,
            }
        )
    )


def _validate_json(text: str) -> dict:
    """Validate that LLM output is parseable JSON. Raises ValueError otherwise."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM output JSON must be an object")
    return data


def _call_openrouter(model: str, messages: list, use_response_format: bool) -> str:
    api_key = _env("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY env missing")
    body: dict = {"model": model, "messages": messages}
    if use_response_format:
        body["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        payload = resp.json()
        return payload["choices"][0]["message"]["content"]


class Orchestrator:
    """Routes a role-task to the LLM chain with retries + fallback."""

    def __init__(self, primary: str = "", fallback: str = "") -> None:
        p, f = _resolve_models()
        self.primary = primary or p
        self.fallback = fallback or f

    def dispatch(self, role: str, prompt: str, trace_id: str = "") -> dict:
        """Dispatch prompt for `role`; returns dict with keys: output, model, status, retries.

        Fallback chain: L1 (response_format) -> L2 (no structured output,
        manual JSON validation) -> degraded_no_trade.
        """
        tid = trace_id or uuid.uuid4().hex
        # Tier 1: L1 with response_format
        for attempt in range(MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                text = _call_openrouter(
                    self.primary,
                    [{"role": "user", "content": prompt}],
                    use_response_format=True,
                )
                out = _validate_json(text)
                _log(tid, role, int((time.monotonic() - start) * 1000), attempt, self.primary, "ok")
                return {"output": out, "model": self.primary, "status": "ok", "retries": attempt}
            except Exception as exc:  # noqa: BLE001 - must fall through to L2
                _log(tid, role, int((time.monotonic() - start) * 1000), attempt, self.primary, f"error:{type(exc).__name__}")
                if attempt >= MAX_RETRIES:
                    break
        # Tier 2: L2 without structured outputs (manual JSON validation)
        for attempt in range(MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                text = _call_openrouter(
                    self.fallback,
                    [{"role": "user", "content": prompt + "\nReturn ONLY valid JSON object."}],
                    use_response_format=False,
                )
                out = _validate_json(text)
                _log(tid, role, int((time.monotonic() - start) * 1000), attempt, self.fallback, "ok_fallback")
                return {"output": out, "model": self.fallback, "status": "ok_fallback", "retries": attempt}
            except Exception as exc:  # noqa: BLE001
                _log(tid, role, int((time.monotonic() - start) * 1000), attempt, self.fallback, f"error:{type(exc).__name__}")
                if attempt >= MAX_RETRIES:
                    break
        # Tier 3: degraded deterministic no-trade
        _log(tid, role, 0, 0, "degraded_code", "degraded_no_trade")
        return {"output": dict(DEGRADED_NO_TRADE), "model": "degraded_code", "status": "degraded_no_trade", "retries": 0}
