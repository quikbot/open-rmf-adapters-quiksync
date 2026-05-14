"""Tests for adapter.py — bootstrap + discovery routing + dry-run mode.

The full EasyFullControl path requires rmf_adapter (only available on
real Open-RMF deployments). These tests cover the testable bits: config
load, fleet picker, robot handle factory, dry-run flow.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from fleet_adapter_quiksync.adapter import (
    _run_dry,
    _try_import_rmf_adapter,
    build_clients,
    build_robot_handles,
    find_our_fleet,
    main,
)
from fleet_adapter_quiksync.config import FleetAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "fleet_name": "service_robots",
}


def make_config() -> FleetAdapterConfig:
    return FleetAdapterConfig.from_dict(dict(REQUIRED))


def test_try_import_rmf_adapter_returns_none_in_ci():
    """rmf_adapter is not installed in CI → None, no exception."""
    assert _try_import_rmf_adapter() is None


def test_build_clients_returns_three_clients():
    config = make_config()
    auth, http, ws = build_clients(config)
    assert auth is not None
    assert http is not None
    assert ws is not None
    auth.close()
    http.close()


def test_find_our_fleet_match():
    discovery = {
        "fleets": [
            {"fleet_name": "other"},
            {"fleet_name": "service_robots", "robots": [{"name": "r1"}]},
        ],
    }
    fleet = find_our_fleet(discovery, "service_robots")
    assert fleet is not None
    assert fleet["fleet_name"] == "service_robots"


def test_find_our_fleet_no_match_returns_none():
    discovery = {"fleets": [{"fleet_name": "other"}]}
    assert find_our_fleet(discovery, "service_robots") is None


def test_find_our_fleet_handles_missing_fleets_key():
    assert find_our_fleet({}, "service_robots") is None
    assert find_our_fleet({"fleets": None}, "service_robots") is None


def test_find_our_fleet_handles_malformed_entries():
    """Non-dict entries in fleets[] are skipped."""
    discovery = {"fleets": ["bad", None, {"fleet_name": "service_robots"}]}
    fleet = find_our_fleet(discovery, "service_robots")
    assert fleet is not None


def test_build_robot_handles_one_per_named_robot():
    fleet_entry = {
        "fleet_name": "service_robots",
        "robots": [
            {"name": "robot-1", "initial_map": "L1"},
            {"name": "robot-2", "initial_map": "L1"},
            {"initial_map": "L1"},  # nameless — skipped
            "bad",  # non-dict — skipped
        ],
    }
    handles = build_robot_handles(fleet_entry)
    assert set(handles.keys()) == {"robot-1", "robot-2"}
    assert handles["robot-1"].robot_name == "robot-1"


def test_build_robot_handles_no_robots():
    handles = build_robot_handles({"fleet_name": "service_robots"})
    assert handles == {}


@pytest.mark.asyncio
async def test_run_dry_dispatches_frames_to_handles():
    """Dry-run should drain a few frames + invoke RobotHandle.on_state."""
    config = make_config()

    class FakeWs:
        def __init__(self) -> None:
            self._closed = False

        def close(self) -> None:
            self._closed = True

        async def subscribe_fleet_state(self, fleet: str):
            yield {
                "name": "service_robots",
                "robots": [
                    {"name": "robot-1", "battery_percent": 87.5, "location": {"x": 0, "y": 0, "yaw": 0, "level_name": "L1"}, "mode": {"mode": "MODE_IDLE"}, "task_id": None, "unix_millis_time": 1},
                    {"name": "robot-2", "battery_percent": 50.0, "location": {"x": 1, "y": 1, "yaw": 0, "level_name": "L1"}, "mode": {"mode": "MODE_IDLE"}, "task_id": None, "unix_millis_time": 1},
                ],
            }

    handles = {
        "robot-1": __import__("fleet_adapter_quiksync.robot_handle", fromlist=["RobotHandle"]).RobotHandle("robot-1"),
        "robot-2": __import__("fleet_adapter_quiksync.robot_handle", fromlist=["RobotHandle"]).RobotHandle("robot-2"),
    }
    rc = await _run_dry(config, http=MagicMock(), ws=FakeWs(), handles=handles)
    assert rc == 0
    assert handles["robot-1"].latest_state() is not None
    assert handles["robot-2"].latest_state() is not None
    assert handles["robot-1"].latest_state()["battery_percent"] == 87.5


@pytest.mark.asyncio
async def test_run_dry_returns_2_when_no_frames():
    """Dry-run with empty WSS stream → return code 2."""
    config = make_config()

    class EmptyWs:
        def __init__(self) -> None:
            self._closed = False

        def close(self) -> None:
            self._closed = True

        async def subscribe_fleet_state(self, fleet: str):
            return
            yield  # unreachable; makes this an async generator

    rc = await _run_dry(config, http=MagicMock(), ws=EmptyWs(), handles={})
    assert rc == 2


def test_main_missing_config_returns_1(monkeypatch):
    """Without --config or env vars → ConfigError → return 1."""
    # Clear any FLEET_ADAPTER_* env vars
    for key in list(__import__("os").environ.keys()):
        if key.startswith("FLEET_ADAPTER_"):
            monkeypatch.delenv(key)
    rc = main([])
    assert rc == 1


def test_main_with_yaml_config_attempts_discovery_fetch(monkeypatch, tmp_path):
    """With valid config + --dry-run, main attempts the discovery call.

    Patch get_discovery to raise so we don't actually hit the network;
    main should return 3 (discovery_fetch_failed)."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items()) + "\n"
    cfg_file = tmp_path / "f.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncHttpClient

    def boom(self):
        raise RuntimeError("network down")

    monkeypatch.setattr(QuikSyncHttpClient, "get_discovery", boom)
    rc = main(["--config", str(cfg_file), "--dry-run"])
    assert rc == 3


def test_main_returns_4_when_fleet_not_in_discovery(monkeypatch, tmp_path):
    """Discovery succeeds but doesn't contain our fleet → return 4."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items()) + "\n"
    cfg_file = tmp_path / "f.yaml"
    cfg_file.write_text(yaml_text)

    from quiksync_client import QuikSyncHttpClient

    def empty_discovery(self):
        return {"fleets": [], "doors": [], "lifts": []}

    monkeypatch.setattr(QuikSyncHttpClient, "get_discovery", empty_discovery)
    rc = main(["--config", str(cfg_file), "--dry-run"])
    assert rc == 4
