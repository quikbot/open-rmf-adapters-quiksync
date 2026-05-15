"""Tests for DoorHandle — state translation + request dispatch."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from door_adapter_quiksync.door_handle import (
    DoorHandle,
    RequestTranslationError,
)


# ----- test doubles -----


class FakeWsClient:
    """Yields a fixed sequence of state frames then exits cleanly."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_door_state(self, door: str, namespace=None):
        for frame in self._frames:
            if self._closed:
                return
            yield frame


class FakeHttpClient:
    """Records calls to post_door_request without making real HTTP."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.raises: Exception | None = None

    def post_door_request(self, *, door: str, requester_id: str,
                          requested_mode: str, execution_id: str,
                          namespace: str | None = None) -> dict[str, Any]:
        if self.raises is not None:
            raise self.raises
        record = {
            "door": door,
            "requester_id": requester_id,
            "requested_mode": requested_mode,
            "execution_id": execution_id,
        }
        self.posts.append(record)
        return {"status": "accepted"}


def make_state_frame(door: str = "door_alpha", door_time_ms: int = 1778760087657,
                     mode_value: int = 0) -> dict:
    return {
        "door_name": door,
        "door_time": door_time_ms,
        "current_mode": {"value": mode_value},
    }


def make_ros_request(door_name: str = "door_alpha",
                     requester_id: str = "rmf:robot-1",
                     mode_value: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        door_name=door_name,
        requester_id=requester_id,
        requested_mode=SimpleNamespace(value=mode_value),
    )


# ----- state translation -----


def _make_handle(door="door_alpha", publish=None,
                 http=None, ws=None) -> tuple[DoorHandle, list[dict], FakeHttpClient]:
    """Build a DoorHandle wired to fakes. Returns (handle, published_dicts,
    fake_http)."""
    published: list[dict] = publish if publish is not None else []

    def _publish(fields: dict) -> None:
        published.append(fields)

    fake_http = http if http is not None else FakeHttpClient()
    fake_ws = ws if ws is not None else FakeWsClient([])
    handle = DoorHandle(door, fake_http, fake_ws, _publish)
    return handle, published, fake_http


def test_translate_state_converts_door_time_ms_to_sec_nanosec():
    handle, _, _ = _make_handle()
    frame = make_state_frame(door="door_alpha", door_time_ms=1234)
    fields = handle.translate_state(frame)
    assert fields["door_time"] == {"sec": 1, "nanosec": 234_000_000}


def test_translate_state_carries_door_name_and_mode():
    handle, _, _ = _make_handle()
    frame = make_state_frame(door="lobby_west", mode_value=2)
    fields = handle.translate_state(frame)
    assert fields["door_name"] == "lobby_west"
    assert fields["current_mode"] == {"value": 2}


def test_translate_state_realistic_epoch_ms():
    """Live timestamp captured from the staging discovery probe."""
    handle, _, _ = _make_handle()
    frame = make_state_frame(door_time_ms=1778760087657)
    fields = handle.translate_state(frame)
    assert fields["door_time"] == {"sec": 1778760087, "nanosec": 657_000_000}


# ----- state pump → publish path -----


@pytest.mark.asyncio
async def test_pump_publishes_translated_state_for_each_frame():
    frames = [
        make_state_frame(door="door_alpha", door_time_ms=1000, mode_value=0),
        make_state_frame(door="door_alpha", door_time_ms=2500, mode_value=2),
    ]
    handle, published, _ = _make_handle(ws=FakeWsClient(frames))
    await handle.start()
    await asyncio.sleep(0.05)
    await handle.stop()

    assert handle.state_dispatched() == 2
    assert published == [
        {"door_name": "door_alpha", "door_time": {"sec": 1, "nanosec": 0},
         "current_mode": {"value": 0}},
        {"door_name": "door_alpha", "door_time": {"sec": 2, "nanosec": 500_000_000},
         "current_mode": {"value": 2}},
    ]


@pytest.mark.asyncio
async def test_pump_state_translation_failure_is_logged_not_raised():
    """A malformed frame (missing door_time) should be skipped, not
    crash the pump."""
    frames = [
        {"door_name": "door_alpha", "current_mode": {"value": 0}},  # missing door_time
        make_state_frame(door="door_alpha"),
    ]
    handle, published, _ = _make_handle(ws=FakeWsClient(frames))
    await handle.start()
    await asyncio.sleep(0.05)
    await handle.stop()

    # Second frame still made it through
    assert handle.state_dispatched() == 1
    assert len(published) == 1


@pytest.mark.asyncio
async def test_pump_publisher_exception_is_logged_not_raised():
    """If the publish callback raises, the pump keeps going + counter
    doesn't increment."""
    raised_for: list[dict] = []

    def boom(fields: dict) -> None:
        raised_for.append(fields)
        raise RuntimeError("ros publisher down")

    frames = [make_state_frame(), make_state_frame(door_time_ms=2000)]
    fake_ws = FakeWsClient(frames)
    handle = DoorHandle("door_alpha", FakeHttpClient(), fake_ws, boom)
    await handle.start()
    await asyncio.sleep(0.05)
    await handle.stop()

    # Both frames attempted, both translated, both raised at publish time
    assert handle.state_dispatched() == 0
    assert len(raised_for) == 2


# ----- request translation -----


def test_dispatch_request_open_posts_correct_body():
    handle, _, http = _make_handle()
    req = make_ros_request(door_name="door_alpha", requester_id="rmf:robot-1",
                           mode_value=2)
    ok = handle.dispatch_request(req, execution_id="exec-001")
    assert ok is True
    assert http.posts == [{
        "door": "door_alpha",
        "requester_id": "rmf:robot-1",
        "requested_mode": "OPEN",
        "execution_id": "exec-001",
    }]
    assert handle.requests_dispatched() == 1
    assert handle.requests_rejected() == 0


def test_dispatch_request_closed_posts_correct_body():
    handle, _, http = _make_handle()
    req = make_ros_request(mode_value=0)
    handle.dispatch_request(req, execution_id="exec-002")
    assert http.posts[0]["requested_mode"] == "CLOSED"


def test_dispatch_request_moving_is_rejected():
    """MODE_MOVING is not a goal — server returns 400, so reject early."""
    handle, _, http = _make_handle()
    req = make_ros_request(mode_value=1)
    ok = handle.dispatch_request(req)
    assert ok is False
    assert http.posts == []
    assert handle.requests_rejected() == 1


def test_dispatch_request_unknown_mode_is_rejected():
    handle, _, http = _make_handle()
    req = make_ros_request(mode_value=99)
    assert handle.dispatch_request(req) is False
    assert handle.requests_rejected() == 1


def test_dispatch_request_missing_door_name_is_rejected():
    handle, _, http = _make_handle()
    req = SimpleNamespace(
        door_name="",
        requester_id="rmf:robot-1",
        requested_mode=SimpleNamespace(value=2),
    )
    assert handle.dispatch_request(req) is False
    assert handle.requests_rejected() == 1


def test_dispatch_request_missing_requested_mode_is_rejected():
    handle, _, http = _make_handle()
    req = SimpleNamespace(door_name="door_alpha", requester_id="rmf:robot-1")
    assert handle.dispatch_request(req) is False
    assert handle.requests_rejected() == 1


def test_dispatch_request_cross_door_is_rejected():
    """A handle owning door_alpha must reject DoorRequests for door_beta —
    defense in depth even if the rclpy subscriber should already filter."""
    handle, _, http = _make_handle(door="door_alpha")
    req = make_ros_request(door_name="door_beta", mode_value=2)
    ok = handle.dispatch_request(req)
    assert ok is False
    assert http.posts == []
    assert handle.requests_rejected() == 1


def test_dispatch_request_generates_uuid_execution_id_when_omitted():
    handle, _, http = _make_handle()
    req = make_ros_request()
    handle.dispatch_request(req)
    # Should parse as a UUID4
    parsed = uuid.UUID(http.posts[0]["execution_id"])
    assert parsed.version == 4


def test_dispatch_request_two_calls_get_different_execution_ids():
    handle, _, http = _make_handle()
    handle.dispatch_request(make_ros_request())
    handle.dispatch_request(make_ros_request())
    assert http.posts[0]["execution_id"] != http.posts[1]["execution_id"]


def test_dispatch_request_http_failure_propagates():
    """The handle doesn't swallow transport errors — caller decides retry."""
    handle, _, http = _make_handle()
    http.raises = RuntimeError("network down")
    req = make_ros_request()
    with pytest.raises(RuntimeError, match="network down"):
        handle.dispatch_request(req)
    # Counter not bumped on failure
    assert handle.requests_dispatched() == 0
    assert handle.requests_rejected() == 0


# ----- counters + accessors -----


def test_door_name_accessor():
    handle, _, _ = _make_handle(door="lobby_west")
    assert handle.door_name == "lobby_west"


def test_counters_start_at_zero():
    handle, _, _ = _make_handle()
    assert handle.state_dispatched() == 0
    assert handle.requests_dispatched() == 0
    assert handle.requests_rejected() == 0
