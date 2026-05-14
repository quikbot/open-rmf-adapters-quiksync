"""Per-robot state handle.

Bridges the QuikSync adapter API's `FleetState` frames into Open-RMF's
`EasyRobotUpdateHandle.update(state, current_activity)`. The handle:

- Caches the latest state dict from the QuikSync WSS stream
- Translates it into an Open-RMF `RobotState` (battery SOC fraction, map
  name, position) when EasyRobotUpdateHandle is bound
- Tracks the current `ActivityIdentifier` for `EasyRobotUpdateHandle.
  update()`'s second arg (the `current_activity`)

Decoupling note: this module imports `rmf_adapter` lazily inside
methods that need it. The package compiles + unit-tests on
`ros:jazzy-ros-base` without rmf_adapter installed (the real Open-RMF
adapter ships on deployments that include the full rmf_ros2 stack).
Methods that require Open-RMF types raise an explicit RuntimeError if
called without binding first.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any, Optional

log = logging.getLogger("fleet_adapter_quiksync.robot_handle")


class RobotHandle:
    """One per managed robot. Holds latest state + the lazy-bound
    EasyRobotUpdateHandle."""

    def __init__(self, robot_name: str) -> None:
        self.robot_name = robot_name
        self._lock = Lock()
        self._latest_state: Optional[dict[str, Any]] = None
        self._rmf_handle: Any = None  # rmf_adapter.EasyRobotUpdateHandle
        self._current_activity: Any = None  # rmf_adapter.ActivityIdentifier
        self._updates_pushed = 0
        self._updates_dropped_no_handle = 0

    def latest_state(self) -> Optional[dict[str, Any]]:
        """Returns the most recent state dict, or None if never observed."""
        with self._lock:
            return self._latest_state

    def updates_pushed(self) -> int:
        """Count of successful push-into-Open-RMF updates (testing helper)."""
        return self._updates_pushed

    def updates_dropped_no_handle(self) -> int:
        """Count of state updates received before Open-RMF handle was bound."""
        return self._updates_dropped_no_handle

    def bind(self, rmf_handle: Any) -> None:
        """Register the rmf_adapter.EasyRobotUpdateHandle this robot belongs to.

        Called by adapter.py after `EasyFullControl.add_robot()` returns. From
        this point onward, every `on_state()` push translates into an Open-RMF
        update call.
        """
        with self._lock:
            self._rmf_handle = rmf_handle

    def is_bound(self) -> bool:
        return self._rmf_handle is not None

    def set_current_activity(self, activity: Any) -> None:
        """Update the ActivityIdentifier we'll pass to Open-RMF on the next push."""
        with self._lock:
            self._current_activity = activity

    def on_state(self, state: dict[str, Any]) -> None:
        """Receive a new state dict from the WSS pump.

        Always caches; pushes into Open-RMF if + only if `bind()` was called.
        """
        with self._lock:
            self._latest_state = state
            if self._rmf_handle is None:
                self._updates_dropped_no_handle += 1
                return
            rmf_state = self._to_rmf_robot_state(state)
            if rmf_state is None:
                return
            handle = self._rmf_handle
            current_activity = self._current_activity

        # Release the lock before crossing the Open-RMF boundary — update() can
        # block briefly and we don't want to serialise multi-robot pumps.
        try:
            handle.update(rmf_state, current_activity)
            self._updates_pushed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("EasyRobotUpdateHandle.update failed for robot=%s: %s", self.robot_name, e)

    def _to_rmf_robot_state(self, state: dict[str, Any]) -> Any:
        """Translate our JSON wire shape to rmf_adapter.RobotState.

        Imported lazily because rmf_adapter isn't available on
        ros:jazzy-ros-base (CI) — only on deployments with rmf_ros2.
        Tests that don't bind never reach this path.
        """
        try:
            from rmf_adapter import RobotState  # type: ignore[import-untyped]
            from rmf_adapter.type import Vector3d  # type: ignore[import-untyped]
        except ImportError:
            log.error(
                "rmf_adapter not importable — cannot translate state for robot=%s. "
                "Adapter binary requires the rmf_ros2 stack at runtime.",
                self.robot_name,
            )
            return None

        location = state.get("location")
        if not isinstance(location, dict):
            return None
        try:
            x = float(location["x"])
            y = float(location["y"])
            yaw = float(location["yaw"])
            level_name = location["level_name"]
        except (KeyError, TypeError, ValueError):
            return None

        battery_percent = state.get("battery_percent")
        if not isinstance(battery_percent, (int, float)):
            return None
        battery_soc = float(battery_percent) / 100.0  # rmf_adapter uses SOC fraction [0,1]

        return RobotState(level_name, Vector3d(x, y, yaw), battery_soc)
