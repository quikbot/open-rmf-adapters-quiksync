# Changelog

## [0.1.2](https://github.com/quikbot/open-rmf-adapters-quiksync/compare/v0.1.1...v0.1.2) (2026-05-14)


### Bug Fixes

* **fleet_adapter_quiksync:** accept Open-RMF schema-conformant map shape for FleetState.robots ([#8](https://github.com/quikbot/open-rmf-adapters-quiksync/issues/8)) ([62cf3b7](https://github.com/quikbot/open-rmf-adapters-quiksync/commit/62cf3b7fba2ed15294e8eeee1d874e3c574d3344))

## [0.1.1](https://github.com/quikbot/open-rmf-adapters-quiksync/compare/v0.1.0...v0.1.1) (2026-05-14)


### Features

* **fleet_adapter_quiksync:** YAML-driven default config + opt-in dynamic mode ([#2](https://github.com/quikbot/open-rmf-adapters-quiksync/issues/2)) ([c033367](https://github.com/quikbot/open-rmf-adapters-quiksync/commit/c033367aa5bf176b5c286ebfc70a7cae21a662d9))

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
