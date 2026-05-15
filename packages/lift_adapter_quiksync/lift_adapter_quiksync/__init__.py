"""QuikSync lift adapter — bridges QuikSync-managed lifts into Open-RMF.

Publishes `/lift_states` (LiftState) from per-lift WSS state frames on
`/api/connector/ws/open-rmf/lifts/<lift>/state/subscribe`, and forwards
`/lift_requests` (LiftRequest) into HTTPS POSTs against
`/api/v1/connector/open-rmf/lifts/<lift>/request` with `execution_id`-based
idempotency. Layers a `LiftSessionManager` on top of the server's
authoritative session lock as defense-in-depth — short-circuits obvious
session-conflict POSTs locally.

Multi-namespace orgs scope the adapter process to one namespace via the
`namespace:` config field (forwarded as `?namespace=<value>` on every
REST + WSS call).
"""

__version__ = "0.2.2"  # x-release-please-version
