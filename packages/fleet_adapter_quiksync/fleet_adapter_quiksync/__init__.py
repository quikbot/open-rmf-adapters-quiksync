"""QuikSync fleet adapter — `RobotCallbacks(navigate, stop, action_executor)`.

Registers QuikSync-managed fleets with the customer's Open-RMF deployment via
Open-RMF's `EasyFullControl` Python API. Subscribes to `/api/connector/ws/open-rmf/
fleets/<fleet>/state/subscribe` for fleet state updates and forwards each
robot's state into Open-RMF via `EasyRobotUpdateHandle.update(state, activity)`.

Outbound Open-RMF callbacks (navigate / stop / action_executor) translate to
HTTPS POSTs against the `/api/v1/connector/open-rmf/fleets/<fleet>/robots/
<robot>/{navigate,stop,perform_action}` endpoints with `execution_id`-based
idempotency.

v1.0: implementation in progress. The package compiles + the entry-point
runs; full implementation lands in subsequent commits per the v1 plan.
"""

__version__ = "0.1.2"  # x-release-please-version
