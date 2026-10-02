"""Test offline live_feed (tanpa network, mock fetch)."""

import json

import src.data.live_feed as lf


def test_to_dataframe_mapping():
    # ccxt 6-kolom + fapi 12-kolom campuran
    rows = [
        [1_700_000_000_000, "60000", "60100", "59900", "60050", "12.5"],
        [1_700_000_000_000 + 3_600_000, 60050, 60200, 60000, 60150, 10.0,
         1_700_000_000_000 + 3_600_000 + 3_599_999,
         0, 0, 0, 0, 0],
    ]
    df = lf.to_dataframe(rows)
    assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    assert len(df) == 2
    assert df.iloc[0]["open_time"] == 1_700_000_000_000
    assert df.iloc[0]["close"] == 60050.0
    assert df.iloc[0]["volume"] == 12.5
    assert df.iloc[1]["close_time"] == 1_700_000_000_000 + 3_600_000 + 3_599_999
    # baris invalid dilewati
    df2 = lf.to_dataframe([["bad"], None, []])
    assert len(df2) == 0


def test_bronze_save_writes_valid_jsonl(tmp_path, monkeypatch):
    rows = [
        [1_700_000_000_000, 60000, 60100, 59900, 60050, 12.5],
        [1_700_000_003_600_000, 60050, 60200, 60000, 60150, 10.0],
    ]

    # mock fetch: tanpa network, fetch_klines dipaksa return sintetis
    monkeypatch.setattr(lf, "fetch_klines", lambda interval="1h", limit=200: rows)
    fetched = lf.fetch_klines("1h", 2)
    assert fetched == rows

    path = lf.save_bronze(fetched, "1h", out_dir=tmp_path, ts=1234567890)
    assert path.name == "klines_1h_1234567890.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line, row in zip(lines, rows):
        rec = json.loads(line)  # JSONL valid
        assert rec["candle"] == row
