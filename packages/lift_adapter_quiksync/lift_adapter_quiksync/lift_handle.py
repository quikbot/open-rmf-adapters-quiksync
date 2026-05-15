"""Per-lift orchestrator — wires the WSS state pump and ROS request
subscriber to the QuikSync REST + WSS surfaces.

One `LiftHandle` instance per lift. The adapter's rclpy node owns N
handles, plus one shared `LiftSessionManager` covering all lifts.

The handle keeps all JSON ↔ ROS-msg-field translation in this module
so it's unit-testable without `rclpy` in the import path. The rclpy
node layer (separate module) is responsible only for:

- Constructing a `rmf_lift_msgs/LiftState` from the dict returned by
  `translate_state(...)` and publishing it
- Calling `dispatch_request(...)` with each `rmf_lift_msgs/LiftRequest`
  message received on the request topic

The handle never touches ROS msg types directly. State-translation
inputs / outputs are plain dicts; request-translation accepts a
duck-typed object with the same attributes as the ROS message.

Wire-shape references:
- WSS state frame:
  ```
  {
    "lift_name": str, "lift_time": <unix ms>,
    "current_floor": str, "destination_floor": str,
    "door_state": int (0|1|2), "motion_state": int (0|1|2|3),
    "available_modes": [{"value": int}, ...],
    "current_mode": {"value": int},
    "session_id": str (may be empty)
  }
  ```
- REST request body:
  ```
  {
    "session_id": str, "request_type": "END_SESSION"|"AGV_MODE"|"HUMAN_MODE",
    "destination_floor": str, "door_state": "OPEN"|"CLOSED",
    "execution_id": <uuid>
  }
  ```
  NO_REQUEST is the rmf-side no-op sentinel; the adapter skips the
  POST entirely. MOVING door_state is rejected upstream with 400.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient, millis_to_time_parts

from .session_manager import LiftSessionManager
from .state_pump import LiftStatePump

log = logging.getLogger("lift_adapter_quiksync.lift_handle")


# ----- ROS msg constants (re-stated to avoid rmf_lift_msgs import) -----

# rmf_lift_msgs/LiftRequest.request_type
_REQUEST_NO_REQUEST = 0
_REQUEST_END_SESSION = 1
_REQUEST_AGV_MODE = 2
_REQUEST_HUMAN_MODE = 3

# rmf_lift_msgs DoorState (LiftRequest.door_state + LiftState.door_state)
_DOOR_CLOSED = 0
_DOOR_MOVING = 1
_DOOR_OPEN = 2


@dataclass(frozen=True)
class _TranslatedLiftRequest:
    """Internal carrier for the fields of a translated LiftRequest.

    `request_type == "NO_REQUEST"` is never carried — the caller short-
    circuits before constructing this.
    """

    lift_name: str
    session_id: str
    request_type: str
    destination_floor: str
    door_state: str  # "OPEN" or "CLOSED"
    execution_id: str


PublishStateFields = Callable[[dict], None]
"""Sync callable — the adapter wires this to: build
`rmf_lift_msgs/LiftState` from the dict and call
`publisher.publish(msg)`. The dict shape is:

    {
        "lift_name": str,
        "lift_time": {"sec": int, "nanosec": int},
        "current_floor": str,
        "destination_floor": str,
        "door_state": int,
        "motion_state": int,
        "available_modes": list[int],   # spread into msg.available_modes
        "current_mode": int,
        "session_id": str,
    }

The handle invokes this synchronously from its async state-pump
callback; same threading caveat as the door handle (no async
callbacks)."""


class RequestTranslationError(Exception):
    """The ROS LiftRequest could not be translated to a wire request."""


class LiftHandle:
    """Owns one lift's state-pump and request-dispatch path."""

    def __init__(
        self,
        lift_name: str,
        http_client: QuikSyncHttpClient,
        ws_client: QuikSyncWsClient,
        publish_state_fields: PublishStateFields,
        session_manager: LiftSessionManager,
        namespace: Optional[str] = None,
    ) -> None:
        self._lift_name = lift_name
        self._http = http_client
        self._ws = ws_client
        self._publish_state_fields = publish_state_fields
        self._session_manager = session_manager
        self._namespace = namespace
        self._pump = LiftStatePump(ws_client, lift_name, self._on_state_frame, namespace=namespace)
        # Counters touched from disjoint contexts; no lock needed.
        self._state_dispatched = 0
        self._requests_dispatched = 0
        self._requests_rejected = 0

    @property
    def lift_name(self) -> str:
        return self._lift_name

    def state_dispatched(self) -> int:
        return self._state_dispatched

    def requests_dispatched(self) -> int:
        return self._requests_dispatched

    def requests_rejected(self) -> int:
        return self._requests_rejected

    # ----- Lifecycle -----

    async def start(self) -> None:
        await self._pump.start()

    async def stop(self) -> None:
        await self._pump.stop()

    # ----- State (inbound: WSS → ROS publish + session-manager sync) -----

    async def _on_state_frame(self, lift_name: str, frame: dict) -> None:
        """State-pump callback — translate the JSON frame, sync the
        session manager, hand off to the ROS publisher."""
        try:
            fields = self.translate_state(frame)
        except Exception as e:  # noqa: BLE001
            log.warning("state translation failed for lift=%s: %s", lift_name, e)
            return
        # Sync the session manager BEFORE publishing — if we just lost
        # the lift to another session, our internal state should
        # reflect that before any subsequent request races in.
        self._session_manager.observe_server_state(lift_name, fields["session_id"])
        try:
            self._publish_state_fields(fields)
            self._state_dispatched += 1
        except Exception as e:  # noqa: BLE001
            log.warning("state publish failed for lift=%s: %s", lift_name, e)

    def translate_state(self, frame: dict) -> dict:
        """Translate a `/lifts/<lift>/state` JSON frame into a dict of
        `rmf_lift_msgs/LiftState` field values.

        Returns:
            ```
            {
                "lift_name": str,
                "lift_time": {"sec": int, "nanosec": int},
                "current_floor": str,
                "destination_floor": str,
                "door_state": int,        # uint8 — flat from wire
                "motion_state": int,      # uint8
                "available_modes": list[int],
                "current_mode": int,
                "session_id": str,
            }
            ```

        Raises (caller catches via `_on_state_frame`'s try/except):
            `KeyError` if the frame is missing a required field.
            `TypeError` if `lift_time` is not an int (e.g. malformed
                server payload). The bool guard in
                `millis_to_time_parts` also surfaces here.
            `ValueError` if any uint8 field isn't int-coercible.
        """
        lift_time_ms = frame["lift_time"]
        sec, nanosec = millis_to_time_parts(lift_time_ms)
        # available_modes is a list of {"value": int} on the wire;
        # rmf_lift_msgs flattens it to uint8[].
        modes_raw = frame.get("available_modes") or []
        modes_flat = [int(m["value"]) for m in modes_raw]
        return {
            "lift_name": frame["lift_name"],
            "lift_time": {"sec": sec, "nanosec": nanosec},
            "current_floor": frame.get("current_floor", "") or "",
            "destination_floor": frame.get("destination_floor", "") or "",
            "door_state": int(frame.get("door_state", 0)),
            "motion_state": int(frame.get("motion_state", 0)),
            "available_modes": modes_flat,
            "current_mode": int(frame["current_mode"]["value"]),
            "session_id": frame.get("session_id", "") or "",
        }

    # ----- Request (outbound: ROS subscribe → REST POST) -----

    def dispatch_request(
        self,
        ros_request: Any,
        *,
        execution_id: Optional[str] = None,
    ) -> bool:
        """Receive a `rmf_lift_msgs/LiftRequest`-shaped object, translate
        to a POST body, send via the http client.

        `ros_request` is duck-typed — any object with `lift_name`,
        `session_id`, `request_type`, `destination_floor`,
        `door_state` attributes works.

        Returns:
            True  — request was forwarded to the server
            False — request was rejected locally (NO_REQUEST sentinel,
                    translation failure, cross-lift mis-route, or
                    adapter-side session-occupant conflict)

        Translation rejections + session-conflict rejections do NOT
        raise — they log + count. Transport failures from the http
        client DO propagate.
        """
        # NO_REQUEST short-circuit before any other work.
        request_type_raw = getattr(ros_request, "request_type", None)
        if request_type_raw is not None:
            try:
                request_type_int = _as_int("request_type", request_type_raw)
            except RequestTranslationError as e:
                log.warning("rejecting LiftRequest for lift=%s: %s", self._lift_name, e)
                self._requests_rejected += 1
                return False
            if request_type_int == _REQUEST_NO_REQUEST:
                # rmf-side no-op sentinel; drop silently (no count bump
                # — this is the expected steady-state when no request
                # is in flight).
                return False

        try:
            translated = self._translate_request(ros_request, execution_id=execution_id)
        except RequestTranslationError as e:
            log.warning("rejecting LiftRequest for lift=%s: %s", self._lift_name, e)
            self._requests_rejected += 1
            return False

        # Cross-lift defense-in-depth (handle owns one lift, not all).
        if translated.lift_name != self._lift_name:
            log.warning(
                "LiftRequest for lift=%s arrived at handle owning lift=%s; rejecting",
                translated.lift_name, self._lift_name,
            )
            self._requests_rejected += 1
            return False

        # Session-manager gate — short-circuit before the POST when we
        # already know the lift is held by someone else.
        if translated.request_type == "AGV_MODE":
            acquired = self._session_manager.try_acquire(
                self._lift_name, translated.session_id,
            )
            if not acquired:
                log.info(
                    "LiftRequest AGV_MODE for lift=%s session=%s rejected by "
                    "adapter-side session lock",
                    self._lift_name, translated.session_id,
                )
                self._requests_rejected += 1
                return False

        self._http.post_lift_request(
            lift=self._lift_name,
            session_id=translated.session_id,
            request_type=translated.request_type,
            destination_floor=translated.destination_floor,
            door_state=translated.door_state,
            execution_id=translated.execution_id,
            namespace=self._namespace,
        )

        # On END_SESSION + HUMAN_MODE, release adapter-side after the
        # POST returns (the server-side release is async; our local
        # state catches up via observe_server_state on the next frame,
        # but we also clear our own request view eagerly).
        if translated.request_type in ("END_SESSION", "HUMAN_MODE"):
            self._session_manager.release(self._lift_name, translated.session_id)

        # Mirrors the fleet adapter's "<callback>(...) dispatched:" log
        # signature so operators have a single observable signal across
        # all three adapters' success paths.
        log.info(
            "LiftRequest dispatched: lift=%s request_type=%s session=%s execution_id=%s",
            self._lift_name, translated.request_type,
            translated.session_id, translated.execution_id,
        )
        self._requests_dispatched += 1
        return True

    def _translate_request(
        self,
        ros_request: Any,
        *,
        execution_id: Optional[str] = None,
    ) -> _TranslatedLiftRequest:
        """Translate a `rmf_lift_msgs/LiftRequest`-shaped object into the
        wire fields for `post_lift_request`.

        Assumes the caller has already short-circuited NO_REQUEST.
        """
        lift_name = _require_str(getattr(ros_request, "lift_name", None), "lift_name")
        session_id = _require_str(
            getattr(ros_request, "session_id", None), "session_id"
        )
        destination_floor = _require_str(
            getattr(ros_request, "destination_floor", None), "destination_floor"
        )

        request_type_raw = getattr(ros_request, "request_type", None)
        request_type_int = _as_int("request_type", request_type_raw)
        if request_type_int == _REQUEST_END_SESSION:
            wire_request_type = "END_SESSION"
        elif request_type_int == _REQUEST_AGV_MODE:
            wire_request_type = "AGV_MODE"
        elif request_type_int == _REQUEST_HUMAN_MODE:
            wire_request_type = "HUMAN_MODE"
        else:
            raise RequestTranslationError(
                f"request_type={request_type_int!r} is not a known constant "
                f"(expected 1=END_SESSION, 2=AGV_MODE, 3=HUMAN_MODE)"
            )

        door_state_raw = getattr(ros_request, "door_state", None)
        if door_state_raw is None:
            raise RequestTranslationError("door_state is missing")
        door_state_value = getattr(door_state_raw, "value", door_state_raw)
        door_state_int = _as_int("door_state", door_state_value)
        if door_state_int == _DOOR_OPEN:
            wire_door_state = "OPEN"
        elif door_state_int == _DOOR_CLOSED:
            wire_door_state = "CLOSED"
        elif door_state_int == _DOOR_MOVING:
            raise RequestTranslationError(
                "door_state=MOVING is not a valid goal; QuikSync rejects with 400"
            )
        else:
            raise RequestTranslationError(
                f"door_state={door_state_int!r} is not a known constant "
                f"(expected 0=CLOSED, 2=OPEN)"
            )

        return _TranslatedLiftRequest(
            lift_name=lift_name,
            session_id=session_id,
            request_type=wire_request_type,
            destination_floor=destination_floor,
            door_state=wire_door_state,
            execution_id=execution_id or str(uuid.uuid4()),
        )


def _require_str(value: Any, field_name: str) -> str:
    if value is None:
        raise RequestTranslationError(f"{field_name} is missing")
    s = str(value)
    if not s:
        raise RequestTranslationError(f"{field_name} must be non-empty")
    return s


def _as_int(field_name: str, raw: Any) -> int:
    """Coerce numeric-ish inputs (numpy.uint8, str, bool) to a plain
    int with a clear error message on failure."""
    if isinstance(raw, bool):
        # bool is an int subclass; True would silently coerce to 1.
        raise RequestTranslationError(f"{field_name} cannot be bool")
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise RequestTranslationError(
            f"{field_name}={raw!r} could not be coerced to int"
        ) from e
