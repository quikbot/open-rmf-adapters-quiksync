"""YAML config loader for door_adapter_quiksync.

The adapter accepts a single YAML file with a `quiksync:` block:

```yaml
quiksync:
  base_url: ...
  auth0_tenant: ...
  auth0_audience: ...
  auth0_client_id: ...
  auth0_client_secret_file: ...
  auth0_organization: ...
  doors:                                 # required list of door IDs
    - door_alpha
    - door_beta
  namespace: Test                        # optional; required only for multi-namespace orgs
  state_subscribe_reconnect_seconds: 1.0
  door_states_topic: door_states         # optional ROS topic remap
  door_requests_topic: door_requests     # optional ROS topic remap
```

The adapter owns the doors named in `doors:` — one rclpy node manages all
of them. Each ID must match a door declared in the customer's QuikSync
tenant and in the rmf-side `building_map.yaml` so that DoorRequest
messages from RMF route to the right adapter.

Door identity (and the lift sibling's identity) is not announced via a
fleet-name-style identifier on the rmf side; rmf addresses individual
doors by their `door_name`. The adapter therefore owns a list of door
IDs rather than a single fleet identifier.

Config can come from:
1. A YAML file (recommended for production — secrets via Docker secret mount).
2. Environment variables (prefix `DOOR_ADAPTER_`; the `doors:` list comes
   from `DOOR_ADAPTER_DOORS` as a comma-separated string).
3. Inline kwargs (tests).

Validation discipline: missing required fields raise `ConfigError` at
load time with a clear message. Unknown keys in the `quiksync:` block
raise too — catches typos. Duplicate or empty door IDs raise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(Exception):
    """Required field missing, unknown field present, or invalid value."""


@dataclass(frozen=True)
class DoorAdapterConfig:
    """QuikSync-side config for the door adapter — parsed from `quiksync:`."""

    # QuikSync HTTPS endpoint + Auth0 M2M wiring (same shape as fleet adapter)
    base_url: str
    auth0_tenant: str
    auth0_audience: str
    auth0_client_id: str
    auth0_client_secret: str       # NOT logged
    auth0_organization: str
    # Doors this adapter owns. Each ID must match a door declared in the
    # customer's QuikSync tenant and in rmf's building_map.yaml.
    doors: tuple[str, ...] = field(default_factory=tuple)
    # Tuning knobs (sensible defaults)
    state_subscribe_reconnect_seconds: float = 1.0
    # ROS topic remaps. Defaults match the rmf community convention.
    door_states_topic: str = "door_states"
    door_requests_topic: str = "door_requests"
    # Multi-namespace scoping (optional). Set when the QuikSync org hosts
    # multiple namespaces side-by-side and this adapter manages doors in
    # only one of them. Appended as `?namespace=<value>` on every REST +
    # WSS call. Leave unset for single-namespace orgs.
    namespace: Optional[str] = None

    # ----- Construction -----

    @classmethod
    def from_yaml(cls, path: str | Path) -> "DoorAdapterConfig":
        """Parse `quiksync:` block from a YAML file.

        Accepts either:
        - Nested form: `{quiksync: {base_url: ..., ...}}` (recommended).
        - Flat form: `{base_url: ..., ...}` (backward-compatible alias).
        """
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with path.open("r") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"config root must be a dict; got {type(data).__name__}")

        if "quiksync" in data and isinstance(data["quiksync"], dict):
            return cls.from_dict(data["quiksync"])
        return cls.from_dict(data)

    @classmethod
    def from_env(cls) -> "DoorAdapterConfig":
        """Build from environment variables (prefix `DOOR_ADAPTER_`).

        `DOOR_ADAPTER_DOORS` is a comma-separated list of door IDs.
        """
        d: dict[str, Any] = {}
        for field_name in cls.__dataclass_fields__:
            env_key = f"DOOR_ADAPTER_{field_name.upper()}"
            if env_key in os.environ:
                d[field_name] = os.environ[env_key]
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, data: dict) -> "DoorAdapterConfig":
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
        if "state_subscribe_reconnect_seconds" in data and isinstance(
            data["state_subscribe_reconnect_seconds"], str
        ):
            raw = data["state_subscribe_reconnect_seconds"]
            try:
                data["state_subscribe_reconnect_seconds"] = float(raw)
            except ValueError as e:
                raise ConfigError(
                    f"state_subscribe_reconnect_seconds must be a number; got {raw!r}"
                ) from e

        # Normalise + validate the doors list.
        if "doors" in data:
            data["doors"] = _normalise_id_list("doors", data["doors"])

        # Required fields check
        required = {
            "base_url", "auth0_tenant", "auth0_audience",
            "auth0_client_id", "auth0_client_secret", "auth0_organization",
            "doors",
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


def _normalise_id_list(field_name: str, raw: Any) -> tuple[str, ...]:
    """Accept list, tuple, or comma-separated string; return a tuple of
    non-empty unique IDs preserving input order."""
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        raise ConfigError(
            f"{field_name} must be a list or comma-separated string; got {type(raw).__name__}"
        )
    if not items:
        raise ConfigError(f"{field_name} must contain at least one entry")
    if any(not s for s in items):
        raise ConfigError(f"{field_name} contains an empty entry")
    seen: set[str] = set()
    for s in items:
        if s in seen:
            raise ConfigError(f"{field_name} contains duplicate entry: {s!r}")
        seen.add(s)
    return tuple(items)
