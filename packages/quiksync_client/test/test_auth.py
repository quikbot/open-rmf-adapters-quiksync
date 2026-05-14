"""Tests for Auth0M2MClient — mint + cache + refresh + 3-strike failure."""

from __future__ import annotations

import time
from unittest.mock import patch

import httpx
import pytest

from quiksync_client.auth import Auth0M2MClient, AuthConfig, AuthError


def make_config() -> AuthConfig:
    return AuthConfig(
        tenant="tenant.example.test",
        audience="https://api.example.test/open-rmf",
        client_id="test-client",
        client_secret="test-secret",
        organization="org_test",
    )


def make_mock_response(status: int = 200, payload: dict | None = None) -> httpx.Response:
    """Build a synchronous httpx.Response without hitting the network."""
    payload = payload or {"access_token": "test.jwt.token", "expires_in": 86400}
    return httpx.Response(status_code=status, json=payload)


def test_first_call_mints_and_caches(monkeypatch):
    """First call hits the wire; subsequent calls return cached token."""
    config = make_config()
    call_count = {"posts": 0}

    def fake_post(self, url, **kwargs):
        call_count["posts"] += 1
        return make_mock_response()

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        token1 = client.get_token()
        token2 = client.get_token()
        token3 = client.get_token()
        assert token1 == token2 == token3 == "test.jwt.token"
        assert call_count["posts"] == 1, "should reuse cached token within TTL"
    finally:
        client.close()


def test_force_refresh_mints_again(monkeypatch):
    """force_refresh=True bypasses the cache."""
    config = make_config()
    call_count = {"posts": 0}

    def fake_post(self, url, **kwargs):
        call_count["posts"] += 1
        # Different token each call
        return make_mock_response(payload={
            "access_token": f"token-{call_count['posts']}",
            "expires_in": 86400,
        })

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        assert client.get_token() == "token-1"
        assert client.get_token() == "token-1"  # cached
        assert client.get_token(force_refresh=True) == "token-2"
        assert call_count["posts"] == 2
    finally:
        client.close()


def test_expired_token_refreshes(monkeypatch):
    """When the cached token has expired, next get_token mints fresh."""
    config = make_config()
    call_count = {"posts": 0}

    def fake_post(self, url, **kwargs):
        call_count["posts"] += 1
        return make_mock_response(payload={
            "access_token": f"token-{call_count['posts']}",
            "expires_in": 1,  # 1-second TTL — will expire instantly
        })

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        client.get_token()
        time.sleep(1.2)  # past expires_at
        client.get_token()
        assert call_count["posts"] == 2
    finally:
        client.close()


def test_non_200_raises_authError(monkeypatch):
    """Non-200 response → AuthError (first attempt, before 3-strike)."""
    config = make_config()

    def fake_post(self, url, **kwargs):
        return make_mock_response(status=401, payload={"error": "invalid_client"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        with pytest.raises(AuthError):
            client.get_token()
    finally:
        client.close()


def test_three_consecutive_failures_raise_authError(monkeypatch):
    """3 failures in a row surface to caller — adapter triggers circuit-breaker."""
    config = make_config()

    def fake_post(self, url, **kwargs):
        return make_mock_response(status=500, payload={"error": "internal"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        # First two failures raise their own AuthError per attempt
        for _ in range(2):
            with pytest.raises(AuthError):
                client.get_token()
        # Third raises AuthError with "3x in a row" message
        with pytest.raises(AuthError, match="3x in a row"):
            client.get_token()
    finally:
        client.close()


def test_missing_access_token_raises(monkeypatch):
    """200 response without `access_token` → AuthError."""
    config = make_config()

    def fake_post(self, url, **kwargs):
        return make_mock_response(payload={"weird": "shape"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        with pytest.raises(AuthError, match="access_token"):
            client.get_token()
    finally:
        client.close()


def test_successful_mint_resets_failure_counter(monkeypatch):
    """After 2 failures + 1 success, the counter resets — next 2 failures don't trip the breaker."""
    config = make_config()
    sequence = [
        make_mock_response(status=500),
        make_mock_response(status=500),
        make_mock_response(),  # success
        make_mock_response(status=500),
        make_mock_response(status=500),
    ]
    seq_iter = iter(sequence)

    def fake_post(self, url, **kwargs):
        return next(seq_iter)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = Auth0M2MClient(config)
    try:
        # Two failures
        with pytest.raises(AuthError):
            client.get_token()
        with pytest.raises(AuthError):
            client.get_token()
        # Force a fresh mint that succeeds
        token = client.get_token(force_refresh=True)
        assert token == "test.jwt.token"
        # Two more failures should NOT trigger the 3-in-a-row breaker
        with pytest.raises(AuthError) as exc1:
            client.get_token(force_refresh=True)
        assert "3x in a row" not in str(exc1.value)
        with pytest.raises(AuthError) as exc2:
            client.get_token(force_refresh=True)
        assert "3x in a row" not in str(exc2.value)
    finally:
        client.close()
