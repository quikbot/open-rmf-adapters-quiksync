"""Tests for DoorStatePump — frame dispatch + lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from door_adapter_quiksync.state_pump import DoorStatePump


class FakeWsClient:
    """Test double for QuikSyncWsClient. Yields a fixed sequence of frames
    then exits cleanly."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def subscribe_door_state(self, door: str):
        for frame in self._frames:
            if self._closed:
                return
            yield frame


def make_door_state(door: str, current_mode: int = 0) -> dict:
    """Build a DoorState-shaped dict per rmf_door_msgs."""
    return {
        "door_name": door,
        "door_time": {"sec": 1747094400, "nanosec": 0},
        "current_mode": {"value": current_mode},
    }


@pytest.mark.asyncio
async def test_dispatches_each_frame():
    received: list[tuple[str, dict]] = []

    async def on_state(door: str, frame: dict) -> None:
        received.append((door, frame))

    frames = [
        make_door_state("door_alpha", current_mode=0),  # CLOSED
        make_door_state("door_alpha", current_mode=2),  # OPEN
    ]
    pump = DoorStatePump(FakeWsClient(frames), "door_alpha", on_state)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 2
    assert pump.frames_dispatched() == 2
    assert [d for d, _ in received] == ["door_alpha", "door_alpha"]
    assert received[0][1]["current_mode"]["value"] == 0
    assert received[1][1]["current_mode"]["value"] == 2


@pytest.mark.asyncio
async def test_callback_exception_is_logged_not_raised():
    """A flaky callback shouldn't kill the pump — it should keep
    pumping subsequent frames."""

    call_count = 0

    async def flaky(door: str, frame: dict) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"explode on {door}")

    frames = [
        make_door_state("door_alpha"),
        make_door_state("door_alpha", current_mode=2),
        make_door_state("door_alpha", current_mode=0),
    ]
    pump = DoorStatePump(FakeWsClient(frames), "door_alpha", flaky)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    # All 3 frames were SEEN; callback raised on each so none counted dispatched.
    assert pump.frames_seen() == 3
    assert pump.frames_dispatched() == 0
    assert call_count == 3


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    async def noop(door: str, frame: dict) -> None:
        pass

    pump = DoorStatePump(FakeWsClient([]), "door_alpha", noop)
    await pump.start()
    await pump.stop()
    await pump.stop()  # must not raise


@pytest.mark.asyncio
async def test_start_idempotent_when_already_running():
    received: list[dict] = []

    async def on_state(door: str, frame: dict) -> None:
        received.append(frame)

    # Many frames so the pump is genuinely running when we start again.
    frames = [make_door_state("door_alpha") for _ in range(100)]
    pump = DoorStatePump(FakeWsClient(frames), "door_alpha", on_state)
    await pump.start()
    await pump.start()  # second start is a no-op
    await asyncio.sleep(0.1)
    await pump.stop()

    # Only one task draining — frames seen ≤ 100.
    assert pump.frames_seen() <= 100


@pytest.mark.asyncio
async def test_no_frames_is_clean_lifecycle():
    """Empty subscription drains immediately + stops cleanly."""

    async def on_state(door: str, frame: dict) -> None:
        pass

    pump = DoorStatePump(FakeWsClient([]), "door_alpha", on_state)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()
    assert pump.frames_seen() == 0
    assert pump.frames_dispatched() == 0


@pytest.mark.asyncio
async def test_partial_callback_failure_keeps_counter_consistent():
    """Mixed success + failure: dispatched counter only reflects success."""
    success_calls = 0

    async def sometimes(door: str, frame: dict) -> None:
        nonlocal success_calls
        if frame["current_mode"]["value"] == 99:
            raise RuntimeError("bad frame")
        success_calls += 1

    frames = [
        make_door_state("door_alpha", current_mode=0),
        make_door_state("door_alpha", current_mode=99),  # callback raises
        make_door_state("door_alpha", current_mode=2),
    ]
    pump = DoorStatePump(FakeWsClient(frames), "door_alpha", sometimes)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 3
    assert pump.frames_dispatched() == 2
    assert success_calls == 2
