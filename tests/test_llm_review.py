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
    assert len(calls) == 2  # 1x L1 + 1x fallback, lalu CONFIRM


def test_timeout_confirms(monkeypatch, _on):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_urlopen(exc=TimeoutError("timed out"),
                                      calls=calls))
    res = llm_review.review(_sig(), _mkt())
    assert res["verdict"] == "CONFIRM"
    assert len(calls) == 2


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
