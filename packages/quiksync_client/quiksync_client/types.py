"""Pydantic models for the the QuikSync adapter API HTTPS wire shape.

Mirrors design §4.3 — discovery, fleet state, navigate/dock/stop/perform_action
response, door + lift state/request. Strict by default (`extra="forbid"`)
on responses we control; permissive on rmf-web-shaped inputs we don't.

Versions match the JSON wire — `unix_millis_*` timestamps stay as ints;
adapter-side conversion to `builtin_interfaces/Time` happens at ROS publish.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Reject unknown fields by default — catches wire-shape regressions early."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ----- Discovery -----

class VehicleTraits(StrictModel):
    linear_velocity_m_s: float
    angular_velocity_rad_s: float
    linear_acceleration_m_s2: float
    angular_acceleration_rad_s2: float
    footprint_radius_m: float
    vicinity_radius_m: float


class BatterySystem(StrictModel):
    recharge_threshold: float
    recharge_soc: float
    type: str
    capacity_ah: float
    nominal_voltage_v: float


class RobotEntry(StrictModel):
    name: str
    initial_map: str


class FleetOut(StrictModel):
    fleet_name: str
    task_categories: list[str] = Field(default_factory=list)
    traits: VehicleTraits
    battery: BatterySystem
    robots: list[RobotEntry] = Field(default_factory=list)
    nav_graph_name: str
    max_action_concurrency: int = 1


class Position(StrictModel):
    x: float
    y: float
    yaw: float = 0.0


class DoorOut(StrictModel):
    door_name: str
    map_name: str
    open_position_m: Position
    closed_position_m: Position
    motion_direction: int


class LiftOut(StrictModel):
    lift_name: str
    available_floors: list[str] = Field(default_factory=list)
    doors_per_floor: dict[str, str] = Field(default_factory=dict)
    reference_coordinates: dict[str, Position] = Field(default_factory=dict)


class DiscoveryResponse(StrictModel):
    fleets: list[FleetOut] = Field(default_factory=list)
    doors: list[DoorOut] = Field(default_factory=list)
    lifts: list[LiftOut] = Field(default_factory=list)


# ----- Fleet state (WSS frame + GET poll) -----

class Location(BaseModel):
    """Permissive — rmf-web's Location shape is our own JSON projection but
    field set may grow; don't fail on extras."""

    x: float
    y: float
    yaw: float
    level_name: str


class RobotMode(BaseModel):
    mode: str  # the MODE_* name; adapter maps to uint32 at publish time


class RobotState(BaseModel):
    """server-side fleet-state frame. Adapter converts to
    `rmf_fleet_msgs.RobotState` at ROS publish time."""

    name: str
    battery_percent: float
    location: Location
    mode: RobotMode
    task_id: Optional[str] = None
    unix_millis_time: int
    unix_millis_battery_time: Optional[int] = None


class FleetState(BaseModel):
    name: str
    robots: list[RobotState] = Field(default_factory=list)


# ----- Navigate / dock / stop / perform_action POST responses -----

class FleetCommandAccepted(BaseModel):
    """Success body for 202 from navigate/dock/stop/perform_action — when the
    real engine-dispatch lands."""

    task_id: str
    execution_id: str
    status: str  # "queued"


class CoordNavigateDiagnostic(BaseModel):
    nearest_waypoint: Optional[str] = None
    distance_to_nearest_m: Optional[float] = None
    tolerance_m: float
    note: Optional[str] = None


class CoordNavigateError(BaseModel):
    """Body of 400 `coord_navigate_not_supported` — adapter surfaces this back
    to Open-RMF as `CommandExecution.failed()` with the diagnostic in the message."""

    error: str
    message: str
    diagnostic: Optional[CoordNavigateDiagnostic] = None


# ----- Task state (GET /tasks/{task_id}/state) -----

class TaskState(BaseModel):
    """Loose mirror of rmf-web's TaskState — schema evolves upstream so don't
    `extra=forbid`. Adapter uses `status` to drive Open-RMF's task lifecycle."""

    status: str  # queued | underway | completed | failed | canceled | killed
    booking: Optional[dict] = None
    category: Optional[str] = None
    phases: Optional[list[dict]] = None
    estimate_millis: Optional[int] = None
    unix_millis_finish_time: Optional[int] = None
