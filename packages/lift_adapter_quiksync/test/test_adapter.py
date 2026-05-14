"""Tests for adapter.py — main / bootstrap / dry-run + full-mode dispatch."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from lift_adapter_quiksync.adapter import (
    _try_import_lift_msgs,
    _try_import_rclpy,
    build_clients,
    main,
)
from lift_adapter_quiksync.config import LiftAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "lifts": ["lift_alpha"],
}


def make_config() -> LiftAdapterConfig:
    return LiftAdapterConfig.from_dict(dict(REQUIRED))


# ----- lazy imports -----


def test_try_import_rclpy_returns_none_when_import_fails(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def faulty(name, *args, **kwargs):
        if name == "rclpy":
            raise ImportError("synthetic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", faulty)
    assert _try_import_rclpy() is None


def test_try_import_lift_msgs_returns_none_in_ci():
    """rmf_lift_msgs is not in ros:jazzy-ros-base; import genuinely fails."""
    assert _try_import_lift_msgs() is None


def test_try_import_lift_msgs_returns_none_when_import_fails(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def faulty(name, *args, **kwargs):
        if name == "rmf_lift_msgs.msg":
            raise ImportError("synthetic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", faulty)
    assert _try_import_lift_msgs() is None


# ----- build_clients -----


def test_build_clients_returns_three_clients():
    auth, http, ws = build_clients(make_config())
    assert all(c is not None for c in (auth, http, ws))
    auth.close()
    http.close()


# ----- main: error + dry-run -----


def test_main_missing_config_returns_1(monkeypatch):
    for key in list(__import__("os").environ.keys()):
        if key.startswith("LIFT_ADAPTER_"):
            monkeypatch.delenv(key)
    assert main([]) == 1


def test_main_dry_run_drains_frames(monkeypatch, tmp_path):
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "lifts")
    yaml_text += "\nlifts:\n  - lift_alpha\n"
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncWsClient

    yielded: list[str] = []

    async def fake_subscribe(self, lift: str):
        yielded.append(lift)
        yield {
            "lift_name": lift, "lift_time": 1000,
            "current_floor": "L1", "destination_floor": "",
            "door_state": 0, "motion_state": 0,
            "available_modes": [{"value": 2}, {"value": 4}],
            "current_mode": {"value": 2}, "session_id": "",
        }

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_lift_state", fake_subscribe)
    assert main(["--config", str(cfg_file), "--dry-run", "--dry-run-seconds", "1.0"]) == 0
    assert yielded == ["lift_alpha"]


def test_main_dry_run_returns_2_when_no_frames(monkeypatch, tmp_path):
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "lifts")
    yaml_text += "\nlifts:\n  - lift_alpha\n"
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncWsClient

    async def empty(self, lift: str):
        return
        yield

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_lift_state", empty)
    assert main(["--config", str(cfg_file), "--dry-run", "--dry-run-seconds", "0.2"]) == 2


# ----- main: full mode via sys.modules injection -----


def _install_fake_rclpy_modules(monkeypatch):
    """Install fake rclpy + rmf_lift_msgs + builtin_interfaces so _run_full
    runs against fakes."""

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

    class _LiftState:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _LiftRequest:
        pass

    lift_mod = ModuleType("rmf_lift_msgs")
    lift_msg_mod = ModuleType("rmf_lift_msgs.msg")
    lift_msg_mod.LiftState = _LiftState
    lift_msg_mod.LiftRequest = _LiftRequest
    lift_mod.msg = lift_msg_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rmf_lift_msgs", lift_mod)
    monkeypatch.setitem(sys.modules, "rmf_lift_msgs.msg", lift_msg_mod)

    spin_recorded: dict[str, Any] = {"spun": False}

    class _Publisher:
        def __init__(self):
            self.published: list[Any] = []
        def publish(self, msg):
            self.published.append(msg)

    class _Logger:
        def warning(self, *a, **kw): pass

    class _Node:
        def __init__(self, name: str):
            self.name = name
            self.publisher = _Publisher()
            self.subscriber_callback = None
            self.destroyed = False
        def create_publisher(self, msg_type, topic, qos): return self.publisher
        def create_subscription(self, msg_type, topic, callback, qos):
            self.subscriber_callback = callback
            return SimpleNamespace()
        def get_logger(self): return _Logger()
        def destroy_node(self): self.destroyed = True

    class _Executor:
        def __init__(self): pass
        def add_node(self, node): pass
        def spin(self): spin_recorded["spun"] = True
        def shutdown(self): pass

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

    return rclpy_mod, lift_msg_mod


def test_main_full_mode_constructs_node_and_spins(monkeypatch, tmp_path):
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "lifts")
    yaml_text += "\nlifts:\n  - lift_alpha\n"
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncWsClient

    async def empty(self, lift: str):
        return
        yield

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_lift_state", empty)
    rclpy_mod, _ = _install_fake_rclpy_modules(monkeypatch)

    assert main(["--config", str(cfg_file)]) == 0
    assert rclpy_mod._spin_recorded["spun"] is True


def test_main_full_mode_route_request_dispatches(monkeypatch, tmp_path):
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items() if k != "lifts")
    yaml_text += "\nlifts:\n  - lift_alpha\n"
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncHttpClient, QuikSyncWsClient

    async def empty(self, lift: str):
        return
        yield

    monkeypatch.setattr(QuikSyncWsClient, "subscribe_lift_state", empty)

    posts: list[dict] = []

    def fake_post(self, **kwargs):
        posts.append(kwargs)
        return {"status": "queued"}

    monkeypatch.setattr(QuikSyncHttpClient, "post_lift_request", fake_post)
    rclpy_mod, _ = _install_fake_rclpy_modules(monkeypatch)

    created: list[Any] = []
    orig = rclpy_mod.create_node

    def trace(name):
        n = orig(name)
        created.append(n)
        return n

    monkeypatch.setattr(rclpy_mod, "create_node", trace)
    main(["--config", str(cfg_file)])

    callback = created[0].subscriber_callback
    msg = SimpleNamespace(
        lift_name="lift_alpha", session_id="rmf:r1", request_type=2,
        destination_floor="L3", door_state=2,
    )
    assert callback(msg) is True
    assert posts[0]["lift"] == "lift_alpha"
    assert posts[0]["request_type"] == "AGV_MODE"
