"""Per-robot state handle.

Bridges the QuikSync adapter API's `FleetState` frames into Open-RMF's
`EasyRobotUpdateHandle.update(state, current_activity)`. The handle:

- Caches the latest state dict from the QuikSync WSS stream
- Lazily registers the robot with Open-RMF on the first valid WSS frame
  (see "Lazy registration" below)
- Translates subsequent frames into an Open-RMF `RobotState` (battery SOC
  fraction, map name, position) and pushes them via the bound update handle
- Tracks the current `ActivityIdentifier` for `EasyRobotUpdateHandle.
  update()`'s second arg (the `current_activity`)

Lazy registration: `rmf_adapter`'s `EasyFullControl.add_robot` requires the
initial `RobotState`'s position to lie on the navigation graph. A synthesised
placeholder (`level_name=""`, `pose=(0,0,0)`) is rejected at the C++ layer
with an "Unable to compute a location on the navigation graph" error and
`add_robot` returns `None`. To avoid this, the adapter's binding layer calls
`prepare_registration(fleet_handle, robot_config, callbacks)` instead of
`add_robot`. The first WSS state frame that translates to a valid on-graph
`RobotState` is what actually invokes `add_robot`; subsequent frames take
the normal update path.

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
    """One per managed robot. Holds latest state + handles lazy
    registration with Open-RMF on the first valid WSS frame."""

    def __init__(self, robot_name: str) -> None:
        self.robot_name = robot_name
        self._lock = Lock()
        self._latest_state: Optional[dict[str, Any]] = None
        self._rmf_handle: Any = None  # rmf_adapter.EasyRobotUpdateHandle
        self._current_activity: Any = None  # rmf_adapter.ActivityIdentifier
        # Lazy-registration state: stashed by prepare_registration(), consumed
        # by on_state() on the first valid frame.
        self._fleet_handle: Any = None  # rmf_adapter.easy_full_control.EasyFullControl
        self._robot_config: Any = None  # rmf_adapter.easy_full_control.RobotConfiguration
        self._callbacks: Any = None  # rmf_adapter.easy_full_control.RobotCallbacks
        # Counters
        self._updates_pushed = 0
        self._updates_dropped_no_handle = 0
        self._registrations_rejected = 0

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

    def registrations_rejected(self) -> int:
        """Count of `add_robot` attempts that returned None (off-graph pose,
        unknown level, etc.). The handle stays in the prepared-but-not-bound
        state and retries on the next frame."""
        return self._registrations_rejected

    def prepare_registration(
        self,
        fleet_handle: Any,
        robot_config: Any,
        callbacks: Any,
    ) -> None:
        """Stash the data `add_robot` will need on the first valid WSS frame.

        Called by the binding layer in lieu of an eager `add_robot`. The
        actual registration is deferred to `on_state()` so that the
        initial `RobotState` carries the real on-graph pose from the
        QuikSync server rather than a synthesised placeholder.
        """
        with self._lock:
            self._fleet_handle = fleet_handle
            self._robot_config = robot_config
            self._callbacks = callbacks

    def is_prepared(self) -> bool:
        """True once `prepare_registration` has been called. The handle
        is ready to register on the first valid WSS frame."""
        return self._fleet_handle is not None

    def bind(self, rmf_handle: Any) -> None:
        """Manually bind a pre-existing `EasyRobotUpdateHandle`.

        Production code path uses lazy registration via
        `prepare_registration()` + `on_state()`. This direct-bind entry
        point is kept for test injection (mock handle) and for any
        future caller that wants to register synchronously with a
        known-good initial state.
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

        Three branches:
        1. Already bound — translate + push update via the normal path.
        2. Prepared but not bound — translate; if the result is a valid
           on-graph RobotState, call `add_robot` and bind the returned
           handle. `add_robot` consumes the state as the initial state,
           so no separate `update()` is issued for this frame.
        3. Not prepared — drop with no-handle counter (binding layer
           hasn't wired this handle yet).
        """
        with self._lock:
            self._latest_state = state

            if self._rmf_handle is not None:
                # Already bound — normal push path.
                rmf_state = self._to_rmf_robot_state(state)
                if rmf_state is None:
                    return
                handle = self._rmf_handle
                current_activity = self._current_activity
                pending_register: Optional[tuple[Any, Any, Any, Any]] = None
            elif self._fleet_handle is not None:
                # Prepared — try registration with this frame.
                rmf_state = self._to_rmf_robot_state(state)
                if rmf_state is None:
                    self._updates_dropped_no_handle += 1
                    return
                pending_register = (
                    self._fleet_handle,
                    rmf_state,
                    self._robot_config,
                    self._callbacks,
                )
                handle = None
                current_activity = None
            else:
                # Not prepared.
                self._updates_dropped_no_handle += 1
                return

        # Release the lock before crossing the Open-RMF boundary — update()
        # and add_robot can block briefly and we don't want to serialise
        # multi-robot pumps.
        if pending_register is not None:
            self._attempt_lazy_register(*pending_register)
            return

        try:
            handle.update(rmf_state, current_activity)
            self._updates_pushed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("EasyRobotUpdateHandle.update failed for robot=%s: %s", self.robot_name, e)

    def _attempt_lazy_register(
        self,
        fleet_handle: Any,
        rmf_state: Any,
        robot_config: Any,
        callbacks: Any,
    ) -> None:
        """Call `add_robot` outside the lock; bind on success.

        On rejection (`add_robot` returns None — typically because the
        initial pose isn't on the nav graph), the handle stays in the
        prepared state and retries on the next frame. The QuikSync state
        pump emits ~1 Hz per robot, so a retry-on-next-frame loop is
        bounded by however long it takes the vehicle to move onto a graph
        waypoint (or for the server to publish on-graph coords).
        """
        try:
            update_handle = fleet_handle.add_robot(
                self.robot_name,
                rmf_state,
                robot_config,
                callbacks,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "add_robot raised for robot=%s; will retry on next frame: %s",
                self.robot_name, e,
            )
            with self._lock:
                self._registrations_rejected += 1
            return

        with self._lock:
            if update_handle is None:
                self._registrations_rejected += 1
                log.warning(
                    "add_robot returned None for robot=%s — pose may be off the nav graph; "
                    "will retry on next frame",
                    self.robot_name,
                )
                return
            self._rmf_handle = update_handle
            # add_robot consumes the state as the initial state; count it as
            # a successful push so callers can use updates_pushed as a
            # liveness signal.
            self._updates_pushed += 1
            log.info("robot=%s registered with Open-RMF (lazy)", self.robot_name)

    def _to_rmf_robot_state(self, state: dict[str, Any]) -> Any:
        """Translate our JSON wire shape to
        rmf_adapter.easy_full_control.RobotState.

        The rmf_adapter Python binding exposes RobotState under
        `easy_full_control` (not as a top-level attribute) and accepts
        the pose as a 3-element numpy array (not a `Vector3d` object).
        Both imports are lazy because rmf_adapter isn't available on
        ros:jazzy-ros-base (CI) — only on deployments with rmf_ros2.
        Tests that don't bind never reach this path.
        """
        try:
            from rmf_adapter.easy_full_control import RobotState  # type: ignore[import-untyped]
            import numpy as np
        except ImportError:
            log.error(
                "rmf_adapter or numpy not importable — cannot translate state for "
                "robot=%s. Adapter binary requires the rmf_ros2 stack at runtime.",
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

        return RobotState(level_name, np.array([x, y, yaw]), battery_soc)
