# door_adapter_quiksync

Open-RMF door adapter for QuikSync-managed doors.

Bridges the [`rmf_door_msgs`](https://github.com/open-rmf/rmf_internal_msgs)
ROS topics (`door_states` publish + `door_requests` subscribe) to the
QuikSync HTTP + WSS surface served by the QuikSync Open-RMF Connector.

## Process model

One rclpy node owns N doors. The node is configured via a single YAML
listing the door IDs to manage; per-door state pumps subscribe to
`/api/connector/ws/open-rmf/doors/{door}/state/subscribe` and republish
each frame as `rmf_door_msgs/DoorState`. Inbound `rmf_door_msgs/DoorRequest`
messages are translated and forwarded to
`POST /api/v1/connector/open-rmf/doors/{door}/request`.

This deviates from the canonical
[`door_adapter_template`](https://github.com/open-rmf/door_adapter_template)
one-process-per-door pattern (sharing Auth0 / HTTP / WSS clients across
doors in one container is operationally simpler) — same pattern as the
Octa LCI + Megazo reference adapters.

## Configuration

```yaml
quiksync:
  base_url: https://<your-quiksync-host>
  auth0_tenant: <your-tenant>.auth0.com
  auth0_audience: https://<your-quiksync-api-audience>
  auth0_client_id: <m2m-client-id>
  auth0_client_secret_file: /run/secrets/quiksync_adapter_credentials
  auth0_organization: <auth0-org-id>
  doors:
    - door_alpha
    - door_beta
  # optional tuning + ROS topic remaps
  state_subscribe_reconnect_seconds: 1.0
  door_states_topic: door_states
  door_requests_topic: door_requests
```

Full template: [`config/quiksync.yaml.example`](config/quiksync.yaml.example).

## Launch

```bash
ros2 launch door_adapter_quiksync door_adapter_quiksync.launch.xml \
  config:=/etc/quiksync/door.yaml
```

Or `--dry-run` for the smoke path (no rclpy required — exercises auth +
HTTP + WSS plumbing end-to-end):

```bash
DOOR_ADAPTER_BASE_URL=... DOOR_ADAPTER_DOORS=door_alpha \
  python3 -m door_adapter_quiksync.adapter --dry-run
```

## Wire-shape notes

- WSS state frame: `{door_name, door_time (unix ms), current_mode: {value}}`.
  `door_time` ms → `builtin_interfaces/Time {sec, nanosec}` translation
  happens at the `DoorState` msg construction site (one-liner via
  `quiksync_client.millis_to_time_parts`). The wire-millis shape matches
  rmf-web's `unix_millis_time` convention across robot / door / lift /
  task surfaces.
- REST request body:
  `{requester_id, requested_mode: "OPEN"|"CLOSED", execution_id}`.
  `MODE_MOVING` is rejected at the server with 400 — the adapter rejects
  locally before the POST. `execution_id` is server-side-deduped.

## License

Apache 2.0 — see the root [`LICENSE`](../../LICENSE).
