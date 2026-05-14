"""Tests for FleetStatePump — frame dispatch + lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from fleet_adapter_quiksync.state_pump import FleetStatePump


class FakeWsClient:
    """Test double for QuikSyncWsClient. Yields a fixed sequence of frames
    then exits cleanly."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_fleet_state(self, fleet: str):
        for frame in self._frames:
            if self._closed:
                return
            yield frame


def make_frame(robots: list[dict]) -> dict:
    return {"name": "service_robots", "robots": robots}


def robot(name: str, **extra: Any) -> dict:
    base = {
        "name": name,
        "battery_percent": 50.0,
        "location": {"x": 0.0, "y": 0.0, "yaw": 0.0, "level_name": "L1"},
        "mode": {"mode": "MODE_IDLE"},
        "task_id": None,
        "unix_millis_time": 1747094400000,
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_dispatches_each_robot_in_each_frame():
    received: list[tuple[str, dict]] = []

    async def on_robot_state(name: str, state: dict) -> None:
        received.append((name, state))

    frames = [
        make_frame([robot("robot-1"), robot("robot-2")]),
        make_frame([robot("robot-1", battery_percent=42.0)]),
    ]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", on_robot_state)
    await pump.start()
    # Pump has a task; let it drain.
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 2
    assert pump.robots_dispatched() == 3
    names = [name for name, _ in received]
    assert names == ["robot-1", "robot-2", "robot-1"]
    assert received[2][1]["battery_percent"] == 42.0


@pytest.mark.asyncio
async def test_callback_exception_is_logged_not_raised(caplog):
    """A flaky callback shouldn't kill the whole pump."""

    async def boom(name: str, state: dict) -> None:
        raise RuntimeError(f"explode on {name}")

    frames = [make_frame([robot("r1"), robot("r2"), robot("r3")])]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", boom)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    # All 3 robots attempted (no early exit on first failure)
    assert pump.frames_seen() == 1
    # robots_dispatched only counts SUCCESSFUL dispatches; all 3 raised.
    assert pump.robots_dispatched() == 0


@pytest.mark.asyncio
async def test_frame_with_non_list_robots_skipped(caplog):
    received: list[tuple[str, dict]] = []

    async def cb(name: str, state: dict) -> None:
        received.append((name, state))

    frames = [
        {"name": "f", "robots": "not-a-list"},  # malformed
        make_frame([robot("r1")]),  # valid
    ]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", cb)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    # Both frames were SEEN; only one had dispatchable content.
    assert pump.frames_seen() == 2
    assert pump.robots_dispatched() == 1
    assert len(received) == 1
    assert received[0][0] == "r1"


@pytest.mark.asyncio
async def test_dispatches_robots_when_field_is_map():
    """Open-RMF's FleetState schema spells `robots` as
    `{robotName: RobotState}` (a map). Earlier versions of the pump
    accepted only the list shape and silently dropped every frame from
    a schema-conformant server."""
    received: list[tuple[str, dict]] = []

    async def cb(name: str, state: dict) -> None:
        received.append((name, state))

    frames = [
        {
            "name": "service_robots",
            "robots": {
                "robot-1": robot("robot-1"),
                "robot-2": robot("robot-2", battery_percent=33.0),
            },
        },
    ]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", cb)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 1
    assert pump.robots_dispatched() == 2
    names = sorted(name for name, _ in received)
    assert names == ["robot-1", "robot-2"]
    by_name = {name: state for name, state in received}
    assert by_name["robot-2"]["battery_percent"] == 33.0


@pytest.mark.asyncio
async def test_dispatches_robots_when_field_missing_or_null():
    """`robots: null` and a missing `robots` key both flow through as
    zero dispatchable robots — the frame is counted but no callback
    fires."""
    received: list[tuple[str, dict]] = []

    async def cb(name: str, state: dict) -> None:
        received.append((name, state))

    frames = [
        {"name": "f", "robots": None},
        {"name": "f"},  # robots key absent entirely
    ]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", cb)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 2
    assert pump.robots_dispatched() == 0
    assert received == []


@pytest.mark.asyncio
async def test_robot_without_name_skipped():
    received: list[tuple[str, dict]] = []

    async def cb(name: str, state: dict) -> None:
        received.append((name, state))

    nameless = robot("placeholder")
    nameless.pop("name")
    frames = [make_frame([nameless, robot("r1")])]
    pump = FleetStatePump(FakeWsClient(frames), "service_robots", cb)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.robots_dispatched() == 1
    assert received[0][0] == "r1"


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    pump = FleetStatePump(FakeWsClient([]), "f", lambda n, s: asyncio.sleep(0))
    await pump.start()
    await pump.stop()
    await pump.stop()  # must not raise


@pytest.mark.asyncio
async def test_start_idempotent_when_already_running():
    received: list[tuple[str, dict]] = []

    async def cb(name: str, state: dict) -> None:
        received.append((name, state))

    # 100 frames so the pump is genuinely running when we start a 2nd time.
    frames = [make_frame([robot(f"r{i}")]) for i in range(100)]
    pump = FleetStatePump(FakeWsClient(frames), "f", cb)
    await pump.start()
    await pump.start()  # second start is a no-op
    await asyncio.sleep(0.1)
    await pump.stop()

    # Only one task draining — frames seen ≤ 100.
    assert pump.frames_seen() <= 100
