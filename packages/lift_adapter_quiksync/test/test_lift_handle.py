"""Tests for LiftHandle — state translation + request dispatch + session-mgr."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from lift_adapter_quiksync.lift_handle import LiftHandle, RequestTranslationError
from lift_adapter_quiksync.session_manager import LiftSessionManager


# ----- test doubles -----


class FakeWsClient:
    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_lift_state(self, lift: str):
        for frame in self._frames:
            if self._closed:
                return
            yield frame


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.raises: Exception | None = None

    def post_lift_request(self, *, lift, session_id, request_type,
                          destination_floor, door_state, execution_id):
        if self.raises is not None:
            raise self.raises
        record = {
            "lift": lift,
            "session_id": session_id,
            "request_type": request_type,
            "destination_floor": destination_floor,
            "door_state": door_state,
            "execution_id": execution_id,
        }
        self.posts.append(record)
        return {"status": "accepted"}


def make_state_frame(
    lift: str = "lift_alpha",
    lift_time_ms: int = 1778760087657,
    current_floor: str = "L1",
    destination_floor: str = "",
    door_state: int = 0,
    motion_state: int = 0,
    current_mode: int = 2,
    session_id: str = "",
) -> dict:
    return {
        "lift_name": lift,
        "lift_time": lift_time_ms,
        "current_floor": current_floor,
        "destination_floor": destination_floor,
        "door_state": door_state,
        "motion_state": motion_state,
        "available_modes": [{"value": 2}, {"value": 4}],
        "current_mode": {"value": current_mode},
        "session_id": session_id,
    }


def make_ros_request(
    lift_name: str = "lift_alpha",
    session_id: str = "rmf:robot-1",
    request_type: int = 2,           # AGV_MODE
    destination_floor: str = "L3",
    door_state: int | object = 2,    # OPEN
) -> SimpleNamespace:
    return SimpleNamespace(
        lift_name=lift_name,
        session_id=session_id,
        request_type=request_type,
        destination_floor=destination_floor,
        door_state=door_state,
    )


def _make_handle(lift: str = "lift_alpha", publish=None,
                 http=None, ws=None, session_manager=None):
    published: list[dict] = publish if publish is not None else []

    def _publish(fields: dict) -> None:
        published.append(fields)

    fake_http = http if http is not None else FakeHttpClient()
    fake_ws = ws if ws is not None else FakeWsClient([])
    sm = session_manager if session_manager is not None else LiftSessionManager()
    handle = LiftHandle(lift, fake_http, fake_ws, _publish, sm)
    return handle, published, fake_http, sm


# ----- state translation -----


def test_translate_state_converts_lift_time_ms_to_sec_nanosec():
    handle, _, _, _ = _make_handle()
    frame = make_state_frame(lift_time_ms=1234)
    fields = handle.translate_state(frame)
    assert fields["lift_time"] == {"sec": 1, "nanosec": 234_000_000}


def test_translate_state_flattens_available_modes():
    """`available_modes` is [{value: 2}, {value: 4}] on wire,
    [2, 4] (flat uint8[]) in the ROS msg."""
    handle, _, _, _ = _make_handle()
    fields = handle.translate_state(make_state_frame())
    assert fields["available_modes"] == [2, 4]


def test_translate_state_carries_all_required_fields():
    handle, _, _, _ = _make_handle()
    fields = handle.translate_state(make_state_frame(
        current_floor="L2", destination_floor="L4",
        door_state=2, motion_state=1, current_mode=4,
        session_id="rmf:robot-1",
    ))
    assert fields["current_floor"] == "L2"
    assert fields["destination_floor"] == "L4"
    assert fields["door_state"] == 2
    assert fields["motion_state"] == 1
    assert fields["current_mode"] == 4
    assert fields["session_id"] == "rmf:robot-1"


def test_translate_state_realistic_epoch_ms():
    handle, _, _, _ = _make_handle()
    fields = handle.translate_state(make_state_frame(lift_time_ms=1778760087657))
    assert fields["lift_time"] == {"sec": 1778760087, "nanosec": 657_000_000}


def test_translate_state_handles_missing_optionals():
    """`current_floor` / `destination_floor` may be absent in some
    server responses. Empty string is the safe default."""
    handle, _, _, _ = _make_handle()
    frame = {
        "lift_name": "lift_alpha", "lift_time": 1000,
        "current_mode": {"value": 2},
        "available_modes": [{"value": 2}, {"value": 4}],
    }
    fields = handle.translate_state(frame)
    assert fields["current_floor"] == ""
    assert fields["destination_floor"] == ""
    assert fields["session_id"] == ""


# ----- state pump → publish path + session-mgr sync -----


@pytest.mark.asyncio
async def test_pump_publishes_translated_state_and_syncs_session_manager():
    """When a state frame arrives, the handle should:
    1. Translate + publish to ROS
    2. Update the session_manager with the server's session_id view"""
    frames = [
        make_state_frame(session_id="rmf:robot-2"),
    ]
    sm = LiftSessionManager()
    handle, published, _, _ = _make_handle(ws=FakeWsClient(frames), session_manager=sm)
    await handle.start()
    await asyncio.sleep(0.05)
    await handle.stop()

    # Frame was published
    assert handle.state_dispatched() == 1
    assert published[0]["session_id"] == "rmf:robot-2"
    # Session manager now reflects the server view
    assert sm.current_holder("lift_alpha") == "rmf:robot-2"


@pytest.mark.asyncio
async def test_pump_publisher_exception_is_logged_not_raised():
    def boom(fields: dict) -> None:
        raise RuntimeError("ros publisher down")

    frames = [make_state_frame()]
    handle = LiftHandle("lift_alpha", FakeHttpClient(), FakeWsClient(frames), boom, LiftSessionManager())
    await handle.start()
    await asyncio.sleep(0.05)
    await handle.stop()
    assert handle.state_dispatched() == 0  # publisher raised


# ----- request translation -----


def test_dispatch_agv_mode_posts_correct_body():
    handle, _, http, _ = _make_handle()
    req = make_ros_request(request_type=2, destination_floor="L3", door_state=2)
    ok = handle.dispatch_request(req, execution_id="exec-1")
    assert ok is True
    assert http.posts == [{
        "lift": "lift_alpha",
        "session_id": "rmf:robot-1",
        "request_type": "AGV_MODE",
        "destination_floor": "L3",
        "door_state": "OPEN",
        "execution_id": "exec-1",
    }]
    assert handle.requests_dispatched() == 1


def test_dispatch_end_session_releases_local_state():
    handle, _, http, sm = _make_handle()
    # First acquire
    handle.dispatch_request(make_ros_request(request_type=2), execution_id="e1")
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"
    # Then END_SESSION
    ok = handle.dispatch_request(
        make_ros_request(request_type=1, door_state=0), execution_id="e2"
    )
    assert ok is True
    # Adapter-side: local request view cleared. Server view still our
    # session until the next state-push, which is fine.
    assert http.posts[-1]["request_type"] == "END_SESSION"


def test_dispatch_human_mode_translates_correctly():
    handle, _, http, _ = _make_handle()
    handle.dispatch_request(make_ros_request(request_type=3), execution_id="e1")
    assert http.posts[0]["request_type"] == "HUMAN_MODE"


def test_dispatch_no_request_is_silent_drop():
    """NO_REQUEST sentinel: no post, no rejected-counter bump (steady state)."""
    handle, _, http, _ = _make_handle()
    req = make_ros_request(request_type=0)
    ok = handle.dispatch_request(req)
    assert ok is False
    assert http.posts == []
    assert handle.requests_rejected() == 0  # silent drop, not a reject


def test_dispatch_moving_door_state_is_rejected():
    handle, _, http, _ = _make_handle()
    req = make_ros_request(door_state=1)
    assert handle.dispatch_request(req) is False
    assert http.posts == []
    assert handle.requests_rejected() == 1


def test_dispatch_unknown_request_type_is_rejected():
    handle, _, http, _ = _make_handle()
    req = make_ros_request(request_type=99)
    assert handle.dispatch_request(req) is False
    assert handle.requests_rejected() == 1


def test_dispatch_door_state_value_struct_unwrap():
    """rmf bindings may wrap door_state as obj.value — accept both."""
    handle, _, http, _ = _make_handle()
    req = make_ros_request(door_state=SimpleNamespace(value=2))
    assert handle.dispatch_request(req) is True
    assert http.posts[0]["door_state"] == "OPEN"


def test_dispatch_door_state_bool_is_rejected():
    """`True` would coerce to 1 (MOVING) without a bool guard."""
    handle, _, http, _ = _make_handle()
    req = make_ros_request(door_state=True)
    assert handle.dispatch_request(req) is False
    assert handle.requests_rejected() == 1


def test_dispatch_cross_lift_is_rejected():
    handle, _, http, _ = _make_handle(lift="lift_alpha")
    req = make_ros_request(lift_name="lift_beta", request_type=2)
    assert handle.dispatch_request(req) is False
    assert http.posts == []


def test_dispatch_missing_fields_rejected():
    handle, _, http, _ = _make_handle()
    # Missing session_id
    bad = SimpleNamespace(
        lift_name="lift_alpha", session_id="", request_type=2,
        destination_floor="L2", door_state=2,
    )
    assert handle.dispatch_request(bad) is False
    assert handle.requests_rejected() == 1


# ----- session-manager integration -----


def test_dispatch_agv_mode_rejected_by_local_session_conflict():
    """If the session_manager already holds the lift for a different
    session_id, the handle short-circuits without a POST."""
    sm = LiftSessionManager()
    sm.try_acquire("lift_alpha", "rmf:robot-other")
    handle, _, http, _ = _make_handle(session_manager=sm)
    req = make_ros_request(session_id="rmf:robot-1", request_type=2)
    assert handle.dispatch_request(req) is False
    assert http.posts == []  # POST never happened
    assert handle.requests_rejected() == 1


def test_dispatch_agv_mode_records_session_in_manager():
    sm = LiftSessionManager()
    handle, _, http, _ = _make_handle(session_manager=sm)
    handle.dispatch_request(make_ros_request(request_type=2))
    # Session manager now records our session as the requester.
    assert sm.current_holder("lift_alpha") == "rmf:robot-1"


def test_dispatch_end_session_does_not_check_local_session_lock():
    """END_SESSION should always be forwarded — the server is the
    authority on whether it's a valid release."""
    sm = LiftSessionManager()
    # Lift is held by someone else on the adapter side.
    sm.try_acquire("lift_alpha", "rmf:robot-other")
    handle, _, http, _ = _make_handle(session_manager=sm)
    # END_SESSION should still POST (server will validate).
    ok = handle.dispatch_request(make_ros_request(request_type=1, door_state=0))
    assert ok is True
    assert http.posts[-1]["request_type"] == "END_SESSION"


# ----- execution_id / counters / failure propagation -----


def test_dispatch_generates_uuid_execution_id_when_omitted():
    handle, _, http, _ = _make_handle()
    handle.dispatch_request(make_ros_request())
    parsed = uuid.UUID(http.posts[0]["execution_id"])
    assert parsed.version == 4


def test_dispatch_http_failure_propagates():
    handle, _, http, _ = _make_handle()
    http.raises = RuntimeError("network down")
    with pytest.raises(RuntimeError, match="network down"):
        handle.dispatch_request(make_ros_request())


def test_lift_name_accessor_and_counters_start_at_zero():
    handle, _, _, _ = _make_handle(lift="lift_alpha")
    assert handle.lift_name == "lift_alpha"
    assert handle.state_dispatched() == 0
    assert handle.requests_dispatched() == 0
    assert handle.requests_rejected() == 0
