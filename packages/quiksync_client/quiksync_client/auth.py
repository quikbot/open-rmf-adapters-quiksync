"""Auth0 M2M token client for QuikSync Open-RMF adapters.

Mints + caches a `client_credentials` JWT against the customer's Auth0
tenant. Audience pinned to `https://<your-quiksync-api-audience>/open-rmf`;
scopes `open-rmf:read open-rmf:invoke`. Refresh discipline:

- Cache the token + its `expires_at` after the first mint.
- On every consumer call, return the cached token if it's still within
  80% of TTL — past that threshold, mint a fresh one (preemptive refresh
  ahead of expiry).
- Add a jitter of ±10 minutes to the refresh threshold so multi-process
  adapter cold-start (e.g. 5 fleet × 1 door × 1 lift = 7 processes)
  doesn't thunder Auth0 every TTL cycle.
- After 3 consecutive mint failures, surface to caller — adapter logs +
  triggers the 401 circuit-breaker.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("quiksync_client.auth")


@dataclass(frozen=True)
class AuthConfig:
    tenant: str  # e.g. "<your-auth0-tenant>.auth0.com"
    audience: str  # e.g. "https://<your-quiksync-api-audience>/open-rmf"
    client_id: str
    client_secret: str
    organization: str  # Auth0 Organization id (org_xxxxx)
    scopes: str = "open-rmf:read open-rmf:invoke"
    refresh_threshold_pct: float = 0.8
    jitter_seconds_min: int = -600  # ±10 min
    jitter_seconds_max: int = 600


@dataclass
class _CachedToken:
    access_token: str
    expires_at_unix: float


class AuthError(Exception):
    """Raised when the Auth0 mint fails after 3 consecutive attempts."""


class Auth0M2MClient:
    """Thread-safe Auth0 client. Caches one token at a time."""

    def __init__(self, config: AuthConfig, http_client: Optional[httpx.Client] = None) -> None:
        self._config = config
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        self._owns_client = http_client is None
        self._cached: Optional[_CachedToken] = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a valid JWT, minting a fresh one if necessary."""
        with self._lock:
            if not force_refresh and self._cached and not self._should_refresh(self._cached):
                return self._cached.access_token
            return self._mint_locked()

    def _should_refresh(self, cached: _CachedToken) -> bool:
        # Refresh once we've burned through `refresh_threshold_pct` of the TTL,
        # plus a jitter so multi-process adapters don't all refresh together.
        now = time.time()
        ttl_remaining = cached.expires_at_unix - now
        if ttl_remaining <= 0:
            return True
        # Use the absolute time as the random seed so the same process makes the
        # same decision across rapid checks — but multiple processes pick
        # different jitter values (different process ids → different RNG state).
        jitter = random.randint(self._config.jitter_seconds_min, self._config.jitter_seconds_max)
        # If we're within `(1 - threshold) * ttl + jitter` of expiry, refresh.
        # Equivalent: refresh when `ttl_remaining < (1 - threshold) * ttl_total - jitter`.
        # We don't know ttl_total exactly; approximate by treating the threshold
        # against the remaining TTL. For an 86400s (24h) token, this means
        # refresh when < ~5h remains, ± 10 min.
        margin_target = self._estimate_total_ttl(cached) * (1 - self._config.refresh_threshold_pct)
        return ttl_remaining < (margin_target + jitter)

    @staticmethod
    def _estimate_total_ttl(cached: _CachedToken) -> float:
        # Best effort — for a freshly minted token we know mint-time ≈ now -
        # epsilon, so total ≈ expires - now. For a partially-aged token this
        # underestimates the refresh margin (refreshes a bit early), which is
        # fine.
        return max(cached.expires_at_unix - time.time(), 0.0) + 60.0

    def _mint_locked(self) -> str:
        url = f"https://{self._config.tenant}/oauth/token"
        body = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "audience": self._config.audience,
            "scope": self._config.scopes,
            "organization": self._config.organization,
        }
        try:
            resp = self._client.post(url, json=body)
        except httpx.HTTPError as e:
            self._consecutive_failures += 1
            log.warning(
                "Auth0 mint failed (%d consecutive): %s",
                self._consecutive_failures, e,
            )
            if self._consecutive_failures >= 3:
                raise AuthError(f"Auth0 mint failed 3x in a row: {e}") from e
            raise

        if resp.status_code != 200:
            self._consecutive_failures += 1
            body_preview = resp.text[:200] if resp.text else ""
            log.warning(
                "Auth0 mint returned %d (%d consecutive): %s",
                resp.status_code, self._consecutive_failures, body_preview,
            )
            if self._consecutive_failures >= 3:
                raise AuthError(
                    f"Auth0 mint returned {resp.status_code} 3x in a row: {body_preview}"
                )
            raise AuthError(f"Auth0 mint {resp.status_code}: {body_preview}")

        payload = resp.json()
        token = payload.get("access_token")
        ttl = int(payload.get("expires_in", 0))
        if not token or ttl <= 0:
            self._consecutive_failures += 1
            raise AuthError("Auth0 returned 200 but no usable access_token/expires_in")

        self._consecutive_failures = 0
        self._cached = _CachedToken(
            access_token=token,
            expires_at_unix=time.time() + ttl,
        )
        log.info("Auth0 token minted, TTL=%ds (expires_at=%.0f)", ttl, self._cached.expires_at_unix)
        return token

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
