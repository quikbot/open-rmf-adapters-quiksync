"""Tests for callbacks.py — the three RobotCallbacks factories.

rmf_adapter isn't installed in CI, so each test that touches a
`Destination` shape mocks it as a `SimpleNamespace`. The callbacks only
read structural attributes (`map`, `position.x/y/yaw`, `dock`,
`speed_limit`) — no class identity check — so this is sufficient.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fleet_adapter_quiksync.callbacks import (
    _activity_identifier,
    _dock_name,
    _extract_destination,
    _new_execution_id,
    _speed_limit,
    make_action_executor,
    make_navigate_callback,
    make_stop_callback,
)
from fleet_adapter_quiksync.robot_handle import RobotHandle
from quiksync_client import (
    QuikSyncClientError,
    QuikSyncConnectionError,
    QuikSyncServerError,
)


def fake_destination(
    map_name: str = "L1",
    x: float = 12.3,
    y: float = 4.5,
    yaw: float = 1.57,
    dock: str = "",
    speed_limit: float = 0.0,
) -> SimpleNamespace:
    """Build a fake rmf_adapter.Destination — only the attrs callbacks read."""
    return SimpleNamespace(
        map=map_name,
        position=SimpleNamespace(x=x, y=y, yaw=yaw),
        dock=dock,
        speed_limit=speed_limit,
    )


# ----- helper tests -----


def test_new_execution_id_uuid_v4():
    eid = _new_execution_id()
    assert isinstance(eid, str)
    # uuid4 strings are 36 chars with 4 hyphens
    assert len(eid) == 36
    assert eid.count("-") == 4


def test_extract_destination_happy_path():
    body = _extract_destination(fake_destination(x=1.0, y=2.0, yaw=0.5, map_name="basement"))
    assert body == {"x": 1.0, "y": 2.0, "yaw": 0.5, "map_name": "basement"}


def test_extract_destination_uses_index_fallback_for_list_position():
    """Some rmf_adapter versions expose position as a list/tuple."""
    dest = SimpleNamespace(map="L2", position=[3.0, 4.0, 1.0], dock="", speed_limit=0.0)
    body = _extract_destination(dest)
    assert body == {"x": 3.0, "y": 4.0, "yaw": 1.0, "map_name": "L2"}


def test_extract_destination_returns_none_on_missing_map():
    assert _extract_destination(fake_destination(map_name="")) is None


def test_extract_destination_returns_none_on_missing_position():
    assert _extract_destination(SimpleNamespace(map="L1", position=None, dock="", speed_limit=0.0)) is None


def test_extract_destination_returns_none_on_bad_position_value():
    dest = SimpleNamespace(
        map="L1",
        position=SimpleNamespace(x="not-a-number", y=4.5, yaw=0.0),
        dock="",
        speed_limit=0.0,
    )
    assert _extract_destination(dest) is None


def test_dock_name_returns_none_on_empty_string():
    """Open-RMF passes '' when there's no dock; we want None so the server
    dispatches MOVE rather than DOCK."""
    assert _dock_name(fake_destination(dock="")) is None


def test_dock_name_returns_string_when_set():
    assert _dock_name(fake_destination(dock="charger_3")) == "charger_3"


def test_speed_limit_returns_none_on_zero():
    """Open-RMF passes 0.0 for no limit; we want None to omit it."""
    assert _speed_limit(fake_destination(speed_limit=0.0)) is None


def test_speed_limit_returns_float_when_positive():
    assert _speed_limit(fake_destination(speed_limit=0.5)) == 0.5


def test_activity_identifier_tries_known_attrs():
    """We try `identifier`, then `activity`, then `activity_identifier`."""
    exec_a = SimpleNamespace(identifier="id-1")
    assert _activity_identifier(exec_a) == "id-1"

    exec_b = SimpleNamespace(activity="act-2")
    assert _activity_identifier(exec_b) == "act-2"

    exec_c = SimpleNamespace(activity_identifier="aid-3")
    assert _activity_identifier(exec_c) == "aid-3"

    exec_none = SimpleNamespace()
    assert _activity_identifier(exec_none) is None


# ----- navigate callback -----


def test_navigate_dispatches_post_navigate():
    http = MagicMock()
    http.post_navigate.return_value = {"task_id": "quiksync-cmd:01HV5T", "execution_id": "eid-fixed", "status": "queued"}
    handle = RobotHandle("r1")

    navigate = make_navigate_callback(
        http=http, fleet="service_robots", robot="r1", handle=handle,
        execution_id_factory=lambda: "eid-fixed",
    )

    execution = SimpleNamespace(identifier="rmf-activity-1")
    navigate(fake_destination(map_name="L1", x=1.0, y=2.0, yaw=0.5), execution)

    http.post_navigate.assert_called_once_with(
        fleet="service_robots",
        robot="r1",
        execution_id="eid-fixed",
        destination={"x": 1.0, "y": 2.0, "yaw": 0.5, "map_name": "L1"},
        dock_name=None,
        speed_limit=None,
        namespace=None,
    )
    # current_activity threaded through so the state pump can correlate
    assert handle._current_activity == "rmf-activity-1"


def test_navigate_passes_dock_name_and_speed_limit():
    http = MagicMock()
    http.post_navigate.return_value = {}
    handle = RobotHandle("r1")
    navigate = make_navigate_callback(
        http=http, fleet="f", robot="r1", handle=handle, execution_id_factory=lambda: "eid",
    )

    navigate(fake_destination(dock="charger_3", speed_limit=0.4), SimpleNamespace())

    _, kwargs = http.post_navigate.call_args
    assert kwargs["dock_name"] == "charger_3"
    assert kwargs["speed_limit"] == 0.4


def test_navigate_ignores_unusable_destination():
    """Missing map → no POST, no crash."""
    http = MagicMock()
    handle = RobotHandle("r1")
    navigate = make_navigate_callback(http=http, fleet="f", robot="r1", handle=handle)
    navigate(SimpleNamespace(map="", position=None, dock="", speed_limit=0.0), SimpleNamespace())
    http.post_navigate.assert_not_called()


def test_navigate_swallows_client_error():
    """A 400 from the server (e.g. coord_navigate_not_supported) shouldn't crash the adapter."""
    http = MagicMock()
    http.post_navigate.side_effect = QuikSyncClientError(400, "coord_navigate_not_supported", {"message": "..."})
    handle = RobotHandle("r1")
    navigate = make_navigate_callback(http=http, fleet="f", robot="r1", handle=handle)
    # Must not raise
    navigate(fake_destination(), SimpleNamespace())


def test_navigate_swallows_server_and_connection_errors():
    http = MagicMock()
    http.post_navigate.side_effect = QuikSyncServerError(503, "service unavailable")
    handle = RobotHandle("r1")
    navigate = make_navigate_callback(http=http, fleet="f", robot="r1", handle=handle)
    navigate(fake_destination(), SimpleNamespace())  # no raise

    http.post_navigate.side_effect = QuikSyncConnectionError("network")
    navigate(fake_destination(), SimpleNamespace())  # no raise


# ----- stop callback -----


def test_stop_dispatches_post_stop():
    http = MagicMock()
    http.post_stop.return_value = {}
    handle = RobotHandle("r1")
    stop = make_stop_callback(
        http=http, fleet="f", robot="r1", handle=handle,
        execution_id_factory=lambda: "stop-eid",
    )

    stop(None)

    http.post_stop.assert_called_once_with(
        fleet="f", robot="r1", execution_id="stop-eid", namespace=None,
    )


def test_stop_swallows_errors():
    http = MagicMock()
    http.post_stop.side_effect = QuikSyncClientError(404, "not_found", {})
    handle = RobotHandle("r1")
    stop = make_stop_callback(http=http, fleet="f", robot="r1", handle=handle)
    stop(None)  # no raise


def test_stop_does_not_touch_current_activity():
    """Stop is server-initiated cancel; the state pump frame will clear
    current_activity when the server flips the command's state."""
    http = MagicMock()
    http.post_stop.return_value = {}
    handle = RobotHandle("r1")
    handle.set_current_activity("existing-activity")
    stop = make_stop_callback(http=http, fleet="f", robot="r1", handle=handle)
    stop(None)
    assert handle._current_activity == "existing-activity"


# ----- action_executor callback -----


def test_action_executor_dispatches_post_perform_action():
    http = MagicMock()
    http.post_perform_action.return_value = {"task_id": "quiksync-cmd:01...", "status": "queued"}
    handle = RobotHandle("r1")
    action = make_action_executor(
        http=http, fleet="f", robot="r1", handle=handle,
        execution_id_factory=lambda: "act-eid",
    )

    description = {"zone_id": "lobby_west", "duration_seconds": 600}
    execution = SimpleNamespace(identifier="rmf-act-1")
    action("clean", description, execution)

    http.post_perform_action.assert_called_once_with(
        fleet="f", robot="r1", execution_id="act-eid",
        category="clean", description=description, namespace=None,
    )
    assert handle._current_activity == "rmf-act-1"


def test_action_executor_swallows_unknown_category_400():
    """Unknown category from server → 400 → logged + no crash."""
    http = MagicMock()
    http.post_perform_action.side_effect = QuikSyncClientError(400, "unknown_action_category", {})
    handle = RobotHandle("r1")
    action = make_action_executor(http=http, fleet="f", robot="r1", handle=handle)
    action("mystery", {}, SimpleNamespace())  # no raise


def test_action_executor_swallows_transport_errors():
    http = MagicMock()
    http.post_perform_action.side_effect = QuikSyncConnectionError("offline")
    handle = RobotHandle("r1")
    action = make_action_executor(http=http, fleet="f", robot="r1", handle=handle)
    action("clean", {}, SimpleNamespace())  # no raise
