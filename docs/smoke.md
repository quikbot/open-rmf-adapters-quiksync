# Live smoke procedure — `fleet_adapter_quiksync`

Manual smoke procedure for verifying the QuikSync fleet adapter against
a real Open-RMF deployment. CI exercises only the dry-run path (no
`rmf_adapter`); the EasyFullControl wire-up runs only against a real
deployment with the `rmf_ros2` stack installed.

Run this whenever:

- A new release candidate is cut.
- The QuikSync adapter API contract changes (REST or WSS shape).
- A customer reports a regression that needs the adapter side
  investigated.

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

## Known limitations

- **No live-Open-RMF in CI.** Steps 2–4 require the `rmf_ros2` stack which
  isn't pip-installable. There's no automated smoke at the moment.
- **No `localize` callback.** v1 omits the optional 4th `RobotCallbacks`
  property. File an enhancement issue against this repo if a customer
  needs `localize` (re-localising on map switch).
- **Named-place dispatch only.** Dispatches with no named waypoint
  return 400; coordinate-only navigate is not supported in v1.
- **Door + lift adapters stubbed.** v1 ships the fleet adapter only;
  the door + lift packages are scaffolds. v2 implements them.
