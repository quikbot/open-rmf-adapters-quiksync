"""REST client for `/api/v1/connector/open-rmf/*` endpoints.

Auth: Bearer token from `Auth0M2MClient` (auth.py). Retries: 3 with
jittered exponential backoff on connection errors and 5xx. Idempotency:
caller MUST supply a unique `execution_id` per logical operation —
server-side dedup is keyed on it (per design §4.5), but our retry storm
is what makes that work.

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


class QuikSyncClientError(Exception):
    """Non-retried 4xx response — adapter surfaces to Open-RMF as `failed()`."""

    def __init__(self, status: int, error_code: Optional[str], body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status} {error_code or '?'}: {body}")
        self.status = status
        self.error_code = error_code
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

    def get_discovery(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/connector/open-rmf/discovery")

    def get_building_map(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/connector/open-rmf/building_map")

    def get_fleet_state(self, fleet: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/connector/open-rmf/fleets/{fleet}/state")

    def post_navigate(
        self,
        fleet: str,
        robot: str,
        execution_id: str,
        destination: dict[str, Any],
        dock_name: Optional[str] = None,
        speed_limit: Optional[float] = None,
        deadline_unix_millis: Optional[int] = None,
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
            "POST", f"/api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/navigate", body=body,
        )

    def post_stop(self, fleet: str, robot: str, execution_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST", f"/api/v1/connector/open-rmf/fleets/{fleet}/robots/{robot}/stop",
            body={"execution_id": execution_id},
        )

    def post_perform_action(
        self,
        fleet: str,
        robot: str,
        execution_id: str,
        category: str,
        description: Any,
        deadline_unix_millis: Optional[int] = None,
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
            body=body,
        )

    def get_task_state(self, task_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/v1/connector/open-rmf/tasks/{task_id}/state")

    # ----- Core request loop -----

    def _request_json(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self._config.max_retries + 1):
            try:
                token = self._auth.get_token()
                headers = {"authorization": f"Bearer {token}"}
                if body is not None:
                    headers["content-type"] = "application/json"
                resp = self._client.request(method, path, headers=headers, json=body)
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
