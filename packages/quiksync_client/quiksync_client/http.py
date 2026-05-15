"""REST client for `/api/v1/connector/open-rmf/*` endpoints.

Auth: Bearer token from `Auth0M2MClient` (auth.py). Retries: 3 with
jittered exponential backoff on connection errors and 5xx. Idempotency:
caller MUST supply a unique `execution_id` per logical operation — the
QuikSync server dedups by it on the receive side, which makes our retry
storm safe.

Error mapping: 4xx → `QuikSyncClientError(status, error_code, body)`;
5xx after retries → `QuikSyncServerError(status, body)`; transport
failures after retries → `QuikSyncConnectionError`.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .auth import Auth0M2MClient

log = logging.getLogger("quiksync_client.http")


@dataclass
class HttpConfig:
    base_url: str  # e.g. "https://<your-quiksync-host>"
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0


_ERROR_BODY_LOG_LIMIT = 200
"""Cap the body excerpt rendered in error `str()`/`repr()`.

The body eventually surfaces in adapter log lines, which may be shipped
to centralised log aggregators. If a future server-side error path
echoes input back in the error body (e.g. a `perform_action`
`description` that carries customer-supplied JSON), this cap bounds the
passive PII / PHI leak surface."""


class QuikSyncClientError(Exception):
    """Non-retried 4xx response — adapter surfaces to Open-RMF as `failed()`."""

    def __init__(self, status: int, error_code: Optional[str], body: dict[str, Any]) -> None:
        body_repr = repr(body)
        if len(body_repr) > _ERROR_BODY_LOG_LIMIT:
            body_repr = body_repr[:_ERROR_BODY_LOG_LIMIT] + "...(truncated)"
        super().__init__(f"HTTP {status} {error_code or '?'}: {body_repr}")
        self.status = status
        self.error_code = error_code
        # Full body retained on the attribute — only the str(exception) form
        # is truncated. Callers that want the full body for structured
        # logging / diagnostic dumps can access `.body` directly.
        self.body = body


class QuikSyncServerError(Exception):
    """5xx after retries exhausted — adapter surfaces to Open-RMF; usually transient."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class QuikSyncConnectionError(Exception):
    """Transport failure after retries exhausted."""


class QuikSyncHttpClient:
    def __init__(self, config: HttpConfig, auth: Auth0M2MClient) -> None:
        self._config = config
        self._auth = auth
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds),
        )

    def close(self) -> None:
        self._client.close()

    # ----- QuikSync Open-RMF adapter endpoints -----

    def get_discovery(self, namespace: Optional[str] = None) -> dict[str, Any]:
        return self._request_json(
            "GET", "/api/v1/connector/open-rmf/discovery",
            params=_ns_params(namespace),
        )

    def get_building_map(self, namespace: Optional[str] = None) -> dict[str, Any]:
        return self._request_json(
            "GET", "/api/v1/connector/open-rmf/building_map",
            params=_ns_params(namespace),
        )

    def get_fleet_state(self, fleet: str, namespace: Optional[str] = None) -> dict[str, Any]:
        return self._request_json(
            "GET", f"/api/v1/connector/open-rmf/fleets/{fleet}/state",
            params=_ns_params(namespace),
        )

    def post_navigate(
        self,
        fleet: str,
        robot: str,
        execution_id: str,
        destination: dict[str, Any],
        dock_name: Optional[str] = None,
        speed_limit: Optional[float] = None,
        deadline_unix_millis: Optional[int] = None,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "execution_id": execution_id,
            "destination": destination,
        }
        if dock_name is not None:
            body["dock_name"] = dock_name
        if speed_limit is not None:
            body["speed_limit"] = speed_limit
        if deadline_unix_millis is not None:
            body["deadline_unix_millis"] = deadline_unix_millis
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/navigate",
            body=body, params=_ns_params(namespace),
        )

    def post_stop(
        self, fleet: str, robot: str, execution_id: str,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/stop",
            body={"execution_id": execution_id},
            params=_ns_params(namespace),
        )

    def post_perform_action(
        self,
        fleet: str,
        robot: str,
        execution_id: str,
        category: str,
        description: Any,
        deadline_unix_millis: Optional[int] = None,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        """Forward an Open-RMF `perform_action` task phase to the QuikSync server.

        The (category, description) pair is opaque to the adapter; the
        server-side `perform_action_map.yaml` resolves it to a workflow.
        Unknown categories return 400 — caller surfaces to Open-RMF.
        """
        body: dict[str, Any] = {
            "execution_id": execution_id,
            "category": category,
            "description": description,
        }
        if deadline_unix_millis is not None:
            body["deadline_unix_millis"] = deadline_unix_millis
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/perform_action",
            body=body, params=_ns_params(namespace),
        )

    def get_task_state(self, task_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/connector/open-rmf/tasks/{task_id}/state")

    # ----- Door endpoints -----

    def get_door_state(self, door: str, namespace: Optional[str] = None) -> dict[str, Any]:
        """Single-shot door-state read. WSS subscribe is the steady-state
        path; this is for cold-start / smoke / health probes.

        `door` is the raw door_name as discovered; URL-encoding is
        handled by httpx.
        """
        return self._request_json(
            "GET", f"/api/v1/connector/open-rmf/doors/{door}/state",
            params=_ns_params(namespace),
        )

    def post_door_request(
        self,
        door: str,
        requester_id: str,
        requested_mode: str,
        execution_id: str,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        """Forward an Open-RMF `DoorRequest` to the QuikSync server.

        `requested_mode` must be `"OPEN"` or `"CLOSED"`; the server
        rejects `"MOVING"` with 400 `invalid_request_mode`. Same
        idempotency contract as the other POSTs — repeated calls with
        the same `execution_id` are server-side-deduped.
        """
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/doors/{door}/request",
            body={
                "requester_id": requester_id,
                "requested_mode": requested_mode,
                "execution_id": execution_id,
            },
            params=_ns_params(namespace),
        )

    # ----- Lift endpoints -----

    def get_lift_state(self, lift: str, namespace: Optional[str] = None) -> dict[str, Any]:
        """Single-shot lift-state read. WSS subscribe is the steady-
        state path; this is for cold-start / smoke / health probes.

        `lift` is the raw lift_name as discovered; URL-encoding is
        handled by httpx.
        """
        return self._request_json(
            "GET", f"/api/v1/connector/open-rmf/lifts/{lift}/state",
            params=_ns_params(namespace),
        )

    def post_lift_request(
        self,
        lift: str,
        session_id: str,
        request_type: str,
        destination_floor: str,
        door_state: str,
        execution_id: str,
        namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        """Forward an Open-RMF `LiftRequest` to the QuikSync server.

        - `request_type` must be one of `"END_SESSION"`, `"AGV_MODE"`,
          `"HUMAN_MODE"`. NO_REQUEST is the rmf-side no-op sentinel and
          must NOT reach this method — the caller short-circuits earlier.
        - `door_state` must be `"OPEN"` or `"CLOSED"`; the server
          rejects `"MOVING"` with 400 `invalid_door_state`.
        - `AGV_MODE` requests attempt to acquire the lift's session
          lock; if held by another `session_id` the server returns 409
          with a `holding_session_id` field echoing the current holder.

        Idempotency: repeated calls with the same `execution_id` are
        server-side-deduped.
        """
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/lifts/{lift}/request",
            body={
                "session_id": session_id,
                "request_type": request_type,
                "destination_floor": destination_floor,
                "door_state": door_state,
                "execution_id": execution_id,
            },
            params=_ns_params(namespace),
        )

    def delete_lift_session(
        self, lift: str, namespace: Optional[str] = None,
    ) -> dict[str, Any]:
        """Admin force-clear the lift's session lock.

        Requires the `open-rmf:invoke` scope on the M2M token. Used to
        recover from a stuck session on the server side (e.g. a fleet
        crashed mid-session and never sent END_SESSION).
        """
        return self._request_json(
            "DELETE", f"/api/v1/connector/open-rmf/lifts/{lift}/session",
            params=_ns_params(namespace),
        )

    # ----- Core request loop -----

    def _request_json(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self._config.max_retries + 1):
            try:
                token = self._auth.get_token()
                headers = {"authorization": f"Bearer {token}"}
                if body is not None:
                    headers["content-type"] = "application/json"
                resp = self._client.request(method, path, headers=headers, json=body, params=params)
            except httpx.HTTPError as e:
                last_error = e
                if attempt < self._config.max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise QuikSyncConnectionError(f"{method} {path}: {e}") from e

            # 4xx → no retry, raise structured error
            if 400 <= resp.status_code < 500:
                payload = self._parse_body(resp)
                # 401 may indicate token refresh needed; one retry with force-refresh
                if resp.status_code == 401 and attempt == 0:
                    log.info("Got 401; forcing token refresh and retrying once")
                    self._auth.get_token(force_refresh=True)
                    continue
                raise QuikSyncClientError(
                    resp.status_code, payload.get("error") if isinstance(payload, dict) else None, payload,
                )

            # 5xx → retry
            if resp.status_code >= 500:
                last_error = QuikSyncServerError(resp.status_code, resp.text)
                if attempt < self._config.max_retries:
                    log.info(
                        "Got %d on %s %s (attempt %d/%d); retrying with backoff",
                        resp.status_code, method, path, attempt + 1, self._config.max_retries,
                    )
                    self._sleep_backoff(attempt)
                    continue
                raise last_error

            # 2xx
            return self._parse_body(resp)

        # If we exit the loop without returning/raising, surface the last error.
        if last_error is not None:
            raise last_error
        raise QuikSyncConnectionError(f"{method} {path}: exhausted retries")

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"_raw_body": resp.text}

    def _sleep_backoff(self, attempt: int) -> None:
        base = self._config.backoff_base_seconds * (2 ** attempt)
        sleep_for = min(base, self._config.backoff_max_seconds)
        # Jitter ±25% so concurrent retries don't synchronise.
        jitter = sleep_for * 0.25 * (random.random() * 2 - 1)
        time.sleep(max(sleep_for + jitter, 0.05))


def _ns_params(namespace: Optional[str]) -> Optional[dict[str, str]]:
    """Build the namespace query-param dict, or None when unset.

    Multi-namespace orgs scope the QuikSync server's lookup via
    `?namespace=<value>`. Single-namespace orgs (most deployments)
    leave the kwarg unset and the server resolves by `org_id` alone.
    """
    return {"namespace": namespace} if namespace else None
