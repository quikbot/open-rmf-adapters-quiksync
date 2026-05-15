"""rclpy.Node wiring for the QuikSync door adapter.

One `DoorAdapterNode` owns N doors — mirrors the lift adapter's
intentional deviation from the canonical one-per-resource
`door_adapter_template` pattern. The motivation: operationally
simpler (one container per adapter binary, regardless of how many
doors a building has), and the doors share an Auth0 client, an
HTTP client, and a WSS client.

Wiring:

- One ROS publisher on `door_states_topic` (default `door_states`)
  publishes `rmf_door_msgs/DoorState` messages for every door this
  node manages, multiplexed by `door_name`.
- One ROS subscriber on `door_requests_topic` (default `door_requests`)
  receives `rmf_door_msgs/DoorRequest` messages. Each message is
  routed by `door_name` to the matching `DoorHandle.dispatch_request`.
  Messages for doors this node does not manage are dropped (the
  canonical Open-RMF pattern — every door adapter sees every request
  and filters).
- One `DoorHandle` per door — owns the WSS state pump for that door
  and the JSON↔ROS-msg-fields translation.

Threading model:

- The rclpy executor runs the subscriber callback synchronously. The
  callback calls `DoorHandle.dispatch_request`, which makes a
  synchronous HTTP POST. For door requests this is acceptable (low
  frequency, sub-second).
- The state pumps run in a background asyncio loop on a dedicated
  thread. When a pump's callback fires it builds a dict and invokes
  the handle's `publish_state_fields` callback, which constructs a
  `rmf_door_msgs/DoorState` msg and calls `publisher.publish()`.
  rclpy publishers are thread-safe.

`rclpy` and `rmf_door_msgs.msg` are lazy-imported via parameters so
this module is unit-testable without those packages in the import
path. Tests inject fakes via the constructor.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Iterable, Optional

from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient

from .door_handle import DoorHandle

log = logging.getLogger("door_adapter_quiksync.node")


class DoorAdapterNode:
    """rclpy-Node-shaped orchestrator for N doors.

    The class does NOT subclass `rclpy.node.Node` directly — instead
    it composes one (passed in at construction time). This keeps the
    module importable without rclpy on PATH and gives tests a clean
    seam for injecting a fake node.

    Lifecycle:
    1. Construct the rclpy node + publisher + subscriber externally,
       then pass them in.
    2. Call `await start()` to spawn the per-door state pump tasks.
    3. Call `route_request(msg)` from the subscriber callback to
       dispatch incoming DoorRequest messages.
    4. Call `await stop()` to cancel all pumps cleanly.
    """

    def __init__(
        self,
        *,
        door_names: Iterable[str],
        http_client: QuikSyncHttpClient,
        ws_client: QuikSyncWsClient,
        msg_module: Any,
        publish_msg: Any,
        log_warning: Optional[Any] = None,
        namespace: Optional[str] = None,
    ) -> None:
        """Build a DoorAdapterNode.

        Args:
            door_names: door IDs this node will manage.
            http_client: shared QuikSync HTTP client.
            ws_client: shared QuikSync WS client.
            msg_module: the `rmf_door_msgs.msg` module (or a duck-typed
                stand-in in tests). Provides `DoorState`, `DoorMode` —
                we construct instances from these.
            publish_msg: a callable `(rmf_door_msgs.msg.DoorState) -> None`
                wired to the rclpy publisher's `publish` method. Adapter
                builds the msg from the translated dict and calls this.
            log_warning: optional `(fmt: str, *args) -> None` for cross-
                door warnings (e.g. unknown door in incoming request).
                Defaults to the module logger.
        """
        self._msg_module = msg_module
        self._publish_msg = publish_msg
        self._warn = log_warning or log.warning
        self._handles: dict[str, DoorHandle] = {
            name: DoorHandle(
                door_name=name,
                http_client=http_client,
                ws_client=ws_client,
                publish_state_fields=self._make_publish_callback(name),
                namespace=namespace,
            )
            for name in door_names
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._started = False

    @property
    def door_names(self) -> tuple[str, ...]:
        return tuple(self._handles)

    def handle_for(self, door_name: str) -> Optional[DoorHandle]:
        """Return the handle managing `door_name`, or None if this
        node doesn't manage it."""
        return self._handles.get(door_name)

    # ----- Lifecycle (called from main thread / rclpy thread) -----

    def start(self) -> None:
        """Spawn the asyncio loop thread and start all state pumps.

        Returns once all pumps are scheduled (does not block on frames).
        """
        if self._started:
            log.debug("DoorAdapterNode already started")
            return
        ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            args=(ready,),
            name="door-adapter-asyncio-loop",
            daemon=True,
        )
        self._loop_thread.start()
        ready.wait()  # wait for the loop to be assigned + handle pumps started
        self._started = True
        log.info("DoorAdapterNode started; %d doors managed", len(self._handles))

    def stop(self) -> None:
        """Stop all state pumps + join the asyncio loop thread."""
        if not self._started or self._loop is None:
            return
        # Schedule pump stops on the loop, wait for completion, then
        # stop the loop itself.
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
        log.info("DoorAdapterNode stopped")

    def _run_loop(self, ready: threading.Event) -> None:
        """Thread target — owns an asyncio loop and runs all state pumps."""
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

    def _make_publish_callback(self, door_name: str):
        """Build a per-handle publish callback. The callback receives the
        translated state dict from `DoorHandle.translate_state` and turns
        it into a `rmf_door_msgs/DoorState` msg, then calls the wired
        publisher.

        Closure over `self._msg_module` lets us run without rclpy/
        rmf_door_msgs on import path — the actual msg classes resolve
        at first publish time, not at module load.
        """
        msg_module = self._msg_module
        publish = self._publish_msg

        def _publish(fields: dict) -> None:
            msg = build_door_state_msg(msg_module, fields)
            publish(msg)

        return _publish

    # ----- Request route (rclpy thread → handle) -----

    def route_request(self, ros_request: Any) -> bool:
        """Dispatch a `rmf_door_msgs/DoorRequest` to the matching handle.

        Called from the rclpy subscriber callback. Returns True if the
        request was dispatched (the handle accepted it and queued the
        POST); False otherwise (unknown door — typical, this node is
        one of many in a building — or translation failure).

        Unknown-door requests are NOT logged at warning level — that
        would be noisy because every door adapter receives every
        request and most are not for this node. Use debug logging in
        the future if a per-call audit is needed.
        """
        door_name = getattr(ros_request, "door_name", None)
        if door_name is None:
            self._warn("dropping DoorRequest with no door_name")
            return False
        handle = self._handles.get(door_name)
        if handle is None:
            # Not for us. Other nodes will pick it up. Silent drop.
            return False
        return handle.dispatch_request(ros_request)

    # ----- testability accessors -----

    def state_dispatched_total(self) -> int:
        return sum(h.state_dispatched() for h in self._handles.values())

    def requests_dispatched_total(self) -> int:
        return sum(h.requests_dispatched() for h in self._handles.values())

    def requests_rejected_total(self) -> int:
        return sum(h.requests_rejected() for h in self._handles.values())


def build_door_state_msg(msg_module: Any, fields: dict) -> Any:
    """Construct a `rmf_door_msgs/DoorState` from the translated dict.

    Pure function — exposed at module level so tests can drive it
    directly with a fake msg module.

    Field shape (from `DoorHandle.translate_state`):

        {
            "door_name": str,
            "door_time": {"sec": int, "nanosec": int},
            "current_mode": {"value": int},  # rmf_door_msgs/DoorMode
        }

    The `msg_module` is the `rmf_door_msgs.msg` namespace; in real
    rclpy it provides `DoorState`, `DoorMode`, and indirectly `Time`
    via the `builtin_interfaces.msg.Time` constructor. We import
    `Time` lazily from there if needed; otherwise rmf_door_msgs's
    `DoorState.door_time` is already typed as a Time and is
    constructible inline.
    """
    door_time = msg_module.Time(
        sec=fields["door_time"]["sec"],
        nanosec=fields["door_time"]["nanosec"],
    )
    mode = msg_module.DoorMode(value=fields["current_mode"]["value"])
    return msg_module.DoorState(
        door_name=fields["door_name"],
        door_time=door_time,
        current_mode=mode,
    )
