# GO-LIVE Checklist: Testnet -> Paper -> Live Kecil

## 1. Testnet (wajib lolos semua)
- `pip install -r requirements-test.txt` lalu `pytest -q` (minimal 3 test lolos).
- Set `.env`: `BINANCE_TESTNET=true`, `DRY_RUN=false`, isi testnet key.
- Verifikasi: `set_leverage_isolated(10)` isolated, entry + SL/TP native
  (`STOP_MARKET` + `TAKE_PROFIT_MARKET closePosition=true`), `cancel_all()` bersih,
  idempotency `trace_id` tidak double-fill, halt -5% trigger manual.
- Lengkapi `src/security/audit_checklist.md`.

## 2. Paper 1-2 minggu (`DRY_RUN=true` lawan data live/testnet)
- `.env`: `DRY_RUN=true`, webhook alert opsional (kosong = log saja).
- Jalankan: `docker compose up --build` atau `python -m src.ops.daemon`.
- Review log harian: tidak ada order terkirim, tidak ada secret di log,
  WS-stale 10s check tidak spam, killswitch tidak false-positive.

## 3. Live kecil
- Key baru trade-only (no withdraw) + IP whitelist; `ALLOW_LIVE=true` hanya di host live.
- Rotasi key < 90 hari (`KEY_ROTATED_AT` diisi).
- Notional kecil (respect risk 1%/trade, leverage <= 10 burn-in).
- Monitor 24/7 minggu pertama; siapkan `runbooks/HALT_RESUME.md`.

## Promosi / rollback
- Promosi hanya jika paper 1-2 minggu + testnet hijau.
- Rollback: `cancel_all()` + close posisi + set `DRY_RUN=true` + latch STATE.
