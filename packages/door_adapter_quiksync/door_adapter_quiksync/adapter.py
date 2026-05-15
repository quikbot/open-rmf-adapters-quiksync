"""QuikSync door adapter entry point.

Two modes:

- **adapter**: full mode. Requires `rclpy` + `rmf_door_msgs` (the
  rmf_ros2 stack). Builds a `DoorAdapterNode` that subscribes to
  `door_requests`, publishes `door_states`, and dispatches each
  per-door state pump.
- **dry-run**: `rclpy` not importable (CI / local-dev). The adapter
  loads its config, builds the http + ws clients, optionally fetches
  /discovery, drains one state frame per door (with a no-op publish
  callback), then exits. Exercises auth + HTTP + WSS plumbing
  end-to-end without rmf_ros2.

CLI:

    door_adapter_quiksync --config /etc/quiksync/door.yaml
    door_adapter_quiksync --config /etc/quiksync/door.yaml --dry-run
    DOOR_ADAPTER_BASE_URL=... door_adapter_quiksync             # env-only

Exit codes:

    0  success (full mode clean shutdown; dry-run got frames)
    1  config load failed
    2  dry-run got no frames within the timeout
    3  /discovery fetch failed (only fetched when ?prefer_dynamic flag set)
    4  fleet not in /discovery (not currently used — see fleet adapter)
    6  rclpy not importable in full mode

Audience: matches the smoke runbook at `docs/smoke.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any, Optional

from quiksync_client import (
    Auth0M2MClient,
    AuthConfig,
    HttpConfig,
    QuikSyncHttpClient,
    QuikSyncWsClient,
    WsConfig,
)

from .config import ConfigError, DoorAdapterConfig
from .node import DoorAdapterNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("door_adapter_quiksync.adapter")


# ----- Lazy imports of the rmf_ros2 stack -----


def _try_import_rclpy() -> Optional[Any]:
    """Lazy-import `rclpy`; returns the module or None if unavailable."""
    try:
        import rclpy  # type: ignore[import-untyped]
        return rclpy
    except Exception as e:  # noqa: BLE001
        log.warning(
            "rclpy not importable (%s) — running in dry-run mode. "
            "The full adapter binary requires the rmf_ros2 stack at runtime.",
            e,
        )
        return None


def _try_import_door_msgs() -> Optional[Any]:
    """Lazy-import `rmf_door_msgs.msg` + `builtin_interfaces.msg`; returns a
    namespace exposing `DoorState`, `DoorMode`, `Time`, or None if any
    import fails."""
    try:
        from rmf_door_msgs.msg import DoorMode, DoorRequest, DoorState  # type: ignore[import-untyped]
        from builtin_interfaces.msg import Time  # type: ignore[import-untyped]
        return SimpleNamespace(
            DoorState=DoorState, DoorMode=DoorMode, DoorRequest=DoorRequest, Time=Time,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("rmf_door_msgs / builtin_interfaces not importable (%s)", e)
        return None


# ----- Client wiring (shared by dry-run + full mode) -----


def build_clients(
    config: DoorAdapterConfig,
) -> tuple[Auth0M2MClient, QuikSyncHttpClient, QuikSyncWsClient]:
    """Construct the auth + HTTP + WS clients from a parsed config.

    Caller is responsible for closing them in a `finally` block.
    """
    auth = Auth0M2MClient(AuthConfig(
        tenant=config.auth0_tenant,
        audience=config.auth0_audience,
        client_id=config.auth0_client_id,
        client_secret=config.auth0_client_secret,
        organization=config.auth0_organization,
    ))
    http = QuikSyncHttpClient(HttpConfig(base_url=config.base_url), auth)
    ws = QuikSyncWsClient(WsConfig(base_url=config.ws_base_url()), auth)
    return auth, http, ws


# ----- CLI entry point -----


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="door_adapter_quiksync")
    parser.add_argument(
        "--config",
        help="path to YAML config; alternatively use DOOR_ADAPTER_* env vars",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="bootstrap + drain one state frame per door + exit (no rclpy required)",
    )
    parser.add_argument(
        "--dry-run-seconds",
        type=float,
        default=3.0,
        help="dry-run timeout — exit after this many seconds without frames (default 3.0)",
    )
    args = parser.parse_args(argv)

    # Load config (file > env)
    try:
        config = (
            DoorAdapterConfig.from_yaml(args.config)
            if args.config
            else DoorAdapterConfig.from_env()
        )
    except ConfigError as e:
        log.error("config load failed: %s", e)
        return 1

    log.info(
        "door_adapter_quiksync starting: doors=%s base=%s org=%s",
        list(config.doors), config.base_url, config.auth0_organization,
    )

    auth, http, ws = build_clients(config)

    rclpy = _try_import_rclpy()
    msgs = _try_import_door_msgs()
    dry_run = args.dry_run or rclpy is None or msgs is None

    try:
        if dry_run:
            return asyncio.run(_run_dry(config, ws, args.dry_run_seconds))
        return _run_full(
            config=config,
            http=http,
            ws=ws,
            rclpy=rclpy,
            msgs=msgs,
        )
    finally:
        ws.close()
        http.close()
        auth.close()


# ----- Dry-run path -----


async def _run_dry(
    config: DoorAdapterConfig,
    ws: QuikSyncWsClient,
    timeout_seconds: float,
) -> int:
    """Spawn one state pump per door. Drain frames + exit.

    Each pump's callback is a no-op — we count frames seen, not the
    publish path. This validates auth + HTTP + WSS plumbing
    end-to-end without rclpy.
    """
    from .state_pump import DoorStatePump

    seen: dict[str, int] = {door: 0 for door in config.doors}

    def make_counter(door_name: str):
        async def _count(name: str, frame: dict) -> None:
            seen[name] = seen.get(name, 0) + 1
        return _count

    pumps = [
        DoorStatePump(ws, door, make_counter(door))
        for door in config.doors
    ]
    for pump in pumps:
        await pump.start()

    # Wait for at least one frame from any door, up to the timeout.
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while sum(seen.values()) == 0:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.05)

    for pump in pumps:
        await pump.stop()

    total = sum(seen.values())
    log.info("dry-run complete: frames=%s (per door: %s)", total, seen)
    return 0 if total > 0 else 2


# ----- Full (rclpy) path -----


def _run_full(
    *,
    config: DoorAdapterConfig,
    http: QuikSyncHttpClient,
    ws: QuikSyncWsClient,
    rclpy: Any,
    msgs: Any,
) -> int:
    """Real ROS runtime path — rclpy + rmf_door_msgs required.

    Sequence:
    1. `rclpy.init()` — ROS Python context.
    2. Build a ROS node named `door_adapter_quiksync`.
    3. Create the door_states publisher + door_requests subscriber.
    4. Build a `DoorAdapterNode` orchestrator with N `DoorHandle`s.
    5. `DoorAdapterNode.start()` — spawns asyncio loop thread + pumps.
    6. Spin the rclpy executor until SIGINT.
    7. Shut down: stop adapter → destroy node → `rclpy.shutdown()`.

    Only reached when rclpy + rmf_door_msgs are importable — typically
    only on hosts with the rmf_ros2 stack installed. CI exercises only
    the dry-run path; structural correctness of this function is
    covered by `test_adapter.py` via `sys.modules`-injected fakes.
    """
    rclpy.init()
    ros_node = None
    adapter_node: Optional[DoorAdapterNode] = None
    try:
        ros_node = rclpy.create_node("door_adapter_quiksync")
        publisher = ros_node.create_publisher(
            msgs.DoorState, config.door_states_topic, 10,
        )
        adapter_node = DoorAdapterNode(
            door_names=config.doors,
            http_client=http,
            ws_client=ws,
            msg_module=msgs,
            publish_msg=publisher.publish,
            log_warning=ros_node.get_logger().warning,
            namespace=config.namespace,
        )
        ros_node.create_subscription(
            msgs.DoorRequest,
            config.door_requests_topic,
            adapter_node.route_request,
            10,
        )
        adapter_node.start()
        log.info(
            "door_adapter_quiksync ready: doors=%s topics=%s/%s",
            list(config.doors),
            config.door_states_topic,
            config.door_requests_topic,
        )

        executor = rclpy.executors.SingleThreadedExecutor()
        executor.add_node(ros_node)
        try:
            executor.spin()
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        finally:
            executor.shutdown()
    finally:
        if adapter_node is not None:
            adapter_node.stop()
        if ros_node is not None:
            ros_node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
