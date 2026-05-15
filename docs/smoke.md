# Live smoke procedure — QuikSync Open-RMF adapters

Manual smoke procedures for verifying the QuikSync `fleet_adapter_quiksync`,
`door_adapter_quiksync`, and `lift_adapter_quiksync` against a real
Open-RMF deployment. CI exercises only the dry-run paths (no
`rmf_adapter` / `rmf_door_msgs` / `rmf_lift_msgs`); the live wire-up
runs only against a real deployment with the `rmf_ros2` stack installed.

Run the relevant section(s) whenever:

- A new release candidate is cut.
- The QuikSync adapter API contract changes (REST or WSS shape) for
  the affected resource type.
- A customer reports a regression that needs the adapter side
  investigated.

§1–§4 cover the fleet adapter; §5 covers the door adapter; §6 covers
the lift adapter. Each section is self-contained — the door and lift
procedures don't require running the fleet smoke first.

## Prerequisites

1. **A QuikSync staging environment** with the Open-RMF adapter API
   enabled. Sanity-check by hitting `GET /api/v1/connector/open-rmf/discovery`
   with a valid M2M token — it should return a non-empty `fleets[]`
   array.
2. **An Auth0 M2M client** minted for your test customer org, with the
   audience `https://<your-quiksync-api-audience>` and scopes
   `open-rmf:read open-rmf:invoke`. QuikSync ops provisions these
   per-customer. The audience is the **standard QuikSync API audience**
   shared with all other platform scopes — open-rmf access is granted
   via the two scopes above on that same audience, not via a separate
   per-service audience.
3. **A registered fleet** in the staging tenant. The fleet name must
   match what you pass via `FLEET_ADAPTER_FLEET_NAME`. At least one
   robot must be online so `FleetState` frames flow.
4. **A real Open-RMF deployment** with `rmf_ros2` on ROS 2 Jazzy +
   `rmf_internal_msgs >= 2.3`. The deployment must have a `nav_graph`
   matching the fleet's advertised graph (see `/discovery` →
   `nav_graph_name`).
5. **Building map** (`building.yaml`) parsed into a graph object whose
   name matches the fleet's `nav_graph_name`.

## Step-by-step

### 1. Dry-run round-trip (no `rmf_adapter`)

Validates auth + HTTP + WSS plumbing end-to-end without needing the
Open-RMF stack. Run from any host (laptop, dev box, CI).

```bash
# From a checkout of this repo:
docker build -t open-rmf-adapters-quiksync:smoke \
    -f docker/Dockerfile .

docker run --rm \
    -e FLEET_ADAPTER_BASE_URL=https://<your-quiksync-staging-host> \
    -e FLEET_ADAPTER_AUTH0_TENANT=<your-auth0-tenant>.auth0.com \
    -e FLEET_ADAPTER_AUTH0_AUDIENCE=https://<your-quiksync-api-audience> \
    -e FLEET_ADAPTER_AUTH0_CLIENT_ID="$ADAPTER_CLIENT_ID" \
    -e FLEET_ADAPTER_AUTH0_CLIENT_SECRET="$ADAPTER_CLIENT_SECRET" \
    -e FLEET_ADAPTER_AUTH0_ORGANIZATION="$ADAPTER_ORG_ID" \
    -e FLEET_ADAPTER_FLEET_NAME="$ADAPTER_FLEET_NAME" \
    open-rmf-adapters-quiksync:smoke \
    python -m fleet_adapter_quiksync.adapter --dry-run
```

> **SOCKS proxy environments**: if the host has `ALL_PROXY` /
> `HTTPS_PROXY` set to a `socks5://` URL (common on dev laptops
> behind VPN tools), both the docker image and any `pip install -e`
> path need the SOCKS shims. The docker image ships
> `httpx[socks]` + `python-socks` by default; for a non-docker
> `pip install -e`, run `pip install 'httpx[socks]' python-socks`
> manually. Without them, you'll see
> `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`
> on the first HTTPS call.

**Expected exit code:** `0` if at least one WSS state frame arrived
within 3 seconds. **Expected log lines:**

```
fleet_adapter_quiksync.adapter: fleet_adapter_quiksync starting: fleet=...
quiksync_client.ws: WSS connected: /api/connector/ws/open-rmf/fleets/<fleet>/state/subscribe
fleet_adapter_quiksync.adapter: dry-run complete: frames=N robots_dispatched=M ...
```

**Failure modes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `discovery fetch failed: HTTP 401` | M2M token missing `open-rmf:read` scope, or wrong audience | Re-mint the M2M client with the right scopes |
| `fleet '...' not found in discovery` | The fleet name doesn't match a registered fleet for your org | Verify the fleet is registered in the staging tenant and visible to this Auth0 org |
| `WSS upgrade failed (status=401)` | `?access_token=<jwt>` query parameter not honored, or token audience mismatch | Verify the WSS path is the `/api/connector/ws/open-rmf/...` shape; verify the M2M token's `aud` claim |
| `frames=0` after timeout | No robot online in fleet, or WSS connected but server isn't pushing | Verify at least one robot is registered and online; check server-side state-publish logs |

If dry-run is green, the auth + HTTP + WSS surfaces are healthy. Move on.

### 2. Full Open-RMF wire-up

Validates the EasyFullControl binding: the adapter registers the fleet
with Open-RMF, robots become visible in Open-RMF's planner, and commands flow.

```bash
# On the Open-RMF deployment host (or a host with rmf_ros2 sourced):
source /opt/ros/jazzy/setup.bash
source /path/to/rmf_ws/install/setup.bash

ros2 launch fleet_adapter_quiksync fleet_adapter_quiksync.launch.xml \
    config:=/path/to/quiksync.yaml \
    client_id:=$ADAPTER_CLIENT_ID \
    client_secret_file:=/run/secrets/quiksync_adapter_credentials
```

**Expected ROS 2 topics** (verify via `ros2 topic echo` in another shell):

- `/fleet_states` — `FleetState` messages for the adapter's fleet at
  the configured `update_interval_seconds` cadence (default 0.5 s).
- `/robot_state` — per-robot status updates.

**Expected Open-RMF dashboard view:** the fleet appears in the Open-RMF web UI
(`rmf-web` dashboard) with robots listed and battery / location
populated from QuikSync's `FleetState`.

### 3. Round-trip a `go_to_place` task

The minimum end-to-end smoke. Dispatch a task via Open-RMF's task
dispatcher; verify the adapter forwards it to QuikSync, the robot
moves, and state reflects completion back to Open-RMF.

```bash
# Open-RMF host:
ros2 run rmf_demos_tasks dispatch_go_to_place \
    -F <fleet_name> -R <robot_name> -p <waypoint_name>
```

**Expected sequence:**

1. Open-RMF planner picks the dispatched robot, calls `navigate(destination)`
   on the adapter.
2. Adapter logs `navigate(<fleet>/<robot>) dispatched: execution_id=...
   task_id=...`.
3. The QuikSync server accepts the navigate request and queues the
   underlying command.
4. The robot moves; `FleetState` frames stream the position updates
   over WSS.
5. On arrival, the underlying QuikSync command's state flips to
   complete; the next `FleetState` carries `task_id=null`.
6. Open-RMF dashboard shows the task as completed.

**Failure modes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `navigate` returns 400 `coord_navigate_not_supported` | Destination coord doesn't resolve to a named waypoint in the fleet's nav graph | Confirm the planner is dispatching by named place; check `nav_graph_name` alignment with the deployed building map |
| Task never moves out of `underway` on Open-RMF dashboard | State pump not pushing into Open-RMF — `EasyRobotUpdateHandle.update()` silently failing | Check adapter logs for `EasyRobotUpdateHandle.update failed for robot=...` |
| Task marked `failed` immediately after dispatch | Auth issue on the POST (likely 401 — token didn't refresh) | Restart adapter; check Auth0 client TTL; verify preemptive token refresh runs |
| Robot moves but Open-RMF never sees completion | Server-side `task_id` correlation issue; state frame's `task_id` field not matching the dispatched `execution_id` | Verify server-side state shaping; capture adapter log + server audit trail for the failing dispatch |

### 4. (Optional) Round-trip a `perform_action`

If the staging tenant has a `perform_action` category mapping
configured, dispatch one and verify the corresponding server-side
workflow fires.

```bash
ros2 run rmf_demos_tasks dispatch_perform_action \
    -F <fleet_name> -R <robot_name> -a <category> -d '{"...":...}'
```

The adapter forwards `(category, description)` opaquely. Success path:
the server-side workflow fires and completes, state returns to idle on
Open-RMF. Unknown categories return 400 — the mapping table is configured
server-side.

## Exit criteria

For a release: steps 1–3 pass against staging for the test fleet.
Step 4 is optional unless the pilot customer has `perform_action` in
scope.

Document the run with: timestamp, staging environment, fleet name,
robot name(s), dispatch command, observed behavior. Paste the adapter
log tail covering one full task lifecycle. Attach to the release notes
or an open issue against this repo.

## 5. Door adapter smoke

### Prerequisites (door-specific)

In addition to the universal Auth0 + staging items in the top-level
"Prerequisites" list:

1. **At least one door** registered in the QuikSync staging tenant.
   Sanity-check by hitting `GET /api/v1/connector/open-rmf/discovery`
   and confirming the `doors[]` array carries your test door's
   `door_name`.
2. **`rmf_door_msgs >= 2.3`** on the live host (comes with the
   Open-RMF stack).

### 5.1 Dry-run round-trip (no `rclpy`)

Validates auth + HTTP + WSS plumbing end-to-end for the door surface,
without needing ROS. Run from any host.

```bash
docker run --rm \
    -e DOOR_ADAPTER_BASE_URL=https://<your-quiksync-staging-host> \
    -e DOOR_ADAPTER_AUTH0_TENANT=<your-auth0-tenant>.auth0.com \
    -e DOOR_ADAPTER_AUTH0_AUDIENCE=https://<your-quiksync-api-audience> \
    -e DOOR_ADAPTER_AUTH0_CLIENT_ID="$ADAPTER_CLIENT_ID" \
    -e DOOR_ADAPTER_AUTH0_CLIENT_SECRET="$ADAPTER_CLIENT_SECRET" \
    -e DOOR_ADAPTER_AUTH0_ORGANIZATION="$ADAPTER_ORG_ID" \
    -e DOOR_ADAPTER_DOORS="<your-test-door-name>" \
    open-rmf-adapters-quiksync:smoke \
    python -m door_adapter_quiksync.adapter --dry-run
```

**Expected exit code:** `0` if at least one DoorState frame arrived
within 3 seconds. **Expected log lines:**

```
door_adapter_quiksync.adapter: door_adapter_quiksync starting: doors=[...]
quiksync_client.ws: WSS connected: /api/connector/ws/open-rmf/doors/<door>/state/subscribe
door_adapter_quiksync.adapter: dry-run complete: doors=N frames=M ...
```

**Failure modes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `HTTP 401` on the door state probe | M2M token missing `open-rmf:read` scope, or wrong audience | Re-mint the M2M client with the right scopes |
| `door '...' not found` | The door name doesn't match a registered door for your org | Check `/discovery` → `doors[]` for the right name |
| `frames=0` after 3-second timeout | The door is registered but the server isn't pushing state — likely no underlying door hardware connected | Verify the door's source connection is online server-side |

If dry-run is green, the auth + HTTP + WSS surfaces are healthy.

### 5.2 Full live: `rmf_door_msgs/DoorState` publishes

Validates that the adapter republishes WSS frames as ROS messages.

```bash
source /opt/ros/jazzy/setup.bash
source /path/to/rmf_ws/install/setup.bash

ros2 launch door_adapter_quiksync door_adapter_quiksync.launch.xml \
    config:=/path/to/quiksync.yaml
```

In another shell, observe the state topic:

```bash
ros2 topic echo /door_states --field current_mode
```

**Expected:** DoorState messages flow at the server's push cadence
(typically 1–5 Hz). `current_mode.value` reflects the door's actual
state (0=CLOSED, 1=MOVING, 2=OPEN).

### 5.3 Dispatch a `DoorRequest`

Validates the outbound REST POST path. Round-trip pattern: publish a
DoorRequest, observe that the next DoorState frame reflects the new
mode.

```bash
# OPEN the door — requested_mode.value=2 means OPEN
ros2 topic pub --once /door_requests rmf_door_msgs/DoorRequest \
  "{request_time: {sec: 0, nanosec: 0}, requester_id: 'smoke-runbook',
    door_name: '<your-test-door-name>',
    requested_mode: {value: 2}}"
```

**Expected sequence:**

1. Adapter logs: `dispatching DoorRequest: door=<door> mode=OPEN execution_id=<uuid>`
2. REST `POST /api/v1/connector/open-rmf/doors/<door>/request` returns 202.
3. Server-side door driver acts on the request.
4. Next WSS state frame reflects `current_mode.value=2` (OPEN) or
   `value=1` (MOVING) on the way there, then `value=2`.
5. Adapter republishes the new state on `/door_states`.

Run the same test with `requested_mode: {value: 0}` (CLOSED) to verify
the close path.

**Failure modes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `MODE_MOVING is not a valid goal` | The dispatching client sent `value=1` (MOVING) as the goal | MOVING is only valid as a state, not a request — fix the upstream publisher |
| `HTTP 400 invalid_request_mode` from the server | The adapter forwarded an unsupported mode (shouldn't happen in v0.2.x — local rejection catches MOVING) | File an issue against this repo with the adapter log line + the DoorRequest message |
| `HTTP 401` on the POST | Token refresh failed mid-run | Restart the adapter; check Auth0 client TTL |
| `HTTP 404 door_not_found` | The door name on the wire doesn't match the server-side registration | Verify `/discovery` lists the door |
| Door state never reflects the request | Server received 202 but the underlying door driver didn't act | Investigate the server side; adapter has done its job once the 202 came back |

### 5.4 (Optional) Multi-door config

Validates that one adapter process can manage N doors. Add a second
door to `doors:` in the YAML config (or `DOOR_ADAPTER_DOORS="d1,d2"`).
Repeat 5.1–5.3 against each. Expected: state frames for both flow
independently; a request for one doesn't affect the other.

## 6. Lift adapter smoke

### Prerequisites (lift-specific)

In addition to the universal Auth0 + staging items:

1. **At least one lift** registered in the QuikSync staging tenant.
   Confirm via `/discovery` → `lifts[]`.
2. **`rmf_lift_msgs >= 2.3`** on the live host.
3. The lift must have at least 2 floors and be in a state that allows
   AGV_MODE acquisition — verify by hitting
   `GET /api/v1/connector/open-rmf/lifts/<lift>/state` and confirming
   the `session_id` field is empty or matches your test fleet.

### 6.1 Dry-run round-trip

Same pattern as the door dry-run:

```bash
docker run --rm \
    -e LIFT_ADAPTER_BASE_URL=... \
    -e LIFT_ADAPTER_AUTH0_TENANT=... \
    -e LIFT_ADAPTER_AUTH0_AUDIENCE=... \
    -e LIFT_ADAPTER_AUTH0_CLIENT_ID="$ADAPTER_CLIENT_ID" \
    -e LIFT_ADAPTER_AUTH0_CLIENT_SECRET="$ADAPTER_CLIENT_SECRET" \
    -e LIFT_ADAPTER_AUTH0_ORGANIZATION="$ADAPTER_ORG_ID" \
    -e LIFT_ADAPTER_LIFTS="<your-test-lift-name>" \
    open-rmf-adapters-quiksync:smoke \
    python -m lift_adapter_quiksync.adapter --dry-run
```

**Expected exit code:** `0` if at least one LiftState frame arrived
within 3 seconds.

### 6.2 Full live: `rmf_lift_msgs/LiftState` publishes

```bash
ros2 launch lift_adapter_quiksync lift_adapter_quiksync.launch.xml \
    config:=/path/to/quiksync.yaml
```

```bash
ros2 topic echo /lift_states
```

**Expected:** LiftState messages flow at 1–5 Hz. `current_floor`,
`motion_state`, `door_state`, `available_modes`, `current_mode`, and
`session_id` all populate from the WSS frame.

### 6.3 AGV_MODE acquire — session lock test

Validates the full outbound path: the adapter-side session manager,
the REST POST, and the state-frame reconciliation back through the
session manager.

```bash
# Acquire the lift for a fresh session, request floor 2 with door CLOSED.
# request_type.value=2 = AGV_MODE; door_state.value=0 = CLOSED.
ros2 topic pub --once /lift_requests rmf_lift_msgs/LiftRequest \
  "{request_time: {sec: 0, nanosec: 0},
    request_type: {value: 2},
    door_state: {value: 0},
    destination_floor: 'L2',
    session_id: 'smoke-runbook-session',
    lift_name: '<your-test-lift-name>'}"
```

**Expected sequence:**

1. Adapter logs: `dispatching LiftRequest: lift=<lift> request_type=AGV_MODE session=smoke-runbook-session execution_id=<uuid>`.
2. `LiftSessionManager.try_acquire` succeeds (lift was free).
3. REST `POST .../lifts/<lift>/request` returns 202.
4. The lift driver starts moving toward L2.
5. The next state frame carries `session_id: "smoke-runbook-session"`
   and `destination_floor: "L2"`.
6. The session manager's `observe_server_state` confirms our hold.

### 6.4 END_SESSION release

```bash
ros2 topic pub --once /lift_requests rmf_lift_msgs/LiftRequest \
  "{request_time: {sec: 0, nanosec: 0},
    request_type: {value: 1},
    door_state: {value: 0},
    destination_floor: '',
    session_id: 'smoke-runbook-session',
    lift_name: '<your-test-lift-name>'}"
```

**Expected sequence:**

1. Adapter logs: `dispatching LiftRequest: lift=<lift> request_type=END_SESSION ...`.
2. `LiftSessionManager.release` succeeds.
3. REST POST returns 202.
4. Next state frame carries `session_id: ""` (empty).
5. The lift is free for the next AGV_MODE from any session.

### 6.5 Concurrent-session conflict

Validates the adapter-side session lock's defense-in-depth role —
conflicting AGV_MODE requests from different sessions should
short-circuit locally without a POST hitting the wire.

With the lift currently held by `smoke-runbook-session` (run 6.3 first
and skip 6.4), publish a second AGV_MODE for a different session:

```bash
ros2 topic pub --once /lift_requests rmf_lift_msgs/LiftRequest \
  "{request_time: {sec: 0, nanosec: 0},
    request_type: {value: 2},
    door_state: {value: 0},
    destination_floor: 'L3',
    session_id: 'other-fleet-session',
    lift_name: '<your-test-lift-name>'}"
```

**Expected:**

1. Adapter logs:
   `LiftRequest AGV_MODE for lift=<lift> session=other-fleet-session rejected by adapter-side session lock`.
2. **No** REST POST is sent (verify by tailing the adapter log for the
   absence of a `dispatching LiftRequest` line for this session).
3. Counter `requests_rejected` increments on the matching handle.

End the session (`/lift_requests` with `request_type: {value: 1}`)
before running other tests so the lift is left free.

### 6.6 Failure-mode table

| Symptom | Likely cause | Fix |
|---|---|---|
| `door_state=MOVING is not a valid goal` | Caller sent `door_state: {value: 1}` (MOVING) | MOVING is a state, not a goal; fix the publisher |
| Local short-circuit when expecting a server 409 | `LiftSessionManager` already knows the lift is held by another session | Expected — the adapter-side lock is doing its job; see 6.5 |
| `HTTP 401` on the POST | Token refresh failed mid-run | Restart the adapter; check Auth0 client TTL |
| `HTTP 404 lift_not_found` | The lift name on the wire doesn't match the server-side registration | Verify `/discovery` lists the lift |
| `HTTP 409 lift_session_held` | Server-side session conflict the adapter didn't catch locally (no recent state push?) | Check the adapter's `observe_server_state` log; the next state frame should reconcile |
| Session lock survives a fleet crash forever | The adapter's `session_ttl_seconds` is set to 0 (eviction disabled) and the fleet stopped emitting state pushes | Set `session_ttl_seconds: 3600.0` in the YAML (or env var) so stale `_requested` entries are auto-evicted |

## Multi-namespace orgs

If the QuikSync org hosts multiple namespaces and the adapter should
manage resources in only one of them, set `namespace:` in the YAML
config (under the `quiksync:` block) or pass `FLEET_ADAPTER_NAMESPACE`
/ `DOOR_ADAPTER_NAMESPACE` / `LIFT_ADAPTER_NAMESPACE` as an env var.
The value is forwarded as `?namespace=<value>` on every REST + WSS
call so the server filters discovery + state-subscribe responses to
that namespace's resources.

Leaving it unset preserves the historical cross-namespace union — fine
for single-namespace deployments.

## Known limitations

- **No live-Open-RMF in CI.** Live steps require the `rmf_ros2` stack
  which isn't pip-installable. There's no automated smoke at the
  moment.
- **No `localize` callback.** The fleet adapter omits the optional 4th
  `RobotCallbacks` property. File an enhancement issue against this
  repo if a customer needs `localize` (re-localising on map switch).
- **Named-place dispatch only.** Fleet-adapter dispatches with no
  named waypoint return 400; coordinate-only navigate is not yet
  supported.
