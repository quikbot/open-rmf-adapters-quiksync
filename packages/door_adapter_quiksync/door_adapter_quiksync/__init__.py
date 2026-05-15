"""QuikSync door adapter — bridges QuikSync-managed doors into Open-RMF.

Publishes `/door_states` (DoorState) from per-door WSS state frames on
`/api/connector/ws/open-rmf/doors/<door>/state/subscribe`, and forwards
`/door_requests` (DoorRequest) into HTTPS POSTs against
`/api/v1/connector/open-rmf/doors/<door>/request` with `execution_id`-based
idempotency.

Multi-namespace orgs scope the adapter process to one namespace via the
`namespace:` config field (forwarded as `?namespace=<value>` on every
REST + WSS call).
"""

__version__ = "0.2.2"  # x-release-please-version
