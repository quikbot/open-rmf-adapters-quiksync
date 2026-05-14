"""Tests for FleetAdapterConfig — load + validate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fleet_adapter_quiksync.config import ConfigError, FleetAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "fleet_name": "service_robots",
}


def test_from_dict_minimal_ok():
    cfg = FleetAdapterConfig.from_dict(dict(REQUIRED))
    assert cfg.fleet_name == "service_robots"
    assert cfg.update_interval_seconds == 0.5  # default
    assert cfg.state_subscribe_reconnect_seconds == 1.0  # default


def test_from_dict_missing_required_raises():
    partial = {k: v for k, v in REQUIRED.items() if k != "auth0_client_id"}
    with pytest.raises(ConfigError, match="auth0_client_id"):
        FleetAdapterConfig.from_dict(partial)


def test_from_dict_unknown_key_raises():
    extra = dict(REQUIRED)
    extra["my_typo_field"] = "oops"
    with pytest.raises(ConfigError, match="my_typo_field"):
        FleetAdapterConfig.from_dict(extra)


def test_from_yaml_round_trip(tmp_path: Path):
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items()) + "\n"
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    cfg = FleetAdapterConfig.from_yaml(cfg_file)
    assert cfg.base_url == REQUIRED["base_url"]


def test_from_yaml_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        FleetAdapterConfig.from_yaml(tmp_path / "nope.yaml")


# ----- nested YAML form (the canonical shape) -----


def test_from_yaml_nested_block_with_rmf_fleet(tmp_path: Path):
    """The canonical YAML form: `rmf_fleet:` + `quiksync:` siblings,
    `dynamic_mode` defaults to False."""
    quiksync_block = "\n".join(f"  {k}: {v}" for k, v in REQUIRED.items())
    yaml_text = (
        "rmf_fleet:\n"
        "  name: service_robots\n"
        "  limits:\n"
        "    linear: [0.5, 0.75]\n"
        "    angular: [0.6, 2.0]\n"
        "  profile:\n"
        "    footprint: 0.3\n"
        "    vicinity: 0.5\n"
        "quiksync:\n"
        + quiksync_block + "\n"
    )
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    cfg = FleetAdapterConfig.from_yaml(cfg_file)
    assert cfg.fleet_name == "service_robots"
    assert cfg.dynamic_mode is False  # YAML mode by default


def test_from_yaml_nested_with_dynamic_mode_true_no_rmf_fleet_block_required(tmp_path: Path):
    """When `quiksync.dynamic_mode: true`, `rmf_fleet:` block is not required."""
    quiksync_block = "\n".join(f"  {k}: {v}" for k, v in REQUIRED.items())
    yaml_text = "quiksync:\n  dynamic_mode: true\n" + quiksync_block + "\n"
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    cfg = FleetAdapterConfig.from_yaml(cfg_file)
    assert cfg.dynamic_mode is True


def test_from_yaml_nested_missing_rmf_fleet_with_dynamic_false_raises(tmp_path: Path):
    """When dynamic_mode is false (default), `rmf_fleet:` block must
    be present alongside `quiksync:`."""
    quiksync_block = "\n".join(f"  {k}: {v}" for k, v in REQUIRED.items())
    yaml_text = "quiksync:\n" + quiksync_block + "\n"
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    with pytest.raises(ConfigError, match="rmf_fleet"):
        FleetAdapterConfig.from_yaml(cfg_file)


def test_from_yaml_flat_form_implies_dynamic_mode(tmp_path: Path):
    """Backward-compat flat YAML (no `quiksync:` block) implies dynamic_mode
    because there's no place to put an `rmf_fleet:` block."""
    yaml_text = "\n".join(f"{k}: {v}" for k, v in REQUIRED.items()) + "\n"
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(yaml_text)
    cfg = FleetAdapterConfig.from_yaml(cfg_file)
    assert cfg.dynamic_mode is True


# ----- dynamic_mode parsing -----


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ("", False),
])
def test_dynamic_mode_string_coercion(raw, expected):
    d = dict(REQUIRED, dynamic_mode=raw)
    cfg = FleetAdapterConfig.from_dict(d)
    assert cfg.dynamic_mode is expected


def test_dynamic_mode_bool_passthrough():
    cfg_true = FleetAdapterConfig.from_dict(dict(REQUIRED, dynamic_mode=True))
    cfg_false = FleetAdapterConfig.from_dict(dict(REQUIRED, dynamic_mode=False))
    assert cfg_true.dynamic_mode is True
    assert cfg_false.dynamic_mode is False


def test_secret_from_file(tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-file-secret\n")
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(secret_file)
    cfg = FleetAdapterConfig.from_dict(data)
    assert cfg.auth0_client_secret == "from-file-secret"


def test_secret_from_missing_file_raises(tmp_path: Path):
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(tmp_path / "missing.txt")
    with pytest.raises(ConfigError, match="not found"):
        FleetAdapterConfig.from_dict(data)


def test_numeric_coercion_from_string():
    """Env-style string numerics get coerced."""
    data = dict(REQUIRED)
    data["update_interval_seconds"] = "0.25"
    cfg = FleetAdapterConfig.from_dict(data)
    assert cfg.update_interval_seconds == 0.25


def test_numeric_coercion_invalid_raises():
    data = dict(REQUIRED)
    data["update_interval_seconds"] = "not-a-number"
    with pytest.raises(ConfigError, match="update_interval_seconds"):
        FleetAdapterConfig.from_dict(data)


def test_ws_base_url_https():
    cfg = FleetAdapterConfig.from_dict(dict(REQUIRED, base_url="https://example.test"))
    assert cfg.ws_base_url() == "wss://example.test"


def test_ws_base_url_http():
    cfg = FleetAdapterConfig.from_dict(dict(REQUIRED, base_url="http://localhost:8080"))
    assert cfg.ws_base_url() == "ws://localhost:8080"


def test_ws_base_url_invalid_scheme_raises():
    cfg = FleetAdapterConfig.from_dict(dict(REQUIRED, base_url="ftp://invalid"))
    with pytest.raises(ConfigError):
        cfg.ws_base_url()


def test_from_env_reads_FLEET_ADAPTER_prefix(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(f"FLEET_ADAPTER_{k.upper()}", v)
    cfg = FleetAdapterConfig.from_env()
    assert cfg.fleet_name == "service_robots"
    assert cfg.base_url == "https://example.test"
