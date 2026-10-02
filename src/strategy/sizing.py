"""Position sizing BTCUSDT-PERP.

Rumus terkunci: qty = (equity * risk_pct) / |entry - SL| / contract_size
- risk_pct default 1% (risk/trade 1%), leverage default 10x isolated.
- Cek notional <= equity * leverage (cap bila lewat).
- Quantize floor ke QTY_STEP (min qty BTC 0.001).
- Cek maintenance-margin bracket (tabel sederhana Binance BTCUSDT).

Return dict {qty, notional, risk_amount, capped, reason, maint_margin, ...}.
qty == 0.0 berarti NO-TRADE (reason menjelaskan kenapa).
"""

from __future__ import annotations

import math

MIN_QTY = 0.001
QTY_STEP = 0.001
DEFAULT_RISK_PCT = 0.01
DEFAULT_LEVERAGE = 10
CONTRACT_SIZE = 1.0  # USDT-M BTC: 1 qty = 1 BTC
MAX_LEVERAGE = 125

# (batas_notional_atas, mmr_rate) — ringkasan bracket Binance BTCUSDT USDT-M.
# Notional <= 1jt -> 0.4%, dst. Cukup untuk estimasi; execution wajib cek live.
MAINT_MARGIN_BRACKETS = [
    (1_000_000, 0.004),
    (5_000_000, 0.005),
    (20_000_000, 0.01),
    (50_000_000, 0.025),
    (100_000_000, 0.05),
    (float("inf"), 0.125),
]


def quantize_down(qty: float, step: float = QTY_STEP) -> float:
    if qty <= 0 or step <= 0:
        return 0.0
    return math.floor(qty / step + 1e-9) * step


def maint_margin_rate(notional: float) -> float:
    for cap, rate in MAINT_MARGIN_BRACKETS:
        if notional <= cap:
            return rate
    return MAINT_MARGIN_BRACKETS[-1][1]


def calc_qty(equity: float, entry: float, sl: float,
             leverage: int = DEFAULT_LEVERAGE,
             risk_pct: float = DEFAULT_RISK_PCT,
             contract_size: float = CONTRACT_SIZE) -> dict:
    """Hitung qty + cek limit. Tidak pernah raise untuk input trading normal.

    >>> r = calc_qty(10000, 60000, 59400, 10)
    >>> abs(r['risk_amount'] - 100.0) < 1e-9
    """
    if equity is None or equity <= 0:
        return {"qty": 0.0, "notional": 0.0, "risk_amount": 0.0,
                "capped": False, "reason": "equity <= 0", "maint_margin": 0.0,
                "leverage": leverage}
    if entry is None or sl is None or entry <= 0 or sl <= 0:
        return {"qty": 0.0, "notional": 0.0, "risk_amount": 0.0,
                "capped": False, "reason": "entry/sl harus > 0", "maint_margin": 0.0,
                "leverage": leverage}
    dist = abs(entry - sl)
    if dist <= 0:
        return {"qty": 0.0, "notional": 0.0, "risk_amount": 0.0,
                "capped": False, "reason": "entry == SL (risiko tak terdefinisi)",
                "maint_margin": 0.0, "leverage": leverage}
    if not (1 <= leverage <= MAX_LEVERAGE):
        return {"qty": 0.0, "notional": 0.0, "risk_amount": 0.0,
                "capped": False, "reason": f"leverage {leverage} di luar 1..{MAX_LEVERAGE}",
                "maint_margin": 0.0, "leverage": leverage}

    risk_amount = equity * risk_pct
    qty_raw = risk_amount / dist / contract_size
    max_notional = equity * leverage
    notional_raw = qty_raw * entry
    capped = False
    reason = "ok"
    qty = qty_raw
    if notional_raw > max_notional:
        qty = max_notional / entry / contract_size
        capped = True
        reason = (f"capped: notional {notional_raw:.2f} > equity*lev {max_notional:.2f}")
    qty = quantize_down(qty, QTY_STEP)
    notional = qty * entry
    if qty < MIN_QTY:
        return {"qty": 0.0, "notional": 0.0, "risk_amount": risk_amount,
                "capped": capped, "reason": f"qty {qty:.4f} < min {MIN_QTY} (risiko/dist terlalu kecil)",
                "maint_margin": 0.0, "leverage": leverage,
                "qty_raw": qty_raw, "max_notional": max_notional}
    mmr = maint_margin_rate(notional)
    maint_margin = notional * mmr
    # margin awal isolated ≈ notional/leverage; pastikan + maint margin < equity
    init_margin = notional / leverage
    if init_margin + maint_margin > equity:
        return {"qty": 0.0, "notional": 0.0, "risk_amount": risk_amount,
                "capped": True, "reason": "init+maint margin > equity",
                "maint_margin": maint_margin, "leverage": leverage,
                "qty_raw": qty_raw, "max_notional": max_notional}
    return {"qty": round(qty, 3), "notional": round(notional, 2),
            "risk_amount": round(risk_amount, 2), "capped": capped,
            "reason": reason, "maint_margin": round(maint_margin, 2),
            "leverage": leverage, "qty_raw": round(qty_raw, 6),
            "max_notional": round(max_notional, 2)}
