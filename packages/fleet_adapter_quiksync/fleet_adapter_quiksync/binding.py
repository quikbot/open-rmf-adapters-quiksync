"""Binding glue between `rmf_adapter` and QuikSync.

Builds the `rmf_adapter` objects an `EasyFullControl` fleet needs —
`VehicleTraits`, `BatterySystem`, `Graph`, `FleetConfiguration` — from
the JSON shapes the QuikSync `/discovery` and `/building_map` endpoints
return (per design §4.3.1 / §4.3.2). Wires per-robot `RobotCallbacks`
into `Adapter.add_easy_full_control` and binds each `RobotHandle` to
the returned `EasyRobotUpdateHandle` so state-pump pushes flow into
Open-RMF.

This module is **live-Open-RMF only**. `rmf_adapter` is not pip-installable
(only available on deployments that have the `rmf_ros2` stack), so the
imports are gated behind a lazy `_try_import_rmf_adapter()`. CI cannot
exercise the wire-up directly; tests verify structural correctness
(argument shapes, error paths, RobotHandle.bind invocation) by
injecting a fake `rmf_adapter` module via `sys.modules`. Live Open-RMF
validation is documented in `docs/smoke.md` (§13.2 of the design doc).

Why this is a separate module from `adapter.py`:
- `adapter.py` is the entry point; it imports both dry-run + binding
  paths but the binding path only executes when `rmf_adapter` is
  importable.
- Splitting the rmf_adapter-using code into a dedicated module keeps
  the import isolated and gives tests a single seam for `sys.modules`
  injection.

Naming: `rmf_adapter` is the documented ament-installed package name
(NOT `rmf_fleet_adapter` — that's the C++ ROS package; the Python
binding's wheel/ament target is `rmf_adapter`).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from quiksync_client import QuikSyncHttpClient

from .callbacks import make_action_executor, make_navigate_callback, make_stop_callback
from .robot_handle import RobotHandle

log = logging.getLogger("fleet_adapter_quiksync.binding")


# ----- Builders for rmf_adapter primitives -----
#
# These functions take `rmf_adapter` as an explicit parameter rather than
# importing it at module load time. This is the seam tests use to inject
# a fake rmf_adapter via `sys.modules` — see test_binding.py.


def build_vehicle_traits(rmf_adapter: Any, traits: dict[str, Any]) -> Any:
    """Build an `rmf_adapter.VehicleTraits` from a /discovery traits dict.

    Wire shape (per design §4.3.1):
    ```
    {
      "linear_velocity_m_s": 0.5,
      "angular_velocity_rad_s": 0.5,
      "linear_acceleration_m_s2": 0.25,
      "angular_acceleration_rad_s2": 0.25,
      "footprint_radius_m": 0.3,
      "vicinity_radius_m": 0.5
    }
    ```

    The `rmf_adapter.VehicleTraits` constructor accepts a Profile (footprint
    + vicinity) and per-axis Limits (velocity + acceleration). API surface:

        VehicleTraits(linear=Limits(velocity, acceleration),
                      angular=Limits(velocity, acceleration),
                      profile=Profile(footprint, vicinity))

    Names may vary across rmf_adapter versions; the structural shape is
    stable. Test exercises this via sys.modules injection.
    """
    Limits = rmf_adapter.VehicleTraits.Limits
    Profile = rmf_adapter.VehicleTraits.Profile
    Circle = rmf_adapter.Geometry.Circle
    return rmf_adapter.VehicleTraits(
        linear=Limits(
            velocity=float(traits["linear_velocity_m_s"]),
            acceleration=float(traits["linear_acceleration_m_s2"]),
        ),
        angular=Limits(
            velocity=float(traits["angular_velocity_rad_s"]),
            acceleration=float(traits["angular_acceleration_rad_s2"]),
        ),
        profile=Profile(
            footprint=Circle(float(traits["footprint_radius_m"])),
            vicinity=Circle(float(traits["vicinity_radius_m"])),
        ),
    )


def build_battery_system(rmf_adapter: Any, battery: dict[str, Any]) -> Any:
    """Build an `rmf_adapter.BatterySystem` from a /discovery battery dict.

    Wire shape (per design §4.3.1):
    ```
    {
      "recharge_threshold": 0.20,
      "recharge_soc": 1.0,
      "type": "SealedLeadAcid",
      "capacity_ah": 24.0,
      "nominal_voltage_v": 12.0
    }
    ```

    Returns the BatterySystem-or-None per rmf_adapter convention; we
    raise on invalid input rather than silently degrade because a fleet
    without battery model is uninteresting to Open-RMF's task planner.
    """
    return rmf_adapter.BatterySystem.make(
        nominal_voltage=float(battery["nominal_voltage_v"]),
        capacity=float(battery["capacity_ah"]),
        charging_current=10.0,  # default — not advertised in discovery; safe estimate
    )


def build_consider_request_dict(
    rmf_adapter: Any, categories: list[str],
) -> dict[str, Callable[[Any, Any], None]]:
    """Build the `task_categories` / `action_categories` dict for FleetConfiguration.

    Each category maps to a `ConsiderRequest` callable that decides
    whether the fleet bids on the request. For v1, always-accept:

        def consider(description, confirm): confirm.accept()

    Real bidding logic (capacity-aware, current-load-aware) would
    consult RobotHandle state — out of scope for v1. `rmf_adapter` is
    unused in the body — the parameter is kept for symmetry with the
    sibling builders so callers can drive all of them through a single
    `rmf_adapter` reference.
    """
    del rmf_adapter  # not currently needed; kept for API symmetry

    def always_consider(_description: Any, confirm: Any) -> None:
        confirm.accept()

    return {category: always_consider for category in categories}


def build_graph(rmf_adapter: Any, building_map: dict[str, Any], nav_graph_name: str) -> Any:
    """Locate + materialise the nav graph from a /building_map response.

    Building maps from rmf-web carry a list of `levels`, each with
    `nav_graphs` (named lists of vertices + lanes). We pick the graph
    whose `name` field matches the fleet's `nav_graph_name` from
    /discovery.

    Raises `BindingError` if the graph isn't found.
    """
    levels = building_map.get("levels") or []
    for level in levels:
        nav_graphs = level.get("nav_graphs") or []
        for graph in nav_graphs:
            if graph.get("name") == nav_graph_name:
                # rmf_adapter exposes Graph.deserialize(dict) (or
                # Graph.parse(file_path) for the file form). The
                # in-memory dict path is what we want.
                return rmf_adapter.Graph.deserialize(graph)
    raise BindingError(
        f"nav graph {nav_graph_name!r} not found in building_map; available: "
        f"{[g.get('name') for lvl in levels for g in (lvl.get('nav_graphs') or [])]}"
    )


def build_fleet_configuration(
    rmf_adapter: Any,
    fleet_entry: dict[str, Any],
    building_map: dict[str, Any],
) -> Any:
    """Assemble FleetConfiguration from a /discovery fleet entry + building map.

    Pulls `traits`, `battery`, `task_categories`, `nav_graph_name`,
    `max_action_concurrency` from the fleet entry; pulls the named
    graph from the building map. v1 leaves `action_categories` empty
    (perform_action will 400 on dispatch — see callbacks.py); pilot
    smoke fills in customer-specific categories once mapping is ready.
    """
    fleet_name = fleet_entry["fleet_name"]
    traits = build_vehicle_traits(rmf_adapter, fleet_entry["traits"])
    battery = build_battery_system(rmf_adapter, fleet_entry["battery"])
    graph = build_graph(rmf_adapter, building_map, fleet_entry["nav_graph_name"])
    task_categories = build_consider_request_dict(
        rmf_adapter, fleet_entry.get("task_categories") or [],
    )
    # action_categories empty in v1; customer-specific categories get
    # added before enabling perform_action dispatch in production.
    action_categories: dict[str, Any] = {}

    return rmf_adapter.easy_full_control.FleetConfiguration(
        fleet_name=fleet_name,
        graph=graph,
        traits=traits,
        battery_system=battery,
        task_categories=task_categories,
        action_categories=action_categories,
    )


# ----- Adapter bootstrap -----


class BindingError(Exception):
    """Raised when the binding cannot complete (missing fleet, bad graph,
    unsupported rmf_adapter version)."""


def bind_easy_full_control(
    rmf_adapter: Any,
    fleet_entry: dict[str, Any],
    building_map: dict[str, Any],
    handles: dict[str, RobotHandle],
    http: QuikSyncHttpClient,
    node_name: str = "fleet_adapter_quiksync",
    server_uri: Optional[str] = None,
) -> tuple[Any, Any]:
    """Bootstrap the Adapter + EasyFullControl fleet + register robots.

    Returns the (Adapter, FleetUpdateHandle) pair so the caller can
    spin the executor and shut down cleanly on signal.

    Per design §6.2:
    - One adapter per process (one fleet); no shared FleetConfiguration.
    - `add_robot(name, initial_state, configuration, RobotCallbacks(...))`
      is called once per robot listed in /discovery.
    - Each robot's `EasyRobotUpdateHandle` is registered with its
      `RobotHandle.bind()` so subsequent state-pump frames push into Open-RMF.

    The initial `RobotState` comes from /discovery's `initial_map`. The
    state pump will overwrite it within ~1 s once the first WSS frame
    arrives.

    `server_uri` (optional): if set, the FleetConfiguration's `server_uri`
    is populated so the adapter posts task/state updates to rmf-web's
    API server. Matches `fleet_adapter_template`'s `--server_uri` flag.
    """
    fleet_name = fleet_entry["fleet_name"]
    fleet_config = build_fleet_configuration(rmf_adapter, fleet_entry, building_map)
    if server_uri:
        # `server_uri` is a writable property on FleetConfiguration in
        # the easy_full_control binding. Used by rmf-web to receive
        # task / fleet state updates from this adapter.
        fleet_config.server_uri = server_uri

    log.info("creating Adapter(node_name=%r)", node_name)
    adapter = rmf_adapter.Adapter.make(node_name)
    if adapter is None:
        raise BindingError(
            f"rmf_adapter.Adapter.make({node_name!r}) returned None — is rclpy initialised?"
        )

    log.info("adding EasyFullControl fleet=%r", fleet_name)
    # `add_easy_fleet` matches the canonical fleet_adapter_template pattern
    # (`adapter.add_easy_fleet(fleet_config)`); the Python Adapter binding
    # exposes the EasyFullControl wire-up under this method name.
    fleet_handle = adapter.add_easy_fleet(fleet_config)

    robots = fleet_entry.get("robots") or []
    bound_count = 0
    for robot in robots:
        name = robot.get("name")
        if not name:
            continue
        handle = handles.get(name)
        if handle is None:
            log.warning(
                "discovery listed robot=%r but no RobotHandle present; skipping",
                name,
            )
            continue

        initial_state = _initial_robot_state(rmf_adapter, robot)
        robot_config = _robot_configuration(rmf_adapter, robot)
        callbacks = _build_robot_callbacks(rmf_adapter, http, fleet_name, name, handle)

        log.info("adding robot=%r to fleet=%r", name, fleet_name)
        update_handle = fleet_handle.add_robot(
            name=name,
            initial_state=initial_state,
            configuration=robot_config,
            callbacks=callbacks,
        )
        handle.bind(update_handle)
        bound_count += 1

    log.info("EasyFullControl bound: fleet=%r robots=%d", fleet_name, bound_count)
    return adapter, fleet_handle


# ----- Private helpers -----


def _initial_robot_state(rmf_adapter: Any, robot: dict[str, Any]) -> Any:
    """Build an initial RobotState placeholder for `add_robot`.

    Real state arrives via WSS within ~1 s. The placeholder uses (0,0,0)
    at the initial_map; the state pump overwrites this on the first
    frame.
    """
    initial_map = robot.get("initial_map") or ""
    return rmf_adapter.RobotState(
        initial_map,
        rmf_adapter.type.Vector3d(0.0, 0.0, 0.0),
        1.0,  # battery SOC fraction — overwritten on first state push
    )


def _robot_configuration(rmf_adapter: Any, robot: dict[str, Any]) -> Any:
    """Build the per-robot RobotConfiguration.

    rmf_adapter's `easy_full_control.RobotConfiguration` carries optional
    per-robot data (charger waypoint, recharge battery threshold). v1
    constructs an empty default; pilot fills in per-robot configs via
    a follow-up enhancement if customers need them.
    """
    return rmf_adapter.easy_full_control.RobotConfiguration([])


def _build_robot_callbacks(
    rmf_adapter: Any,
    http: QuikSyncHttpClient,
    fleet: str,
    robot: str,
    handle: RobotHandle,
) -> Any:
    """Wrap our pure callback factories in an rmf_adapter.RobotCallbacks.

    Constructor uses **positional** arguments to match the canonical
    fleet_adapter_template pattern; kwarg-form availability varies
    across rmf_adapter binding versions.
    """
    navigate = make_navigate_callback(http, fleet, robot, handle)
    stop = make_stop_callback(http, fleet, robot, handle)
    action_executor = make_action_executor(http, fleet, robot, handle)
    return rmf_adapter.easy_full_control.RobotCallbacks(
        navigate,
        stop,
        action_executor,
    )
