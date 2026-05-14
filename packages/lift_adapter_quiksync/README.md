# lift_adapter_quiksync

Open-RMF lift adapter for QuikSync-managed lifts.

**v1 status: stub.** The package compiles and the entry-point binary
runs, but the real `rmf_lift_msgs` integration lands in **v2**. The stub
exists in v1 for the same reason as
[`door_adapter_quiksync`](../door_adapter_quiksync) — so docker-compose
configurations can include the lift adapter container from day one and
operators don't have to alter compose topology when v2 lands.

The stub binary logs a "deferred to v2" message at startup and idles
until SIGINT.

## v1 behaviour

```bash
$ ros2 run lift_adapter_quiksync lift_adapter_quiksync
2026-05-14 12:34:56 [INFO] lift_adapter_quiksync: v1 stub —
lift adapter ships in v2. Idling so the container doesn't exit-loop in
docker-compose.
```

That's it. No ROS topics published, no QuikSync API calls, no state
maintained.

## v2 (planned)

In v2 the package will:

- Subscribe to per-lift state via the QuikSync WSS endpoint
  (`/api/connector/ws/open-rmf/lifts/{lift}/state/subscribe`).
- Publish `rmf_lift_msgs/LiftState` to `/lift_states`.
- Subscribe to `/lift_requests` and translate each request to the
  corresponding QuikSync `POST /lifts/{lift}/request` call.
- Support the `(deck, side)` axes documented in the QuikSync adapter
  API; multi-deck / multi-side decomposition deferred to a later
  release.

See the [root README](../../README.md) for the project overview.

## License

Apache 2.0 — see the root [`LICENSE`](../../LICENSE).
