# HALT / RESUME Runbook

## Halt (otomatis saat daily PnL <= -5% atau STATE latched)
1. Daemon memanggil killswitch -> `cancel_all()` semua open order simbol BTCUSDT-PERP.
2. Close posisi terbuka via market `closePosition` (jangan biarkan SL/TP orphan).
3. Kunci STATE: tulis `{"halt": true, "ts": ..., "reason": ...}` ke `data/STATE.json`
   (file STATE dimiliki worker lain; jika belum ada, buat minimal tersebut).
4. Kirim alert via `src/ops/alerts.py` (Telegram/Discord webhook atau log).
5. Verifikasi di exchange UI: tidak ada posisi, tidak ada open order.

## Investigasi
- Kumpulkan log 24 jam terakhir + `data/` snapshot.
- Catat penyebab: slippage, beruntun loss, bug, WS-stale, parameter salah.
- Jangan ubah `configs/risk.json` / `configs/models.json` (milik worker lain).

## Resume (MANUAL ONLY — tidak ada auto-resume)
1. Pastikan penyebab halt sudah dipahami dan diperbaiki.
2. Dua orang review checklist `src/security/audit_checklist.md` bagian live-kecil.
3. Hapus latch HANYA manual: set `{"halt": false}` di STATE + catat alasan + approver.
4. Start kecil: `DRY_RUN=true` dulu 1 sesi, lalu live kecil sesuai `runbooks/GO_LIVE.md`.
5. Monitor 24 jam pertama penuh.

## Kontak eskalasi
| Peran | Nama | Kontak |
|-------|------|--------|
| Owner/bot | _isi_ | _isi_ |
| Risk | _isi_ | _isi_ |
| Exchange support | — | Binance Futures support + API status page |
