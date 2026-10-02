"""Shared pydantic contracts for orchestrator <-> subagents <-> execution.

Scope: worker-1 (orchestrator). Other workers must import, not redefine.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """Inbound work unit routed by the Orchestrator."""

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str = Field(default="BTCUSDT-PERP", description="Binance USDT-M Futures perp symbol")
    timeframe: str = Field(default="15m")
    intent: str = Field(description="What the pipeline should do, e.g. evaluate_signal, execute_order")


class AgentStatus(str, Enum):
    OK = "ok"
    RETRY = "retry"
    FAILED = "failed"
    DEGRADED = "degraded"
    HALTED = "halted"


class AgentResult(BaseModel):
    """Structured output every subagent must return."""

    trace_id: str
    status: AgentStatus = AgentStatus.OK
    payload: Dict[str, Any] = Field(default_factory=dict)
    cost_ms: int = Field(default=0, ge=0)
    evidence: List[str] = Field(default_factory=list)


class SignalAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Signal(BaseModel):
    """Trading signal produced by strategy, validated by risk."""

    action: SignalAction
    entry: Optional[float] = Field(default=None, gt=0)
    sl: Optional[float] = Field(default=None, gt=0)
    tp: Optional[float] = Field(default=None, gt=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    thesis_breaker: str = Field(default="", description="Condition that invalidates the thesis")


class OrderRequest(BaseModel):
    """Order intent. clientOrderId MUST equal trace_id for idempotency."""

    trace_id: str
    symbol: str = "BTCUSDT-PERP"
    side: Literal["BUY", "SELL"] = "BUY"
    quantity: float = Field(gt=0)
    leverage: int = Field(default=10, ge=1, le=50)
    clientOrderId: str = ""

    def model_post_init(self, __context: Any) -> None:
        # clientOrderId defaults to trace_id (idempotency key).
        if not self.clientOrderId:
            object.__setattr__(self, "clientOrderId", self.trace_id)
