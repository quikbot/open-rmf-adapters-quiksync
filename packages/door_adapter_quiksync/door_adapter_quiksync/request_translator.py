"""Translate ROS `rmf_door_msgs/DoorRequest` into the QuikSync
`POST /doors/{door}/request` body dict.

Pure function — accepts duck-typed inputs with the same attributes as
the ROS message so the module can be unit-tested without rclpy in the
classpath. The adapter wires this between the ROS subscription and the
HTTP client.

Wire mapping (per the QuikSync adapter API contract):

| ROS DoorRequest field        | POST body field       | Notes                       |
|------------------------------|-----------------------|-----------------------------|
| `door_name`                  | (path parameter)      | Carried out separately.     |
| `requester_id`               | `requester_id`        | Passthrough.                |
| `requested_mode.value == 0`  | `requested_mode: CLOSED` | rmf MODE_CLOSED          |
| `requested_mode.value == 2`  | `requested_mode: OPEN`   | rmf MODE_OPEN            |
| `requested_mode.value == 1`  | rejected               | MODE_MOVING is not a goal — |
|                              |                       | server returns 400.         |
| —                            | `execution_id`        | Generated client-side; used |
|                              |                       | for server-side dedup.      |
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


# ROS DoorMode.value constants — re-stated here so the translator can
# work without importing rmf_door_msgs.
_MODE_CLOSED = 0
_MODE_MOVING = 1
_MODE_OPEN = 2


class TranslationError(Exception):
    """The DoorRequest could not be translated — e.g. requested_mode is
    MODE_MOVING (not a meaningful goal state)."""


@dataclass(frozen=True)
class TranslatedDoorRequest:
    """The POST body + matching URL path parameter for a translated
    DoorRequest."""

    door_name: str       # URL path parameter
    body: dict[str, Any]  # JSON body for POST /doors/{door_name}/request


def translate_door_request(
    request: Any,
    *,
    execution_id: str | None = None,
) -> TranslatedDoorRequest:
    """Translate a `rmf_door_msgs/DoorRequest`-shaped object into a POST
    body dict.

    `request` is duck-typed — any object with `door_name`, `requester_id`,
    and `requested_mode.value` attributes works. This keeps the module
    unit-testable without rclpy in the import path.

    `execution_id` defaults to a fresh UUID4. Tests can pin it for
    deterministic assertions.

    Raises `TranslationError` if `requested_mode.value` is
    MODE_MOVING (= 1) — the server rejects MOVING as a goal.
    """
    door_name = _require_str(getattr(request, "door_name", None), "door_name")
    requester_id = _require_str(getattr(request, "requester_id", None), "requester_id")

    mode = getattr(request, "requested_mode", None)
    if mode is None:
        raise TranslationError("DoorRequest.requested_mode is missing")
    mode_value = getattr(mode, "value", None)
    if mode_value is None:
        raise TranslationError("DoorRequest.requested_mode.value is missing")

    if mode_value == _MODE_OPEN:
        wire_mode = "OPEN"
    elif mode_value == _MODE_CLOSED:
        wire_mode = "CLOSED"
    elif mode_value == _MODE_MOVING:
        raise TranslationError(
            "DoorRequest.requested_mode = MODE_MOVING is not a valid goal "
            "state; the QuikSync adapter API rejects MOVING with 400."
        )
    else:
        raise TranslationError(
            f"DoorRequest.requested_mode.value = {mode_value!r} is not a "
            f"known DoorMode constant (expected 0=CLOSED, 2=OPEN)."
        )

    body: dict[str, Any] = {
        "requester_id": requester_id,
        "requested_mode": wire_mode,
        "execution_id": execution_id or str(uuid.uuid4()),
    }
    return TranslatedDoorRequest(door_name=door_name, body=body)


def _require_str(value: Any, field_name: str) -> str:
    """Coerce non-str values (e.g. ROS-generated str subclasses) and
    require non-empty input."""
    if value is None:
        raise TranslationError(f"DoorRequest.{field_name} is missing")
    s = str(value)
    if not s:
        raise TranslationError(f"DoorRequest.{field_name} must be non-empty")
    return s
