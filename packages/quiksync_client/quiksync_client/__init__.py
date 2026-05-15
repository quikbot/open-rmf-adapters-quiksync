"""HTTPS + WSS client for the QuikSync adapter API surface.

Shared core used by `fleet_adapter_quiksync`, `door_adapter_quiksync`,
`lift_adapter_quiksync`. Hides the per-adapter Auth0 + httpx + WebSocket
plumbing behind a typed interface.

Public surface:
- `AuthConfig`, `Auth0M2MClient`, `AuthError`
- `HttpConfig`, `QuikSyncHttpClient`, `QuikSyncClientError`, `QuikSyncServerError`,
  `QuikSyncConnectionError`
- `WsConfig`, `QuikSyncWsClient`, `WsCircuitOpen`
- Pydantic models in `quiksync_client.types`
"""

from .auth import Auth0M2MClient, AuthConfig, AuthError
from .http import (
    QuikSyncClientError,
    QuikSyncConnectionError,
    QuikSyncHttpClient,
    QuikSyncServerError,
    HttpConfig,
)
from .time_conv import millis_to_time_parts
from .ws import QuikSyncWsClient, WsCircuitOpen, WsConfig

__version__ = "0.2.1"  # x-release-please-version

__all__ = [
    "Auth0M2MClient",
    "AuthConfig",
    "AuthError",
    "QuikSyncHttpClient",
    "QuikSyncClientError",
    "QuikSyncConnectionError",
    "QuikSyncServerError",
    "HttpConfig",
    "QuikSyncWsClient",
    "WsCircuitOpen",
    "WsConfig",
    "millis_to_time_parts",
    "__version__",
]
