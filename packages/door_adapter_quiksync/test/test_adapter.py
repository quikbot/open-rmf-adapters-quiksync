"""Tests for adapter.py — main / bootstrap / dry-run + full-mode dispatch.

CI cannot exercise the rclpy path; `sys.modules` injection of fake
`rclpy` + `rmf_door_msgs.msg` + `builtin_interfaces.msg` modules
lets us cover the wire-up structure end-to-end without a ROS install.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from door_adapter_quiksync.adapter import (
    _try_import_door_msgs,
    _try_import_rclpy,
    build_clients,
    main,
)
from door_adapter_quiksync.config import DoorAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "doors": ["door_alpha", "door_beta"],
}


def make_config() -> DoorAdapterConfig:
    return DoorAdapterConfig.from_dict(dict(REQUIRED))


# ----- lazy imports -----
#
# `rclpy` IS available in CI's ros:jazzy-ros-base image; `rmf_door_msgs`
# is NOT (it's part of rmf_internal_msgs, only in the full rmf_ros2 stack).
# So we can't assert "returns None in CI" for rclpy. Instead, test that
# the helpers degrade cleanly when the import genuinely fails (simulated
# via builtins.__import__ monkeypatching).


def test_try_import_rclpy_returns_none_when_import_fails(monkeypatch):
    """The helper must swallow ImportError, log, and return None — the
    full-mode path then falls through to dry-run."""
    import builtins
    real_import = builtins.__import__

    def faulty_import(name, *args, **kwargs):
        if name == "rclpy":
            raise ImportError("synthetic: rclpy unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", faulty_import)
    assert _try_import_rclpy() is None


def test_try_import_door_msgs_returns_none_in_ci():
    """rmf_door_msgs is not in ros:jazzy-ros-base (only in the full
    rmf_ros2 stack), so the import genuinely fails in CI → None."""
    assert _try_import_door_msgs() is None


def test_try_import_door_msgs_returns_none_when_import_fails(monkeypatch):
    """Belt + suspenders for environments where rmf_door_msgs IS on
    PATH (e.g. local dev with rmf_ros2 sourced) — simulate the missing
    import and verify the helper still degrades cleanly."""
    import builtins
    real_import = builtins.__import__

    def faulty_import(name, *args, **kwargs):
        if name == "rmf_door_msgs.msg":
            raise ImportError("synthetic: rmf_door_msgs unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", faulty_import)
    assert _try_import_door_msgs() is None


# ----- build_clients -----


def test_build_clients_returns_three_clients():
    config = make_config()
    auth, http, ws = build_clients(config)
    assert auth is not None
    assert http is not None
    assert ws is not None
    auth.close()
    http.close()


# ----- main: dry-run path (no rclpy / no rmf_door_msgs) -----


def test_main_missing_config_returns_1(monkeypatch):
    """Without --config or env vars → ConfigError → return 1."""
    for key in list(__import__("os").environ.keys()):
        if key.startswith("DOOR_ADAPTER_"):
            monkeypatch.delenv(key)
    rc = main([])
    assert rc == 1


def test_main_dry_run_drains_frames(monkeypatch, tmp_path):
    """With valid config + --dry-run, main spawns pumps and drains
    the simulated WS stream. Returns 0 when frames are seen."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "doors")
    yaml_text += "\ndoors:\n  - door_alpha\n  - door_beta\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(yaml_text)

    # Monkey-patch the WS client's subscribe to yield a synthetic frame
    # for each door.
    from quiksync_client import QuikSyncWsClient

    yielded: list[str] = []

    async def fake_subscribe(self, door: str, namespace=None):
        yielded.append(door)
        yield {
            "door_name": door,
            "door_time": 1000,
            "current_mode": {"value": 0},
        }

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_door_state", fake_subscribe)

    rc = main(["--config", str(cfg_file), "--dry-run", "--dry-run-seconds", "1.0"])
    assert rc == 0
    # Both doors had a pump attached
    assert sorted(yielded) == ["door_alpha", "door_beta"]


def test_main_dry_run_returns_2_when_no_frames(monkeypatch, tmp_path):
    """Empty WS stream + dry-run-seconds=0.2 → return 2."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "doors")
    yaml_text += "\ndoors:\n  - door_alpha\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncWsClient

    async def empty_subscribe(self, door: str, namespace=None):
        return
        yield  # unreachable; makes this an async generator

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_door_state", empty_subscribe)

    rc = main(["--config", str(cfg_file), "--dry-run", "--dry-run-seconds", "0.2"])
    assert rc == 2


# ----- main: full-mode dispatch via sys.modules injection -----


def _install_fake_rclpy_modules(monkeypatch) -> tuple[Any, Any]:
    """Inject fake rclpy + rmf_door_msgs + builtin_interfaces modules
    so `_try_import_*` succeeds and `_run_full` runs against fakes.

    Returns `(fake_rclpy, fake_msgs_module)` so the test can inspect
    the recorded calls.
    """

    # ----- builtin_interfaces.msg -----
    class _Time:
        def __init__(self, sec=0, nanosec=0):
            self.sec = sec
            self.nanosec = nanosec

    bi_mod = ModuleType("builtin_interfaces")
    bi_msg_mod = ModuleType("builtin_interfaces.msg")
    bi_msg_mod.Time = _Time
    bi_mod.msg = bi_msg_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "builtin_interfaces", bi_mod)
    monkeypatch.setitem(sys.modules, "builtin_interfaces.msg", bi_msg_mod)

    # ----- rmf_door_msgs.msg -----
    class _DoorMode:
        def __init__(self, value=0):
            self.value = value

    class _DoorState:
        def __init__(self, door_name="", door_time=None, current_mode=None):
            self.door_name = door_name
            self.door_time = door_time
            self.current_mode = current_mode

    class _DoorRequest:
        pass

    door_mod = ModuleType("rmf_door_msgs")
    door_msg_mod = ModuleType("rmf_door_msgs.msg")
    door_msg_mod.DoorMode = _DoorMode
    door_msg_mod.DoorState = _DoorState
    door_msg_mod.DoorRequest = _DoorRequest
    door_mod.msg = door_msg_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rmf_door_msgs", door_mod)
    monkeypatch.setitem(sys.modules, "rmf_door_msgs.msg", door_msg_mod)

    # ----- rclpy + rclpy.executors -----
    spin_recorded: dict[str, Any] = {"spun": False}

    class _Publisher:
        def __init__(self) -> None:
            self.published: list[Any] = []
        def publish(self, msg):
            self.published.append(msg)

    class _Subscription:
        pass

    class _Logger:
        def warning(self, *a, **kw): pass
        def info(self, *a, **kw): pass

    class _Node:
        def __init__(self, name: str) -> None:
            self.name = name
            self.publisher = _Publisher()
            self.subscriber_callback = None
            self.destroyed = False
        def create_publisher(self, msg_type, topic, qos):
            return self.publisher
        def create_subscription(self, msg_type, topic, callback, qos):
            self.subscriber_callback = callback
            return _Subscription()
        def get_logger(self) -> _Logger:
            return _Logger()
        def destroy_node(self) -> None:
            self.destroyed = True

    class _Executor:
        def __init__(self) -> None:
            self.added: list[Any] = []
        def add_node(self, node):
            self.added.append(node)
        def spin(self):
            spin_recorded["spun"] = True
            # Don't actually block; exit immediately so the test ends.
        def shutdown(self):
            pass

    executors_mod = ModuleType("rclpy.executors")
    executors_mod.SingleThreadedExecutor = _Executor

    rclpy_mod = ModuleType("rclpy")
    rclpy_mod.executors = executors_mod  # type: ignore[attr-defined]
    rclpy_mod._spin_recorded = spin_recorded  # type: ignore[attr-defined]
    rclpy_mod._node_class = _Node  # type: ignore[attr-defined]
    rclpy_mod.init = lambda *a, **kw: None
    rclpy_mod.shutdown = lambda *a, **kw: None
    rclpy_mod.create_node = lambda name: _Node(name)
    monkeypatch.setitem(sys.modules, "rclpy", rclpy_mod)
    monkeypatch.setitem(sys.modules, "rclpy.executors", executors_mod)

    return rclpy_mod, door_msg_mod


def test_main_full_mode_constructs_node_and_spins(monkeypatch, tmp_path):
    """End-to-end: sys.modules-injected rclpy + rmf_door_msgs allow
    _run_full to construct the publisher + subscriber + DoorAdapterNode
    and call executor.spin(). Verify the wire-up happens."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "doors")
    yaml_text += "\ndoors:\n  - door_alpha\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(yaml_text)

    # Synthetic WSS that exits immediately so the pump has nothing to
    # do and the adapter's stop() completes quickly.
    from quiksync_client import QuikSyncWsClient

    async def empty_subscribe(self, door: str, namespace=None):
        return
        yield  # unreachable; async generator marker

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_door_state", empty_subscribe)

    rclpy_mod, msgs_mod = _install_fake_rclpy_modules(monkeypatch)

    rc = main(["--config", str(cfg_file)])
    assert rc == 0
    assert rclpy_mod._spin_recorded["spun"] is True


def test_main_full_mode_route_request_dispatches_to_handle(monkeypatch, tmp_path):
    """The subscriber-callback path: when a fake DoorRequest msg is
    passed to the registered callback, it routes through to a real
    DoorHandle.dispatch_request → real http_client.post_door_request."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "doors")
    yaml_text += "\ndoors:\n  - door_alpha\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient

    async def empty_subscribe(self, door: str, namespace=None):
        return
        yield

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_door_state", empty_subscribe)

    posts: list[dict] = []

    def fake_post_door_request(self, *, door, requester_id, requested_mode, execution_id,
                               namespace=None):
        posts.append({
            "door": door,
            "requester_id": requester_id,
            "requested_mode": requested_mode,
            "execution_id": execution_id,
        })
        return {"status": "accepted"}

    monkeypatch.setattr(QuikSyncHttpClient, "post_door_request", fake_post_door_request)

    rclpy_mod, msgs_mod = _install_fake_rclpy_modules(monkeypatch)

    # Capture the node that gets built so we can grab its subscriber
    # callback after main() runs.
    created_nodes: list[Any] = []
    orig_create_node = rclpy_mod.create_node

    def trace_create_node(name):
        n = orig_create_node(name)
        created_nodes.append(n)
        return n

    monkeypatch.setattr(rclpy_mod, "create_node", trace_create_node)

    rc = main(["--config", str(cfg_file)])
    assert rc == 0
    assert len(created_nodes) == 1
    callback = created_nodes[0].subscriber_callback
    assert callback is not None

    # Simulate an inbound DoorRequest.
    msg = SimpleNamespace(
        door_name="door_alpha",
        requester_id="rmf:robot-1",
        requested_mode=SimpleNamespace(value=2),
    )
    assert callback(msg) is True
    assert posts == [{
        "door": "door_alpha",
        "requester_id": "rmf:robot-1",
        "requested_mode": "OPEN",
        "execution_id": posts[0]["execution_id"],  # uuid; just shape-check
    }]


def test_main_full_mode_unknown_door_request_is_silently_dropped(monkeypatch, tmp_path):
    """DoorRequest for a door not in `doors:` config returns False; no
    HTTP POST, no log warning."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "doors")
    yaml_text += "\ndoors:\n  - door_alpha\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient

    async def empty_subscribe(self, door: str, namespace=None):
        return
        yield

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_door_state", empty_subscribe)

    posts: list[dict] = []

    def fake_post_door_request(self, **kwargs):
        posts.append(kwargs)
        return {}

    monkeypatch.setattr(QuikSyncHttpClient, "post_door_request", fake_post_door_request)

    rclpy_mod, _ = _install_fake_rclpy_modules(monkeypatch)
    created_nodes: list[Any] = []
    orig_create_node = rclpy_mod.create_node

    def trace_create_node(name):
        n = orig_create_node(name)
        created_nodes.append(n)
        return n

    monkeypatch.setattr(rclpy_mod, "create_node", trace_create_node)

    main(["--config", str(cfg_file)])
    callback = created_nodes[0].subscriber_callback
    foreign = SimpleNamespace(
        door_name="door_zulu",  # not in our config
        requester_id="rmf:r1",
        requested_mode=SimpleNamespace(value=2),
    )
    assert callback(foreign) is False
    assert posts == []
