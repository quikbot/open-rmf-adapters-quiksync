"""Tests for RobotHandle — state caching, lazy registration, and Open-RMF
push when bound."""

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
    assert h.is_prepared() is False
    assert h.updates_pushed() == 0
    assert h.updates_dropped_no_handle() == 0
    assert h.registrations_rejected() == 0


def test_on_state_caches_without_preparation():
    """State updates before prepare_registration() are cached but not pushed."""
    h = RobotHandle("r1")
    h.on_state(sample_state())
    h.on_state(sample_state(battery=42.0))
    assert h.latest_state() is not None
    assert h.latest_state()["battery_percent"] == 42.0
    assert h.is_bound() is False
    assert h.is_prepared() is False
    assert h.updates_pushed() == 0
    assert h.updates_dropped_no_handle() == 2


def test_bind_sets_is_bound():
    """The direct-bind entry point is kept for test injection."""
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    h.bind(mock_handle)
    assert h.is_bound() is True


def test_prepare_registration_sets_is_prepared_not_bound():
    """prepare_registration stashes data but doesn't bind — that
    happens lazily on the first valid state frame."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    robot_config = MagicMock(name="RobotConfiguration")
    callbacks = MagicMock(name="RobotCallbacks")
    h.prepare_registration(fleet_handle, robot_config, callbacks)
    assert h.is_prepared() is True
    assert h.is_bound() is False
    # No add_robot call yet.
    fleet_handle.add_robot.assert_not_called()


def test_first_valid_frame_lazily_calls_add_robot(monkeypatch):
    """A prepared handle invokes add_robot on the first frame whose state
    translates to a non-None RobotState. The returned EasyRobotUpdateHandle
    is bound on the handle; updates_pushed counts the registration as 1."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    update_handle = MagicMock(name="EasyRobotUpdateHandle")
    fleet_handle.add_robot.return_value = update_handle
    robot_config = MagicMock(name="RobotConfiguration")
    callbacks = MagicMock(name="RobotCallbacks")
    h.prepare_registration(fleet_handle, robot_config, callbacks)

    sentinel = object()
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: sentinel)

    h.on_state(sample_state())

    fleet_handle.add_robot.assert_called_once_with(
        "r1", sentinel, robot_config, callbacks,
    )
    assert h.is_bound() is True
    assert h.updates_pushed() == 1
    assert h.registrations_rejected() == 0


def test_add_robot_returning_none_does_not_bind_and_retries(monkeypatch):
    """If add_robot returns None (e.g. off-graph pose), the handle stays
    in the prepared state. A second frame triggers a second attempt."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    update_handle = MagicMock(name="EasyRobotUpdateHandle")
    # First call rejected, second call accepts.
    fleet_handle.add_robot.side_effect = [None, update_handle]
    h.prepare_registration(fleet_handle, MagicMock(), MagicMock())

    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: object())

    h.on_state(sample_state())
    assert h.is_bound() is False
    assert h.registrations_rejected() == 1
    assert h.updates_pushed() == 0

    h.on_state(sample_state(battery=80.0))
    assert h.is_bound() is True
    assert h.registrations_rejected() == 1
    assert h.updates_pushed() == 1
    assert fleet_handle.add_robot.call_count == 2


def test_add_robot_raising_is_caught_and_retries(monkeypatch):
    """An exception inside add_robot doesn't crash on_state. The handle
    stays prepared; the next frame retries."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    update_handle = MagicMock(name="EasyRobotUpdateHandle")
    fleet_handle.add_robot.side_effect = [RuntimeError("boom"), update_handle]
    h.prepare_registration(fleet_handle, MagicMock(), MagicMock())

    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: object())

    h.on_state(sample_state())  # raises internally; counted as rejected
    assert h.is_bound() is False
    assert h.registrations_rejected() == 1

    h.on_state(sample_state())  # second attempt succeeds
    assert h.is_bound() is True


def test_translator_returning_none_during_lazy_register_drops_no_attempt(monkeypatch):
    """If the WSS frame can't be translated (malformed), don't call
    add_robot — the placeholder/empty state would just get rejected.
    Count it under updates_dropped_no_handle so the operator can
    distinguish 'no frames yet' from 'frames but unusable'."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    h.prepare_registration(fleet_handle, MagicMock(), MagicMock())

    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: None)
    h.on_state(sample_state())

    fleet_handle.add_robot.assert_not_called()
    assert h.is_bound() is False
    assert h.registrations_rejected() == 0
    assert h.updates_dropped_no_handle() == 1


def test_subsequent_frames_after_lazy_register_take_update_path(monkeypatch):
    """First frame registers; second + later frames push via update()."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    update_handle = MagicMock(name="EasyRobotUpdateHandle")
    fleet_handle.add_robot.return_value = update_handle
    h.prepare_registration(fleet_handle, MagicMock(), MagicMock())

    sentinel1 = object()
    sentinel2 = object()
    states_iter = iter([sentinel1, sentinel2])
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: next(states_iter))

    h.on_state(sample_state())  # lazy register with sentinel1
    h.on_state(sample_state(battery=80.0))  # update with sentinel2

    fleet_handle.add_robot.assert_called_once()
    args, _ = fleet_handle.add_robot.call_args
    assert args[1] is sentinel1

    update_handle.update.assert_called_once()
    update_args, _ = update_handle.update.call_args
    assert update_args[0] is sentinel2
    assert h.updates_pushed() == 2


def test_set_current_activity_passes_to_update_after_lazy_register(monkeypatch):
    """The current_activity arg should travel through to the update call
    on frames after lazy registration completes."""
    h = RobotHandle("r1")
    fleet_handle = MagicMock(name="FleetHandle")
    update_handle = MagicMock(name="EasyRobotUpdateHandle")
    fleet_handle.add_robot.return_value = update_handle
    h.prepare_registration(fleet_handle, MagicMock(), MagicMock())

    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: "state-sentinel")

    activity = MagicMock(name="ActivityIdentifier")
    h.set_current_activity(activity)

    h.on_state(sample_state())  # lazy register
    h.on_state(sample_state())  # update path

    args, _ = update_handle.update.call_args
    assert args[1] is activity


def test_on_state_after_direct_bind_attempts_rmf_push(monkeypatch):
    """The direct-bind path (test injection) skips the lazy-register flow
    and pushes immediately."""
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    h.bind(mock_handle)

    sentinel = object()
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: sentinel)

    h.on_state(sample_state())

    mock_handle.update.assert_called_once()
    args, kwargs = mock_handle.update.call_args
    assert args[0] is sentinel
    assert args[1] is None
    assert h.updates_pushed() == 1


def test_rmf_update_exception_does_not_propagate(monkeypatch):
    """If Open-RMF's update() raises, we log + count but don't crash."""
    h = RobotHandle("r1")
    mock_handle = MagicMock()
    mock_handle.update.side_effect = RuntimeError("rmf is angry")
    h.bind(mock_handle)
    monkeypatch.setattr(h, "_to_rmf_robot_state", lambda state: "state")
    h.on_state(sample_state())  # must not raise
    assert h.updates_pushed() == 0


def test_to_rmf_robot_state_returns_none_when_rmf_adapter_missing():
    """In CI, rmf_adapter isn't installed → translator returns None.

    This pins the import-time guard: the binary compiles + tests pass
    without rmf_adapter in the environment.
    """
    h = RobotHandle("r1")
    result = h._to_rmf_robot_state(sample_state())
    assert result is None


def test_translator_returns_none_on_malformed_state(monkeypatch):
    """Even with rmf_adapter present, malformed state → None.

    Simulate rmf_adapter being available by stubbing the import.
    """
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
