"""Tests for DoorAdapterNode — request routing + state publishing.

The node composes a real `rclpy.node.Node` at runtime but for unit
tests we inject a fake msg module + a fake publish callable. The
DoorHandle layer is real (PR #18) and is exercised end-to-end via
its state pump.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from door_adapter_quiksync.node import DoorAdapterNode, build_door_state_msg


# ----- fakes -----


class FakeWsClient:
    """Yields a fixed sequence of state frames then exits cleanly."""

    def __init__(self, frames_by_door: dict[str, list[dict]]) -> None:
        self._frames_by_door = frames_by_door
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_door_state(self, door: str):
        for frame in self._frames_by_door.get(door, []):
            if self._closed:
                return
            yield frame


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post_door_request(self, *, door, requester_id, requested_mode, execution_id):
        self.posts.append({
            "door": door,
            "requester_id": requester_id,
            "requested_mode": requested_mode,
            "execution_id": execution_id,
        })
        return {"status": "accepted"}


def make_fake_msg_module() -> Any:
    """Build a SimpleNamespace exposing DoorState / DoorMode / Time
    constructors that record what they were called with.

    Uses dataclass-shaped constructors so the test can introspect."""

    class _DoorMode:
        def __init__(self, value):
            self.value = value
        def __repr__(self):
            return f"DoorMode(value={self.value!r})"

    class _Time:
        def __init__(self, sec, nanosec):
            self.sec = sec
            self.nanosec = nanosec
        def __repr__(self):
            return f"Time(sec={self.sec}, nanosec={self.nanosec})"

    class _DoorState:
        def __init__(self, door_name, door_time, current_mode):
            self.door_name = door_name
            self.door_time = door_time
            self.current_mode = current_mode

    class _DoorRequest:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    return SimpleNamespace(
        DoorState=_DoorState,
        DoorMode=_DoorMode,
        Time=_Time,
        DoorRequest=_DoorRequest,
    )


def make_state_frame(door="door_alpha", door_time_ms=1234, mode_value=0):
    return {
        "door_name": door,
        "door_time": door_time_ms,
        "current_mode": {"value": mode_value},
    }


def make_ros_request(door_name="door_alpha", requester_id="rmf:r1", mode_value=2):
    return SimpleNamespace(
        door_name=door_name,
        requester_id=requester_id,
        requested_mode=SimpleNamespace(value=mode_value),
    )


# ----- build_door_state_msg (pure function) -----


def test_build_door_state_msg_spreads_translated_dict():
    msgs = make_fake_msg_module()
    fields = {
        "door_name": "door_alpha",
        "door_time": {"sec": 1, "nanosec": 234_000_000},
        "current_mode": {"value": 2},
    }
    msg = build_door_state_msg(msgs, fields)
    assert msg.door_name == "door_alpha"
    assert msg.door_time.sec == 1
    assert msg.door_time.nanosec == 234_000_000
    assert msg.current_mode.value == 2


# ----- node lifecycle + state publish path -----


def test_node_constructor_builds_handle_per_door():
    msgs = make_fake_msg_module()
    published: list[Any] = []
    node = DoorAdapterNode(
        door_names=["a", "b"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=published.append,
    )
    assert node.door_names == ("a", "b")
    assert node.handle_for("a") is not None
    assert node.handle_for("b") is not None
    assert node.handle_for("c") is None


def test_node_state_pump_dispatches_translated_msgs():
    """End-to-end: state frame → DoorHandle translation → node msg
    construction → publish callback."""
    msgs = make_fake_msg_module()
    published: list[Any] = []
    frames = {
        "door_alpha": [make_state_frame("door_alpha", door_time_ms=1234, mode_value=0)],
        "door_beta": [make_state_frame("door_beta", door_time_ms=5678, mode_value=2)],
    }
    node = DoorAdapterNode(
        door_names=["door_alpha", "door_beta"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient(frames),
        msg_module=msgs,
        publish_msg=published.append,
    )
    node.start()
    # Give the asyncio loop a moment to drain frames.
    deadline = time.time() + 1.0
    while node.state_dispatched_total() < 2 and time.time() < deadline:
        time.sleep(0.05)
    node.stop()

    assert node.state_dispatched_total() == 2
    by_door = {m.door_name: m for m in published}
    assert by_door["door_alpha"].current_mode.value == 0
    assert by_door["door_alpha"].door_time.sec == 1
    assert by_door["door_alpha"].door_time.nanosec == 234_000_000
    assert by_door["door_beta"].current_mode.value == 2
    assert by_door["door_beta"].door_time.sec == 5
    assert by_door["door_beta"].door_time.nanosec == 678_000_000


def test_node_double_start_is_idempotent():
    msgs = make_fake_msg_module()
    node = DoorAdapterNode(
        door_names=["a"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
    )
    node.start()
    node.start()  # must not raise / spawn second thread
    node.stop()


def test_node_stop_without_start_is_safe():
    msgs = make_fake_msg_module()
    node = DoorAdapterNode(
        door_names=["a"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
    )
    node.stop()  # no-op


# ----- route_request (rclpy subscriber thread → handle) -----


def test_route_request_dispatches_to_matching_handle():
    msgs = make_fake_msg_module()
    http = FakeHttpClient()
    node = DoorAdapterNode(
        door_names=["door_alpha"],
        http_client=http,
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
    )
    req = make_ros_request(door_name="door_alpha", mode_value=2)
    assert node.route_request(req) is True
    assert http.posts[0]["door"] == "door_alpha"
    assert http.posts[0]["requested_mode"] == "OPEN"
    assert node.requests_dispatched_total() == 1


def test_route_request_drops_unknown_door_silently():
    """Every door adapter sees every DoorRequest in the building. Drops
    for unmanaged doors must be silent (no warning) — otherwise log
    noise scales with door count."""
    msgs = make_fake_msg_module()
    http = FakeHttpClient()
    warns: list[tuple] = []

    def fake_warn(fmt: str, *args):
        warns.append((fmt, args))

    node = DoorAdapterNode(
        door_names=["door_alpha"],
        http_client=http,
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
        log_warning=fake_warn,
    )
    req = make_ros_request(door_name="door_beta", mode_value=2)
    assert node.route_request(req) is False
    assert http.posts == []
    assert warns == []  # silent drop


def test_route_request_warns_on_missing_door_name():
    msgs = make_fake_msg_module()
    warns: list[tuple] = []

    def fake_warn(fmt: str, *args):
        warns.append((fmt, args))

    node = DoorAdapterNode(
        door_names=["door_alpha"],
        http_client=FakeHttpClient(),
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
        log_warning=fake_warn,
    )
    bad = SimpleNamespace(door_name=None, requester_id="x",
                          requested_mode=SimpleNamespace(value=2))
    assert node.route_request(bad) is False
    assert len(warns) == 1  # warn on malformed msg


def test_route_request_moving_mode_rejected_via_handle():
    """The handle's MOVING reject path is exercised through route_request."""
    msgs = make_fake_msg_module()
    http = FakeHttpClient()
    node = DoorAdapterNode(
        door_names=["door_alpha"],
        http_client=http,
        ws_client=FakeWsClient({}),
        msg_module=msgs,
        publish_msg=lambda m: None,
    )
    req = make_ros_request(door_name="door_alpha", mode_value=1)  # MOVING
    assert node.route_request(req) is False
    assert http.posts == []
    assert node.requests_rejected_total() == 1
