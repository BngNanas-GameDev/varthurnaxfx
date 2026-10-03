# Jurnal Trade

`src/trading/journal.py` mencatat siklus hidup penuh tiap posisi ke
`data/journal.jsonl` (runtime, git-ignored). Override path via
env `JOURNAL_FILE`. Tanpa network, tanpa key.

## Format JSONL

Satu objek JSON per baris, di-join via `trace_id`:

```json
{"entry": 60000.0, "event": "open", "qty": 0.01, "side": "LONG", "sl": 59700.0, "source": "TREND_BREAKOUT_H1", "tp": 60600.0, "trace_id": "abc123", "ts": "2026-10-03T00:00:00+00:00"}
{"event": "close", "estimated": false, "exit_price": 60500.0, "fee_paid": 1.0, "funding_paid": 0.5, "reason": "sl_or_tp_unknown", "trace_id": "abc123", "ts": "2026-10-03T01:00:00+00:00"}
```

- `open`: `side` LONG/SHORT, `source` = nama setup (`TREND_BREAKOUT_H1`,
  `MEAN_REVERSION`) atau `adopted-exchange` untuk posisi adopsi.
- `close`: `reason` = `sl_or_tp_unknown` bila posisi hilang di luar STATE
  (SL/TP native). `estimated=true` bila `exit_price` dari markPrice
  (bukan harga fill pasti).

## Membaca ringkasan

```python
from trading.journal import summarize
print(summarize())
# {'trades': 2, 'wins': 1, 'total_pnl': -2.5,
#  'total_fee': 2.5,
#  'by_setup': {'TREND_BREAKOUT_H1': {'trades': 1, 'pnl': 9.0}, ...}}
```

- `total_pnl` = Σ `(exit-entry)*qty*side_sign - fee - funding`
  (`side_sign` +1 LONG, −1 SHORT).
- `total_fee` = Σ `fee_paid + funding_paid`.
- `by_setup` = agregat per `source` open: `{trades, pnl}`.
