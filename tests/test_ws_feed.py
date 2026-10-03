"""Test offline MiniFeed WS (tanpa network, tanpa websocket-client)."""

import json
import time

import src.data.ws_feed as wf
from src.data.ws_feed import MiniFeed, backoff_delay


def test_get_mark_default_none_and_unhealthy_when_never_connected():
    feed = MiniFeed()
    assert feed.get_mark() is None
    assert feed.last_msg_ts == 0.0
    assert feed.healthy() is False
    assert feed.healthy(stale_s=10) is False


def test_healthy_true_after_injected_message_miniticker():
    feed = MiniFeed()
    msg = json.dumps({"stream": "btcusdt@miniTicker",
                      "data": {"c": "67500.5"}})
    feed.on_message(None, msg)  # injeksi langsung, tanpa network
    assert feed.healthy() is True
    assert feed.get_mark() == 67500.5
    assert feed.last_msg_ts > 0


def test_healthy_true_after_injected_message_kline():
    feed = MiniFeed()
    msg = json.dumps({"stream": "btcusdt@kline_1m",
                      "data": {"k": {"c": "67600.25"}}})
    feed.on_message(None, msg)
    assert feed.healthy() is True
    assert feed.get_mark() == 67600.25


def test_healthy_false_when_stale_and_bad_message_ignored():
    feed = MiniFeed()
    feed.on_message(None, "bukan-json{{{")
    # pesan rusak diabaikan: tanpa ts valid -> tetap unhealthy
    # (atau ts ter-set tapi mark tetap None bila parse gagal total)
    assert feed.get_mark() is None
    # paksa ts tua -> stale
    feed.last_msg_ts = time.time() - 30
    assert feed.healthy(stale_s=10) is False
    # pesan segar -> healthy lagi
    feed.on_message(None, json.dumps({"c": "67000.0"}))
    assert feed.healthy(stale_s=10) is True


def test_backoff_sequence():
    assert backoff_delay(0) == 2.0
    assert backoff_delay(1) == 5.0
    assert backoff_delay(2) == 15.0
    assert backoff_delay(3) == 60.0
    assert backoff_delay(10) == 60.0
    assert backoff_delay(-1) == 2.0
    assert list(wf.BACKOFF_STEPS) == [2.0, 5.0, 15.0]
    assert wf.BACKOFF_MAX == 60.0


def test_reconnect_loop_uses_backoff(monkeypatch):
    """_run_loop tidur dengan urutan backoff 2,5,15,60 (mock sleep/ws)."""
    sleeps: list[float] = []

    class FakeApp:
        def __init__(self, *a, **k):
            pass

        def run_forever(self, *a, **k):
            raise ConnectionError("putus")

    import types
    fake_mod = types.SimpleNamespace(WebSocketApp=FakeApp)
    monkeypatch.setitem(__import__("sys").modules, "websocket", fake_mod)

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 4:
            raise StopIteration  # hentikan loop setelah 4 backoff

    monkeypatch.setattr(wf.time, "sleep", fake_sleep)
    feed = MiniFeed()
    feed._running = True
    try:
        feed._run_loop()
    except StopIteration:
        pass
    assert sleeps == [2.0, 5.0, 15.0, 60.0]


def test_connect_without_lib_is_fail_closed(monkeypatch):
    """Tanpa websocket-client: connect tak raise, healthy False."""
    import builtins
    real_import = builtins.__import__

    def no_ws(name, *a, **k):
        if name == "websocket":
            raise ImportError("no websocket-client")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_ws)
    feed = MiniFeed()
    feed.connect()  # tak boleh raise
    if feed._thread is not None:
        feed._thread.join(timeout=5)
    assert feed.healthy() is False
    assert feed.get_mark() is None
