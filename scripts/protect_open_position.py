"""Lindungi posisi yang telanjang (SL/TP native belum terpasang).

Dipakai setelah insiden attach gagal: posisi sudah fill tapi tanpa
proteksi. Script ini TIDAK membuka posisi baru, hanya memasang
STOP_MARKET + TAKE_PROFIT_MARKET closePosition=true pada posisi yang
sudah ada, dengan level yang dijamin valid terhadap mark price saat ini
(Binance menolak -2021 kalau level sudah terlewati).

Contoh:
    python scripts/protect_open_position.py --risk-pct 0.6 --rr 2.0
    python scripts/protect_open_position.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.binance_client import BinanceClient, SYMBOL  # noqa: E402
from src.ops.alerts import send_alert  # noqa: E402


def _existing_protection(client, symbol) -> int:
    orders = client._call_with_retry("fetch_open_orders", symbol) or []
    return len([o for o in orders
                if str(o.get("type", "")).upper() in
                ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP_MARKET_PROFIT")])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk-pct", type=float, default=0.6,
                    help="jarak SL dari mark price, persen (default 0.6)")
    ap.add_argument("--rr", type=float, default=2.0, help="rasio reward/risk")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.risk_pct <= 0 or a.rr <= 0:
        print("risk-pct dan rr harus > 0")
        return 1

    client = BinanceClient(dry_run=False)
    pos = client.get_position(SYMBOL)
    qty = abs(float(pos.get("contracts") or 0.0))
    mark = float(pos.get("markPrice") or 0.0)
    if qty <= 0 or mark <= 0:
        print("posisi flat -> tak ada yang diproteksi")
        return 0

    side = str(pos.get("side", "")).lower()
    close_side = "SELL" if side == "long" else "BUY"
    entry_side = "BUY" if side == "long" else "SELL"
    dist = mark * a.risk_pct / 100.0
    if side == "long":
        sl, tp = mark - dist, mark + dist * a.rr
    else:
        sl, tp = mark + dist, mark - dist * a.rr

    print("POSITION %s qty=%s entry=%s mark=%s"
          % (side, qty, pos.get("entryPrice"), mark))
    print("PLAN sl=%.2f tp=%.2f (risk %.2f%%, rr %.1f)"
          % (sl, tp, a.risk_pct, a.rr))
    print("PROTECTION orders already open: %d"
          % _existing_protection(client, SYMBOL))

    if a.dry_run:
        print("DRY_RUN: tak ada order dikirim")
        return 0

    client._attach_sltp_native(SYMBOL, close_side, sl, tp,
                               f"RECOVER-{int(mark)}",
                               entry_side=entry_side, fill_px=mark)
    print("PROTECT_OK sl=%.2f tp=%.2f" % (sl, tp))
    send_alert("PROTECT posisi %s qty=%s: SL %s / TP %s dipasang "
               "(recover manual)" % (side, qty, round(sl, 2), round(tp, 2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
