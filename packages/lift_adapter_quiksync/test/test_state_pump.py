"""Tests for LiftStatePump — frame dispatch + lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from lift_adapter_quiksync.state_pump import LiftStatePump


class FakeWsClient:
    """Test double for QuikSyncWsClient. Yields a fixed sequence of frames
    then exits cleanly."""

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


def make_lift_state(
    lift: str,
    current_floor: str = "L1",
    current_mode: int = 2,
    session_id: str = "",
    destination_floor: str = "",
) -> dict:
    """Build a LiftState-shaped dict per rmf_lift_msgs.

    `current_mode = 2` is MODE_AGV (normal); `4` is MODE_OFFLINE / fault.
    """
    return {
        "lift_name": lift,
        "lift_time": {"sec": 1747094400, "nanosec": 0},
        "current_floor": current_floor,
        "destination_floor": destination_floor,
        "door_state": 2,  # OPEN
        "motion_state": 0,  # STOPPED
        "available_modes": [{"value": 2}, {"value": 4}],
        "current_mode": {"value": current_mode},
        "session_id": session_id,
    }


@pytest.mark.asyncio
async def test_dispatches_each_frame():
    received: list[tuple[str, dict]] = []

    async def on_state(lift: str, frame: dict) -> None:
        received.append((lift, frame))

    frames = [
        make_lift_state("lift_alpha", current_floor="L1", session_id=""),
        make_lift_state("lift_alpha", current_floor="L2", session_id="rmf:robot-1",
                        destination_floor="L2"),
        make_lift_state("lift_alpha", current_floor="L3", session_id=""),
    ]
    pump = LiftStatePump(FakeWsClient(frames), "lift_alpha", on_state)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 3
    assert pump.frames_dispatched() == 3
    assert [l for l, _ in received] == ["lift_alpha", "lift_alpha", "lift_alpha"]
    assert received[1][1]["session_id"] == "rmf:robot-1"


@pytest.mark.asyncio
async def test_callback_exception_is_logged_not_raised():
    """A flaky callback shouldn't kill the pump — it should keep
    pumping subsequent frames."""
    call_count = 0

    async def flaky(lift: str, frame: dict) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"explode on {lift}")

    frames = [
        make_lift_state("lift_alpha"),
        make_lift_state("lift_alpha", current_floor="L2"),
        make_lift_state("lift_alpha", current_floor="L3"),
    ]
    pump = LiftStatePump(FakeWsClient(frames), "lift_alpha", flaky)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 3
    assert pump.frames_dispatched() == 0
    assert call_count == 3


@pytest.mark.asyncio
async def test_fault_frame_is_forwarded():
    """MODE_OFFLINE (current_mode=4) frames must reach the callback so
    the adapter can publish the fault state to ROS."""
    received: list[dict] = []

    async def on_state(lift: str, frame: dict) -> None:
        received.append(frame)

    frames = [make_lift_state("lift_alpha", current_mode=4)]
    pump = LiftStatePump(FakeWsClient(frames), "lift_alpha", on_state)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()

    assert pump.frames_seen() == 1
    assert pump.frames_dispatched() == 1
    assert received[0]["current_mode"]["value"] == 4


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    async def noop(lift: str, frame: dict) -> None:
        pass

    pump = LiftStatePump(FakeWsClient([]), "lift_alpha", noop)
    await pump.start()
    await pump.stop()
    await pump.stop()  # must not raise


@pytest.mark.asyncio
async def test_start_idempotent_when_already_running():
    received: list[dict] = []

    async def on_state(lift: str, frame: dict) -> None:
        received.append(frame)

    frames = [make_lift_state("lift_alpha") for _ in range(100)]
    pump = LiftStatePump(FakeWsClient(frames), "lift_alpha", on_state)
    await pump.start()
    await pump.start()  # second start is a no-op
    await asyncio.sleep(0.1)
    await pump.stop()

    assert pump.frames_seen() <= 100


@pytest.mark.asyncio
async def test_no_frames_is_clean_lifecycle():
    async def on_state(lift: str, frame: dict) -> None:
        pass

    pump = LiftStatePump(FakeWsClient([]), "lift_alpha", on_state)
    await pump.start()
    await asyncio.sleep(0.05)
    await pump.stop()
    assert pump.frames_seen() == 0
    assert pump.frames_dispatched() == 0
