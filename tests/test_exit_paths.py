"""Jalur exit/PnL: fallback ``_resolve_exit``/``_classify_exit`` TANPA network.

Fokus: kasus "exchange tidak mengembalikan data" (income history kosong,
fetch_my_trades kosong, markPrice 0 saat posisi sudah flat). Semua test lama
memakai MagicMock yang selalu mengembalikan data, sehingga jalur ini tak pernah
teruji -- akar bug notif CLOSE yang menampilkan PnL 0.00.

StubClient sengaja BUKAN MagicMock: nilai balik dict-nya persis, supaya
"tak ada data" benar-benar diuji (bukan objek truthy yang lolos).
"""

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading import loop as trading_loop  # noqa: E402

# Bentuk persis dict yang dikembalikan fetch_realized saat gagal / dry-run.
EMPTY_INCOME = {"realized_pnl": None, "fee": None, "funding": None,
                "exit_price": None, "exit_ts": None}

LONG_POS = {"side": "LONG", "qty": 0.03, "entry": 60000.0, "sl": 59700.0,
            "tp": 60600.0, "trace_id": "t-long", "opened_ts": 1_700_000_000_000}
SHORT_POS = {"side": "SHORT", "qty": 0.03, "entry": 60000.0, "sl": 60300.0,
             "tp": 59400.0, "trace_id": "t-short", "opened_ts": 1_700_000_000_000}


class StubClient:
    """Client palsu: fetch_realized dikontrol persis, tanpa network."""

    def __init__(self, realized=None, exc: Exception | None = None):
        self.realized = realized
        self.exc = exc
        self.since_calls: list = []

    def fetch_realized(self, symbol="BTC/USDT:USDT", since_ms=None):
        self.since_calls.append(since_ms)
        if self.exc is not None:
            raise self.exc
        if self.realized is None:
            return dict(EMPTY_INCOME)
        if isinstance(self.realized, dict):
            return dict(self.realized)
        return self.realized  # mis. None / non-dict (skenario rusak)


def resolve(pos, realized=None, ex_pos=None, exc=None):
    client = StubClient(realized, exc)
    return trading_loop._resolve_exit(client, dict(pos), ex_pos or {})


def assert_clean(out: dict) -> dict:
    """Guard kontrak: field numerik selalu float finite, tak pernah None/NaN."""
    for key in ("exit_price", "pnl", "fee", "funding"):
        v = out.get(key)
        assert isinstance(v, float), (key, type(v), out)
        assert math.isfinite(v), (key, v, out)          # tak boleh NaN/inf bocor
    assert isinstance(out.get("estimated"), bool), out
    assert isinstance(out.get("reason"), str) and out["reason"], out
    return out


# -- 1. exchange benar-benar tak mengembalikan data -------------------------

def test_no_exchange_data_falls_back_to_state_sl_estimated():
    """realized_pnl None + exit_price None + markPrice 0 -> estimated + reason riil."""
    out = assert_clean(resolve(LONG_POS, EMPTY_INCOME,
                               {"contracts": 0.0, "markPrice": 0.0}))
    assert out["estimated"] is True
    assert out["reason"] != "unknown"
    assert out["reason"] == "stop-loss"          # exitPx fallback ke SL
    assert out["exit_price"] == pytest.approx(59700.0)
    # PnL TIDAK boleh 0.00/hilang: dihitung dari (exit-entry)*qty*sign
    assert out["pnl"] == pytest.approx(-9.0)
    assert not math.isnan(out["pnl"])


@pytest.mark.parametrize("payload", [EMPTY_INCOME, {}, None, {"realized_pnl": None},
                                     {"realized_pnl": "", "exit_price": 0}])
def test_any_empty_exchange_payload_yields_usable_estimate(payload):
    """Berbagai bentuk "tak ada data" -> hasil tetap usable, bukan unknown/NaN."""
    out = assert_clean(resolve(LONG_POS, payload, {"contracts": 0.0}))
    assert out["estimated"] is True
    assert out["reason"] != "unknown"
    assert out["exit_price"] > 0
    assert out["pnl"] == pytest.approx(-9.0)


def test_exchange_exception_degrades_to_state_fallback():
    out = assert_clean(resolve(LONG_POS, exc=RuntimeError("fetch_income 500")))
    assert out["estimated"] is True
    assert out["reason"] == "stop-loss"
    assert out["pnl"] == pytest.approx(-9.0)


def test_no_sl_tp_state_pos_uses_tp_then_entry():
    """Posisi tanpa SL Recorded (mis. SL dilepas manual): fallback ke TP."""
    pos = dict(LONG_POS, sl=None, tp=60600.0)
    out = assert_clean(resolve(pos, EMPTY_INCOME, {}))
    assert out["exit_price"] == pytest.approx(60600.0)
    assert out["reason"] == "take-profit"
    assert out["pnl"] == pytest.approx(18.0)
    assert out["estimated"] is True


# -- 2. realized ada, exit price tidak -------------------------------------

def test_realized_pnl_without_exit_price_keeps_reason_realized():
    """PnL riil dari exchange tapi exit price tak ada -> reason 'realized'."""
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 150.0, "fee": 0.45,
                                          "funding": -0.12, "exit_price": None}))
    assert out["reason"] == "realized"        # tak diklasifikasi dari harga tebakan
    assert out["estimated"] is False           # PnL bukan estimasi
    assert out["pnl"] == pytest.approx(150.0)
    assert out["exit_price"] == pytest.approx(59700.0)   # fallback level STATE


def test_realized_with_exit_price_classifies_reason():
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 150.0, "fee": 0.45,
                                          "funding": 0.0, "exit_price": 60500.0}))
    assert out["estimated"] is False
    assert out["reason"] == "manual/unknown"
    assert out["exit_price"] == pytest.approx(60500.0)


def test_realized_zero_is_not_treated_as_missing():
    """PnL 0.00 itu data sah (scratch/flat), bukan 'tak ada data'."""
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 0.0, "fee": 0.0,
                                          "funding": 0.0, "exit_price": 60000.0}))
    assert out["reason"] == "manual/unknown"
    assert out["estimated"] is False
    assert out["pnl"] == 0.0


# -- 3. fee & funding -------------------------------------------------------

def test_negative_fee_from_exchange_becomes_positive():
    """Binance tulis komisi negatif; output WAJIB positif (biaya)."""
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 10.0, "fee": -1.35,
                                          "funding": 0.0, "exit_price": 60100.0}))
    assert out["fee"] == pytest.approx(1.35)


def test_negative_funding_stays_negative():
    """Funding dibayar -> tetap negatif (ekspsi positif)."""
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 10.0, "fee": 0.5,
                                          "funding": -0.12, "exit_price": 60100.0}))
    assert out["funding"] == pytest.approx(-0.12)
    out2 = assert_clean(resolve(LONG_POS, {"realized_pnl": 10.0, "fee": 0.5,
                                           "funding": 0.25, "exit_price": 60100.0}))
    assert out2["funding"] == pytest.approx(0.25)


def test_fee_and_funding_none_do_not_break():
    out = assert_clean(resolve(LONG_POS, {"realized_pnl": 5.0, "fee": None,
                                          "funding": None, "exit_price": 60100.0}))
    assert out["fee"] == 0.0 and out["funding"] == 0.0


# -- 4. klasifikasi exit SL/TP semua arah -----------------------------------

@pytest.mark.parametrize("pos,exit_px,expected", [
    (LONG_POS, 59000.0, "stop-loss"),        # LONG keluar di bawah SL
    (LONG_POS, 59700.0, "stop-loss"),        # LONG tepat di SL
    (SHORT_POS, 61000.0, "stop-loss"),       # SHORT keluar di atas SL
    (SHORT_POS, 60300.0, "stop-loss"),       # SHORT tepat di SL
    (LONG_POS, 61000.0, "take-profit"),      # LONG keluar di atas TP
    (LONG_POS, 60600.0, "take-profit"),      # LONG tepat di TP
    (SHORT_POS, 59000.0, "take-profit"),     # SHORT keluar di bawah TP
    (SHORT_POS, 59400.0, "take-profit"),     # SHORT tepat di TP
    (LONG_POS, 60000.0, "manual/unknown"),   # di antara SL & TP
    (SHORT_POS, 60000.0, "manual/unknown"),  # di antara TP & SL
])
def test_classify_exit_all_directions(pos, exit_px, expected):
    assert trading_loop._classify_exit(pos["side"], exit_px, pos["entry"],
                                      pos["sl"], pos["tp"]) == expected


@pytest.mark.parametrize("pos,exit_px,expected", [
    (LONG_POS, 59000.0, "stop-loss"),
    (SHORT_POS, 61000.0, "stop-loss"),
    (LONG_POS, 61000.0, "take-profit"),
    (SHORT_POS, 59000.0, "take-profit"),
    (LONG_POS, 60100.0, "manual/unknown"),
])
def test_resolve_exit_classifies_from_real_exchange_price(pos, exit_px, expected):
    out = assert_clean(resolve(pos, {"realized_pnl": 1.0, "fee": 0.1,
                                     "funding": 0.0, "exit_price": exit_px}))
    assert out["reason"] == expected
    assert out["estimated"] is False


def test_classify_exit_zero_and_bogus_price_is_unknown():
    assert trading_loop._classify_exit("LONG", 0.0, 60000.0, 59700.0, 60600.0) == "unknown"
    assert trading_loop._classify_exit("LONG", float("nan"), 60000.0,
                                       59700.0, 60600.0) == "unknown"
    assert trading_loop._classify_exit("LONG", None, 60000.0, 59700.0, 60600.0) == "unknown"


# -- 5. STATE rusak / posisi adopted ----------------------------------------

def test_adopted_position_without_sl_tp_does_not_crash():
    """Posisi adopted: sl/tp None, entry dari exchange."""
    pos = {"side": "LONG", "qty": 0.001, "entry": 84000.0, "sl": None,
           "tp": None, "trace_id": "adopted-exchange", "adopted": True}
    out = assert_clean(resolve(pos, EMPTY_INCOME, {"contracts": 0.0}))
    assert out["estimated"] is True
    assert out["reason"] == "manual/unknown"   # tak ada SL/TP untuk disimpulkan
    assert out["exit_price"] == pytest.approx(84000.0)
    assert out["pnl"] == pytest.approx(0.0)


@pytest.mark.parametrize("pos", [
    {"side": "LONG", "qty": None, "entry": None, "sl": None, "tp": None},
    {"side": "", "qty": 0.0, "entry": 0.0, "sl": 0.0, "tp": 0.0},
    {"side": "LONG", "qty": "x", "entry": "y", "sl": "z", "tp": "w"},
    {"side": None, "entry": None, "qty": None, "sl": None, "tp": None,
     "trace_id": "jane-uuid", "opened_ts": "bukan-angka"},
])
def test_none_or_bogus_levels_never_crash_and_pnl_zero(pos):
    out = assert_clean(resolve(pos, EMPTY_INCOME, {}))
    assert out["pnl"] == 0.0                    # bukan NaN
    assert out["exit_price"] >= 0.0
    assert out["estimated"] is True


def test_short_pnl_sign_is_inverted():
    """SHORT: harga naik = rugi (tanda berlawanan dengan LONG)."""
    loss = assert_clean(resolve(SHORT_POS, EMPTY_INCOME, {"markPrice": 60300.0}))
    assert loss["pnl"] == pytest.approx(-9.0)
    win = assert_clean(resolve(SHORT_POS, EMPTY_INCOME, {"markPrice": 59400.0}))
    assert win["pnl"] == pytest.approx(18.0)


def test_markprice_used_when_realized_absent():
    out = assert_clean(resolve(LONG_POS, EMPTY_INCOME,
                               {"contracts": 0.0, "markPrice": 60500.0}))
    assert out["exit_price"] == pytest.approx(60500.0)
    assert out["estimated"] is True
    assert out["reason"] == "manual/unknown"
    assert out["pnl"] == pytest.approx(15.0)


@pytest.mark.parametrize("key,val", [("markPrice", "abc"), ("markPrice", float("inf")),
                                     ("lastPrice", None), ("mark_price", float("nan"))])
def test_bogus_markprice_fields_ignored(key, val):
    out = assert_clean(resolve(LONG_POS, EMPTY_INCOME, {key: val}))
    assert out["exit_price"] == pytest.approx(59700.0)   # jatuh ke SL, bukan inf/NaN
    assert out["estimated"] is True


# -- 6. guard kontrak global ------------------------------------------------

@pytest.mark.parametrize("realized", [
    EMPTY_INCOME,
    {},
    None,
    {"realized_pnl": float("nan"), "fee": float("nan"), "funding": float("nan"),
     "exit_price": float("nan")},
    {"realized_pnl": float("inf"), "fee": -float("inf"),
     "funding": float("-inf"), "exit_price": float("inf")},
    {"realized_pnl": "abc", "fee": "x", "funding": None, "exit_price": "y"},
    {"realized_pnl": 150.0, "fee": -1.35, "funding": -0.12, "exit_price": 60500.0},
    {"realized_pnl": -42.5, "fee": 0.0, "funding": 0.75, "exit_price": 59000.0},
])
@pytest.mark.parametrize("pos", [LONG_POS, SHORT_POS,
                                 {"side": "LONG", "qty": 0.01, "entry": 100.0,
                                  "sl": None, "tp": None}])
@pytest.mark.parametrize("ex_pos", [{}, {"contracts": 0.0}, {"markPrice": 0.0},
                                    {"markPrice": 60500.0}, {"last_price": 123.5}])
def test_resolve_exit_never_yields_nan_or_none(realized, pos, ex_pos):
    """Guard: tak satu pun kombinasi boleh menghasilkan NaN/None di field numerik."""
    out = assert_clean(resolve(pos, realized, ex_pos))
    assert out["reason"] != ""


@pytest.mark.parametrize("pos", [LONG_POS, SHORT_POS])
def test_nan_from_exchange_falls_back_instead_of_notifying_nan(pos):
    """NaN dari income history = data tak valid, bukan PnL."""
    out = assert_clean(resolve(pos, {"realized_pnl": float("nan"), "fee": None,
                                     "funding": None, "exit_price": float("nan")}))
    assert out["estimated"] is True
    assert out["pnl"] == pytest.approx(-9.0)


def test_opened_ms_passed_to_exchange_as_since_filter():
    client = StubClient(EMPTY_INCOME)
    trading_loop._resolve_exit(client, dict(LONG_POS), {})
    assert client.since_calls == [LONG_POS["opened_ts"]]


def test_no_opened_ts_means_no_exchange_call_at_all():
    """Tanpa opened_ts, income history TAK boleh dipanggil sama sekali.

    Default window Binance = 7 hari, jadi penjumlahan tanpa since_ms akan
    mencampur PnL trade lain lalu angka itu diklaim "riil" (bug H3).
    """
    client = StubClient(EMPTY_INCOME)
    out = trading_loop._resolve_exit(
        client, {"side": "LONG", "entry": 60000.0}, {})
    assert client.since_calls == []
    assert out["estimated"] is True
    assert out["reason"] != "realized"