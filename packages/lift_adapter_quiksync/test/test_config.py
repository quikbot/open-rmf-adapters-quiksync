"""Tests for LiftAdapterConfig — load + validate."""

from __future__ import annotations

from pathlib import Path

import pytest

from lift_adapter_quiksync.config import ConfigError, LiftAdapterConfig

REQUIRED = {
    "base_url": "https://example.test",
    "auth0_tenant": "tenant.example.test",
    "auth0_audience": "https://api.example.test/open-rmf",
    "auth0_client_id": "test-client",
    "auth0_client_secret": "test-secret",
    "auth0_organization": "org_test",
    "lifts": ["lift_alpha"],
}


def test_from_dict_minimal_ok():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED))
    assert cfg.lifts == ("lift_alpha",)
    assert cfg.state_subscribe_reconnect_seconds == 1.0  # default
    assert cfg.session_ttl_seconds == 3600.0  # default
    assert cfg.lift_states_topic == "lift_states"  # default
    assert cfg.lift_requests_topic == "lift_requests"  # default


def test_from_dict_missing_required_raises():
    partial = {k: v for k, v in REQUIRED.items() if k != "auth0_client_id"}
    with pytest.raises(ConfigError, match="auth0_client_id"):
        LiftAdapterConfig.from_dict(partial)


def test_from_dict_missing_lifts_raises():
    partial = {k: v for k, v in REQUIRED.items() if k != "lifts"}
    with pytest.raises(ConfigError, match="lifts"):
        LiftAdapterConfig.from_dict(partial)


def test_from_dict_unknown_key_raises():
    extra = dict(REQUIRED)
    extra["my_typo_field"] = "oops"
    with pytest.raises(ConfigError, match="my_typo_field"):
        LiftAdapterConfig.from_dict(extra)


# ----- lifts list normalisation -----


def test_lifts_accepts_list():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=["a", "b", "c"]))
    assert cfg.lifts == ("a", "b", "c")


def test_lifts_accepts_comma_separated_string():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, lifts="a, b , c"))
    assert cfg.lifts == ("a", "b", "c")


def test_lifts_strips_whitespace():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=["  alpha  ", "beta"]))
    assert cfg.lifts == ("alpha", "beta")


def test_lifts_rejects_empty_list():
    with pytest.raises(ConfigError, match="at least one entry"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=[]))


def test_lifts_rejects_empty_string():
    with pytest.raises(ConfigError, match="at least one entry"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=""))


def test_lifts_rejects_empty_entry():
    with pytest.raises(ConfigError, match="empty entry"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=["alpha", ""]))


def test_lifts_rejects_duplicates():
    with pytest.raises(ConfigError, match="duplicate entry"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=["alpha", "alpha"]))


def test_lifts_rejects_wrong_type():
    with pytest.raises(ConfigError, match="list or comma-separated"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, lifts=42))


# ----- session_ttl_seconds -----


def test_session_ttl_default():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED))
    assert cfg.session_ttl_seconds == 3600.0


def test_session_ttl_override():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, session_ttl_seconds=120.0))
    assert cfg.session_ttl_seconds == 120.0


def test_session_ttl_string_coercion():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, session_ttl_seconds="900"))
    assert cfg.session_ttl_seconds == 900.0


def test_session_ttl_invalid_raises():
    with pytest.raises(ConfigError, match="session_ttl_seconds"):
        LiftAdapterConfig.from_dict(dict(REQUIRED, session_ttl_seconds="not-a-number"))


# ----- YAML parsing -----


def _yaml_text_nested(lifts_block: str = "  lifts:\n    - lift_alpha\n") -> str:
    body = "".join(
        f"  {k}: {v}\n"
        for k, v in REQUIRED.items()
        if k != "lifts"
    )
    return "quiksync:\n" + body + lifts_block


def test_from_yaml_nested_block(tmp_path: Path):
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(_yaml_text_nested())
    cfg = LiftAdapterConfig.from_yaml(cfg_file)
    assert cfg.lifts == ("lift_alpha",)
    assert cfg.base_url == REQUIRED["base_url"]


def test_from_yaml_flat_form(tmp_path: Path):
    body = "".join(f"{k}: {v}\n" for k, v in REQUIRED.items() if k != "lifts")
    body += "lifts:\n  - lift_alpha\n"
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text(body)
    cfg = LiftAdapterConfig.from_yaml(cfg_file)
    assert cfg.lifts == ("lift_alpha",)


def test_from_yaml_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        LiftAdapterConfig.from_yaml(tmp_path / "nope.yaml")


def test_from_yaml_non_dict_root_raises(tmp_path: Path):
    cfg_file = tmp_path / "lift.yaml"
    cfg_file.write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="must be a dict"):
        LiftAdapterConfig.from_yaml(cfg_file)


# ----- secret-from-file -----


def test_secret_from_file(tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("from-file-secret\n")
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(secret_file)
    cfg = LiftAdapterConfig.from_dict(data)
    assert cfg.auth0_client_secret == "from-file-secret"


def test_secret_from_missing_file_raises(tmp_path: Path):
    data = {k: v for k, v in REQUIRED.items() if k != "auth0_client_secret"}
    data["auth0_client_secret_file"] = str(tmp_path / "missing.txt")
    with pytest.raises(ConfigError, match="not found"):
        LiftAdapterConfig.from_dict(data)


# ----- ws_base_url helper -----


def test_ws_base_url_https():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, base_url="https://example.test"))
    assert cfg.ws_base_url() == "wss://example.test"


def test_ws_base_url_http():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, base_url="http://localhost:8080"))
    assert cfg.ws_base_url() == "ws://localhost:8080"


def test_ws_base_url_invalid_scheme_raises():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED, base_url="ftp://invalid"))
    with pytest.raises(ConfigError, match="http"):
        cfg.ws_base_url()


# ----- env loader -----


def test_from_env_reads_LIFT_ADAPTER_prefix(monkeypatch):
    for k, v in REQUIRED.items():
        if k == "lifts":
            monkeypatch.setenv("LIFT_ADAPTER_LIFTS", "lift_alpha,lift_beta")
        else:
            monkeypatch.setenv(f"LIFT_ADAPTER_{k.upper()}", v)
    cfg = LiftAdapterConfig.from_env()
    assert cfg.lifts == ("lift_alpha", "lift_beta")
    assert cfg.base_url == REQUIRED["base_url"]


# ----- topic remap defaults + overrides -----


def test_topic_remap_overrides():
    cfg = LiftAdapterConfig.from_dict(
        dict(REQUIRED, lift_states_topic="/custom/states",
             lift_requests_topic="/custom/requests")
    )
    assert cfg.lift_states_topic == "/custom/states"
    assert cfg.lift_requests_topic == "/custom/requests"


# ----- frozen dataclass guarantee -----


def test_config_is_frozen():
    cfg = LiftAdapterConfig.from_dict(dict(REQUIRED))
    with pytest.raises(Exception):
        cfg.base_url = "https://other.test"  # type: ignore[misc]
