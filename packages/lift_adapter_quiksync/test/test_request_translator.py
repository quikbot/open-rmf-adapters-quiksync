"""Tests for translate_lift_request — LiftRequest → POST body mapping."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from lift_adapter_quiksync.request_translator import (
    TranslatedLiftRequest,
    TranslationError,
    translate_lift_request,
)


def make_request(
    lift_name: str = "lift_alpha",
    session_id: str = "rmf:robot-1",
    request_type: int = 2,           # AGV_MODE
    destination_floor: str = "L2",
    door_state: int | object = 2,     # OPEN — accepts int or .value-shaped
) -> SimpleNamespace:
    """Build a LiftRequest-shaped object via duck typing."""
    return SimpleNamespace(
        lift_name=lift_name,
        session_id=session_id,
        request_type=request_type,
        destination_floor=destination_floor,
        door_state=door_state,
    )


# ----- happy paths -----


def test_translates_agv_mode_request():
    req = make_request(request_type=2, destination_floor="L3", door_state=2)
    translated = translate_lift_request(req, execution_id="exec-001")
    assert isinstance(translated, TranslatedLiftRequest)
    assert translated.lift_name == "lift_alpha"
    assert translated.body == {
        "session_id": "rmf:robot-1",
        "request_type": "AGV_MODE",
        "destination_floor": "L3",
        "door_state": "OPEN",
        "execution_id": "exec-001",
    }


def test_translates_end_session_request():
    req = make_request(request_type=1, door_state=0)
    translated = translate_lift_request(req, execution_id="exec-002")
    assert translated.body["request_type"] == "END_SESSION"
    assert translated.body["door_state"] == "CLOSED"


def test_translates_human_mode_request():
    req = make_request(request_type=3)
    translated = translate_lift_request(req, execution_id="exec-003")
    assert translated.body["request_type"] == "HUMAN_MODE"


def test_no_request_returns_none():
    """NO_REQUEST is the rmf-side 'no-op' sentinel — the translator
    returns None so the adapter skips the POST entirely."""
    req = make_request(request_type=0)
    assert translate_lift_request(req) is None


# ----- door_state handling -----


def test_door_state_accepts_value_struct():
    """rmf bindings may wrap door_state as `obj.value` — accept both
    shapes."""
    req = make_request(door_state=SimpleNamespace(value=2))
    translated = translate_lift_request(req)
    assert translated is not None
    assert translated.body["door_state"] == "OPEN"


def test_door_state_moving_is_rejected():
    req = make_request(door_state=1)
    with pytest.raises(TranslationError, match="MOVING"):
        translate_lift_request(req)


def test_door_state_unknown_value_is_rejected():
    req = make_request(door_state=99)
    with pytest.raises(TranslationError, match="99"):
        translate_lift_request(req)


def test_door_state_missing_is_rejected():
    req = SimpleNamespace(
        lift_name="lift_alpha",
        session_id="rmf:robot-1",
        request_type=2,
        destination_floor="L2",
        door_state=None,
    )
    with pytest.raises(TranslationError, match="door_state"):
        translate_lift_request(req)


def test_door_state_bool_rejected():
    """`True` would coerce to 1 (MOVING) if we didn't guard against bool."""
    req = make_request(door_state=True)
    with pytest.raises(TranslationError, match="bool"):
        translate_lift_request(req)


# ----- request_type validation -----


def test_unknown_request_type_is_rejected():
    req = make_request(request_type=99)
    with pytest.raises(TranslationError, match="99"):
        translate_lift_request(req)


def test_missing_request_type_is_rejected():
    req = SimpleNamespace(
        lift_name="lift_alpha",
        session_id="rmf:robot-1",
        destination_floor="L2",
        door_state=2,
    )
    with pytest.raises(TranslationError, match="request_type"):
        translate_lift_request(req)


# ----- field validation (non-NO_REQUEST paths) -----


def test_missing_lift_name_is_rejected():
    req = make_request(lift_name="")
    with pytest.raises(TranslationError, match="lift_name"):
        translate_lift_request(req)


def test_missing_session_id_is_rejected():
    req = make_request(session_id="")
    with pytest.raises(TranslationError, match="session_id"):
        translate_lift_request(req)


def test_missing_destination_floor_is_rejected():
    req = make_request(destination_floor="")
    with pytest.raises(TranslationError, match="destination_floor"):
        translate_lift_request(req)


# ----- execution_id -----


def test_generates_uuid_execution_id_when_omitted():
    req = make_request()
    translated = translate_lift_request(req)
    assert translated is not None
    parsed = uuid.UUID(translated.body["execution_id"])
    assert parsed.version == 4


def test_execution_id_uniqueness_when_generated():
    req = make_request()
    a = translate_lift_request(req).body["execution_id"]
    b = translate_lift_request(req).body["execution_id"]
    assert a != b


# ----- TranslatedLiftRequest invariants -----


def test_translated_request_is_frozen():
    req = make_request()
    translated = translate_lift_request(req)
    with pytest.raises(Exception):
        translated.lift_name = "other"  # type: ignore[misc]


# ----- NO_REQUEST short-circuit doesn't validate other fields -----


def test_no_request_skips_field_validation():
    """NO_REQUEST returns None before validating session_id /
    destination_floor / door_state — those fields can legitimately be
    empty in a 'do nothing' sentinel."""
    req = SimpleNamespace(
        lift_name="",
        session_id="",
        request_type=0,
        destination_floor="",
        door_state=0,
    )
    assert translate_lift_request(req) is None
