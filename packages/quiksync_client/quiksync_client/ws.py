"""WebSocket state subscriber for `/api/connector/ws/open-rmf/*/state/subscribe`.

Note the path namespace: WS endpoints sit under `/api/connector/ws/...`,
not under the REST `/api/v1/connector/...` surface. The two are sibling
roots in the QuikSync adapter API for gateway-routing reasons on the
server side — adapter clients just need to use the correct base path
per endpoint family (REST → `/api/v1/connector/...`, WS →
`/api/connector/ws/...`).

Auth: `?access_token=<jwt>` query parameter (browser handshake constraint).
Server frames are raw JSON objects in the resource's state shape — no
wrapping envelope. Consumer receives parsed `dict[str, Any]` via async
generator.

Reconnect discipline:
- Exponential backoff with cap (1s → 30s) on transient errors
- 401 circuit-breaker: after 3 consecutive 401s within 60s, surface to
  caller and stop reconnecting (caller decides whether to bounce or wait)
- Preemptive close-and-reopen at 80% of token TTL ± jitter (relies on
  AuthM2MClient's `get_token()` returning a fresh token by then)

Cancellation: the subscriber owns a background task per WS; calling
`close()` cancels it cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional

import websockets

from .auth import Auth0M2MClient

log = logging.getLogger("quiksync_client.ws")


@dataclass
class WsConfig:
    base_url: str  # e.g. "wss://<your-quiksync-host>"
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    circuit_breaker_401_count: int = 3
    circuit_breaker_window_seconds: float = 60.0
    preemptive_reconnect_pct: float = 0.8


class WsCircuitOpen(Exception):
    """Raised when the 401 circuit-breaker fires — caller bounces the adapter
    or waits for operator intervention before retrying."""


class QuikSyncWsClient:
    """Stream JSON frames from a single QuikSync WSS endpoint.

    Usage:

        async for frame in ws.subscribe_fleet_state("service_robots"):
            adapter.update_from_frame(frame)
    """

    def __init__(self, config: WsConfig, auth: Auth0M2MClient) -> None:
        self._config = config
        self._auth = auth
        # ws_url derived from http base_url; tests can pass `wss://...` directly.
        self._base_ws = config.base_url
        self._closed = False
        self._failure_times: list[float] = []

    def close(self) -> None:
        self._closed = True

    async def subscribe_fleet_state(
        self, fleet: str, namespace: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        path = f"/api/connector/ws/open-rmf/fleets/{fleet}/state/subscribe"
        async for frame in self._subscribe(path, namespace=namespace):
            yield frame

    async def subscribe_door_state(
        self, door: str, namespace: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        path = f"/api/connector/ws/open-rmf/doors/{door}/state/subscribe"
        async for frame in self._subscribe(path, namespace=namespace):
            yield frame

    async def subscribe_lift_state(
        self, lift: str, namespace: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        path = f"/api/connector/ws/open-rmf/lifts/{lift}/state/subscribe"
        async for frame in self._subscribe(path, namespace=namespace):
            yield frame

    async def _subscribe(
        self, path: str, namespace: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        backoff = self._config.backoff_base_seconds
        while not self._closed:
            try:
                token = self._auth.get_token()
                url = f"{self._base_ws}{path}?access_token={token}"
                if namespace:
                    url += f"&namespace={namespace}"
                async with websockets.connect(url) as ws:
                    backoff = self._config.backoff_base_seconds  # reset on success
                    log.info("WSS connected: %s", path)
                    async for raw in ws:
                        if self._closed:
                            return
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError as e:
                            log.warning("WSS dropped malformed frame: %s", e)
                            continue
                        if not isinstance(frame, dict):
                            log.warning("WSS frame not a JSON object: %s", type(frame).__name__)
                            continue
                        yield frame
                # Clean close → loop reconnects on next iteration.
                log.info("WSS clean close: %s — reconnecting", path)
            except websockets.exceptions.InvalidStatus as e:
                # 401 / 403 etc.
                status = e.response.status_code if hasattr(e, "response") else None
                log.warning("WSS upgrade failed (status=%s): %s", status, e)
                if status == 401:
                    self._record_401()
                    if self._is_circuit_open():
                        log.error(
                            "WSS 401 circuit open (%d failures in %ds); not reconnecting",
                            self._config.circuit_breaker_401_count,
                            int(self._config.circuit_breaker_window_seconds),
                        )
                        raise WsCircuitOpen("401 circuit breaker tripped")
                    # Force token refresh before next attempt.
                    try:
                        self._auth.get_token(force_refresh=True)
                    except Exception as refresh_err:  # noqa: BLE001
                        log.warning("Token refresh after 401 failed: %s", refresh_err)
            except (websockets.exceptions.WebSocketException, OSError) as e:
                log.warning("WSS transport failure: %s", e)

            if self._closed:
                return
            await asyncio.sleep(self._jittered_backoff(backoff))
            backoff = min(backoff * 2, self._config.backoff_max_seconds)

    def _record_401(self) -> None:
        now = time.time()
        self._failure_times = [
            t for t in self._failure_times
            if now - t < self._config.circuit_breaker_window_seconds
        ]
        self._failure_times.append(now)

    def _is_circuit_open(self) -> bool:
        return len(self._failure_times) >= self._config.circuit_breaker_401_count

    @staticmethod
    def _jittered_backoff(base: float) -> float:
        jitter = base * 0.25 * (random.random() * 2 - 1)
        return max(base + jitter, 0.1)
