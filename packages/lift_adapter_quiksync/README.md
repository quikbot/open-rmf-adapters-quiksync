# lift_adapter_quiksync

Open-RMF lift adapter for QuikSync-managed lifts.

Bridges the [`rmf_lift_msgs`](https://github.com/open-rmf/rmf_internal_msgs)
ROS topics (`lift_states` publish + `lift_requests` subscribe) to the
QuikSync HTTP + WSS surface served by the QuikSync Open-RMF Connector.

## Process model

One rclpy node owns N lifts. Per-lift state pumps subscribe to
`/api/connector/ws/open-rmf/lifts/{lift}/state/subscribe` and republish
each frame as `rmf_lift_msgs/LiftState`. Inbound `rmf_lift_msgs/LiftRequest`
messages are translated and forwarded to
`POST /api/v1/connector/open-rmf/lifts/{lift}/request`.

Deviates from the canonical
[`lift_adapter_template`](https://github.com/open-rmf/lift_adapter_template)
one-process-per-lift pattern for the same operational reason as the door
adapter — shared clients across lifts in one container.

### Adapter-side session occupant lock

Lifts have an additional concern over doors: they're a shared resource
that one fleet acquires for the duration of a transit. The QuikSync
server owns the authoritative session lock via a Hazelcast IMap; this
adapter layers a **`LiftSessionManager`** on top as defense-in-depth.

It tracks per-lift the most recent `session_id` RMF asked for and the
most recent `session_id` the server reports holds the lock, reconciling
them on every state-push frame. AGV_MODE requests for a session that
conflicts with the current adapter view are short-circuited locally
(no POST hits the wire), avoiding the noisy 409 path. Pattern is lifted
from the [Octa `lci-rmf-adapter`](https://github.com/octarobotics/lci-rmf-adapter)
reference implementation.

## Configuration

```yaml
quiksync:
  base_url: https://<your-quiksync-host>
  auth0_tenant: <your-tenant>.auth0.com
  auth0_audience: https://<your-quiksync-api-audience>
  auth0_client_id: <m2m-client-id>
  auth0_client_secret_file: /run/secrets/quiksync_adapter_credentials
  auth0_organization: <auth0-org-id>
  lifts:
    - lift_alpha
  # optional tuning + ROS topic remaps
  state_subscribe_reconnect_seconds: 1.0
  session_ttl_seconds: 3600.0     # mirrors server-side session-lock TTL
  lift_states_topic: lift_states
  lift_requests_topic: lift_requests
```

Full template: [`config/quiksync.yaml.example`](config/quiksync.yaml.example).

## Launch

```bash
ros2 launch lift_adapter_quiksync lift_adapter_quiksync.launch.xml \
  config:=/etc/quiksync/lift.yaml
```

Or `--dry-run` for the smoke path:

```bash
LIFT_ADAPTER_BASE_URL=... LIFT_ADAPTER_LIFTS=lift_alpha \
  python3 -m lift_adapter_quiksync.adapter --dry-run
```

## Wire-shape notes

- WSS state frame:
  `{lift_name, lift_time (unix ms), current_floor, destination_floor,
  door_state, motion_state, available_modes: [{value}], current_mode: {value},
  session_id}`.
  - `lift_time` ms → `builtin_interfaces/Time {sec, nanosec}` translation
    via `quiksync_client.millis_to_time_parts`.
  - `available_modes` flattens from `[{value: 2}, {value: 4}]` (wire) to
    `uint8[]` `[2, 4]` (ROS msg).
- REST request body:
  `{session_id, request_type: "END_SESSION"|"AGV_MODE"|"HUMAN_MODE",
  destination_floor, door_state: "OPEN"|"CLOSED", execution_id}`.
  - `NO_REQUEST` is the rmf-side no-op sentinel; the adapter short-circuits
    before any POST.
  - `MOVING` door_state is rejected at the server with 400 — the adapter
    rejects locally before the POST.
  - `AGV_MODE` requests pass through `LiftSessionManager.try_acquire`
    first; conflicting sessions short-circuit without a POST.

## Multi-deck / multi-side lifts

v0.2 exposes primary `(deck, side)` only. Multi-shape elevators (multi-
deck, front+rear door, etc.) are deferred — the server-side discovery
emits a `platform.open_rmf.lift.unmapped_axes` observability event when
it detects an unsupported configuration, so the gap surfaces explicitly.

## License

Apache 2.0 — see the root [`LICENSE`](../../LICENSE).
