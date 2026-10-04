# SEQ_REVERSAL_H1 — hasil backtest (REJECT)

Aturan terkuantifikasi dari artikel 3-candle (exhaustion wick>=0.6R +
doji/inside + engulf breakthrough + vol>=median + konteks EMA/funding,
SL ekstrem sequence + 0.3 ATR, TP 1.5R).

## H1, 1000 bar real (`tune_1h_1000`), split 70/30
- Stage: C0=165 -> C1=37 -> engulf+break=6 -> vol(1.3x)=0 -> vol(1.0x)=1
- IS: 1 trade +137.77 | OOS: 0 trade → **REJECT (sampel tak cukup)**

## M5, 5000 bar real (`tune_5m_5000`), split 70/30
- IS (3500 bar): 8 trade, winrate 0.50, net **-217.89**, dd -0.031
- OOS (1500 bar): 1 trade, net **-149.96** → **REJECT (net<0, trade<10)**

## Keputusan
SEQ tidak di-wire ke live (`SEQ_ENABLED` default false). Pola terlalu jarang
di H1 dan negatif di M5. Artikel benar soal "tunggu konfirmasi", tapi versi
ketat ini bukan edge yang bisa dibuktikan. Jangan ulangi tanpa data baru /
varian continuation + level signifikan.

---

# STEROID_3C — hasil backtest (CONDITIONAL ACCEPT, H1 only)

Pola sederhana TradingView: C1 new-high/low + C2 close lampaui ekstrem C1,
entry = ekstrem C1, SL = ekstrem lawan, TP 1.5R, konteks EMA+funding.

## H1, 1000 bar real, split 70/30
- IS (700 bar): 40 trade, winrate 0.475, net **-225.47**, dd -0.089
- OOS (300 bar): 12 trade, winrate 0.667, net **+651.29**, dd -0.025
- Gate OOS lolos (net>0, >=10 trade, dd>-0.08) TAPI IS negatif + OOS tipis
  (12 trade) -> bukti LEMAH. HANYA forward-paper demo, BUKAN live.

## M5, 5000 bar real, split 70/30
- IS: negatif | OOS (1500 bar): 42 trade, net **-1631.89**, dd -0.163
- **REJECT untuk M5.**

## Keputusan
Wire di `evaluate()` belakang `STEROID_ENABLED` (default false). H1 boleh
forward-paper demo. M5 dilarang. Evaluasi ulang setelah >=30 trade forward.
