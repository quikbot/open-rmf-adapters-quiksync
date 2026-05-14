# door_adapter_quiksync

Open-RMF door adapter for QuikSync-managed doors.

**v1 status: stub.** The package compiles and the entry-point binary
runs, but the real `rmf_door_msgs` integration lands in **v2**. The stub
exists in v1 so docker-compose configurations don't need to special-case
"fleet only" between the v1 and v2 releases — operators can include the
door adapter container from day one, and it idles until v2 ships real
control logic.

The stub binary logs a clear "deferred to v2" message at startup and
then sleeps indefinitely (until SIGINT), keeping a `restart: unless-stopped`
container from exit-looping.

## v1 behaviour

```bash
$ ros2 run door_adapter_quiksync door_adapter_quiksync
2026-05-14 12:34:56 [INFO] door_adapter_quiksync: v1 stub —
door adapter ships in v2. Idling so the container doesn't exit-loop in
docker-compose.
```

That's it. No ROS topics published, no QuikSync API calls, no state
maintained.

## v2 (planned)

In v2 the package will:

- Subscribe to per-door state via the QuikSync WSS endpoint
  (`/api/connector/ws/open-rmf/doors/{door}/state/subscribe`).
- Publish `rmf_door_msgs/DoorState` to `/door_states`.
- Subscribe to `/door_requests` and translate each request to the
  corresponding QuikSync `POST /doors/{door}/request` call.
- Idempotency + auth + retries inherited from `quiksync_client`.

See the [root README](../../README.md) for the project overview.

## License

Apache 2.0 — see the root [`LICENSE`](../../LICENSE).
