"""QuikSync fleet adapter — `RobotCallbacks(navigate, stop, action_executor)`.

Registers QuikSync-managed fleets with the customer's Open-RMF deployment via
Open-RMF's `EasyFullControl` Python API. Subscribes to `/api/connector/ws/open-rmf/
fleets/<fleet>/state/subscribe` for fleet state updates and forwards each
robot's state into Open-RMF via `EasyRobotUpdateHandle.update(state, activity)`.

Outbound Open-RMF callbacks (navigate / stop / action_executor) translate to
HTTPS POSTs against the `/api/v1/connector/open-rmf/fleets/<fleet>/robots/
<robot>/{navigate,stop,perform_action}` endpoints with `execution_id`-based
idempotency.

Robots register lazily: the first WSS state frame whose pose lies on the
nav graph triggers `EasyFullControl.add_robot`, so the initial RobotState
carries real on-graph coordinates rather than a placeholder.

Multi-namespace orgs scope each adapter process to one namespace via the
`namespace:` config field (forwarded as `?namespace=<value>` on every
REST + WSS call).
"""

__version__ = "0.2.2"  # x-release-please-version
