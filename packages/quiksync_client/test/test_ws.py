"""Tests for QuikSyncWsClient — 401 circuit-breaker, jittered backoff math,
subscribe-path composition.

WSS-end-to-end behavior is covered by integration smoke against staging
(not feasible in CI); pure-function tests here pin the circuit-breaker
state machine + URL composition + backoff jitter bounds.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from quiksync_client.auth import Auth0M2MClient
from quiksync_client.ws import QuikSyncWsClient, WsConfig


def make_ws_config() -> WsConfig:
    return WsConfig(base_url="wss://example.test")


def make_auth() -> Auth0M2MClient:
    auth = MagicMock(spec=Auth0M2MClient)
    auth.get_token.return_value = "test.jwt.token"
    return auth


def test_circuit_breaker_records_failure():
    """A single 401 record doesn't trip the breaker."""
    ws = QuikSyncWsClient(make_ws_config(), make_auth())
    ws._record_401()
    assert not ws._is_circuit_open()


def test_circuit_breaker_opens_at_threshold():
    """3 failures within the window → circuit open."""
    config = WsConfig(
        base_url="wss://example.test",
        circuit_breaker_401_count=3,
        circuit_breaker_window_seconds=60.0,
    )
    ws = QuikSyncWsClient(config, make_auth())
    ws._record_401()
    ws._record_401()
    assert not ws._is_circuit_open()
    ws._record_401()
    assert ws._is_circuit_open()


def test_circuit_breaker_window_expires():
    """Failures older than the window are dropped — breaker can re-arm."""
    config = WsConfig(
        base_url="wss://example.test",
        circuit_breaker_401_count=3,
        circuit_breaker_window_seconds=0.1,  # 100ms — short for tests
    )
    ws = QuikSyncWsClient(config, make_auth())
    ws._record_401()
    ws._record_401()
    ws._record_401()
    assert ws._is_circuit_open()
    # Wait past the window
    time.sleep(0.15)
    # Next record_401 prunes old entries before appending
    ws._record_401()
    assert not ws._is_circuit_open()
    # Two more to re-trip
    ws._record_401()
    ws._record_401()
    assert ws._is_circuit_open()


def test_jittered_backoff_bounds():
    """Jitter is ±25% of base — output is in [0.75 * base, 1.25 * base]."""
    base = 4.0
    for _ in range(100):
        val = QuikSyncWsClient._jittered_backoff(base)
        assert 3.0 <= val <= 5.0


def test_jittered_backoff_minimum():
    """Even with negative jitter on a small base, output is at least 0.1s."""
    base = 0.05
    for _ in range(50):
        val = QuikSyncWsClient._jittered_backoff(base)
        assert val >= 0.1


def test_close_sets_closed_flag():
    ws = QuikSyncWsClient(make_ws_config(), make_auth())
    assert not ws._closed
    ws.close()
    assert ws._closed


# ----- subscribe path composition -----
#
# Pin the gateway-facing WSS paths under `/api/connector/ws/...` — sibling
# root to the REST `/api/v1/connector/...` surface (split required by the
# QuikSync adapter API contract). Regressing these strings would silently
# break the live Open-RMF path even with green dry-run.


def _captured_first_url(monkeypatch, subscribe_factory) -> str:
    """Helper: kick off a subscribe iterator, capture the URL passed to
    `websockets.connect`, then bail out so the test doesn't actually open
    a socket. Returns the captured URL.

    `websockets.connect` returns a `Connect` instance — an async context
    manager. We replace it with a synchronous factory that returns a
    fake whose `__aenter__` raises a sentinel, so we can break out of
    the `async with` without crossing the network.
    """
    captured: dict[str, str] = {}

    class _CancelImmediately(Exception):
        pass

    class _FakeConnect:
        async def __aenter__(self):
            raise _CancelImmediately()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_connect(url, *args, **kwargs):
        captured["url"] = url
        return _FakeConnect()

    import quiksync_client.ws as ws_module
    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)

    async def run() -> None:
        ws_client = QuikSyncWsClient(make_ws_config(), make_auth())
        try:
            async for _ in subscribe_factory(ws_client):
                pytest.fail("should not yield before connect succeeds")
        except _CancelImmediately:
            pass
        finally:
            ws_client.close()

    asyncio.run(run())
    return captured["url"]


def test_subscribe_fleet_state_uses_ws_subroot(monkeypatch):
    url = _captured_first_url(monkeypatch, lambda c: c.subscribe_fleet_state("service_robots"))
    assert url == "wss://example.test/api/connector/ws/open-rmf/fleets/service_robots/state/subscribe?access_token=test.jwt.token"


def test_subscribe_door_state_uses_ws_subroot(monkeypatch):
    url = _captured_first_url(monkeypatch, lambda c: c.subscribe_door_state("kitchen_gate_north"))
    assert url == "wss://example.test/api/connector/ws/open-rmf/doors/kitchen_gate_north/state/subscribe?access_token=test.jwt.token"


def test_subscribe_lift_state_uses_ws_subroot(monkeypatch):
    url = _captured_first_url(monkeypatch, lambda c: c.subscribe_lift_state("service_lift_A"))
    assert url == "wss://example.test/api/connector/ws/open-rmf/lifts/service_lift_A/state/subscribe?access_token=test.jwt.token"
