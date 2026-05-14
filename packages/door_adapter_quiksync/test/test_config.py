"""Tests for DoorAdapterConfig — load + validate."""

from __future__ import annotations

from pathlib import Path

import pytest

from door_adapter_quiksync.config import ConfigError, DoorAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "doors": ["door_alpha", "door_beta"],
}


def test_from_dict_minimal_ok():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED))
    assert cfg.doors == ("door_alpha", "door_beta")
    assert cfg.state_subscribe_reconnect_seconds == 1.0  # default
    assert cfg.door_states_topic == "door_states"  # default
    assert cfg.door_requests_topic == "door_requests"  # default


def test_from_dict_missing_required_raises():
    partial = {k: v for k, v in REQUIRED.items() if k != "auth0_client_id"}
    with pytest.raises(ConfigError, match="auth0_client_id"):
        DoorAdapterConfig.from_dict(partial)


def test_from_dict_missing_doors_raises():
    partial = {k: v for k, v in REQUIRED.items() if k != "doors"}
    with pytest.raises(ConfigError, match="doors"):
        DoorAdapterConfig.from_dict(partial)


def test_from_dict_unknown_key_raises():
    extra = dict(REQUIRED)
    extra["my_typo_field"] = "oops"
    with pytest.raises(ConfigError, match="my_typo_field"):
        DoorAdapterConfig.from_dict(extra)


# ----- doors list normalisation -----


def test_doors_accepts_list():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, doors=["a", "b", "c"]))
    assert cfg.doors == ("a", "b", "c")


def test_doors_accepts_comma_separated_string():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, doors="a, b , c"))
    assert cfg.doors == ("a", "b", "c")


def test_doors_strips_whitespace():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, doors=["  alpha  ", "beta"]))
    assert cfg.doors == ("alpha", "beta")


def test_doors_rejects_empty_list():
    with pytest.raises(ConfigError, match="at least one entry"):
        DoorAdapterConfig.from_dict(dict(REQUIRED, doors=[]))


def test_doors_rejects_empty_string_list():
    with pytest.raises(ConfigError, match="at least one entry"):
        DoorAdapterConfig.from_dict(dict(REQUIRED, doors=""))


def test_doors_rejects_empty_entry():
    with pytest.raises(ConfigError, match="empty entry"):
        DoorAdapterConfig.from_dict(dict(REQUIRED, doors=["alpha", ""]))


def test_doors_rejects_duplicates():
    with pytest.raises(ConfigError, match="duplicate entry"):
        DoorAdapterConfig.from_dict(dict(REQUIRED, doors=["alpha", "alpha"]))


def test_doors_rejects_wrong_type():
    with pytest.raises(ConfigError, match="list or comma-separated"):
        DoorAdapterConfig.from_dict(dict(REQUIRED, doors=42))


# ----- YAML parsing -----


def _yaml_text_nested(doors_block: str = "  doors:\n    - door_alpha\n    - door_beta\n") -> str:
    """Build a canonical nested YAML doc for tests."""
    body = "".join(
        f"  {k}: {v}\n"
        for k, v in REQUIRED.items()
        if k != "doors"
    )
    return "quiksync:\n" + body + doors_block


def test_from_yaml_nested_block(tmp_path: Path):
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(_yaml_text_nested())
    cfg = DoorAdapterConfig.from_yaml(cfg_file)
    assert cfg.doors == ("door_alpha", "door_beta")
    assert cfg.base_url == REQUIRED["base_url"]


def test_from_yaml_flat_form(tmp_path: Path):
    """Flat YAML (no `quiksync:` block) is accepted as a backward-compat alias."""
    body = "".join(f"{k}: {v}\n" for k, v in REQUIRED.items() if k != "doors")
    body += "doors:\n  - door_alpha\n  - door_beta\n"
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text(body)
    cfg = DoorAdapterConfig.from_yaml(cfg_file)
    assert cfg.doors == ("door_alpha", "door_beta")


def test_from_yaml_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        DoorAdapterConfig.from_yaml(tmp_path / "nope.yaml")


def test_from_yaml_non_dict_root_raises(tmp_path: Path):
    cfg_file = tmp_path / "door.yaml"
    cfg_file.write_text("- a\n- b\n")  # list at root
    with pytest.raises(ConfigError, match="must be a dict"):
        DoorAdapterConfig.from_yaml(cfg_file)


# ----- secret-from-file -----


def test_secret_from_file(tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-file-secret\n")
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(secret_file)
    cfg = DoorAdapterConfig.from_dict(data)
    assert cfg.auth0_client_secret == "from-file-secret"


def test_secret_from_missing_file_raises(tmp_path: Path):
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(tmp_path / "missing.txt")
    with pytest.raises(ConfigError, match="not found"):
        DoorAdapterConfig.from_dict(data)


# ----- numeric coercion -----


def test_numeric_coercion_from_string():
    data = dict(REQUIRED, state_subscribe_reconnect_seconds="0.25")
    cfg = DoorAdapterConfig.from_dict(data)
    assert cfg.state_subscribe_reconnect_seconds == 0.25


def test_numeric_coercion_invalid_raises():
    data = dict(REQUIRED, state_subscribe_reconnect_seconds="not-a-number")
    with pytest.raises(ConfigError, match="state_subscribe_reconnect_seconds"):
        DoorAdapterConfig.from_dict(data)


# ----- ws_base_url helper -----


def test_ws_base_url_https():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, base_url="https://example.test"))
    assert cfg.ws_base_url() == "wss://example.test"


def test_ws_base_url_http():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, base_url="http://localhost:8080"))
    assert cfg.ws_base_url() == "ws://localhost:8080"


def test_ws_base_url_invalid_scheme_raises():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED, base_url="ftp://invalid"))
    with pytest.raises(ConfigError, match="http"):
        cfg.ws_base_url()


# ----- env loader -----


def test_from_env_reads_DOOR_ADAPTER_prefix(monkeypatch):
    for k, v in REQUIRED.items():
        if k == "doors":
            monkeypatch.setenv("DOOR_ADAPTER_DOORS", "door_alpha,door_beta")
        else:
            monkeypatch.setenv(f"DOOR_ADAPTER_{k.upper()}", v)
    cfg = DoorAdapterConfig.from_env()
    assert cfg.doors == ("door_alpha", "door_beta")
    assert cfg.base_url == REQUIRED["base_url"]


# ----- topic remap defaults + overrides -----


def test_topic_remap_overrides():
    cfg = DoorAdapterConfig.from_dict(
        dict(REQUIRED, door_states_topic="/custom/states",
             door_requests_topic="/custom/requests")
    )
    assert cfg.door_states_topic == "/custom/states"
    assert cfg.door_requests_topic == "/custom/requests"


# ----- frozen dataclass guarantee -----


def test_config_is_frozen():
    cfg = DoorAdapterConfig.from_dict(dict(REQUIRED))
    with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
        cfg.base_url = "https://other.test"  # type: ignore[misc]
