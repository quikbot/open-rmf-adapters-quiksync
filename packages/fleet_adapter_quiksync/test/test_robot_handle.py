"""Tests for RobotHandle — state caching + Open-RMF push when bound."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fleet_adapter_quiksync.robot_handle import RobotHandle


def sample_state(name: str = "robot-1", battery: float = 87.5) -> dict[str, Any]:
    return {
        "name": name,
        "battery_percent": battery,
        "location": {"x": 12.3, "y": 4.5, "yaw": 1.57, "level_name": "L1"},
        "mode": {"mode": "MODE_IDLE"},
        "task_id": None,
        "unix_millis_time": 1747094400000,
    }


def test_starts_with_no_state_no_handle():
    h = RobotHandle("r1")
    assert h.latest_state() is None
    assert h.is_bound() is False
    assert h.updates_pushed() == 0
    assert h.updates_dropped_no_handle() == 0


def test_on_state_caches_without_binding():
    """State updates before bind() are cached but not pushed."""
    h = RobotHandle("r1")
    h.on_state(sample_state())
    h.on_state(sample_state(battery=42.0))
    assert h.latest_state() is not None
    assert h.latest_state()["battery_percent"] == 42.0
    assert h.is_bound() is False
    assert h.updates_pushed() == 0
    assert h.updates_dropped_no_handle() == 2


def test_bind_sets_is_bound():
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    h.bind(mock_handle)
    assert h.is_bound() is True


def test_on_state_after_bind_attempts_rmf_push(monkeypatch):
    """Once bound, on_state should call rmf_handle.update().

    rmf_adapter isn't importable in CI; `_to_rmf_robot_state` returns
    None on import failure → no update call. We patch the translator
    to return a sentinel so we can verify the update path fires."""
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    h.bind(mock_handle)

    # Replace the translator so the test doesn't need rmf_adapter installed.
    sentinel = object()
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: sentinel)

    h.on_state(sample_state())

    mock_handle.update.assert_called_once()
    args, kwargs = mock_handle.update.call_args
    assert args[0] is sentinel  # the rmf RobotState
    assert args[1] is None  # current_activity (not set)
    assert h.updates_pushed() == 1


def test_set_current_activity_passes_to_update(monkeypatch):
    """The current_activity arg should travel through to the next update."""
    h = RobotHandle("r1")
    h.bind(MagicMock())
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: "state-sentinel")
    activity = MagicMock(name="ActivityIdentifier")
    h.set_current_activity(activity)
    h.on_state(sample_state())
    args, _ = h._rmf_handle.update.call_args
    assert args[1] is activity


def test_rmf_update_exception_does_not_propagate(monkeypatch):
    """If Open-RMF's update() raises, we log + count but don't crash."""
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    mock_handle.update.side_effect = RuntimeError("rmf is angry")
    h.bind(mock_handle)
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: "state")
    h.on_state(sample_state())  # must not raise
    # Failed push doesn't count as success
    assert h.updates_pushed() == 0


def test_to_rmf_robot_state_returns_none_when_rmf_adapter_missing():
    """In CI, rmf_adapter isn't installed → translator returns None.

    This pins the import-time guard: the binary compiles + tests pass
    without rmf_adapter in the environment.
    """
    h = RobotHandle("r1")
    result = h._to_rmf_robot_state(sample_state())
    # rmf_adapter is not present in this test environment
    assert result is None


def test_translator_returns_none_on_malformed_state(monkeypatch):
    """Even with rmf_adapter present, malformed state → None.

    Simulate rmf_adapter being available by stubbing the import.
    """
    # Inject a fake rmf_adapter module so the lazy import succeeds
    import sys
    import types

    fake_mod = types.ModuleType("rmf_adapter")
    fake_type_mod = types.ModuleType("rmf_adapter.type")

    class FakeRobotState:
        def __init__(self, level: str, pos: Any, soc: float) -> None:
            self.level = level
            self.pos = pos
            self.soc = soc

    class FakeVector3d:
        def __init__(self, x: float, y: float, yaw: float) -> None:
            self.x, self.y, self.yaw = x, y, yaw

    fake_mod.RobotState = FakeRobotState
    fake_type_mod.Vector3d = FakeVector3d

    monkeypatch.setitem(sys.modules, "rmf_adapter", fake_mod)
    monkeypatch.setitem(sys.modules, "rmf_adapter.type", fake_type_mod)

    h = RobotHandle("r1")

    # Missing location entirely
    assert h._to_rmf_robot_state({"name": "r1"}) is None
    # Missing battery_percent
    state = sample_state()
    del state["battery_percent"]
    assert h._to_rmf_robot_state(state) is None
    # Bad type for battery_percent
    state = sample_state()
    state["battery_percent"] = "not a number"
    assert h._to_rmf_robot_state(state) is None
    # Bad x in location
    state = sample_state()
    state["location"]["x"] = "x"
    assert h._to_rmf_robot_state(state) is None
    # Valid state
    valid_state = sample_state()
    valid = h._to_rmf_robot_state(valid_state)
    assert valid is not None
    assert valid.level == "L1"
    assert valid.soc == 0.875  # 87.5 / 100
