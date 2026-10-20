"""Runtime configuration, read from environment variables.

Pointing the service at a real vCenter instead of the vcsim simulator only
means changing these values. No code changes are needed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    vcenter_host: str
    vcenter_port: int
    vcenter_user: str
    vcenter_password: str
    # vcsim and many lab vCenters use self-signed certificates.
    # Set VCENTER_VERIFY_SSL=true against a vCenter with a trusted certificate.
    vcenter_verify_ssl: bool
    # When set, every endpoint except /health requires this value in the
    # X-API-Key header. When empty, the API is open (fine for local demos only).
    api_key: Optional[str]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        vcenter_host=os.getenv("VCENTER_HOST", "localhost"),
        vcenter_port=int(os.getenv("VCENTER_PORT", "8989")),
        vcenter_user=os.getenv("VCENTER_USER", "user"),
        vcenter_password=os.getenv("VCENTER_PASSWORD", "pass"),
        vcenter_verify_ssl=_as_bool(os.getenv("VCENTER_VERIFY_SSL"), False),
        api_key=os.getenv("API_KEY") or None,
    )
