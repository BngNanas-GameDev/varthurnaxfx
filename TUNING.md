# TUNING — hasil NEGATIF (data real tune_1h_1000, 1000 bar H1 BTC)

> `setups.py` TIDAK diubah. Tidak ada kombinasi yang lolos kriteria OOS.
> Dilaporkan apa adanya — no overfit, no paksa.

## Setup eksperimen

- Data: `data/bronze/tune_1h_1000.jsonl` (1000 bar H1 BTC real, git-ignored, tidak di-commit)
  → `load_bronze_klines` → `to_silver(timeframe="1h")` → `build_features`.
- Split kronologis 70/30 tanpa shuffle: IS = bar 0–699 (700 bar), OOS = bar 700–999 (300 bar).
- Grid TERBATAS 12 kombinasi (`scripts/tune_params.py`): baseline + 9 variasi satu-faktor
  + 2 combo gabungan (agresif/konservatif). `atr_mult_sl`/`tp_mult` yang divariasikan
  adalah param BREAKOUT; MR `atr_mult_sl`/`tp_mult` dikunci (1.0/1.5), MR hanya `z`.
- Backtest: `run_backtest` default (equity 10000, leverage 10, max_holding 24,
  fee 0.05%/sisi + slippage 5bps — fee/slippage TIDAK disentuh).
- Kriteria pemenang OOS: `net_pnl > 0` DAN `total_trades >= 10` DAN `max_drawdown > -0.08`.
  Pemenang = net_pnl OOS tertinggi (tie-break sharpe OOS).

## Hasil: 12/12 GAGAL di OOS → tidak ada pemenang

| # | kombinasi | IS trades | IS net | IS dd | OOS trades | OOS net | OOS wr | OOS sharpe | OOS dd | status |
|---|-----------|-----------|--------|-------|------------|---------|--------|------------|--------|--------|
| 0 | baseline (n20/sl1.5/tp2.0/z2.0/atr0.0008) | 34 | -1779.66 | -0.1904 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL (net<0) |
| 7 | mr_z25 | 23 | -973.73 | -0.0995 | 6 | -465.71 | 0.167 | -1.833 | -0.0485 | FAIL (net<0, <10 trade) |
| 11 | consv (n28/sl2.0/tp2.0/z2.5/atr0.0012) | 22 | -510.83 | -0.0529 | 7 | -567.85 | 0.143 | -2.268 | -0.0587 | FAIL (net<0, <10 trade) |
| 1 | bo_n14 | 34 | -1769.52 | -0.1894 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 2 | bo_n28 | 34 | -1779.66 | -0.1904 | 12 | -766.60 | 0.250 | -2.217 | -0.0767 | FAIL |
| 3 | sl_tight12 | 35 | -1680.16 | -0.1908 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 4 | sl_wide20 | 34 | -1722.84 | -0.1848 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 5 | tp_15 | 34 | -1779.66 | -0.1904 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 6 | mr_z15 | 34 | -1779.66 | -0.1904 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 8 | atr_loose (0.0005) | 34 | -1779.66 | -0.1904 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 9 | atr_strict (0.0012) | 34 | -1779.66 | -0.1904 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |
| 10 | aggr (n14/sl1.2/tp1.5/z1.5/atr0.0005) | 35 | -1249.70 | -0.1490 | 12 | -766.39 | 0.250 | -2.217 | -0.0766 | FAIL |

(diurutkan per net OOS; baseline = baris #0. IS juga negatif di semua kombinasi —
bukan sekadar overfit IS→OOS, strateginya memang kalah di kedua regime data ini.)

## Diagnosis (terverifikasi, bukan spekulasi)

1. **Mean-reversion mendominasi dan menghantam SL.** Baseline IS: 32/34 trade = MR
   (23 SL vs 8 TP + 1 timeout); OOS: 12/12 = MR (9 SL vs 2 TP + 1 timeout).
   Breakout hampir tidak pernah fire (2/34 IS, 0/12 OOS). MR menangkap pisau jatuh
   saat harga trending — bukan noise yang revert.
2. **Grid `min_atr_pct` adalah no-op di data ini.** ATR/close aktual ∈ [0.00211, …],
   median 0.00574 — seluruh bar JAUH di atas 0.0012. Itu sebabnya `atr_loose` /
   `atr_strict` / baseline hasilnya identik persis. Filter volatilitas tidak
   mengikat; variasinya sia-sia pada regime volatilitas setinggi ini.
3. **Breakout_N / SL / TP breakout nyaris tak berpengaruh** karena breakout jarang
   fire; satu-satunya tuas yang menggerakkan hasil adalah `z` MR (mr_z25/consv
   memangkas trade 34→22 IS, 12→6-7 OOS) — tapi tetap net negatif.
4. Winrate OOS baseline 0.250, sharpe -2.22, max_dd -0.077 (lolos dd tapi gagal
   net dan — untuk kandidat ketat — gagal jumlah trade).

## Saran tindak lanjut (tanpa mengubah kontrak terkunci)

1. **Filter regime ATR relatif** (disarankan sbg follow-up): hanya trade bila ATR%
   di atas **median rolling 50 bar** (bukan ambang absolut — ambang absolut
   terbukti no-op di sini). Butuh tuning worker terpisah + validasi OOS ulang.
2. **Nonaktifkan salah satu setup dan ukur ulang**: kandidat pertama = MR-only-off
   (breakout saja) atau sebaliknya breakout-only-off; dari diagnosis, MR adalah
   sumber kerugian dominan. Jangan kedua-duanya sekaligus tanpa data.
3. **Asimetri SL/TP MR**: SL MR 1.0×ATR dengan TP 1.5× tampak terlalu ketat di
   regime trending — coba SL lebih lebar + TP lebih jauh HANYA bila ditemani
   filter regime (1), dan tetap lewat IS/OOS 70/30 yang sama.
4. **Forward-paper dulu**: jangan live. Paper-trade baseline ≥2 minggu, bandingkan
   distribusi setup/SL-rate paper vs backtest ini; bila MR SL-rate paper ≈ 70%+
   seperti di sini, itu konfirmasi untuk menonaktifkan MR.
5. **Warning overfit**: 1000 bar H1 (≈42 hari) terlalu pendek untuk 5-dimensi grid;
   grid lebih besar = overfit pasti. Setiap perubahan param wajib lolos kriteria
   OOS yang sama (net>0, ≥10 trade, dd>-0.08) sebelum menyentuh `setups.py`.

## Reproduksi

```
python scripts/tune_params.py            # exit 1 = negatif (expected saat ini)
python -m pytest tests -q               # 20 passed
python -m py_compile scripts/tune_params.py
```

File diubah/ditambah: `scripts/tune_params.py` (baru), `TUNING.md` (baru).
`src/strategy/setups.py` SENGAJA tidak diubah. Data `data/bronze/*` tidak di-commit.

## Follow-up worker-1: filter regime anti-loss (NEGATIF — ditolak, setups.py di-revert)

- Mekanisme yang diuji (aditif, di `setups.py`, sudah di-revert): konstanta
  `REGIME_EMA_SLOPE_LOOKBACK=10`, `REGIME_EMA_SLOPE_PCT=0.0015` (0.15%/10 bar),
  `REGIME_TREND_ATR_MULT=2.0`, `REGIME_ATR_MEDIAN_LOOKBACK=50`,
  `REGIME_STORM_LOOKBACK=100`, `REGIME_STORM_PCTILE=90`. Aturan: trend kuat
  (`|slope EMA50/10bar|>0.15%` ATAU `|close-EMA50|/EMA50 > 2*atr_pct`) memblokir
  sinyal MR berlawanan arah; ATR% di atas P90 100 bar memblokir SEMUA entry baru
  (volatility storm). Fail-open bila histori kurang. Breakout searah trend tetap
  boleh jalan. Skrip ad-hoc (di luar repo, tidak di-commit): filter on vs
  monkeypatch-off pada split 70/30 yang SAMA (IS 700 / OOS 300 bar).
- Hasil (fee/slippage/dll identik dengan eksperimen utama):

| split | baseline trades | baseline net | baseline dd | +filter trades | +filter net | +filter dd |
|-------|----------------|--------------|-------------|----------------|-------------|------------|
| IS    | 34 | -1779.66 | -0.1904 | 28 | -997.61 | -0.1237 |
| OOS   | 12 | -766.39 | -0.0766 | 12 | -697.92 | -0.0870 |

- OOS breakdown +filter: MR 6 SL / 1 TP / 1 timeout + breakout 3 SL / 1 TP
  (baseline OOS: 12/12 MR, 0 breakout — filter menggeser jendela posisi
  non-overlapping sehingga breakout ikut fire, 3 di antaranya SL).
- Kriteria terima worker-1: OOS trades ≥ 6 (terpenuhi: 12) DAN net membaik ≥20%
  (butuh ≥ -613.11; aktual -697.92 = +8.9% → GAGAL) ATAU dd membaik ≥20% tanpa
  net memburuk (dd -0.0766 → -0.0870 = -13.6%, net juga tidak lolos → GAGAL).
- Keputusan: TOLAK. `src/strategy/setups.py` di-revert ke HEAD (tidak ada diff),
  tidak di-commit. Verifikasi pasca-revert: `pytest tests -q` 36 passed,
  `py_compile` setups.py + tune_params.py OK.
- Pelajaran: filter trend benar memangkas MR yang kalah di IS (34→28 trade,
  net IS +44%) tapi tidak mentransfer ke OOS (+8.9% net, dd malah memburuk);
  signal-timing backtest non-overlapping membuat pemblokiran menggeser trade
  berikutnya (breakout SL ikut masuk). Hipotesis "MR kalah saat trend kuat"
  belum terbukti dengan ambang ini — jangan naikkan ambang/grid tanpa data baru.
