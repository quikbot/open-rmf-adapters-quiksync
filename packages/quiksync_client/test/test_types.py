"""Tests for pydantic wire models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quiksync_client.types import (
    BatterySystem,
    DiscoveryResponse,
    FleetState,
    RobotState,
    VehicleTraits,
)


def test_discovery_response_parses_design_example():
    """Mirrors the design §4.3.1 example body."""
    payload = {
        "fleets": [
            {
                "fleet_name": "service_robots",
                "task_categories": ["delivery", "loop"],
                "traits": {
                    "linear_velocity_m_s": 0.5,
                    "angular_velocity_rad_s": 0.5,
                    "linear_acceleration_m_s2": 0.25,
                    "angular_acceleration_rad_s2": 0.25,
                    "footprint_radius_m": 0.3,
                    "vicinity_radius_m": 0.5,
                },
                "battery": {
                    "recharge_threshold": 0.20,
                    "recharge_soc": 1.0,
                    "type": "SealedLeadAcid",
                    "capacity_ah": 24.0,
                    "nominal_voltage_v": 12.0,
                },
                "robots": [
                    {"name": "robot-1", "initial_map": "L1"},
                    {"name": "robot-2", "initial_map": "L1"},
                ],
                "nav_graph_name": "office",
                "max_action_concurrency": 1,
            },
        ],
        "doors": [],
        "lifts": [],
    }
    response = DiscoveryResponse.model_validate(payload)
    assert len(response.fleets) == 1
    fleet = response.fleets[0]
    assert fleet.fleet_name == "service_robots"
    assert fleet.task_categories == ["delivery", "loop"]
    assert fleet.traits.linear_velocity_m_s == 0.5
    assert fleet.battery.type == "SealedLeadAcid"
    assert len(fleet.robots) == 2


def test_discovery_response_empty():
    """All arrays default to []."""
    response = DiscoveryResponse.model_validate({})
    assert response.fleets == []
    assert response.doors == []
    assert response.lifts == []


def test_discovery_response_rejects_unknown_field():
    """StrictModel forbids extras — catches wire-shape regressions."""
    with pytest.raises(ValidationError):
        DiscoveryResponse.model_validate({"fleets": [], "future_field": "boom"})


def test_fleet_state_parses_loose():
    """FleetState is BaseModel (permissive) — accepts upstream-evolving fields."""
    payload = {
        "name": "service_robots",
        "robots": [
            {
                "name": "robot-1",
                "battery_percent": 87.5,
                "location": {"x": 12.3, "y": 4.5, "yaw": 1.57, "level_name": "L1"},
                "mode": {"mode": "MODE_IDLE"},
                "task_id": None,
                "unix_millis_time": 1747094400000,
                "unix_millis_battery_time": 1747094390000,
            },
        ],
    }
    state = FleetState.model_validate(payload)
    assert state.name == "service_robots"
    assert len(state.robots) == 1
    assert state.robots[0].mode.mode == "MODE_IDLE"
    assert state.robots[0].task_id is None
    assert state.robots[0].battery_percent == 87.5


def test_fleet_state_accepts_unknown_fields():
    """RobotState is BaseModel (not StrictModel) — forward-compat for upstream."""
    payload = {
        "name": "f",
        "robots": [
            {
                "name": "r",
                "battery_percent": 50.0,
                "location": {"x": 0.0, "y": 0.0, "yaw": 0.0, "level_name": "L1"},
                "mode": {"mode": "MODE_IDLE", "mode_request_id": 7},  # extra field
                "unix_millis_time": 1,
            },
        ],
    }
    state = FleetState.model_validate(payload)  # should not raise
    assert state.robots[0].mode.mode == "MODE_IDLE"


def test_vehicle_traits_required_fields():
    """All 6 trait fields required — missing → ValidationError."""
    with pytest.raises(ValidationError):
        VehicleTraits.model_validate({"linear_velocity_m_s": 0.5})


def test_battery_system_required_fields():
    """All 5 battery fields required."""
    with pytest.raises(ValidationError):
        BatterySystem.model_validate({"recharge_threshold": 0.2})
