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

## 4. PAPER TESTNET (validasi kering tanpa key)
- Validasi paper end-to-end tanpa API key (public REST saja):
  1. `python -m src.data.live_feed --interval 1h --limit 200 --save`
     (cek `fetched=N bars=N` + file `data/bronze/klines_1h_<ts>.jsonl`).
  2. `python scripts/paper_validate.py` (atau `--synthetic` bila network diblokir;
     catat sumber data di ringkasan). Exit 0 = semua langkah jalan
     (sinyal FLAT/NO-TRADE adalah hasil valid, bukan gagal).
- Bila hijau, lanjut testnet strategi:
  `ENABLE_STRATEGY=true` di testnet, pantau 48 jam.
- Kriteria lolos paper-testnet:
  - Tidak ada order ganda (cek idempotency `trace_id` / satu sinyal = maks satu order).
  - Tidak ada halt palsu (killswitch -5% tidak trigger tanpa drawdown riil).
  - Sizing <= risk 1%/trade (cek output `calc_qty` / `risk_check OK`).
