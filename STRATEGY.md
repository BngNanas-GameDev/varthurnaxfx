# STRATEGI — BTCUSDT-PERP (isolated 10x, risk 1%, daily stop -5%)

Simbol: **BTCUSDT-PERP** (Binance USDT-M, `BTC/USDT:USDT` di ccxt).
Timeframe utama **H1**; `1m/5m` hanya konteks. Margin **isolated**, leverage default **10x**.
Wajib: setiap order entry selalu disertai **SL + TP native** (tidak ada posisi naked).
Risk/trade **1% equity**, daily stop **-5% equity** (tutup semua + kunci hari itu).
Fee taker **0.05%**, slippage default **5 bps/sisi** (dipakai di backtest & sizing).

Kode: `src/strategy/setups.py::evaluate`, `src/strategy/sizing.py::calc_qty`,
`src/strategy/backtest.py`. Parameter eksplisit di `BREAKOUT_PARAMS` / `MR_PARAMS`.

---

## 1. Setup A — TREND_BREAKOUT_H1

`breakout_n=20, ema_fast=20, ema_slow=50, min_atr_pct=0.0008, atr_mult_sl=1.5, tp_mult=2.0`

**Entry LONG** (semua harus true pada candle H1 yang sudah close):
1. `close > max(high[-21:-1])` (Donchian-20, current bar excluded).
2. `EMA20 > EMA50` (uptrend).
3. `ATR14 / close >= 0.0008` (filter pasar mati).

**Entry SHORT** (cerminan): `close < min(low[-21:-1])` + `EMA20 < EMA50` + filter ATR sama.

**SL/TP** (dari `entry = close` sinyal):
- LONG: `SL = entry − ATR×1.5`, `TP = entry + |entry−SL|×2.0` (RR 1:2).
- SHORT: `SL = entry + ATR×1.5`, `TP = entry − |entry−SL|×2.0`.

**Exit**: TP/SL native tersentuh, atau time-stop 24 bar H1, atau thesis_breaker terpicu
(close kembali ke dalam range / EMA cross berlawanan) → tutup manual.

**Thesis contoh (LONG)**: "Breakout H1: close 67200 > Donchian-HH(20) 66950,
EMA20 66800 > EMA50 66200 (uptrend), ATR 320 lolos filter volatilitas."
**Thesis_breaker**: "Close H1 kembali di bawah 66950 (failed breakout) atau
EMA20 memotong ke bawah EMA50."

## 2. Setup B — MEAN_REVERSION (+ filter funding)

`bb_period=20, bb_std=2.0, rsi_oversold=30, rsi_overbought=70, zscore_threshold=2.0,`
`atr_mult_sl=1.0, tp_mult=1.5, funding_long_max=+0.0001, funding_short_min=−0.0001`

**Entry LONG**: `close < BB_lower(20,2σ)` DAN (`RSI14 < 30` ATAU `z ≤ −2`)
DAN `funding ≤ +0.0001`.
**Entry SHORT**: `close > BB_upper(20,2σ)` DAN (`RSI14 > 70` ATAU `z ≥ +2`)
DAN `funding ≥ −0.0001`.

Logika funding: funding positif besar = long crowded membayar mahal → **jangan ikut LONG**;
sebaliknya untuk SHORT.

**SL/TP**: LONG `SL = entry − ATR×1.0`, `TP = entry + risk×1.5`;
SHORT cerminan. RR 1:1.5 (target balik ke SMA20, bukan tren panjang).

**Thesis contoh (SHORT)**: "Mean-reversion: close 68500 > BB-upper 68200 (z=+2.3),
RSI 74 overbought, funding +0.00002 ≥ −0.0001. Target balik ke SMA 67800."
**Thesis_breaker**: "Close H1 di atas SL (breakout trend, bukan noise) atau
funding anjlok < −0.0001."

## 3. Sizing

`qty = (equity × 1%) / |entry − SL| / contract_size(1.0)`, lalu:
- `notional = qty × entry ≤ equity × leverage(10)`; bila lewat → cap + flag `capped`.
- Quantize **floor** ke step **0.001**, tolak bila `< 0.001` (min qty BTC).
- Cek `init_margin (notional/lev) + maint_margin (bracket) ≤ equity`, else qty=0.

## 4. Kapan NO-TRADE (wajib)

1. Data `< 60` bar closed / indikator NaN / kolom Gold hilang.
2. ATR/close `< 0.0008` (pasar mati) untuk breakout.
3. Funding memblokir (crowded) untuk mean-reversion.
4. **Konflik**: kedua setup valid tapi beda arah → NO-TRADE.
5. Confidence `< 0.5`, atau SL/TP invalid.
6. Daily PnL ≤ −5% (di orkestrator, bukan di `evaluate`).
7. Spread/fee abnormal, gap besar (`is_gap=true`) tanpa konfirmasi close berikutnya.

Setiap NO-TRADE wajib mencatat `reason` (lihat `signal["reason"]`).
