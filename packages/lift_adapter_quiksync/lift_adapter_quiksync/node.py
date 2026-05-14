"""rclpy.Node wiring for the QuikSync lift adapter.

One `LiftAdapterNode` owns N lifts — same one-per-many pattern as the
door adapter (deliberate deviation from canonical
`lift_adapter_template`'s one-per-lift). Shared Auth0 client, HTTP
client, WSS client, and one `LiftSessionManager` across all lifts.

Wiring:

- One ROS publisher on `lift_states_topic` (default `lift_states`)
  publishes `rmf_lift_msgs/LiftState` messages for every lift this
  node manages, multiplexed by `lift_name`.
- One ROS subscriber on `lift_requests_topic` (default `lift_requests`)
  receives `rmf_lift_msgs/LiftRequest` messages. Each is routed by
  `lift_name` to the matching `LiftHandle.dispatch_request`. Messages
  for unmanaged lifts are silently dropped (canonical pattern).
- One `LiftHandle` per lift + one shared `LiftSessionManager`.

Threading model:
- Subscriber callback runs sync on the rclpy executor thread; calls
  `LiftHandle.dispatch_request` which makes a sync HTTP POST.
- State pumps run in a background asyncio loop on a dedicated thread.
- Frame callbacks invoke `publish_state_fields` from the asyncio
  thread; rclpy publishers are thread-safe.

`rclpy` and `rmf_lift_msgs.msg` are lazy-imported via parameters so
this module is unit-testable without those packages on PATH.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Iterable, Optional

from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient

from .lift_handle import LiftHandle
from .session_manager import LiftSessionManager

log = logging.getLogger("lift_adapter_quiksync.node")


class LiftAdapterNode:
    """rclpy-Node-shaped orchestrator for N lifts.

    Composes a rclpy.Node passed in at construction (doesn't subclass);
    keeps the module importable without rclpy on PATH and gives tests
    a clean seam for injecting fakes.

    Lifecycle:
    1. Construct the rclpy node + publisher + subscriber externally,
       then pass them in.
    2. Call `start()` to spawn the asyncio loop thread + per-lift pumps.
    3. Call `route_request(msg)` from the subscriber callback.
    4. Call `stop()` to cancel all pumps cleanly.
    """

    def __init__(
        self,
        *,
        lift_names: Iterable[str],
        http_client: QuikSyncHttpClient,
        ws_client: QuikSyncWsClient,
        msg_module: Any,
        publish_msg: Any,
        log_warning: Optional[Any] = None,
        session_manager: Optional[LiftSessionManager] = None,
    ) -> None:
        """Build a LiftAdapterNode.

        Args:
            lift_names: lift IDs this node will manage.
            http_client: shared QuikSync HTTP client.
            ws_client: shared QuikSync WS client.
            msg_module: the `rmf_lift_msgs.msg` namespace (or duck-typed
                stand-in in tests). Provides `LiftState` + `Time`.
            publish_msg: `(rmf_lift_msgs.msg.LiftState) -> None` wired
                to the rclpy publisher's `publish` method.
            log_warning: optional `(fmt: str, *args) -> None`.
            session_manager: optional override (tests). Defaults to a
                fresh `LiftSessionManager`.
        """
        self._msg_module = msg_module
        self._publish_msg = publish_msg
        self._warn = log_warning or log.warning
        self._session_manager = session_manager or LiftSessionManager()
        self._handles: dict[str, LiftHandle] = {
            name: LiftHandle(
                lift_name=name,
                http_client=http_client,
                ws_client=ws_client,
                publish_state_fields=self._make_publish_callback(name),
                session_manager=self._session_manager,
            )
            for name in lift_names
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._started = False

    @property
    def lift_names(self) -> tuple[str, ...]:
        return tuple(self._handles)

    @property
    def session_manager(self) -> LiftSessionManager:
        return self._session_manager

    def handle_for(self, lift_name: str) -> Optional[LiftHandle]:
        return self._handles.get(lift_name)

    # ----- Lifecycle -----

    def start(self) -> None:
        if self._started:
            log.debug("LiftAdapterNode already started")
            return
        ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            args=(ready,),
            name="lift-adapter-asyncio-loop",
            daemon=True,
        )
        self._loop_thread.start()
        ready.wait()
        self._started = True
        log.info("LiftAdapterNode started; %d lifts managed", len(self._handles))

    def stop(self) -> None:
        if not self._started or self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_all_pumps(), self._loop)
        try:
            future.result(timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("stop_all_pumps failed: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None
        self._started = False
        log.info("LiftAdapterNode stopped")

    def _run_loop(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._start_all_pumps())
        finally:
            ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _start_all_pumps(self) -> None:
        for handle in self._handles.values():
            await handle.start()

    async def _stop_all_pumps(self) -> None:
        for handle in self._handles.values():
            await handle.stop()

    # ----- State publish (asyncio thread → rclpy publisher) -----

    def _make_publish_callback(self, lift_name: str):
        msg_module = self._msg_module
        publish = self._publish_msg

        def _publish(fields: dict) -> None:
            msg = build_lift_state_msg(msg_module, fields)
            publish(msg)

        return _publish

    # ----- Request route (rclpy thread → handle) -----

    def route_request(self, ros_request: Any) -> bool:
        """Dispatch a `rmf_lift_msgs/LiftRequest` to the matching handle.

        Returns True if the request was dispatched, False on:
        - unknown lift (this node doesn't manage it — silent drop)
        - NO_REQUEST sentinel (silent drop, expected steady-state)
        - translation failure / session-conflict reject
        """
        lift_name = getattr(ros_request, "lift_name", None)
        if lift_name is None:
            self._warn("dropping LiftRequest with no lift_name")
            return False
        handle = self._handles.get(lift_name)
        if handle is None:
            # Not for us. Silent drop.
            return False
        return handle.dispatch_request(ros_request)

    # ----- testability accessors -----

    def state_dispatched_total(self) -> int:
        return sum(h.state_dispatched() for h in self._handles.values())

    def requests_dispatched_total(self) -> int:
        return sum(h.requests_dispatched() for h in self._handles.values())

    def requests_rejected_total(self) -> int:
        return sum(h.requests_rejected() for h in self._handles.values())


def build_lift_state_msg(msg_module: Any, fields: dict) -> Any:
    """Construct a `rmf_lift_msgs/LiftState` from the translated dict.

    Pure function exposed at module level for testability.

    The msg_module is the `rmf_lift_msgs.msg` namespace; provides
    `LiftState` + (indirectly) `builtin_interfaces.msg.Time`.

    Field shape from `LiftHandle.translate_state`:

        {
            "lift_name": str,
            "lift_time": {"sec": int, "nanosec": int},
            "current_floor": str, "destination_floor": str,
            "door_state": int, "motion_state": int,
            "available_modes": list[int],
            "current_mode": int,
            "session_id": str,
        }

    rmf_lift_msgs/LiftState fields:
        string lift_name
        builtin_interfaces/Time lift_time
        string[] available_floors  # not in our state — left empty
        string current_floor
        string destination_floor
        uint8 door_state
        uint8 motion_state
        uint8[] available_modes
        uint8 current_mode
        string session_id
    """
    lift_time = msg_module.Time(
        sec=fields["lift_time"]["sec"],
        nanosec=fields["lift_time"]["nanosec"],
    )
    return msg_module.LiftState(
        lift_name=fields["lift_name"],
        lift_time=lift_time,
        # available_floors is static per-lift; populated server-side via
        # building_map / discovery, not in steady-state frames. Leave
        # empty here; consumers should read from the building map.
        available_floors=[],
        current_floor=fields["current_floor"],
        destination_floor=fields["destination_floor"],
        door_state=fields["door_state"],
        motion_state=fields["motion_state"],
        available_modes=list(fields["available_modes"]),
        current_mode=fields["current_mode"],
        session_id=fields["session_id"],
    )
