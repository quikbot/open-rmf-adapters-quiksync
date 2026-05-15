"""Tests for LiftAdapterNode — request routing + state publishing."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from lift_adapter_quiksync.node import LiftAdapterNode, build_lift_state_msg


# ----- fakes -----


class FakeWsClient:
    def __init__(self, frames_by_lift: dict[str, list[dict]]) -> None:
        self._frames_by_lift = frames_by_lift
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_lift_state(self, lift: str, namespace=None):
        for frame in self._frames_by_lift.get(lift, []):
            if self._closed:
                return
            yield frame


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post_lift_request(self, **kwargs):
        self.posts.append(kwargs)
        return {"status": "accepted"}


def make_fake_msg_module() -> Any:
    class _Time:
        def __init__(self, sec, nanosec):
            self.sec = sec
            self.nanosec = nanosec

    class _LiftState:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _LiftRequest:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    return SimpleNamespace(LiftState=_LiftState, LiftRequest=_LiftRequest, Time=_Time)


def make_state_frame(
    lift: str = "lift_alpha",
    lift_time_ms: int = 1234,
    current_floor: str = "L1",
    current_mode: int = 2,
    session_id: str = "",
) -> dict:
    return {
        "lift_name": lift,
        "lift_time": lift_time_ms,
        "current_floor": current_floor,
        "destination_floor": "",
        "door_state": 0,
        "motion_state": 0,
        "available_modes": [{"value": 2}, {"value": 4}],
        "current_mode": {"value": current_mode},
        "session_id": session_id,
    }


def make_ros_request(
    lift_name: str = "lift_alpha", session_id: str = "rmf:robot-1",
    request_type: int = 2, destination_floor: str = "L3", door_state: int = 2,
):
    return SimpleNamespace(
        lift_name=lift_name, session_id=session_id, request_type=request_type,
        destination_floor=destination_floor, door_state=door_state,
    )


# ----- build_lift_state_msg (pure function) -----


def test_build_lift_state_msg_spreads_translated_dict():
    msgs = make_fake_msg_module()
    fields = {
        "lift_name": "lift_alpha",
        "lift_time": {"sec": 1, "nanosec": 234_000_000},
        "current_floor": "L1", "destination_floor": "L3",
        "door_state": 2, "motion_state": 1,
        "available_modes": [2, 4],
        "current_mode": 2,
        "session_id": "rmf:robot-1",
    }
    msg = build_lift_state_msg(msgs, fields)
    assert msg.lift_name == "lift_alpha"
    assert msg.lift_time.sec == 1
    assert msg.lift_time.nanosec == 234_000_000
    assert msg.current_floor == "L1"
    assert msg.destination_floor == "L3"
    assert msg.door_state == 2
    assert msg.motion_state == 1
    assert msg.available_modes == [2, 4]
    assert msg.current_mode == 2
    assert msg.session_id == "rmf:robot-1"
    assert msg.available_floors == []  # static; not in steady-state frames


# ----- node lifecycle + state publish path -----


def test_node_constructor_builds_handle_per_lift_with_shared_session_manager():
    msgs = make_fake_msg_module()
    node = LiftAdapterNode(
        lift_names=["a", "b"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
    )
    assert node.lift_names == ("a", "b")
    assert node.handle_for("a") is not None
    assert node.handle_for("b") is not None
    assert node.handle_for("c") is None
    # Session manager is shared across handles.
    sm = node.session_manager
    assert sm is not None


def test_node_state_pump_dispatches_translated_msgs():
    msgs = make_fake_msg_module()
    published: list[Any] = []
    frames = {
        "lift_alpha": [make_state_frame("lift_alpha", lift_time_ms=1234,
                                        current_mode=2, session_id="rmf:r1")],
        "lift_beta": [make_state_frame("lift_beta", lift_time_ms=5678,
                                       current_mode=4, session_id="")],
    }
    node = LiftAdapterNode(
        lift_names=["lift_alpha", "lift_beta"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient(frames),
        msg_module=msgs,
        publish_msg=published.append,
    )
    node.start()
    deadline = time.time() + 1.0
    while node.state_dispatched_total() < 2 and time.time() < deadline:
        time.sleep(0.05)
    node.stop()

    assert node.state_dispatched_total() == 2
    by_lift = {m.lift_name: m for m in published}
    assert by_lift["lift_alpha"].session_id == "rmf:r1"
    assert by_lift["lift_alpha"].current_mode == 2
    assert by_lift["lift_beta"].current_mode == 4
    # Session manager reflects server-pushed state for lift_alpha
    assert node.session_manager.current_holder("lift_alpha") == "rmf:r1"
    assert node.session_manager.current_holder("lift_beta") is None  # empty


def test_node_double_start_is_idempotent():
    node = LiftAdapterNode(
        lift_names=["a"], http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
    )
    node.start()
    node.start()
    node.stop()


def test_node_stop_without_start_is_safe():
    node = LiftAdapterNode(
        lift_names=["a"], http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
    )
    node.stop()


# ----- route_request -----


def test_route_request_dispatches_to_matching_handle():
    http = FakeHttpClient()
    node = LiftAdapterNode(
        lift_names=["lift_alpha"], http_client=http,
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
    )
    req = make_ros_request(lift_name="lift_alpha", request_type=2)
    assert node.route_request(req) is True
    assert http.posts[0]["lift"] == "lift_alpha"
    assert http.posts[0]["request_type"] == "AGV_MODE"
    assert node.requests_dispatched_total() == 1


def test_route_request_drops_unknown_lift_silently():
    http = FakeHttpClient()
    warns: list[tuple] = []
    node = LiftAdapterNode(
        lift_names=["lift_alpha"], http_client=http,
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
        log_warning=lambda *args: warns.append(args),
    )
    req = make_ros_request(lift_name="lift_beta", request_type=2)
    assert node.route_request(req) is False
    assert http.posts == []
    assert warns == []  # silent drop


def test_route_request_warns_on_missing_lift_name():
    warns: list[tuple] = []
    node = LiftAdapterNode(
        lift_names=["lift_alpha"], http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
        log_warning=lambda fmt, *args: warns.append((fmt, args)),
    )
    bad = SimpleNamespace(
        lift_name=None, session_id="x", request_type=2,
        destination_floor="L2", door_state=2,
    )
    assert node.route_request(bad) is False
    assert len(warns) == 1


def test_route_request_no_request_sentinel_is_silent_drop():
    """NO_REQUEST routes to the handle but the handle short-circuits
    without raising / counting."""
    http = FakeHttpClient()
    node = LiftAdapterNode(
        lift_names=["lift_alpha"], http_client=http,
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
    )
    req = make_ros_request(request_type=0)
    assert node.route_request(req) is False
    assert http.posts == []
    assert node.requests_rejected_total() == 0  # NO_REQUEST is silent


def test_route_request_session_conflict_rejected_via_handle():
    """Adapter-side session lock blocks AGV_MODE for a conflicting session."""
    http = FakeHttpClient()
    node = LiftAdapterNode(
        lift_names=["lift_alpha"], http_client=http,
        ws_client=FakeWsClient({}), msg_module=make_fake_msg_module(),
        publish_msg=lambda m: None,
    )
    # Pre-populate session_manager
    node.session_manager.try_acquire("lift_alpha", "rmf:robot-1")
    # Conflicting session
    req = make_ros_request(session_id="rmf:robot-2", request_type=2)
    assert node.route_request(req) is False
    assert http.posts == []
    assert node.requests_rejected_total() == 1
