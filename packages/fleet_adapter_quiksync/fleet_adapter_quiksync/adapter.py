"""QuikSync fleet adapter entry point.

Wires the QuikSync HTTPS + WSS surface (`quiksync_client`) into
Open-RMF's `EasyFullControl` so the customer's Open-RMF deployment can dispatch
to QuikSync-managed robots as native peers.

Architecture (per design §6.2):
  config.yaml ──► FleetAdapterConfig
                  │
                  ▼
            Auth0M2MClient ──► QuikSyncHttpClient ──► get_discovery()
                                                          │
                                                          ▼
                                                    Discover our fleet entry
                                                          │
                                                          ▼
                                QuikSyncWsClient ──► FleetStatePump ──► RobotHandle.on_state
                                                                         │
                                                                         ▼ bind() once add_robot returns
                                                          EasyRobotUpdateHandle.update(state, activity)

  Open-RMF outbound: RobotCallbacks(navigate, stop, action_executor)
                ──► QuikSyncHttpClient.post_navigate / post_stop

Runtime modes:
- **adapter**: full mode. Requires rmf_adapter (rmf_ros2). Registers fleet
  with EasyFullControl, spawns state pump, runs ROS event loop until
  killed.
- **dry-run**: rmf_adapter not importable (CI / local-dev). Adapter
  bootstraps Auth0 + HTTP + discovery + state pump but doesn't register
  with Open-RMF. Logs the discovery result + exits cleanly. Used to smoke
  the QuikSync side without a full Open-RMF stack.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Optional

from quiksync_client import (
    QuikSyncHttpClient,
    QuikSyncWsClient,
    Auth0M2MClient,
    AuthConfig,
    HttpConfig,
    WsConfig,
)

from .binding import BindingError, bind_easy_full_control, bind_from_yaml
from .config import ConfigError, FleetAdapterConfig
from .robot_handle import RobotHandle
from .state_pump import FleetStatePump

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("fleet_adapter_quiksync.adapter")


def _try_import_rmf_adapter() -> Optional[Any]:
    """Lazy-import rmf_adapter; returns the module or None if unavailable."""
    try:
        import rmf_adapter  # type: ignore[import-untyped]
        return rmf_adapter
    except ImportError as e:
        log.warning(
            "rmf_adapter not importable (%s) — running in dry-run mode. "
            "The adapter binary requires the rmf_ros2 stack at runtime.",
            e,
        )
        return None


def build_clients(config: FleetAdapterConfig) -> tuple[Auth0M2MClient, QuikSyncHttpClient, QuikSyncWsClient]:
    """Construct the three QuikSync API clients from config. Pure construction —
    no network I/O until first use."""
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


def find_our_fleet(discovery: dict[str, Any], fleet_name: str) -> Optional[dict[str, Any]]:
    """From the /discovery response, pick the fleet entry matching our config."""
    fleets = discovery.get("fleets") or []
    if not isinstance(fleets, list):
        return None
    for fleet in fleets:
        if isinstance(fleet, dict) and fleet.get("fleet_name") == fleet_name:
            return fleet
    return None


def _fetch_fleet_entry(
    http: QuikSyncHttpClient,
    fleet_name: str,
    namespace: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[int]]:
    """Fetch /discovery + locate our fleet entry, with consistent error
    reporting. Returns ``(fleet_entry, exit_code)`` where exactly one
    side is ``None``: on success ``(entry, None)``; on failure
    ``(None, exit_code)`` matching `main()`'s documented exit-code
    contract (3 = discovery fetch failed; 4 = fleet not found).
    """
    try:
        discovery = http.get_discovery(namespace=namespace)
    except Exception as e:  # noqa: BLE001
        log.error("discovery fetch failed: %s", e)
        return None, 3

    fleet_entry = find_our_fleet(discovery, fleet_name)
    if fleet_entry is None:
        available = [
            f.get("fleet_name") for f in (discovery.get("fleets") or [])
            if isinstance(f, dict)
        ]
        log.error("fleet %r not found in discovery; available=%s", fleet_name, available)
        return None, 4

    return fleet_entry, None


def build_robot_handles(fleet_entry: dict[str, Any]) -> dict[str, RobotHandle]:
    """One RobotHandle per robot listed in the fleet entry."""
    handles: dict[str, RobotHandle] = {}
    robots = fleet_entry.get("robots") or []
    for robot in robots:
        if isinstance(robot, dict):
            name = robot.get("name")
            if name:
                handles[name] = RobotHandle(name)
    return handles


async def _run_dry(
    config: FleetAdapterConfig,
    http: QuikSyncHttpClient,
    ws: QuikSyncWsClient,
    handles: dict[str, RobotHandle],
) -> int:
    """Dry-run mode: bootstrap + log + exit. Used for CI smoke + dev sanity
    checks before the rmf_adapter stack is installed.

    Drains a few WSS frames so we exercise the auth-+-WSS path end-to-end,
    then logs handle state. Exits 0 if at least one frame arrived; non-zero
    otherwise (caller can use this for a smoke gate)."""

    async def on_state(name: str, state: dict[str, Any]) -> None:
        if name in handles:
            handles[name].on_state(state)

    pump = FleetStatePump(ws, config.fleet_name, on_state, namespace=config.namespace)
    await pump.start()
    try:
        # Wait briefly for the first frame; log + exit either way.
        for _ in range(30):  # ~3 seconds
            await asyncio.sleep(0.1)
            if pump.frames_seen() > 0:
                break
    finally:
        await pump.stop()

    log.info(
        "dry-run complete: frames=%d robots_dispatched=%d. handles=%s",
        pump.frames_seen(), pump.robots_dispatched(),
        {n: {"updates_dropped_no_handle": h.updates_dropped_no_handle()} for n, h in handles.items()},
    )
    return 0 if pump.frames_seen() > 0 else 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet_adapter_quiksync")
    parser.add_argument(
        "--config",
        help="path to YAML config; alternatively use FLEET_ADAPTER_* env vars",
    )
    parser.add_argument(
        "--nav-graph",
        help=(
            "path to nav graph YAML (required in YAML mode; ignored in dynamic mode). "
            "Standard Open-RMF nav graph format, same shape consumed by "
            "`FleetConfiguration.from_config_files`."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="bootstrap + log + exit (no rmf_adapter required)",
    )
    parser.add_argument(
        "-s", "--server-uri",
        type=str,
        default="",
        help=(
            "Open-RMF API server URI (e.g. ws://localhost:7878) for the "
            "fleet adapter to publish task/state information to rmf-web. "
            "Empty (default) = don't publish. Matches the canonical "
            "fleet_adapter_template --server_uri argument."
        ),
    )
    parser.add_argument(
        "--use-sim-time",
        action="store_true",
        help="Use ROS simulated time (for offline / simulator testing).",
    )
    args = parser.parse_args(argv)

    # Load config (file > env)
    try:
        config = FleetAdapterConfig.from_yaml(args.config) if args.config else FleetAdapterConfig.from_env()
    except ConfigError as e:
        log.error("config load failed: %s", e)
        return 1

    log.info(
        "fleet_adapter_quiksync starting: fleet=%s base=%s org=%s mode=%s",
        config.fleet_name, config.base_url, config.auth0_organization,
        "dynamic" if config.dynamic_mode else "yaml",
    )

    auth, http, ws = build_clients(config)

    rmf_adapter = _try_import_rmf_adapter()
    dry_run = args.dry_run or rmf_adapter is None

    # Fleet entry from /discovery is required for:
    # - dynamic mode (drives FleetConfiguration construction in-memory)
    # - any dry-run (used to build RobotHandles + drain WSS frames)
    # YAML-mode + non-dry-run skips discovery; the fleet shape comes from
    # the YAML's `rmf_fleet:` block instead.
    needs_discovery = config.dynamic_mode or dry_run
    fleet_entry: Optional[dict[str, Any]] = None
    handles: dict[str, RobotHandle] = {}
    if needs_discovery:
        fleet_entry, exit_code = _fetch_fleet_entry(http, config.fleet_name, namespace=config.namespace)
        if exit_code is not None:
            auth.close()
            http.close()
            return exit_code
        assert fleet_entry is not None  # _fetch_fleet_entry contract
        handles = build_robot_handles(fleet_entry)
        log.info(
            "registered %d robots locally (%s): %s",
            len(handles),
            "dynamic mode" if config.dynamic_mode else "dry-run pre-flight",
            sorted(handles),
        )

    try:
        if dry_run:
            return asyncio.run(_run_dry(config, http, ws, handles))
        else:
            return _run_full(
                config=config,
                rmf_adapter=rmf_adapter,
                http=http,
                ws=ws,
                handles=handles,
                fleet_entry=fleet_entry,
                config_path=args.config,
                nav_graph_path=args.nav_graph,
                server_uri=args.server_uri or None,
                use_sim_time=args.use_sim_time,
            )
    finally:
        ws.close()
        http.close()
        auth.close()


def _run_full(
    config: FleetAdapterConfig,
    rmf_adapter: Any,
    http: QuikSyncHttpClient,
    ws: QuikSyncWsClient,
    handles: dict[str, RobotHandle],
    fleet_entry: Optional[dict[str, Any]] = None,
    config_path: Optional[str] = None,
    nav_graph_path: Optional[str] = None,
    server_uri: Optional[str] = None,
    use_sim_time: bool = False,
) -> int:
    """Real-Open-RMF runtime path.

    Mirrors the wiring sequence from the canonical
    [fleet_adapter_template](https://github.com/open-rmf/fleet_adapter_template):

    1. `rclpy.init()` (the ROS Python context).
    2. `rmf_adapter.init_rclcpp()` (the C++ side of the rmf_adapter binding).
    3. Fetch the building_map from the QuikSync Open-RMF Connector —
       needed by FleetConfiguration.
    4. Hand off to `binding.bind_easy_full_control(...)` which constructs
       the Adapter, configures the fleet, registers robots with
       RobotCallbacks, and binds each RobotHandle to its
       EasyRobotUpdateHandle.
    5. `adapter.start()` (non-blocking — kicks off the rmf_adapter
       worker threads).
    6. Spawn the WSS state pump on a dedicated thread.
    7. Build a `rclpy.executors.SingleThreadedExecutor()` and
       `executor.spin()` to block until SIGINT.
    8. Cleanly shut down: stop pump → join thread → executor shutdown →
       `rclpy.shutdown()`.

    Only reached when `rmf_adapter` is importable — typically only on
    deployments with the rmf_ros2 stack installed. CI cannot exercise
    this path; structural correctness is covered by `test_binding.py`
    via `sys.modules` injection.
    """
    rclpy = _try_import_rclpy()
    if rclpy is None:
        log.error("rclpy not importable — required for the EasyFullControl runtime path")
        return 6

    rclpy.init()
    try:
        # Initialise the C++ side of the rmf_adapter binding. Without
        # this, the Adapter created below cannot start its worker
        # threads. Matches fleet_adapter_template's `rmf_adapter.init_rclcpp()`
        # call after `rclpy.init()`.
        if hasattr(rmf_adapter, "init_rclcpp"):
            rmf_adapter.init_rclcpp()

        try:
            if config.dynamic_mode:
                # Dynamic path: fetch /building_map, build FleetConfiguration
                # in-memory from discovery + building map.
                if fleet_entry is None:
                    raise BindingError(
                        "dynamic mode requires a fleet entry from /discovery "
                        "(internal invariant — was main() short-circuited?)"
                    )
                log.info("dynamic mode: fetching building_map for fleet=%s", config.fleet_name)
                building_map = http.get_building_map(namespace=config.namespace)
                adapter, fleet_handle = bind_easy_full_control(
                    rmf_adapter=rmf_adapter,
                    fleet_entry=fleet_entry,
                    building_map=building_map,
                    handles=handles,
                    http=http,
                    server_uri=server_uri,
                    namespace=config.namespace,
                )
            else:
                # YAML path: hand the config + nav_graph file paths to
                # `FleetConfiguration.from_config_files`. Matches the
                # canonical fleet_adapter_template entry point.
                adapter, fleet_handle = bind_from_yaml(
                    rmf_adapter=rmf_adapter,
                    config_path=config_path or "",
                    nav_graph_path=nav_graph_path or "",
                    http=http,
                    handles=handles,
                    fleet_name=config.fleet_name,
                    server_uri=server_uri,
                    namespace=config.namespace,
                )
        except BindingError as e:
            log.error("EasyFullControl binding failed: %s", e)
            return 7

        # Optional: enable simulated time on the adapter's ROS node.
        if use_sim_time:
            try:
                from rclpy.parameter import Parameter  # type: ignore[import-untyped]
                param = Parameter("use_sim_time", Parameter.Type.BOOL, True)
                adapter.node.set_parameters([param])
                adapter.node.use_sim_time()
                log.info("use_sim_time enabled on adapter node")
            except Exception as e:  # noqa: BLE001
                log.warning("could not enable use_sim_time on adapter node: %s", e)

        # `adapter.start()` is non-blocking; it kicks off rmf_adapter's
        # worker threads. We then build an rclpy executor on the
        # adapter's command-handle node and spin that executor in the
        # main thread until SIGINT.
        adapter.start()

        pump_thread, pump = _spawn_state_pump(config, ws, handles)
        executor = rclpy.executors.SingleThreadedExecutor()
        adapter_node = getattr(adapter, "node", None)
        if adapter_node is not None:
            executor.add_node(adapter_node)

        try:
            log.info("entering ROS executor spin for fleet=%s", config.fleet_name)
            executor.spin()
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        finally:
            pump.request_stop()
            pump_thread.join(timeout=5.0)
            executor.shutdown()
    finally:
        try:
            rclpy.shutdown()
        except Exception as e:  # noqa: BLE001
            log.warning("rclpy.shutdown failed: %s", e)
    return 0


def _try_import_rclpy() -> Optional[Any]:
    """Lazy-import rclpy; returns the module or None if unavailable."""
    try:
        import rclpy  # type: ignore[import-untyped]
        return rclpy
    except ImportError as e:
        log.warning("rclpy not importable (%s) — runtime path requires the rmf_ros2 stack", e)
        return None


def _spawn_state_pump(
    config: FleetAdapterConfig,
    ws: QuikSyncWsClient,
    handles: dict[str, RobotHandle],
) -> tuple[Any, "_StatePumpRunner"]:
    """Run the FleetStatePump on a dedicated thread with its own asyncio
    loop. Returns the (thread, runner) pair so the caller can request
    shutdown + join.

    Background-thread asyncio (rather than running the pump in the same
    thread as `adapter.spin()`) keeps the Open-RMF ROS spin loop unblocked
    while WSS frames arrive."""
    import threading

    runner = _StatePumpRunner(config, ws, handles)
    thread = threading.Thread(target=runner.run, name="fleet_state_pump", daemon=True)
    thread.start()
    return thread, runner


class _StatePumpRunner:
    """Holds the asyncio loop + pump and exposes a sync request_stop."""

    def __init__(
        self,
        config: FleetAdapterConfig,
        ws: QuikSyncWsClient,
        handles: dict[str, RobotHandle],
    ) -> None:
        self._config = config
        self._ws = ws
        self._handles = handles
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pump: Optional[FleetStatePump] = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def on_state(name: str, state: dict[str, Any]) -> None:
            handle = self._handles.get(name)
            if handle is not None:
                handle.on_state(state)

        self._pump = FleetStatePump(
            self._ws, self._config.fleet_name, on_state, namespace=self._config.namespace,
        )

        try:
            loop.run_until_complete(self._pump.start())
            log.info("state pump started")
            loop.run_forever()
        except Exception as e:  # noqa: BLE001
            log.exception("state pump crashed: %s", e)
        finally:
            try:
                if self._pump is not None:
                    loop.run_until_complete(self._pump.stop())
            except Exception as e:  # noqa: BLE001
                log.warning("state pump stop failed: %s", e)
            loop.close()

    def request_stop(self) -> None:
        """Sync entry to ask the pump's loop to exit cleanly."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
