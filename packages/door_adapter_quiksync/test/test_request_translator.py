"""Tests for translate_door_request — DoorRequest → POST body mapping."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from door_adapter_quiksync.request_translator import (
    TranslatedDoorRequest,
    TranslationError,
    translate_door_request,
)


def make_request(
    door_name: str = "door_alpha",
    requester_id: str = "rmf:robot-1",
    mode_value: int = 2,
) -> SimpleNamespace:
    """Build a DoorRequest-shaped object via duck typing."""
    return SimpleNamespace(
        door_name=door_name,
        requester_id=requester_id,
        requested_mode=SimpleNamespace(value=mode_value),
    )


def test_translates_open_request():
    req = make_request(mode_value=2)
    translated = translate_door_request(req, execution_id="exec-001")
    assert isinstance(translated, TranslatedDoorRequest)
    assert translated.door_name == "door_alpha"
    assert translated.body == {
        "requester_id": "rmf:robot-1",
        "requested_mode": "OPEN",
        "execution_id": "exec-001",
    }


def test_translates_closed_request():
    req = make_request(mode_value=0)
    translated = translate_door_request(req, execution_id="exec-002")
    assert translated.body["requested_mode"] == "CLOSED"


def test_generates_uuid_execution_id_when_omitted():
    req = make_request()
    translated = translate_door_request(req)
    # Should parse as a UUID4
    parsed = uuid.UUID(translated.body["execution_id"])
    assert parsed.version == 4


def test_moving_mode_is_rejected():
    """MOVING (= 1) is not a goal state; the server returns 400."""
    req = make_request(mode_value=1)
    with pytest.raises(TranslationError, match="MOVING"):
        translate_door_request(req)


def test_unknown_mode_is_rejected():
    req = make_request(mode_value=99)
    with pytest.raises(TranslationError, match="99"):
        translate_door_request(req)


def test_missing_door_name_is_rejected():
    req = make_request(door_name="")
    with pytest.raises(TranslationError, match="door_name"):
        translate_door_request(req)


def test_missing_requester_id_is_rejected():
    req = make_request(requester_id="")
    with pytest.raises(TranslationError, match="requester_id"):
        translate_door_request(req)


def test_none_door_name_is_rejected():
    req = SimpleNamespace(
        door_name=None,
        requester_id="rmf:robot-1",
        requested_mode=SimpleNamespace(value=2),
    )
    with pytest.raises(TranslationError, match="door_name"):
        translate_door_request(req)


def test_missing_requested_mode_is_rejected():
    req = SimpleNamespace(door_name="door_alpha", requester_id="rmf:robot-1")
    with pytest.raises(TranslationError, match="requested_mode"):
        translate_door_request(req)


def test_missing_requested_mode_value_is_rejected():
    req = SimpleNamespace(
        door_name="door_alpha",
        requester_id="rmf:robot-1",
        requested_mode=SimpleNamespace(),  # no .value
    )
    with pytest.raises(TranslationError, match="requested_mode.value"):
        translate_door_request(req)


def test_translated_request_is_frozen():
    req = make_request()
    translated = translate_door_request(req)
    with pytest.raises(Exception):
        translated.door_name = "other"  # type: ignore[misc]


def test_passthrough_requester_id_arbitrary_string():
    """The translator doesn't validate requester_id format — it's an
    rmf-side opaque identifier."""
    req = make_request(requester_id="some-arbitrary-string/with:chars")
    translated = translate_door_request(req)
    assert translated.body["requester_id"] == "some-arbitrary-string/with:chars"


def test_execution_id_uniqueness_when_generated():
    """Distinct calls with omitted execution_id get distinct UUIDs."""
    req = make_request()
    a = translate_door_request(req).body["execution_id"]
    b = translate_door_request(req).body["execution_id"]
    assert a != b
