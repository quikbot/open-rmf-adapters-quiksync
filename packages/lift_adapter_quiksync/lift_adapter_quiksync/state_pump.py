"""Lift state pump — drains the QuikSync `/lifts/{lift}/state/subscribe`
WSS stream for a single lift and dispatches each LiftState frame to the
supplied async callback.

One pump per lift. The adapter owns N pumps for its N lifts and keeps
them in a flat list — there's no fan-out logic in the pump itself
because each WSS subscription is already scoped to a single lift.

The session-manager TTL refresh discipline (refresh on every state-push
frame) is implemented in the lift handle / session manager layer, not
here. This module's only job is "frame in → callback called."

Lifecycle: `start()` spawns an asyncio task; `stop()` cancels it. Safe
to start/stop multiple times.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from quiksync_client import QuikSyncWsClient

log = logging.getLogger("lift_adapter_quiksync.state_pump")

LiftStateCallback = Callable[[str, dict], Awaitable[None]]
"""(lift_name, lift_state_dict) → awaitable. Adapter wires this to its
ROS publisher for the matching `rmf_lift_msgs/LiftState` topic, plus
the session manager's TTL refresh."""


class LiftStatePump:
    """Stream LiftState frames for one lift + dispatch each to a callback."""

    def __init__(
        self,
        ws_client: QuikSyncWsClient,
        lift_name: str,
        on_state: LiftStateCallback,
    ) -> None:
        self._ws = ws_client
        self._lift = lift_name
        self._on_state = on_state
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._frames_seen = 0
        self._frames_dispatched = 0

    def frames_seen(self) -> int:
        """Total LiftState frames received (testing helper)."""
        return self._frames_seen

    def frames_dispatched(self) -> int:
        """Total frames that the callback handled without raising
        (testing helper)."""
        return self._frames_dispatched

    async def start(self) -> None:
        """Spawn the pump task. Returns immediately."""
        if self._task is not None and not self._task.done():
            log.debug("LiftStatePump already running for lift=%s", self._lift)
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run(), name=f"lift-state-pump:{self._lift}")
        log.info("LiftStatePump started for lift=%s", self._lift)

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
        log.info("LiftStatePump stopped for lift=%s", self._lift)

    async def _run(self) -> None:
        try:
            async for frame in self._ws.subscribe_lift_state(self._lift):
                if self._stop_requested:
                    return
                await self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # WS client surfaces WsCircuitOpen + other transport errors. We
            # log + exit rather than crash the adapter; the caller decides
            # whether to restart the pump.
            log.error("LiftStatePump terminated for lift=%s: %s", self._lift, e)

    async def _dispatch_frame(self, frame: dict) -> None:
        self._frames_seen += 1
        try:
            await self._on_state(self._lift, frame)
            self._frames_dispatched += 1
        except Exception as e:  # noqa: BLE001
            log.warning("on_state callback failed for lift=%s: %s", self._lift, e)
