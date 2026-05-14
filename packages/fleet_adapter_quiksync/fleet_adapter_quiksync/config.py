"""YAML config loader for fleet_adapter_quiksync.

The adapter accepts a single YAML file with **two top-level blocks**:

```yaml
rmf_fleet:        # Standard Open-RMF fleet config (matches fleet_adapter_template).
  name: ...
  limits: ...
  profile: ...
  battery_system: ...
  task_capabilities: ...
  robots: ...
quiksync:         # QuikSync-specific extension block — Auth0 + endpoint wiring.
  base_url: ...
  auth0_tenant: ...
  auth0_audience: ...
  auth0_client_id: ...
  auth0_client_secret_file: ...
  auth0_organization: ...
  fleet_name: ...
  dynamic_mode: false   # opt-in
```

This module parses the `quiksync:` block into `FleetAdapterConfig`. The
`rmf_fleet:` block is opaque to this module — the adapter passes the
*YAML file path* to `rmf_adapter.easy_full_control.FleetConfiguration.from_config_files(...)`
which parses that block natively (matches `fleet_adapter_template`).

Two operating modes, selected by `quiksync.dynamic_mode`:

- **`false` (default)**: YAML-driven. The operator provides both `rmf_fleet:`
  and `quiksync:` blocks plus a separate `nav_graph.yaml` (passed to the
  launch file via `nav_graph:=...`). The adapter calls `from_config_files`.
  This is the recommended path; matches the canonical Open-RMF community
  pattern.
- **`true`**: dynamic. The `rmf_fleet:` block is ignored (and not required
  in the YAML); the adapter fetches `/discovery` + `/building_map` from
  the QuikSync Open-RMF Connector and builds `FleetConfiguration`
  in-memory. The `nav_graph` launch arg is also not required in this
  mode. Convenient when the catalogue is the source of truth and the
  operator doesn't want to maintain YAML in sync.

Config can come from:
1. A YAML file (recommended for production — secrets via Docker secret mount).
2. Environment variables (prefix `FLEET_ADAPTER_`; sets the `quiksync:`
   fields; implies `dynamic_mode=true` since there's no `rmf_fleet:` block
   from env).
3. Inline kwargs (tests).

Validation discipline: missing required fields raise `ConfigError` at
load time with a clear message. Unknown keys in the `quiksync:` block
raise too — catches typos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Required field missing or unknown field present."""


@dataclass(frozen=True)
class FleetAdapterConfig:
    """QuikSync-side extension config — parsed from the YAML's `quiksync:` block."""

    # QuikSync Connector + Auth0 wiring
    base_url: str          # e.g. "https://<your-quiksync-host>"
    auth0_tenant: str              # e.g. "<your-auth0-tenant>.auth0.com"
    auth0_audience: str            # e.g. "https://<your-quiksync-api-audience>/open-rmf"
    auth0_client_id: str
    auth0_client_secret: str       # NOT logged
    auth0_organization: str        # Auth0 Org id matching the customer
    # Open-RMF fleet identity
    fleet_name: str                # must match a fleet registered server-side
    # Tuning knobs (sensible defaults)
    update_interval_seconds: float = 0.5
    state_subscribe_reconnect_seconds: float = 1.0
    # Mode selector
    dynamic_mode: bool = False     # false = YAML-driven; true = fetch /discovery + /building_map

    # ----- Construction -----

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FleetAdapterConfig":
        """Parse `quiksync:` block from a YAML file.

        Accepts either:
        - Nested form: `{quiksync: {base_url: ..., ...}}` (recommended).
        - Flat form: `{base_url: ..., ...}` (backward-compatible; assumed
          dynamic_mode since there's no `rmf_fleet:` block alongside).

        The `rmf_fleet:` sibling block (if present) is not parsed here —
        it's the adapter's job to pass the file path verbatim to
        `FleetConfiguration.from_config_files`.
        """
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"config root must be a dict; got {type(data).__name__}")

        # Prefer the nested `quiksync:` block; otherwise treat the whole
        # document as flat.
        if "quiksync" in data and isinstance(data["quiksync"], dict):
            quiksync_block = data["quiksync"]
            rmf_fleet_block = data.get("rmf_fleet")

            # If the YAML has no `rmf_fleet:` block, dynamic_mode is the
            # only viable mode regardless of what the operator set — they
            # have nothing to feed `from_config_files`. Surface a clear
            # error rather than silently flipping the mode.
            if not quiksync_block.get("dynamic_mode", False) and rmf_fleet_block is None:
                raise ConfigError(
                    "YAML mode (dynamic_mode=false) requires an `rmf_fleet:` block "
                    "alongside `quiksync:` in the config file. Either add the "
                    "block or set `quiksync.dynamic_mode: true`."
                )

            # When both blocks exist, the fleet identifier must match
            # across them — a typo here would otherwise produce a
            # confusing failure where the adapter registers a fleet
            # under one name while logging another.
            if isinstance(rmf_fleet_block, dict):
                rmf_name = rmf_fleet_block.get("name")
                quiksync_name = quiksync_block.get("fleet_name")
                if (
                    isinstance(rmf_name, str)
                    and isinstance(quiksync_name, str)
                    and rmf_name != quiksync_name
                ):
                    raise ConfigError(
                        f"fleet identifier mismatch: rmf_fleet.name={rmf_name!r} "
                        f"vs quiksync.fleet_name={quiksync_name!r}. These must be "
                        f"equal so the adapter registers the same fleet it subscribes to."
                    )

            return cls.from_dict(quiksync_block)

        # Flat form — backward compat. Implies dynamic_mode since there's
        # no `rmf_fleet:` block possible here.
        flat = dict(data)
        flat.setdefault("dynamic_mode", True)
        return cls.from_dict(flat)

    @classmethod
    def from_env(cls) -> "FleetAdapterConfig":
        """Build from environment variables (prefix `FLEET_ADAPTER_`).

        Env-only configuration implies `dynamic_mode=true` since there's
        no `rmf_fleet:` block from env. The operator can override by
        setting `FLEET_ADAPTER_DYNAMIC_MODE=false` AND providing a
        separate config YAML via the launch arg, but typical env-driven
        deployments use dynamic mode.
        """
        d: dict[str, Any] = {}
        for field in cls.__dataclass_fields__:
            env_key = f"FLEET_ADAPTER_{field.upper()}"
            if env_key in os.environ:
                d[field] = os.environ[env_key]
        # Env mode defaults to dynamic.
        d.setdefault("dynamic_mode", True)
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

        # Coerce dynamic_mode from string (env / YAML loose-typing)
        if "dynamic_mode" in data and isinstance(data["dynamic_mode"], str):
            data["dynamic_mode"] = data["dynamic_mode"].strip().lower() in ("1", "true", "yes", "on")

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
