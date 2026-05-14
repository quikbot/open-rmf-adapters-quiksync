"""YAML config loader for fleet_adapter_quiksync.

Per design §6.2: the adapter needs Auth0 M2M credentials, the QuikSync
HTTPS base URL, the Open-RMF fleet name to register, and a path to the
building map / nav graph. Config can come from:

  1. A YAML file (recommended for production — secrets via Docker secret
     mount referenced from YAML)
  2. Environment variables (recommended for dev / smoke)
  3. Inline kwargs (tests)

Validation discipline: missing required fields raise `ConfigError` at
load time with a clear message. Unknown YAML keys raise too — catches
typos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


class ConfigError(Exception):
    """Required field missing or unknown field present."""


@dataclass(frozen=True)
class FleetAdapterConfig:
    # QuikSync API + Auth0 wiring
    base_url: str          # e.g. "https://<your-quiksync-host>"
    auth0_tenant: str              # e.g. "<your-auth0-tenant>.auth0.com"
    auth0_audience: str            # always "https://<your-quiksync-api-audience>/open-rmf"
    auth0_client_id: str
    auth0_client_secret: str       # NOT logged
    auth0_organization: str        # Auth0 Org id matching the customer
    # Open-RMF fleet identity
    fleet_name: str                # must match a fleet registered server-side
    # Tuning knobs (sensible defaults)
    update_interval_seconds: float = 0.5
    state_subscribe_reconnect_seconds: float = 1.0

    # ----- Construction -----

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FleetAdapterConfig":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"config root must be a dict; got {type(data).__name__}")
        return cls.from_dict(data)

    @classmethod
    def from_env(cls) -> "FleetAdapterConfig":
        """Build from environment variables. Convention: CONFIG_<UPPER_FIELD>."""
        d = {}
        for field in cls.__dataclass_fields__:
            env_key = f"FLEET_ADAPTER_{field.upper()}"
            if env_key in os.environ:
                d[field] = os.environ[env_key]
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, data: dict) -> "FleetAdapterConfig":
        # Work on a shallow copy so we don't mutate the caller's dict.
        data = dict(data)

        # Resolve the secret-from-file convention BEFORE the unknown-key
        # check — `auth0_client_secret_file` is a valid input alias but
        # not a dataclass field. (Docker-secret-mount recommended path.)
        if "auth0_client_secret_file" in data:
            secret_path = Path(data.pop("auth0_client_secret_file"))
            if not secret_path.exists():
                raise ConfigError(f"auth0_client_secret_file not found: {secret_path}")
            data["auth0_client_secret"] = secret_path.read_text().strip()

        known_fields = set(cls.__dataclass_fields__)
        unknown = set(data.keys()) - known_fields
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")

        # Coerce known numeric fields from string (env case)
        for numeric in ("update_interval_seconds", "state_subscribe_reconnect_seconds"):
            if numeric in data and isinstance(data[numeric], str):
                try:
                    data[numeric] = float(data[numeric])
                except ValueError as e:
                    raise ConfigError(f"{numeric} must be a number; got {data[numeric]!r}") from e

        # Required fields check
        required = {
            "base_url", "auth0_tenant", "auth0_audience",
            "auth0_client_id", "auth0_client_secret", "auth0_organization",
            "fleet_name",
        }
        missing = required - set(data.keys())
        if missing:
            raise ConfigError(f"missing required config fields: {sorted(missing)}")

        return cls(**data)

    # ----- Helpers -----

    def ws_base_url(self) -> str:
        """Convert the HTTPS base to a WSS base for state subscriptions."""
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://"):]
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://"):]
        raise ConfigError(f"base_url must start with http(s)://; got {self.base_url!r}")
