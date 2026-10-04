"""LLM veto-only review tests (offline: urllib HTTP di-mock)."""

import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import strategy.llm_review as llm_review  # noqa: E402
from test_loop import make_flat_gold, make_long_gold  # noqa: E402
from trading import loop as trading_loop  # noqa: E402


def _sig():
    return {"action": "LONG", "setup": "TREND_BREAKOUT_H1", "entry": 60000.0,
            "sl": 59700.0, "tp": 60600.0, "confidence": 0.7,
            "thesis": "t", "thesis_breaker": "b", "reason": "ok",
            "funding_rate": 0.0, "atr": 200.0}


def _long():
    return {"action": "LONG", "setup": "TREND_BREAKOUT_H1", "entry": 60000.0,
            "sl": 59500.0, "tp": 61000.0, "confidence": 0.7,
            "thesis": "t", "thesis_breaker": "b", "reason": "ok",
            "funding_rate": 0.0, "atr": 200.0}


def _short():
    return {"action": "SHORT", "setup": "MEAN_REVERSION", "entry": 60000.0,
            "sl": 60500.0, "tp": 59000.0, "confidence": 0.6,
            "thesis": "t", "thesis_breaker": "b", "reason": "ok",
            "funding_rate": 0.0, "atr": 200.0}


def _conflict():
    return {"action": "NO_TRADE", "setup": None, "entry": None, "sl": None,
            "tp": None, "confidence": 0.0, "thesis": "konflik",
            "thesis_breaker": "n/a",
            "reason": "konflik: TREND_BREAKOUT_H1=LONG vs MEAN_REVERSION=SHORT",
            "candidates": [_long(), _short()]}


def test_arbitrate_pick_long(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"pick":"LONG","confidence_mult":0.8,'
                                      '"reason":"trend"}', calls=calls))
    res = llm_review.arbitrate(_long(), _short(), _mkt())
    assert res["pick"] == "LONG"
    assert res["confidence_mult"] == pytest.approx(0.8)
    assert res["model"]
    assert len(calls) == 1


def test_arbitrate_garbage_neither(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen("BUKAN JSON{{{", calls=calls))
    res = llm_review.arbitrate(_long(), _short(), _mkt())
    assert res["pick"] == "NEITHER"
    assert len(calls) == 3


def test_arbitrate_off_no_http(monkeypatch):
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen("{}", calls=called))
    res = llm_review.arbitrate(_long(), _short(), _mkt())
    assert res["pick"] == "NEITHER"
    assert called == []


def test_arbitrate_invalid_input(monkeypatch, _on):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen("{}", calls=called))
    res = llm_review.arbitrate(_long(), _long(), _mkt())
    assert res["pick"] == "NEITHER"
    assert called == []


def test_loop_conflict_arbiter_long_orders(monkeypatch, tmp_path):
    import src.ops.alerts as alerts_mod

    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    sent = []
    monkeypatch.setattr(alerts_mod, "send_alert", lambda m: sent.append(m) or True)
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"pick":"LONG","confidence_mult":1.0,'
                                      '"reason":"uptrend"}'))
    monkeypatch.setattr(trading_loop, "evaluate", lambda df: _conflict())
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.get_position.return_value = {"contracts": 0.0}
    m.place_entry.return_value = {"clientOrderId": "x"}
    sf = str(tmp_path / "st.json")
    res = trading_loop.run_cycle(m, 1000.0, df=make_long_gold(), state_file=sf)
    assert res["ordered"] is True and res["action"] == "LONG", res
    assert m.place_entry.call_count == 1
    assert any(s.startswith("ARBITER") for s in sent)


def test_loop_conflict_arbiter_neither_no_trade(monkeypatch, tmp_path):
    import src.ops.alerts as alerts_mod

    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    sent = []
    monkeypatch.setattr(alerts_mod, "send_alert", lambda m: sent.append(m) or True)
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"pick":"NEITHER","confidence_mult":1.0,'
                                      '"reason":"ragu"}'))
    monkeypatch.setattr(trading_loop, "evaluate", lambda df: _conflict())
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.get_position.return_value = {"contracts": 0.0}
    sf = str(tmp_path / "st.json")
    res = trading_loop.run_cycle(m, 1000.0, df=make_long_gold(), state_file=sf)
    assert res["ordered"] is False
    assert res["reason"].startswith("arbiter-neither")
    m.place_entry.assert_not_called()
    assert sent == []  # NEITHER sepi, tanpa spam


def test_loop_conflict_dedup_per_bar(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"pick":"NEITHER","confidence_mult":1.0,'
                                      '"reason":"ragu"}', calls=calls))
    monkeypatch.setattr(trading_loop, "evaluate", lambda df: _conflict())
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.get_position.return_value = {"contracts": 0.0}
    sf = str(tmp_path / "st.json")
    df = make_long_gold()
    trading_loop.run_cycle(m, 1000.0, df=df, state_file=sf)
    trading_loop.run_cycle(m, 1000.0, df=df, state_file=sf)
    assert len(calls) == 1  # hanya siklus pertama yang HTTP (bar sama)


def _mkt():
    return {"symbol": "BTCUSDT-PERP", "atr_pct": 0.003, "funding": 0.0,
            "rsi": 55.0, "closes": [59900.0, 59950.0, 59980.0, 59990.0, 60000.0]}


class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _wrap(content):
    return {"choices": [{"message": {"content": content}}]}


def _fake_urlopen(content=None, exc=None, calls=None):
    def _fake(req, timeout=None):
        if calls is not None:
            calls.append(timeout)
        if exc is not None:
            raise exc
        return _FakeResp(_wrap(content))
    return _fake


@pytest.fixture
def _on(monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)


def test_confirm_valid(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":1.0,"reason":"ok"}',
                                      calls=calls))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert res["confidence_mult"] == 1.0
    assert res["model"]  # model terisi
    assert len(calls) == 1  # L1 saja, tanpa retry


def test_veto_valid(monkeypatch, _on):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO",'
                                      '"confidence_mult":0.4,"reason":"funding"}'))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "VETO"
    assert res["confidence_mult"] == pytest.approx(0.4)
    assert res["reason"] == "funding"


def test_malformed_json_confirms(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen("BUKAN JSON{{{", calls=calls))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert res["confidence_mult"] == 1.0
    assert len(calls) == 3  # L1 rf + L1 plain + fallback, lalu CONFIRM


def test_timeout_confirms(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen(exc=TimeoutError("timed out"),
                                      calls=calls))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert len(calls) == 3


def test_unknown_verdict_confirms(monkeypatch, _on):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"MAYBE",'
                                      '"confidence_mult":0.1,"reason":"x"}'))
    assert llm_review.review(_sig(), _mkt())["verdict"] == "CONFIRM"


def test_confidence_mult_never_raises(monkeypatch, _on):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":2.5,"reason":"x"}'))
    assert llm_review.review(_sig(), _mkt())["confidence_mult"] == 1.0
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":-3.0,"reason":"x"}'))
    assert llm_review.review(_sig(), _mkt())["confidence_mult"] == 0.0
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":"banyak","reason":"x"}'))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM" and res["confidence_mult"] == 1.0


def test_off_by_default_no_http(monkeypatch):
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO"}', calls=called))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert called == []  # OFF -> nol network call


def test_missing_api_key_no_http(monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO"}', calls=called))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert called == []


def test_non_signal_never_calls_http(monkeypatch, _on):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO"}', calls=called))
    flat = {"action": "NO_TRADE"}
    assert llm_review.review(flat, _mkt())["verdict"] == "CONFIRM"
    assert called == []


# --- hook loop ---

@pytest.fixture
def client():
    m = MagicMock()
    m.get_daily_pnl.return_value = 0.0
    m.get_position.return_value = {"contracts": 0.0}
    m.place_entry.return_value = {"clientOrderId": "x", "status": "filled"}
    return m


@pytest.fixture(autouse=True)
def _loop_env(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)


def test_loop_off_makes_no_http_call(client, tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_REVIEW", raising=False)
    called = []

    def _boom(req, timeout=None):
        called.append(1)
        raise AssertionError("HTTP tidak boleh dipanggil saat OFF")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=str(tmp_path / "s.json"))
    assert res["ordered"] is True  # perilaku identik seperti sebelumnya
    assert called == []


def test_loop_veto_blocks_order_and_notifies(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO",'
                                      '"confidence_mult":0.2,'
                                      '"reason":"crowded"}'))
    import src.ops.alerts as alerts_mod

    sent = []
    monkeypatch.setattr(alerts_mod, "send_alert", lambda m: sent.append(m) or True)
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=str(tmp_path / "s.json"))
    assert res["ordered"] is False
    assert res["action"] == "NO_TRADE"
    assert res["reason"].startswith("llm-veto:")
    client.place_entry.assert_not_called()
    assert any(s.startswith("VETO") for s in sent)


def test_loop_confirm_lowers_but_never_raises(client, tmp_path, monkeypatch):
    from strategy.setups import evaluate

    base = evaluate(make_long_gold())["confidence"]
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":0.5,"reason":"ok"}'))
    res = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                 state_file=str(tmp_path / "s.json"))
    assert res["ordered"] is True
    assert res["signal"]["confidence"] == pytest.approx(base * 0.5)
    # mult > 1 dijepit: confidence tak pernah naik
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"CONFIRM",'
                                      '"confidence_mult":5.0,"reason":"ok"}'))
    res2 = trading_loop.run_cycle(client, 1000.0, df=make_long_gold(),
                                  state_file=str(tmp_path / "s2.json"))
    assert res2["ordered"] is True
    assert res2["signal"]["confidence"] == pytest.approx(base)


def test_loop_flat_no_http(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_REVIEW", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    called = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen('{"verdict":"VETO"}', calls=called))
    res = trading_loop.run_cycle(client, 1000.0, df=make_flat_gold(),
                                 state_file=str(tmp_path / "s.json"))
    assert res["ordered"] is False
    assert called == []  # NO_TRADE rule-based -> hook tak dipanggil
