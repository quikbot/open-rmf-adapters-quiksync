"""Translate ROS `rmf_lift_msgs/LiftRequest` into the QuikSync
`POST /lifts/{lift}/request` body dict.

Pure function — accepts duck-typed inputs with the same attributes as
the ROS message so the module can be unit-tested without rclpy in the
classpath. The adapter wires this between the ROS subscription and the
HTTP client.

Wire mapping (per the QuikSync adapter API contract):

| ROS LiftRequest field        | POST body field       | Notes                       |
|------------------------------|-----------------------|-----------------------------|
| `lift_name`                  | (path parameter)      | Carried out separately.     |
| `session_id`                 | `session_id`          | Passthrough.                |
| `request_type == 0`          | (NO_REQUEST — skipped)| The translator returns None.|
| `request_type == 1`          | `request_type: END_SESSION` |                       |
| `request_type == 2`          | `request_type: AGV_MODE`    |                       |
| `request_type == 3`          | `request_type: HUMAN_MODE`  |                       |
| `destination_floor`          | `destination_floor`   | Passthrough.                |
| `door_state.value == 0`      | `door_state: CLOSED`  | rmf DOOR_CLOSED             |
| `door_state.value == 2`      | `door_state: OPEN`    | rmf DOOR_OPEN               |
| `door_state.value == 1`      | rejected              | MOVING is not a goal —      |
|                              |                       | server returns 400.         |
| —                            | `execution_id`        | Generated client-side; used |
|                              |                       | for server-side dedup.      |

`door_state` here may be either a uint8 (rmf_lift_msgs/LiftRequest spells
it as a primitive) or a struct with a `.value` attribute (some bindings
wrap it) — the translator accepts both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional


# rmf_lift_msgs/LiftRequest.request_type constants
_REQUEST_TYPE_NO_REQUEST = 0
_REQUEST_TYPE_END_SESSION = 1
_REQUEST_TYPE_AGV_MODE = 2
_REQUEST_TYPE_HUMAN_MODE = 3

# rmf_lift_msgs DoorState constants
_DOOR_CLOSED = 0
_DOOR_MOVING = 1
_DOOR_OPEN = 2


class TranslationError(Exception):
    """The LiftRequest could not be translated — e.g. door_state is
    MOVING (not a meaningful goal state)."""


@dataclass(frozen=True)
class TranslatedLiftRequest:
    """The POST body + matching URL path parameter for a translated
    LiftRequest."""

    lift_name: str       # URL path parameter
    body: dict[str, Any]  # JSON body for POST /lifts/{lift_name}/request


def translate_lift_request(
    request: Any,
    *,
    execution_id: str | None = None,
) -> Optional[TranslatedLiftRequest]:
    """Translate a `rmf_lift_msgs/LiftRequest`-shaped object into a POST
    body dict.

    Returns `None` if `request_type == NO_REQUEST` — the adapter should
    skip the POST entirely (it's the rmf-side "do nothing" sentinel).

    `request` is duck-typed — any object with `lift_name`, `session_id`,
    `request_type`, `destination_floor`, and `door_state` attributes works.
    This keeps the module unit-testable without rclpy in the import path.

    `execution_id` defaults to a fresh UUID4. Tests can pin it for
    deterministic assertions.

    Raises `TranslationError` if `door_state` is MOVING (= 1) — the
    server rejects MOVING door_state with 400.
    """
    request_type = getattr(request, "request_type", None)
    if request_type is None:
        raise TranslationError("LiftRequest.request_type is missing")
    request_type = _as_int("request_type", request_type)

    if request_type == _REQUEST_TYPE_NO_REQUEST:
        return None  # NO_REQUEST is a no-op; caller skips POST.

    lift_name = _require_str(getattr(request, "lift_name", None), "lift_name")
    session_id = _require_str(getattr(request, "session_id", None), "session_id")
    destination_floor = _require_str(
        getattr(request, "destination_floor", None), "destination_floor"
    )

    wire_request_type = _request_type_name(request_type)
    wire_door_state = _door_state_name(getattr(request, "door_state", None))

    body: dict[str, Any] = {
        "session_id": session_id,
        "request_type": wire_request_type,
        "destination_floor": destination_floor,
        "door_state": wire_door_state,
        "execution_id": execution_id or str(uuid.uuid4()),
    }
    return TranslatedLiftRequest(lift_name=lift_name, body=body)


def _request_type_name(value: int) -> str:
    if value == _REQUEST_TYPE_END_SESSION:
        return "END_SESSION"
    if value == _REQUEST_TYPE_AGV_MODE:
        return "AGV_MODE"
    if value == _REQUEST_TYPE_HUMAN_MODE:
        return "HUMAN_MODE"
    raise TranslationError(
        f"LiftRequest.request_type = {value!r} is not a known constant "
        f"(expected 0=NO_REQUEST, 1=END_SESSION, 2=AGV_MODE, 3=HUMAN_MODE)."
    )


def _door_state_name(raw: Any) -> str:
    """Accept either a uint8 (rmf_lift_msgs raw form) or a struct with
    `.value`. Rejects MOVING."""
    if raw is None:
        raise TranslationError("LiftRequest.door_state is missing")
    value = getattr(raw, "value", raw)  # support both shapes
    value = _as_int("door_state", value)
    if value == _DOOR_OPEN:
        return "OPEN"
    if value == _DOOR_CLOSED:
        return "CLOSED"
    if value == _DOOR_MOVING:
        raise TranslationError(
            "LiftRequest.door_state = MOVING is not a valid goal state; "
            "the QuikSync adapter API rejects MOVING with 400."
        )
    raise TranslationError(
        f"LiftRequest.door_state = {value!r} is not a known constant "
        f"(expected 0=CLOSED, 2=OPEN)."
    )


def _as_int(field_name: str, raw: Any) -> int:
    """Coerce numeric-ish inputs (`numpy.uint8`, `str`, `bool`-as-0/1)
    to a plain int with a clear error message on failure."""
    if isinstance(raw, bool):
        # bool is an int subclass, but we don't want True/False sneaking in.
        raise TranslationError(f"LiftRequest.{field_name} cannot be bool")
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise TranslationError(
            f"LiftRequest.{field_name} = {raw!r} could not be coerced to int"
        ) from e


def _require_str(value: Any, field_name: str) -> str:
    if value is None:
        raise TranslationError(f"LiftRequest.{field_name} is missing")
    s = str(value)
    if not s:
        raise TranslationError(f"LiftRequest.{field_name} must be non-empty")
    return s
