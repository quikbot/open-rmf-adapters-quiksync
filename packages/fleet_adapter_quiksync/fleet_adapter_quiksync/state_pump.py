"""Fleet state pump — drains the QuikSync `/state/subscribe` WSS stream
and dispatches each frame to a per-robot consumer.

Per design §11.1: each WSS frame is the full FleetState shape; the pump
splits per-robot and forwards to a registered async callback. The
callback is what the adapter wires to Open-RMF's `EasyRobotUpdateHandle.update`
(in adapter.py — not exercised in CI since rmf_adapter isn't in
ros:jazzy-ros-base).

Lifecycle: `start()` spawns an asyncio task; `stop()` cancels it. Safe
to start/stop multiple times.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from quiksync_client import QuikSyncWsClient

log = logging.getLogger("fleet_adapter_quiksync.state_pump")

RobotStateCallback = Callable[[str, dict], Awaitable[None]]
"""(robot_name, robot_state_dict) → awaitable. Adapter wires this to
EasyRobotUpdateHandle.update(state, current_activity)."""


class FleetStatePump:
    """Stream FleetState frames + dispatch per-robot to the supplied callback."""

    def __init__(
        self,
        ws_client: QuikSyncWsClient,
        fleet_name: str,
        on_robot_state: RobotStateCallback,
    ) -> None:
        self._ws = ws_client
        self._fleet = fleet_name
        self._on_robot_state = on_robot_state
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._frames_seen = 0
        self._robots_dispatched = 0

    def frames_seen(self) -> int:
        """Total FleetState frames received (testing helper)."""
        return self._frames_seen

    def robots_dispatched(self) -> int:
        """Total per-robot dispatches (testing helper). One frame with
        N robots → N dispatches."""
        return self._robots_dispatched

    async def start(self) -> None:
        """Spawn the pump task. Returns immediately."""
        if self._task is not None and not self._task.done():
            log.debug("FleetStatePump already running for fleet=%s", self._fleet)
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run(), name=f"state-pump:{self._fleet}")
        log.info("FleetStatePump started for fleet=%s", self._fleet)

    async def stop(self) -> None:
        """Cancel the pump task. Safe under double-stop."""
        self._stop_requested = True
        self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("FleetStatePump stopped for fleet=%s", self._fleet)

    async def _run(self) -> None:
        try:
            async for frame in self._ws.subscribe_fleet_state(self._fleet):
                if self._stop_requested:
                    return
                await self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # WS client surfaces WsCircuitOpen + other transport errors. We
            # log + exit rather than crash the adapter; the caller decides
            # whether to restart the pump.
            log.error("FleetStatePump terminated for fleet=%s: %s", self._fleet, e)

    async def _dispatch_frame(self, frame: dict) -> None:
        self._frames_seen += 1
        robots = frame.get("robots")
        # The Open-RMF FleetState schema specifies `robots` as a
        # `{robotName → RobotState}` map. We also accept the array shape
        # (`[RobotState, ...]`) for resilience against server variants
        # that emit the older shape.
        if isinstance(robots, dict):
            robot_iter = list(robots.values())
        elif isinstance(robots, list):
            robot_iter = robots
        elif robots is None:
            robot_iter = []
        else:
            log.warning(
                "FleetState frame.robots is neither map nor list (got %s); skipping",
                type(robots).__name__,
            )
            return
        for robot in robot_iter:
            if not isinstance(robot, dict):
                continue
            name = robot.get("name")
            if not name:
                continue
            try:
                await self._on_robot_state(name, robot)
                self._robots_dispatched += 1
            except Exception as e:  # noqa: BLE001
                log.warning("on_robot_state callback failed for robot=%s: %s", name, e)
