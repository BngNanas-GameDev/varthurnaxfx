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

## 5. MAINNET (modal kecil — HANYA setelah 1-4 hijau + preflight PASS)
> Gate: `python scripts/preflight.py` harus exit 0. Bila FAIL, berhenti di sini.

Urutan promosi demo -> mainnet kecil (jangan loncat langkah):
1. Akun mainnet khusus bot, modal kecil (siap hilang). Key BARU trade-only:
   Futures enable, Spot/Margin/Withdrawals OFF.
2. IP whitelist di dashboard Binance = egress IP host live saja. Tes dari IP
   lain harus gagal (bukti429/block, catat tanggal).
3. Withdraw-disabled double-check: dashboard Withdrawals OFF + env
   `BINANCE_WITHDRAW_ENABLED` unset/false (`validate_permissions()` ok).
4. `.env` host live: `BINANCE_DEMO=false`, `BINANCE_TESTNET=false`,
   `DRY_RUN=false`, leverage tetap 10x isolated, risk 1%/trade.
5. `ALLOW_LIVE=true` PALING TERAKHIR, hanya di host live, setelah 1-4 + preflight
   PASS. Jangan pernah commit `.env` berisi `ALLOW_LIVE=true`.
6. Pantau 72 jam penuh: posisi tunggal, SL/TP native menempel tiap entry,
   WS-stale 10s tanpa spam, alert webhook bunyi.
7. Rotasi key < 90 hari (`KEY_ROTATED_AT` diisi, `rotation_reminder()` > 14d).

Kriteria rollback (salah satu terpenuhi -> rollback SEKARANG):
- Drawdown harian menyentuh -5% (killswitch latch) -> ikut `runbooks/HALT_RESUME.md`.
- SL/TP orphan (posisi tanpa protective order) terlihat di UI.
- Order ganda / double-fill (`trace_id` duplikat) walau sekali.
- Secret muncul di log, atau key/secret terekspos di mana pun.
- Perilaku di luar burn-in: leverage > 10, notional > equity*lev, posisi > 1.
- Cara rollback: `cancel_all()` + close posisi via market `closePosition` +
  set `DRY_RUN=true` + latch STATE + cabut `ALLOW_LIVE` (unset/false).
