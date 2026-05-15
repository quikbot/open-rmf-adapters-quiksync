"""Per-door orchestrator — wires the WSS state pump and ROS request
subscriber to the QuikSync REST + WSS surfaces.

One `DoorHandle` instance per door. The adapter's rclpy node owns N
handles (one per ID in the `doors:` config), one shared
`QuikSyncHttpClient` and `QuikSyncWsClient`, and one ROS publisher per
the configured `door_states_topic`.

The handle keeps all JSON ↔ ROS-msg-field translation in this module
so it's unit-testable without `rclpy` in the import path. The rclpy
node layer (still to be added) is responsible only for:

- Constructing a `rmf_door_msgs/DoorState` from the dict returned by
  `translate_state(...)` and publishing it
- Calling `dispatch_request(...)` with each `rmf_door_msgs/DoorRequest`
  message received on the request topic

The handle never touches ROS msg types directly. State-translation
inputs / outputs are plain dicts; request-translation accepts a
duck-typed object with the same attributes as the ROS message.

Wire-shape references:
- WSS state frame: `{door_name, door_time, current_mode: {value}}` —
  `door_time` is unix epoch ms (see `quiksync_client.millis_to_time_parts`).
- REST request body: `{requester_id, requested_mode, execution_id}` —
  `requested_mode` is `"OPEN"` or `"CLOSED"`; `MOVING` is rejected
  upstream with 400.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient, millis_to_time_parts

from .state_pump import DoorStatePump

log = logging.getLogger("door_adapter_quiksync.door_handle")


@dataclass(frozen=True)
class _TranslatedDoorRequest:
    """Internal carrier for the fields of a translated DoorRequest."""

    door_name: str
    requester_id: str
    requested_mode: str  # "OPEN" or "CLOSED"
    execution_id: str


# rmf_door_msgs/DoorMode constants — re-stated here so the handle can
# translate without importing rmf_door_msgs (kept ROS-import-free for
# pure-Python unit tests).
_MODE_CLOSED = 0
_MODE_MOVING = 1
_MODE_OPEN = 2


PublishStateFields = Callable[[dict], None]
"""Sync callable — the adapter wires this to: build
`rmf_door_msgs/DoorState` from the dict and call
`publisher.publish(msg)`. The dict has the shape:
`{door_name: str, door_time: {sec: int, nanosec: int}, current_mode: {value: int}}`.

The handle invokes this synchronously from its async state-pump
callback. `async def` callbacks will not be awaited — the coroutine
object is discarded and Python emits a "coroutine was never awaited"
warning. Wrap any async work in a fire-and-forget `asyncio.create_task`
inside a sync wrapper if you need it."""


class RequestTranslationError(Exception):
    """The ROS DoorRequest could not be translated to a wire request."""


class DoorHandle:
    """Owns one door's state-pump and request-dispatch path."""

    def __init__(
        self,
        door_name: str,
        http_client: QuikSyncHttpClient,
        ws_client: QuikSyncWsClient,
        publish_state_fields: PublishStateFields,
        namespace: Optional[str] = None,
    ) -> None:
        self._door_name = door_name
        self._http = http_client
        self._ws = ws_client
        self._publish_state_fields = publish_state_fields
        self._namespace = namespace
        self._pump = DoorStatePump(ws_client, door_name, self._on_state_frame, namespace=namespace)
        # Counters below are touched from disjoint contexts and so don't
        # need a lock:
        # - `_state_dispatched` only from the async state-pump task
        # - `_requests_dispatched` / `_requests_rejected` only from
        #   `dispatch_request` (typically called on the rclpy subscriber
        #   thread by the future node layer).
        self._state_dispatched = 0
        self._requests_dispatched = 0
        self._requests_rejected = 0

    @property
    def door_name(self) -> str:
        return self._door_name

    def state_dispatched(self) -> int:
        """Total state frames forwarded to the publisher (testing helper)."""
        return self._state_dispatched

    def requests_dispatched(self) -> int:
        """Total POSTs successfully dispatched (testing helper)."""
        return self._requests_dispatched

    def requests_rejected(self) -> int:
        """Total ROS requests rejected by translation (testing helper)."""
        return self._requests_rejected

    # ----- Lifecycle -----

    async def start(self) -> None:
        """Begin draining the WSS state stream for this door."""
        await self._pump.start()

    async def stop(self) -> None:
        await self._pump.stop()

    # ----- State (inbound: WSS → ROS publish) -----

    async def _on_state_frame(self, door_name: str, frame: dict) -> None:
        """State-pump callback — translate the JSON frame, hand off to
        the ROS publisher."""
        try:
            fields = self.translate_state(frame)
        except Exception as e:  # noqa: BLE001
            log.warning("state translation failed for door=%s: %s", door_name, e)
            return
        try:
            self._publish_state_fields(fields)
            self._state_dispatched += 1
        except Exception as e:  # noqa: BLE001
            log.warning("state publish failed for door=%s: %s", door_name, e)

    def translate_state(self, frame: dict) -> dict:
        """Translate a `/doors/<door>/state` JSON frame into a dict of
        `rmf_door_msgs/DoorState` field values.

        Returns:
            `{"door_name": str, "door_time": {"sec": int, "nanosec": int},
              "current_mode": {"value": int}}`

        Raises (caller catches via `_on_state_frame`'s try/except):
            `KeyError` if the frame is missing a required field.
            `TypeError` if `door_time` is not an int (e.g. a malformed
                server payload). The bool guard in
                `millis_to_time_parts` also surfaces here.
            `ValueError` if `current_mode.value` is not int-coercible.
        """
        door_time_ms = frame["door_time"]
        sec, nanosec = millis_to_time_parts(door_time_ms)
        mode_value = int(frame["current_mode"]["value"])
        return {
            "door_name": frame["door_name"],
            "door_time": {"sec": sec, "nanosec": nanosec},
            "current_mode": {"value": mode_value},
        }

    # ----- Request (outbound: ROS subscribe → REST POST) -----

    def dispatch_request(
        self,
        ros_request: Any,
        *,
        execution_id: Optional[str] = None,
    ) -> bool:
        """Receive a `rmf_door_msgs/DoorRequest`-shaped object, translate
        to a POST body, send via the http client.

        `ros_request` is duck-typed — any object with `door_name`,
        `requester_id`, `requested_mode.value` attributes works.

        Returns `True` if the POST was dispatched, `False` if the
        request was rejected (translation failure or door-name
        mismatch — handle owns one door, not all of them).

        Translation rejections do NOT raise — they're logged and counted.
        Transport failures from the http client DO propagate, so the
        caller can decide retry policy.
        """
        try:
            translated = self._translate_request(ros_request, execution_id=execution_id)
        except RequestTranslationError as e:
            log.warning(
                "rejecting DoorRequest for door=%s: %s", self._door_name, e
            )
            self._requests_rejected += 1
            return False

        # Translation gives us the door_name from the ROS msg. Reject
        # cross-door requests so a misrouted msg can't drive the wrong
        # door — defense in depth, the rclpy subscriber will already
        # filter on door_name in the canonical pattern.
        if translated.door_name != self._door_name:
            log.warning(
                "DoorRequest for door=%s arrived at handle owning door=%s; rejecting",
                translated.door_name,
                self._door_name,
            )
            self._requests_rejected += 1
            return False

        self._http.post_door_request(
            door=self._door_name,
            requester_id=translated.requester_id,
            requested_mode=translated.requested_mode,
            execution_id=translated.execution_id,
            namespace=self._namespace,
        )
        # Mirrors the fleet adapter's "<callback>(...) dispatched:" log
        # signature so operators have a single observable signal across
        # all three adapters' success paths.
        log.info(
            "DoorRequest dispatched: door=%s mode=%s execution_id=%s",
            self._door_name, translated.requested_mode, translated.execution_id,
        )
        self._requests_dispatched += 1
        return True

    def _translate_request(
        self,
        ros_request: Any,
        *,
        execution_id: Optional[str] = None,
    ) -> _TranslatedDoorRequest:
        """Translate a `rmf_door_msgs/DoorRequest`-shaped object into the
        wire fields for `post_door_request`."""
        door_name = _require_str(getattr(ros_request, "door_name", None), "door_name")
        requester_id = _require_str(
            getattr(ros_request, "requester_id", None), "requester_id"
        )

        mode = getattr(ros_request, "requested_mode", None)
        if mode is None:
            raise RequestTranslationError("requested_mode is missing")
        mode_value = getattr(mode, "value", None)
        if mode_value is None:
            raise RequestTranslationError("requested_mode.value is missing")

        if mode_value == _MODE_OPEN:
            wire_mode = "OPEN"
        elif mode_value == _MODE_CLOSED:
            wire_mode = "CLOSED"
        elif mode_value == _MODE_MOVING:
            raise RequestTranslationError(
                "MODE_MOVING is not a valid goal; QuikSync rejects with 400"
            )
        else:
            raise RequestTranslationError(
                f"requested_mode.value={mode_value!r} is not a known DoorMode "
                f"(expected 0=CLOSED, 2=OPEN)"
            )

        return _TranslatedDoorRequest(
            door_name=door_name,
            requester_id=requester_id,
            requested_mode=wire_mode,
            execution_id=execution_id or str(uuid.uuid4()),
        )


def _require_str(value: Any, field_name: str) -> str:
    if value is None:
        raise RequestTranslationError(f"{field_name} is missing")
    s = str(value)
    if not s:
        raise RequestTranslationError(f"{field_name} must be non-empty")
    return s
