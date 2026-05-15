"""Tests for QuikSyncHttpClient — auth header, retries, 4xx/5xx error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from quiksync_client.auth import Auth0M2MClient
from quiksync_client.http import (
    QuikSyncClientError,
    QuikSyncConnectionError,
    QuikSyncHttpClient,
    QuikSyncServerError,
    HttpConfig,
)


def make_auth() -> Auth0M2MClient:
    """Return a mock auth client that always returns the same token."""
    auth = MagicMock(spec=Auth0M2MClient)
    auth.get_token.return_value = "test.jwt.token"
    return auth


def make_http(monkeypatch, responses: list[httpx.Response]) -> QuikSyncHttpClient:
    """Build an QuikSyncHttpClient whose underlying httpx.Client is mocked
    to return `responses` in order on successive .request() calls."""
    iterator = iter(responses)
    captured_headers: list[dict] = []
    captured_params: list[dict] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured_headers.append(dict(headers or {}))
        captured_params.append(dict(params or {}))
        return next(iterator)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(
        base_url="https://example.test",
        max_retries=2,  # fewer retries → faster tests
        backoff_base_seconds=0.01,
    )
    client = QuikSyncHttpClient(config, make_auth())
    client._captured_headers = captured_headers  # type: ignore[attr-defined]
    client._captured_params = captured_params  # type: ignore[attr-defined]
    return client


def test_get_includes_bearer_token(monkeypatch):
    response = httpx.Response(status_code=200, json={"fleets": [], "doors": [], "lifts": []})
    client = make_http(monkeypatch, [response])
    payload = client.get_discovery()
    assert payload == {"fleets": [], "doors": [], "lifts": []}
    headers = client._captured_headers[0]  # type: ignore[attr-defined]
    assert headers["authorization"] == "Bearer test.jwt.token"
    client.close()


def test_400_raises_client_error_without_retry(monkeypatch):
    response = httpx.Response(
        status_code=400,
        json={"error": "coord_navigate_not_supported", "message": "..."},
    )
    client = make_http(monkeypatch, [response, response, response])  # extras shouldn't be consumed
    with pytest.raises(QuikSyncClientError) as exc:
        client.post_navigate("f", "r", "e1", {"x": 0.0, "y": 0.0, "yaw": 0.0, "map_name": "L1"})
    assert exc.value.status == 400
    assert exc.value.error_code == "coord_navigate_not_supported"
    # No retries — only one request consumed
    assert len(client._captured_headers) == 1  # type: ignore[attr-defined]
    client.close()


def test_401_triggers_force_refresh_and_one_retry(monkeypatch):
    from unittest.mock import call

    auth = make_auth()
    # First call returns 401, then 200 after force-refresh.
    sequence = [
        httpx.Response(status_code=401, json={"error": "Unauthorized"}),
        httpx.Response(status_code=200, json={"fleets": []}),
    ]
    iterator = iter(sequence)

    def fake_request(self, method, path, headers=None, json=None, params=None):
        return next(iterator)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=2, backoff_base_seconds=0.01)
    client = QuikSyncHttpClient(config, auth)
    try:
        payload = client.get_discovery()
        assert payload == {"fleets": []}
        # The 401 path calls get_token(force_refresh=True) once. The retry
        # then calls get_token() again with no kwargs to fetch the (just-
        # refreshed) cached token. Assert the force-refresh call appears
        # somewhere in the call list (it's the second-to-last; the last is
        # the retry's plain-argument fetch).
        assert call(force_refresh=True) in auth.get_token.call_args_list
        # Total: initial → force-refresh → retry-fetch = 3 calls
        assert auth.get_token.call_count == 3
    finally:
        client.close()


def test_500_retries_then_raises_server_error(monkeypatch):
    response = httpx.Response(status_code=500, text="boom")
    client = make_http(monkeypatch, [response, response, response])  # 3 = initial + 2 retries
    with pytest.raises(QuikSyncServerError) as exc:
        client.get_discovery()
    assert exc.value.status == 500
    # Retried twice
    assert len(client._captured_headers) == 3  # type: ignore[attr-defined]
    client.close()


def test_503_first_then_200_returns_success(monkeypatch):
    sequence = [
        httpx.Response(status_code=503, text="bootstrapping"),
        httpx.Response(status_code=200, json={"fleets": []}),
    ]
    client = make_http(monkeypatch, sequence)
    payload = client.get_discovery()
    assert payload == {"fleets": []}
    assert len(client._captured_headers) == 2  # type: ignore[attr-defined]
    client.close()


def test_connection_error_retries_then_raises(monkeypatch):
    auth = make_auth()

    def fake_request(self, method, path, headers=None, json=None, params=None):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=2, backoff_base_seconds=0.01)
    client = QuikSyncHttpClient(config, auth)
    try:
        with pytest.raises(QuikSyncConnectionError):
            client.get_discovery()
    finally:
        client.close()


def test_post_navigate_includes_body(monkeypatch):
    captured_bodies: list[dict] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured_bodies.append(json or {})
        return httpx.Response(status_code=202, json={"task_id": "t1", "execution_id": "e1", "status": "queued"})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.post_navigate(
            "service", "robot-1", "exec-1",
            destination={"x": 10.0, "y": 5.0, "yaw": 0.0, "map_name": "L1"},
            dock_name="charger_1",
            speed_limit=0.5,
        )
        assert len(captured_bodies) == 1
        body = captured_bodies[0]
        assert body["execution_id"] == "exec-1"
        assert body["destination"]["x"] == 10.0
        assert body["dock_name"] == "charger_1"
        assert body["speed_limit"] == 0.5
    finally:
        client.close()


def test_post_perform_action_includes_body(monkeypatch):
    captured: list[tuple[str, str, dict]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path, json or {}))
        return httpx.Response(status_code=202, json={"task_id": "act-t1", "execution_id": "act-e1", "status": "queued"})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.post_perform_action(
            fleet="service", robot="robot-1",
            execution_id="act-1",
            category="clean",
            description={"zone_id": "lobby_west"},
            deadline_unix_millis=1747095000000,
        )
        assert len(captured) == 1
        method, path, body = captured[0]
        assert method == "POST"
        assert path == "/api/v1/connector/open-rmf/fleets/service/robots/robot-1/perform_action"
        assert body["execution_id"] == "act-1"
        assert body["category"] == "clean"
        assert body["description"] == {"zone_id": "lobby_west"}
        assert body["deadline_unix_millis"] == 1747095000000
    finally:
        client.close()


def test_post_perform_action_omits_deadline_when_unset(monkeypatch):
    captured_bodies: list[dict] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured_bodies.append(json or {})
        return httpx.Response(status_code=202, json={})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.post_perform_action(
            fleet="f", robot="r", execution_id="e",
            category="cat", description={"k": "v"},
        )
        assert captured_bodies[0] == {
            "execution_id": "e", "category": "cat", "description": {"k": "v"},
        }
    finally:
        client.close()


# ----- Door endpoints -----


def test_get_door_state_path_and_method(monkeypatch):
    captured: list[tuple[str, str]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path))
        return httpx.Response(status_code=200, json={
            "door_name": "door_alpha", "door_time": 1234,
            "current_mode": {"value": 2},
        })

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        payload = client.get_door_state("door_alpha")
        assert captured == [("GET", "/api/v1/connector/open-rmf/doors/door_alpha/state")]
        assert payload["current_mode"]["value"] == 2
    finally:
        client.close()


def test_post_door_request_body_and_method(monkeypatch):
    captured: list[tuple[str, str, dict]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path, json or {}))
        return httpx.Response(status_code=202, json={"status": "queued"})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.post_door_request(
            door="door_alpha",
            requester_id="rmf:robot-1",
            requested_mode="OPEN",
            execution_id="exec-1",
        )
        method, path, body = captured[0]
        assert method == "POST"
        assert path == "/api/v1/connector/open-rmf/doors/door_alpha/request"
        assert body == {
            "requester_id": "rmf:robot-1",
            "requested_mode": "OPEN",
            "execution_id": "exec-1",
        }
    finally:
        client.close()


# ----- Lift endpoints -----


def test_get_lift_state_path_and_method(monkeypatch):
    captured: list[tuple[str, str]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path))
        return httpx.Response(status_code=200, json={
            "lift_name": "lift_alpha", "lift_time": 1234,
            "current_floor": "L1", "destination_floor": "",
            "door_state": 0, "motion_state": 0,
            "available_modes": [{"value": 2}, {"value": 4}],
            "current_mode": {"value": 2}, "session_id": "",
        })

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        payload = client.get_lift_state("lift_alpha")
        assert captured == [("GET", "/api/v1/connector/open-rmf/lifts/lift_alpha/state")]
        assert payload["current_mode"]["value"] == 2
    finally:
        client.close()


def test_post_lift_request_body_and_method(monkeypatch):
    captured: list[tuple[str, str, dict]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path, json or {}))
        return httpx.Response(status_code=202, json={"status": "queued"})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.post_lift_request(
            lift="lift_alpha",
            session_id="rmf:robot-1",
            request_type="AGV_MODE",
            destination_floor="L3",
            door_state="OPEN",
            execution_id="exec-1",
        )
        method, path, body = captured[0]
        assert method == "POST"
        assert path == "/api/v1/connector/open-rmf/lifts/lift_alpha/request"
        assert body == {
            "session_id": "rmf:robot-1",
            "request_type": "AGV_MODE",
            "destination_floor": "L3",
            "door_state": "OPEN",
            "execution_id": "exec-1",
        }
    finally:
        client.close()


def test_delete_lift_session_path_and_method(monkeypatch):
    captured: list[tuple[str, str]] = []

    def fake_request(self, method, path, headers=None, json=None, params=None):
        captured.append((method, path))
        return httpx.Response(status_code=200, json={"status": "cleared"})

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        client.delete_lift_session("lift_alpha")
        assert captured == [("DELETE", "/api/v1/connector/open-rmf/lifts/lift_alpha/session")]
    finally:
        client.close()


def test_post_lift_request_409_surfaces_holding_session(monkeypatch):
    """When AGV_MODE conflicts on the server side, 409 surfaces with
    holding_session_id; the http client raises a client error with the
    full payload."""
    response = httpx.Response(status_code=409, json={
        "error": "lift_session_held",
        "holding_session_id": "rmf:robot-2",
    })

    def fake_request(self, method, path, headers=None, json=None, params=None):
        return response

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    config = HttpConfig(base_url="https://example.test", max_retries=0)
    client = QuikSyncHttpClient(config, make_auth())
    try:
        with pytest.raises(QuikSyncClientError) as exc:
            client.post_lift_request(
                lift="lift_alpha", session_id="rmf:robot-1",
                request_type="AGV_MODE", destination_floor="L3",
                door_state="OPEN", execution_id="exec-1",
            )
        assert exc.value.status == 409
        assert exc.value.body["holding_session_id"] == "rmf:robot-2"
    finally:
        client.close()


# ----- namespace query param -----


def test_namespace_omitted_when_unset(monkeypatch):
    """When `namespace` is not passed, no `?namespace=...` query param
    is sent — preserves back-compat against servers that don't filter."""
    response = httpx.Response(status_code=200, json={"fleets": [], "doors": [], "lifts": []})
    client = make_http(monkeypatch, [response])
    try:
        client.get_discovery()
        assert client._captured_params == [{}]  # type: ignore[attr-defined]
    finally:
        client.close()


def test_namespace_propagates_on_discovery(monkeypatch):
    response = httpx.Response(status_code=200, json={"fleets": [], "doors": [], "lifts": []})
    client = make_http(monkeypatch, [response])
    try:
        client.get_discovery(namespace="Test")
        assert client._captured_params == [{"namespace": "Test"}]  # type: ignore[attr-defined]
    finally:
        client.close()


def test_namespace_propagates_on_door_request(monkeypatch):
    response = httpx.Response(status_code=200, json={})
    client = make_http(monkeypatch, [response])
    try:
        client.post_door_request(
            door="Main Gate",
            requester_id="rmf:robot-1",
            requested_mode="OPEN",
            execution_id="exec-1",
            namespace="Test",
        )
        assert client._captured_params == [{"namespace": "Test"}]  # type: ignore[attr-defined]
    finally:
        client.close()


def test_namespace_propagates_on_lift_request(monkeypatch):
    response = httpx.Response(status_code=200, json={})
    client = make_http(monkeypatch, [response])
    try:
        client.post_lift_request(
            lift="Tower A/Lift 1",
            session_id="sess-1",
            request_type="AGV_MODE",
            destination_floor="3",
            door_state="OPEN",
            execution_id="exec-1",
            namespace="Test",
        )
        assert client._captured_params == [{"namespace": "Test"}]  # type: ignore[attr-defined]
    finally:
        client.close()


def test_namespace_propagates_on_navigate(monkeypatch):
    response = httpx.Response(status_code=200, json={"task_id": "t1"})
    client = make_http(monkeypatch, [response])
    try:
        client.post_navigate(
            fleet="service_robots",
            robot="robot-1",
            execution_id="exec-1",
            destination={"x": 1.0, "y": 2.0, "yaw": 0.0, "map_name": "L1"},
            namespace="Test",
        )
        assert client._captured_params == [{"namespace": "Test"}]  # type: ignore[attr-defined]
    finally:
        client.close()
