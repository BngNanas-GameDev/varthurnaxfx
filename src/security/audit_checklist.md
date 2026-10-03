# Pre-Mainnet Security Audit Checklist

Locked context: BTCUSDT-PERP, isolated 10x, API trade-only (no withdraw) + IP whitelist.

> Do NOT log or paste raw secrets anywhere while working through this list.
> Use `mask_key()` output only.

## 1. API key posture
- [ ] Key created with **Enable Futures** only; **Enable Withdrawals: OFF**
- [ ] `BINANCE_WITHDRAW_ENABLED` env is unset or `false`
- [ ] `validate_permissions()` returns `(True, "ok")`
- [ ] IP whitelist contains only bot host egress IP(s); test from non-whitelisted IP fails
- [ ] `ALLOW_LIVE` is `false` everywhere except the live host env

## 2. Key rotation
- [ ] `KEY_ROTATED_AT` set at issuance; `rotation_reminder()` shows > 14d remaining
- [ ] Previous key disabled/deleted on Binance after rotation
- [ ] Rotation runbook owner + calendar reminder set (90d policy)

## 3. Testnet evidence
- [ ] Testnet suite green: `pytest -q` (>= 3 tests pass)
- [ ] `set_leverage_isolated(10)` verified isolated on testnet UI
- [ ] Entry places native `STOP_MARKET` + `TAKE_PROFIT_MARKET` with `closePosition=true`
- [ ] `cancel_all()` verified; no orphan SL/TP left after close
- [ ] Idempotency: re-sending same `trace_id` does not double-fill
- [ ] Daily -5% halt triggered on purpose at least once on testnet; `STATE` latched

## 4. Paper phase (1-2 minggu)
- [ ] `DRY_RUN=true` logs reviewed, no real orders sent
- [ ] No secret material in logs (`grep -r "BINANCE_API_SECRET" logs/` empty)

## 5. Live-kecil guardrails
- [ ] `configs/risk.json` unchanged from approved (risk 1%/trade, daily -5% halt, lev <= 10) — owned by other worker, verify read-only
- [ ] Single position / single direction enforced (killswitch + `get_position()` check)
- [ ] Daemon `restart unless-stopped`; WS-stale 10s check active; alerts webhook tested
- [ ] `runbooks/HALT_RESUME.md` printed/known; escalation contacts filled in

## Sign-off
| Role | Name | Date | Signature |
|------|------|------|-----------|
| Eng  |      |      |           |
| Risk |      |      |           |

## 6. Preflight (cek mesin) vs manual (dashboard Binance)
Hasil `python scripts/preflight.py` (exit 0 = PASS, 1 = FAIL). Yang TIDAK bisa
dicek mesin HARUS dicek manual di dashboard — jangan skip.

| # | Item | Cara cek |
|---|------|----------|
| M1 | Key Futures-only, Withdrawals OFF | MANUAL dashboard API Management (preflight hanya cek env `BINANCE_WITHDRAW_ENABLED`) |
| M2 | IP whitelist = egress IP host live; akses IP lain ditolak | MANUAL: tes dari IP non-whitelist, catat tanggal |
| M3 | Isolated 10x terlihat di UI posisi | MANUAL di position/margin mode (preflight cek konstanta kode saja) |
| M4 | SL/TP native menempel tiap entry live | MANUAL di open orders (preflight cek `validate_order` menolak tanpa SL) |
| M5 | Key lama di-disable setelah rotasi | MANUAL dashboard (preflight cek `KEY_ROTATED_AT` umur saja) |
| P1 | `.env` ada & key terisi (panjang saja, tanpa nilai) | MESIN: preflight (a) |
| P2 | Tanpa silent-live (DRY_RUN/DEMO/TESTNET/ALLOW_LIVE konsisten) | MESIN: preflight (b) |
| P3 | `configs/risk.json` cocok konstanta kode (lev 10, halt -5%, risk 1%) | MESIN: preflight (c). CATATAN: `leverage_max` harus <= 10 (burn-in); nilai 50 = FAIL sampai direkonsiliasi owner risk.json |
| P4 | Guard menolak tanpa-SL + leverage > 10 | MESIN: preflight (d) |
| P5 | Killswitch halt pada -5% | MESIN: preflight (e) |
| P6 | `pytest -q` hijau | MESIN: CI/lokal (bukti testnet di seksi 3 di atas) |

Aturan: P1-P6 + M1-M5 semua hijau sebelum mainnet. Satu FAIL = berhenti.
