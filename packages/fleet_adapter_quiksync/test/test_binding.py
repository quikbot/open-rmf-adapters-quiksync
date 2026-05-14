"""Tests for binding.py — the rmf_adapter wire-up glue.

`rmf_adapter` isn't pip-installable; tests inject a fake module via
`sys.modules` with the structural shape we depend on. The fake captures
constructor arguments so we can assert wire shape correctness. Live Open-RMF
smoke (per `docs/smoke.md`) catches any divergence in the actual
`rmf_adapter` API surface.

What we verify here:
- Builders pass through expected fields from /discovery + /building_map
  to the rmf_adapter constructors.
- `bind_easy_full_control` calls `Adapter.make`, `add_easy_fleet`,
  and `add_robot` once per robot in the fleet entry.
- Each `RobotHandle` is bound to the returned `EasyRobotUpdateHandle`.
- Missing nav graph raises `BindingError`.
- Robots in /discovery without a corresponding `RobotHandle` log a
  warning + skip rather than crash.
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from fleet_adapter_quiksync.binding import (
    BindingError,
    bind_easy_full_control,
    build_battery_system,
    build_consider_request_dict,
    build_fleet_configuration,
    build_graph,
    build_vehicle_traits,
)
from fleet_adapter_quiksync.robot_handle import RobotHandle


# ----- Fake rmf_adapter fixtures -----


class _FakeCircle:
    def __init__(self, radius: float) -> None:
        self.radius = radius


class _FakeLimits:
    def __init__(self, velocity: float, acceleration: float) -> None:
        self.velocity = velocity
        self.acceleration = acceleration


class _FakeProfile:
    def __init__(self, footprint: Any, vicinity: Any) -> None:
        self.footprint = footprint
        self.vicinity = vicinity


class _FakeVehicleTraits:
    Limits = _FakeLimits
    Profile = _FakeProfile

    def __init__(self, linear: Any, angular: Any, profile: Any) -> None:
        self.linear = linear
        self.angular = angular
        self.profile = profile


class _FakeGeometry:
    Circle = _FakeCircle


class _FakeBatterySystem:
    @staticmethod
    def make(nominal_voltage: float, capacity: float, charging_current: float) -> "_FakeBatterySystem":
        b = _FakeBatterySystem()
        b.nominal_voltage = nominal_voltage
        b.capacity = capacity
        b.charging_current = charging_current
        return b


class _FakeGraph:
    @staticmethod
    def deserialize(graph_dict: dict[str, Any]) -> "_FakeGraph":
        g = _FakeGraph()
        g.spec = graph_dict
        return g


class _FakeFleetConfiguration:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeRobotConfiguration:
    def __init__(self, accepts: list[Any]) -> None:
        self.accepts = accepts


class _FakeRobotCallbacks:
    def __init__(self, navigate: Any, stop: Any, action_executor: Any) -> None:
        self.navigate = navigate
        self.stop = stop
        self.action_executor = action_executor


class _FakeUpdateHandle:
    def __init__(self, robot_name: str) -> None:
        self.robot_name = robot_name
        self.updates: list[tuple[Any, Any]] = []

    def update(self, state: Any, current_activity: Any) -> None:
        self.updates.append((state, current_activity))


class _FakeFleetHandle:
    def __init__(self) -> None:
        self.added_robots: list[dict[str, Any]] = []

    def add_robot(self, name: str, initial_state: Any, configuration: Any, callbacks: Any) -> _FakeUpdateHandle:
        self.added_robots.append(
            {"name": name, "initial_state": initial_state, "configuration": configuration, "callbacks": callbacks},
        )
        return _FakeUpdateHandle(name)


class _FakeAdapter:
    def __init__(self, node_name: str) -> None:
        self.node_name = node_name
        self.fleet_handle = _FakeFleetHandle()
        self.added_fleet_configs: list[Any] = []

    @classmethod
    def make(cls, node_name: str) -> "_FakeAdapter":
        return cls(node_name)

    def add_easy_fleet(self, fleet_config: Any) -> _FakeFleetHandle:
        self.added_fleet_configs.append(fleet_config)
        return self.fleet_handle


class _FakeRobotState:
    def __init__(self, level: str, position: Any, battery_soc: float) -> None:
        self.level = level
        self.position = position
        self.battery_soc = battery_soc


class _FakeVector3d:
    def __init__(self, x: float, y: float, yaw: float) -> None:
        self.x, self.y, self.yaw = x, y, yaw


def make_fake_rmf_adapter() -> Any:
    """Build a fake rmf_adapter module with the structural surface
    binding.py depends on. Each test gets a fresh instance — the fakes
    record call args."""
    mod = types.SimpleNamespace()

    type_mod = types.SimpleNamespace()
    type_mod.Vector3d = _FakeVector3d
    mod.type = type_mod

    geometry = types.SimpleNamespace()
    geometry.Circle = _FakeCircle
    mod.Geometry = geometry

    mod.VehicleTraits = _FakeVehicleTraits
    mod.BatterySystem = _FakeBatterySystem
    mod.Graph = _FakeGraph
    mod.RobotState = _FakeRobotState
    mod.Adapter = _FakeAdapter

    efc = types.SimpleNamespace()
    efc.FleetConfiguration = _FakeFleetConfiguration
    efc.RobotConfiguration = _FakeRobotConfiguration
    efc.RobotCallbacks = _FakeRobotCallbacks
    mod.easy_full_control = efc

    return mod


# ----- Fixture data -----


TRAITS = {
    "linear_velocity_m_s": 0.5,
    "angular_velocity_rad_s": 0.5,
    "linear_acceleration_m_s2": 0.25,
    "angular_acceleration_rad_s2": 0.25,
    "footprint_radius_m": 0.3,
    "vicinity_radius_m": 0.5,
}


BATTERY = {
    "recharge_threshold": 0.20,
    "recharge_soc": 1.0,
    "type": "SealedLeadAcid",
    "capacity_ah": 24.0,
    "nominal_voltage_v": 12.0,
}


FLEET_ENTRY = {
    "fleet_name": "service_robots",
    "task_categories": ["delivery", "loop"],
    "traits": TRAITS,
    "battery": BATTERY,
    "robots": [
        {"name": "robot-1", "initial_map": "L1"},
        {"name": "robot-2", "initial_map": "L1"},
    ],
    "nav_graph_name": "office",
    "max_action_concurrency": 1,
}


BUILDING_MAP = {
    "levels": [
        {
            "name": "L1",
            "nav_graphs": [
                {"name": "office", "vertices": [], "lanes": []},
                {"name": "other_graph", "vertices": [], "lanes": []},
            ],
        },
    ],
}


# ----- build_vehicle_traits -----


def test_build_vehicle_traits_passes_velocity_acceleration_footprint():
    rmf = make_fake_rmf_adapter()
    traits = build_vehicle_traits(rmf, TRAITS)
    assert traits.linear.velocity == 0.5
    assert traits.linear.acceleration == 0.25
    assert traits.angular.velocity == 0.5
    assert traits.angular.acceleration == 0.25
    assert traits.profile.footprint.radius == 0.3
    assert traits.profile.vicinity.radius == 0.5


def test_build_vehicle_traits_raises_on_missing_field():
    rmf = make_fake_rmf_adapter()
    incomplete = {k: v for k, v in TRAITS.items() if k != "footprint_radius_m"}
    with pytest.raises(KeyError):
        build_vehicle_traits(rmf, incomplete)


# ----- build_battery_system -----


def test_build_battery_system_passes_voltage_and_capacity():
    rmf = make_fake_rmf_adapter()
    battery = build_battery_system(rmf, BATTERY)
    assert battery.nominal_voltage == 12.0
    assert battery.capacity == 24.0
    assert battery.charging_current == 10.0  # default


# ----- build_consider_request_dict -----


def test_consider_request_dict_always_accepts():
    rmf = make_fake_rmf_adapter()
    d = build_consider_request_dict(rmf, ["delivery", "loop"])
    assert set(d.keys()) == {"delivery", "loop"}

    confirm = MagicMock()
    d["delivery"]({"some": "request"}, confirm)
    confirm.accept.assert_called_once()


def test_consider_request_dict_empty_input_produces_empty_dict():
    rmf = make_fake_rmf_adapter()
    assert build_consider_request_dict(rmf, []) == {}


# ----- build_graph -----


def test_build_graph_finds_named_graph():
    rmf = make_fake_rmf_adapter()
    g = build_graph(rmf, BUILDING_MAP, "office")
    assert g.spec == {"name": "office", "vertices": [], "lanes": []}


def test_build_graph_raises_on_unknown_name():
    rmf = make_fake_rmf_adapter()
    with pytest.raises(BindingError) as exc:
        build_graph(rmf, BUILDING_MAP, "nonexistent")
    assert "nonexistent" in str(exc.value)
    assert "office" in str(exc.value)  # the diagnostic lists known names
    assert "other_graph" in str(exc.value)


def test_build_graph_raises_on_empty_building_map():
    rmf = make_fake_rmf_adapter()
    with pytest.raises(BindingError):
        build_graph(rmf, {"levels": []}, "office")


# ----- build_fleet_configuration -----


def test_build_fleet_configuration_packs_everything():
    rmf = make_fake_rmf_adapter()
    cfg = build_fleet_configuration(rmf, FLEET_ENTRY, BUILDING_MAP)
    assert cfg.fleet_name == "service_robots"
    assert cfg.graph.spec["name"] == "office"
    assert cfg.traits.linear.velocity == 0.5
    assert cfg.battery_system.capacity == 24.0
    assert set(cfg.task_categories.keys()) == {"delivery", "loop"}
    # v1 always-empty per binding.py docstring
    assert cfg.action_categories == {}


# ----- bind_easy_full_control -----


def test_bind_registers_all_robots_and_binds_handles():
    rmf = make_fake_rmf_adapter()
    http = MagicMock()
    handles = {"robot-1": RobotHandle("robot-1"), "robot-2": RobotHandle("robot-2")}

    adapter, fleet_handle = bind_easy_full_control(
        rmf_adapter=rmf,
        fleet_entry=FLEET_ENTRY,
        building_map=BUILDING_MAP,
        handles=handles,
        http=http,
    )

    # Adapter constructed; fleet config registered
    assert isinstance(adapter, _FakeAdapter)
    assert adapter.node_name == "fleet_adapter_quiksync"
    assert len(adapter.added_fleet_configs) == 1
    assert adapter.added_fleet_configs[0].fleet_name == "service_robots"

    # Both robots added
    assert {r["name"] for r in fleet_handle.added_robots} == {"robot-1", "robot-2"}

    # RobotHandle.bind() called for both robots — they're bound now
    assert handles["robot-1"].is_bound()
    assert handles["robot-2"].is_bound()


def test_bind_skips_robots_without_local_handle():
    """If /discovery lists more robots than the adapter has handles for
    (e.g. a robot came online server-side between adapter startup and
    /discovery fetch — unusual but possible), skip rather than crash."""
    rmf = make_fake_rmf_adapter()
    http = MagicMock()
    # Only one of the two discovery-listed robots has a local handle
    handles = {"robot-1": RobotHandle("robot-1")}

    adapter, fleet_handle = bind_easy_full_control(
        rmf_adapter=rmf,
        fleet_entry=FLEET_ENTRY,
        building_map=BUILDING_MAP,
        handles=handles,
        http=http,
    )

    # Only robot-1 added to the fleet
    assert {r["name"] for r in fleet_handle.added_robots} == {"robot-1"}
    assert handles["robot-1"].is_bound()


def test_bind_uses_custom_node_name():
    rmf = make_fake_rmf_adapter()
    http = MagicMock()
    handles = {"robot-1": RobotHandle("robot-1"), "robot-2": RobotHandle("robot-2")}

    adapter, _ = bind_easy_full_control(
        rmf_adapter=rmf,
        fleet_entry=FLEET_ENTRY,
        building_map=BUILDING_MAP,
        handles=handles,
        http=http,
        node_name="custom_node_name",
    )
    assert adapter.node_name == "custom_node_name"


def test_bind_callbacks_wired_to_http():
    """Each robot's RobotCallbacks must be a callable backed by our
    HTTP client + handle — verify the navigate callback fires a POST
    with the right fleet/robot identity."""
    from types import SimpleNamespace

    rmf = make_fake_rmf_adapter()
    http = MagicMock()
    http.post_navigate.return_value = {"task_id": "t1"}
    handles = {"robot-1": RobotHandle("robot-1"), "robot-2": RobotHandle("robot-2")}

    _, fleet_handle = bind_easy_full_control(
        rmf_adapter=rmf,
        fleet_entry=FLEET_ENTRY,
        building_map=BUILDING_MAP,
        handles=handles,
        http=http,
    )

    # Find robot-1's callbacks
    robot1_callbacks = next(r["callbacks"] for r in fleet_handle.added_robots if r["name"] == "robot-1")
    destination = SimpleNamespace(
        map="L1",
        position=SimpleNamespace(x=1.0, y=2.0, yaw=0.5),
        dock="",
        speed_limit=0.0,
    )
    robot1_callbacks.navigate(destination, SimpleNamespace())

    http.post_navigate.assert_called_once()
    _, kwargs = http.post_navigate.call_args
    assert kwargs["fleet"] == "service_robots"
    assert kwargs["robot"] == "robot-1"


def test_bind_raises_when_adapter_make_returns_none():
    """If rclpy isn't initialised, rmf_adapter.Adapter.make returns None.
    We surface this as a clear error rather than crashing later."""
    rmf = make_fake_rmf_adapter()

    class NoneAdapter:
        @staticmethod
        def make(_node_name: str) -> None:
            return None

    rmf.Adapter = NoneAdapter
    handles = {"robot-1": RobotHandle("robot-1")}

    with pytest.raises(BindingError) as exc:
        bind_easy_full_control(
            rmf_adapter=rmf,
            fleet_entry=FLEET_ENTRY,
            building_map=BUILDING_MAP,
            handles=handles,
            http=MagicMock(),
        )
    assert "rclpy" in str(exc.value).lower()
