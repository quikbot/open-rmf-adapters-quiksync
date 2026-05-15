"""Door state pump — drains the QuikSync `/doors/{door}/state/subscribe`
WSS stream for a single door and dispatches each DoorState frame to the
supplied async callback.

One pump per door. The adapter owns N pumps for its N doors and keeps
them in a flat list — there's no fan-out logic in the pump itself
because each WSS subscription is already scoped to a single door.

Lifecycle: `start()` spawns an asyncio task; `stop()` cancels it. Safe
to start/stop multiple times.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from quiksync_client import QuikSyncWsClient

log = logging.getLogger("door_adapter_quiksync.state_pump")

DoorStateCallback = Callable[[str, dict], Awaitable[None]]
"""(door_name, door_state_dict) → awaitable. Adapter wires this to its
ROS publisher for the matching `rmf_door_msgs/DoorState` topic."""


class DoorStatePump:
    """Stream DoorState frames for one door + dispatch each to a callback."""

    def __init__(
        self,
        ws_client: QuikSyncWsClient,
        door_name: str,
        on_state: DoorStateCallback,
        namespace: Optional[str] = None,
    ) -> None:
        self._ws = ws_client
        self._door = door_name
        self._on_state = on_state
        self._namespace = namespace
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._frames_seen = 0
        self._dispatches_ok = 0

    def frames_seen(self) -> int:
        """Total DoorState frames received (testing helper)."""
        return self._frames_seen

    def dispatches_ok(self) -> int:
        """Total successful callback invocations (testing helper).
        For door pumps, 1:1 with frames seen (one frame → one
        callback). Named consistently with the fleet + lift pumps."""
        return self._dispatches_ok

    async def start(self) -> None:
        """Spawn the pump task. Returns immediately."""
        if self._task is not None and not self._task.done():
            log.debug("DoorStatePump already running for door=%s", self._door)
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run(), name=f"door-state-pump:{self._door}")
        log.info("DoorStatePump started for door=%s", self._door)

    async def stop(self) -> None:
        """Cancel the pump task. Safe under double-stop."""
        self._stop_requested = True
        self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected — we just cancelled it. Swallow on stop().
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("DoorStatePump task exited with %s: %s", type(e).__name__, e)
            self._task = None
        log.info("DoorStatePump stopped for door=%s", self._door)

    async def _run(self) -> None:
        try:
            async for frame in self._ws.subscribe_door_state(self._door, namespace=self._namespace):
                if self._stop_requested:
                    return
                await self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # WS client surfaces WsCircuitOpen + other transport errors. We
            # log + exit rather than crash the adapter; the caller decides
            # whether to restart the pump.
            log.error("DoorStatePump terminated for door=%s: %s", self._door, e)

    async def _dispatch_frame(self, frame: dict) -> None:
        self._frames_seen += 1
        try:
            await self._on_state(self._door, frame)
            self._dispatches_ok += 1
        except Exception as e:  # noqa: BLE001
            log.warning("on_state callback failed for door=%s: %s", self._door, e)
