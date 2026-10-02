# Orchestrator — Trading Bot (BTCUSDT-PERP, Binance USDT-M Futures)

Locked scope: Isolated margin, leverage default 10x (50x only after burn-in pass),
daily stop -5% equity (00:00 UTC Binance reset), risk/trade max 1% equity,
1 position 1 direction, native SL+TP required
(STOP_MARKET + TAKE_PROFIT_MARKET, closePosition=true).

## 1. Hierarchy

```text
                    Orchestrator (router.py + killswitch.py)
                    trace_id ledger, dispatch(role), HITL gates, halt
        +---------------+---------------+----------------+--------+------+
        |               |               |                |        |      |
   data (W2)     strategy (W2)   execution (W3)    security (W3)  qa  ops
   WS+REST       LLM signal      Binance only      secrets,       evals  deploy,
   normalize     Signal{...}     SL+TP native      perms          logs   runbooks
```

- Orchestrator decomposes, delegates, synthesizes. It does NOT call Binance directly.
- Subagents return `AgentResult{trace_id,status,payload,cost_ms,evidence}` (see `src/common/schemas.py`).
- Every agent call logs `{trace_id, agent, latency_ms, retry, model, status}`.

## 2. Permission table (least privilege)

| Role / Agent | Binance trade | Binance read | LLM call | Secrets access | File write | Notes |
|---|---|---|---|---|---|---|
| Orchestrator | NO | NO | YES (via router) | NO | ledger only | dispatch + gates + halt |
| data | NO | YES (market data) | NO | NO | cache only | WS staleness >10s -> halt feed |
| strategy | NO | NO | YES (fin model) | NO | NO | emits Signal LONG/SHORT/FLAT |
| risk | NO | NO | YES (fin model) | NO | NO | vetoes sizing/leverage |
| execution | YES (sole caller) | YES | NO (fast path only) | NO (via security) | orders log | must attach native SL+TP |
| security | NO | NO | NO | YES (vault only) | audit log | key rotation, no key passthrough |
| qa / ops | NO | NO | NO | NO | reports | evals, runbooks, deploy |

Only `execution` may call Binance order endpoints. All other agents pass intent
through the Orchestrator envelope (`trace_id` = `clientOrderId` for idempotency).

## 3. Failure -> Fallback -> Escalation

| # | Failure | Detection | Fallback | Escalation |
|---|---|---|---|---|
| F1 | LLM timeout (>25s) / invalid JSON | router retry 2x, schema validation | L1 -> L2 (manual JSON check) -> degraded no-trade (FLAT) | Advisory log; if 3 consecutive degraded, page ops (G2) |
| F2 | Binance -1003 rate limit / IP ban | `rate_limited` flag, CCXT error | killswitch `should_halt` -> halt order flow, exponential backoff | Ops review; manual resume |
| F3 | WS disconnect / stale >10s | `ws_last_update_ts` vs now | halt signal generation, use last snapshot read-only | Auto-reconnect; if >5 min stale, G2 page |
| F4 | Signal conflict (strategy vs risk) | risk veto / confidence < threshold | no-trade (FLAT), keep existing SL+TP | G1 human review if position open |
| F5 | Partial fill | execution order status PARTIALLY_FILLED | reconcile via REST, never double-order (clientOrderId=trace_id) | G2 if exposure != intended >60s |
| F6 | Secret invalid / missing env | security preflight check | halt all trading, degraded mode | G3 incident (rotate keys, manual resume via STATE delete) |
| F7 | Daily PnL <= -5% | equity tracker (00:00 UTC reset) | trip lock file STATE, close-or-protect flow | Manual resume only (G3) |

Lock semantics: `should_halt() -> (bool, reason)`; daily-stop writes `STATE` file.
Trading stays locked until an operator manually resumes (deletes `STATE`) after review.

## 4. HITL gates

- **G1 — Pre-trade review (blocking, sampling):** triggers on low confidence
  (<0.6), strategy/risk disagreement, or leverage >10x request. Pipeline pauses
  for that `trace_id`; approver sees signal, thesis_breaker, SL/TP, sizing math.
  Timeout default: reject (no-trade).
- **G2 — Ops page (advisory -> blocking on repeat):** triggers on repeated
  degraded outputs, WS stale >5 min, partial-fill mismatch, fee ratio
  >0.3 (fee_halt_ratio). First occurrence logs + alerts; repeat within 15 min
  halts new orders.
- **G3 — Incident / kill-switch (blocking, manual resume):** triggers on daily
  stop breach, secret invalid, suspected compromise. Writes `STATE`, halts all
  order flow. Resume requires human incident review + `rm STATE` + restart note
  in runbook.

## 5. LLM routing (env-driven, no hardcoded keys)

- `OPENROUTER_API_KEY`, `OPENROUTER_PRIMARY`, `OPENROUTER_FALLBACK` from env.
- `configs/models.json` pins: L1 `inclusionai/ling-3.0-flash` (response_format),
  L2 `inclusionai/ling-3.1-flash` (tools, no structured_outputs -> manual JSON
  validation), strategy/risk `inclusionai/ling-3.0-flash-fin`, L3 degraded code no-trade.
- `configs/risk.json` pins: symbol BTCUSDT, isolated, lev 10 (max 50),
  daily_stop 5%, risk/trade 1%, max_positions 1, ws_stale 10s, fee_halt 0.3.
