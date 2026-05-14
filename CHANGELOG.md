# Changelog

## 0.1.0 (2026-05-14)

Initial release.

### Features

- `quiksync_client` — shared Python core. Auth0 M2M `client_credentials`
  flow with token caching + preemptive refresh; `httpx`-based REST
  client with retries and jittered exponential backoff; `websockets`-based
  state subscriber with 401 circuit-breaker and reconnect-on-token-expiry.
- `fleet_adapter_quiksync` — `EasyFullControl` adapter. Loads YAML
  config, fetches `/discovery` + `/building_map` from the QuikSync
  adapter API, builds `VehicleTraits` / `BatterySystem` / `Graph` /
  `FleetConfiguration`, registers each robot via `add_robot(...)` with
  `RobotCallbacks(navigate, stop, action_executor)`, runs the WSS state
  pump on a dedicated thread.
- `door_adapter_quiksync` / `lift_adapter_quiksync` — v1 stubs (compile +
  idle); real implementations land in v2.
- `--dry-run` mode on the fleet adapter for CI smoke without `rmf_adapter`
  installed.
- `docs/smoke.md` — manual pilot-stage smoke procedure with failure tables.
- Dockerfile + docker-compose example + combined ROS 2 launch file
  covering all three adapter packages.
- `release-please` workflow for subsequent releases.
