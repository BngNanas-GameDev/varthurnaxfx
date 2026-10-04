"""Diagnosa stage SEQ di data real: hitung lolos tiap tahap (bukan trading)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.pipeline import build_features, load_bronze_klines, to_silver  # noqa: E402
from src.strategy.seq import SEQ_PARAMS, _m  # noqa: E402

p = SEQ_PARAMS
gold = build_features(to_silver(load_bronze_klines(
    PROJECT_ROOT / "data/bronze/tune_1h_1000.jsonl"), "1h"))
d = gold.sort_values("open_time").reset_index(drop=True)
n = len(d)
c0n = c1n = c2n = voln = ctxn = 0
for i in range(60, n):
    w = d.iloc[i - 2:i + 1]
    b = [_m(w.iloc[j]) for j in range(3)]
    if any(x is None for x in b):
        continue
    c0, c1, c2 = b
    s_ok = (c0["upper"] >= p["wick_min"] * c0["rng"]
            and c0["body"] <= p["body_max_c0"] * c0["rng"]
            and c0["pos"] <= p["close_edge"])
    l_ok = (c0["lower"] >= p["wick_min"] * c0["rng"]
            and c0["body"] <= p["body_max_c0"] * c0["rng"]
            and c0["pos"] >= 1.0 - p["close_edge"])
    if not (s_ok or l_ok):
        continue
    c0n += 1
    doji = c1["body"] <= p["doji_max"] * c1["rng"]
    inside = c1["h"] <= c0["h"] and c1["l"] >= c0["l"]
    if not (doji or inside):
        continue
    c1n += 1
    eng = (min(c2["o"], c2["c"]) <= min(c1["o"], c1["c"])
           and max(c2["o"], c2["c"]) >= max(c1["o"], c1["c"]))
    brk_s = c2["c"] < c2["o"] and c2["c"] < min(c0["l"], c1["l"])
    brk_l = c2["c"] > c2["o"] and c2["c"] > max(c0["h"], c1["h"])
    if not (eng and (brk_s or brk_l)):
        continue
    c2n += 1
    vols = d["volume"].iloc[max(0, i - 19):i + 1].astype(float)
    if not (float(w.iloc[-1]["volume"]) >= p["vol_mult"] * float(vols.median())):
        continue
    voln += 1
    ema_f, ema_s = float(w.iloc[-1]["ema20"]), float(w.iloc[-1]["ema50"])
    want_s = ema_f > ema_s and brk_s
    want_l = ema_f < ema_s and brk_l
    if want_s or want_l:
        ctxn += 1
print(f"bars={n} C0={c0n} C1={c1n} C2engulf+break={c2n} vol={voln} ctx={ctxn}")
