"""QuikSync lift adapter entry point.

Two modes:

- **adapter**: full mode. Requires `rclpy` + `rmf_lift_msgs` (the
  rmf_ros2 stack). Builds a `LiftAdapterNode` that subscribes to
  `lift_requests`, publishes `lift_states`, and dispatches each
  per-lift state pump.
- **dry-run**: `rclpy` not importable (CI / local-dev). The adapter
  loads its config, builds the http + ws clients, optionally fetches
  /discovery, drains one state frame per lift (with a no-op publish
  callback), then exits.

CLI:

    lift_adapter_quiksync --config /etc/quiksync/lift.yaml
    lift_adapter_quiksync --config /etc/quiksync/lift.yaml --dry-run
    LIFT_ADAPTER_BASE_URL=... lift_adapter_quiksync             # env-only

Exit codes:

    0  success (full mode clean shutdown; dry-run got frames)
    1  config load failed
    2  dry-run got no frames within the timeout
    6  rclpy not importable in full mode
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

from .config import ConfigError, LiftAdapterConfig
from .node import LiftAdapterNode
from .session_manager import LiftSessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lift_adapter_quiksync.adapter")


# ----- Lazy imports of the rmf_ros2 stack -----


def _try_import_rclpy() -> Optional[Any]:
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


def _try_import_lift_msgs() -> Optional[Any]:
    """Lazy-import `rmf_lift_msgs.msg` + `builtin_interfaces.msg`; returns a
    namespace exposing `LiftState`, `LiftRequest`, `Time`, or None on
    failure."""
    try:
        from rmf_lift_msgs.msg import LiftRequest, LiftState  # type: ignore[import-untyped]
        from builtin_interfaces.msg import Time  # type: ignore[import-untyped]
        return SimpleNamespace(
            LiftState=LiftState, LiftRequest=LiftRequest, Time=Time,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("rmf_lift_msgs / builtin_interfaces not importable (%s)", e)
        return None


# ----- Client wiring -----


def build_clients(
    config: LiftAdapterConfig,
) -> tuple[Auth0M2MClient, QuikSyncHttpClient, QuikSyncWsClient]:
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
    parser = argparse.ArgumentParser(prog="lift_adapter_quiksync")
    parser.add_argument(
        "--config",
        help="path to YAML config; alternatively use LIFT_ADAPTER_* env vars",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="bootstrap + drain one state frame per lift + exit (no rclpy required)",
    )
    parser.add_argument(
        "--dry-run-seconds",
        type=float,
        default=3.0,
        help="dry-run timeout — exit after this many seconds without frames (default 3.0)",
    )
    args = parser.parse_args(argv)

    try:
        config = (
            LiftAdapterConfig.from_yaml(args.config)
            if args.config
            else LiftAdapterConfig.from_env()
        )
    except ConfigError as e:
        log.error("config load failed: %s", e)
        return 1

    log.info(
        "lift_adapter_quiksync starting: lifts=%s base=%s org=%s",
        list(config.lifts), config.base_url, config.auth0_organization,
    )

    auth, http, ws = build_clients(config)

    rclpy = _try_import_rclpy()
    msgs = _try_import_lift_msgs()
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
    config: LiftAdapterConfig,
    ws: QuikSyncWsClient,
    timeout_seconds: float,
) -> int:
    from .state_pump import LiftStatePump

    seen: dict[str, int] = {lift: 0 for lift in config.lifts}

    def make_counter(lift_name: str):
        async def _count(name: str, frame: dict) -> None:
            seen[name] = seen.get(name, 0) + 1
        return _count

    pumps = [
        LiftStatePump(ws, lift, make_counter(lift))
        for lift in config.lifts
    ]
    for pump in pumps:
        await pump.start()

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while sum(seen.values()) == 0:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.05)

    for pump in pumps:
        await pump.stop()

    total = sum(seen.values())
    log.info("dry-run complete: frames=%s (per lift: %s)", total, seen)
    return 0 if total > 0 else 2


# ----- Full (rclpy) path -----


def _run_full(
    *,
    config: LiftAdapterConfig,
    http: QuikSyncHttpClient,
    ws: QuikSyncWsClient,
    rclpy: Any,
    msgs: Any,
) -> int:
    """Real ROS runtime path — rclpy + rmf_lift_msgs required.

    Sequence mirrors the door adapter:
    1. `rclpy.init()`
    2. Build a ROS node named `lift_adapter_quiksync`
    3. Create the lift_states publisher + lift_requests subscriber
    4. Build a `LiftAdapterNode` with N `LiftHandle`s + shared session manager
    5. `LiftAdapterNode.start()` — asyncio loop thread + pumps
    6. Spin the rclpy executor until SIGINT
    7. Clean shutdown
    """
    rclpy.init()
    ros_node = None
    adapter_node: Optional[LiftAdapterNode] = None
    try:
        ros_node = rclpy.create_node("lift_adapter_quiksync")
        publisher = ros_node.create_publisher(
            msgs.LiftState, config.lift_states_topic, 10,
        )
        adapter_node = LiftAdapterNode(
            lift_names=config.lifts,
            http_client=http,
            ws_client=ws,
            msg_module=msgs,
            publish_msg=publisher.publish,
            log_warning=ros_node.get_logger().warning,
            namespace=config.namespace,
            session_manager=LiftSessionManager(ttl_seconds=config.session_ttl_seconds),
        )
        ros_node.create_subscription(
            msgs.LiftRequest,
            config.lift_requests_topic,
            adapter_node.route_request,
            10,
        )
        adapter_node.start()
        log.info(
            "lift_adapter_quiksync ready: lifts=%s topics=%s/%s",
            list(config.lifts),
            config.lift_states_topic,
            config.lift_requests_topic,
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
