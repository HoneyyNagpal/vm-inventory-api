"""Response models. Computed percentages live here so every consumer
(API, alert rules, CLI) uses the same math."""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, computed_field


class PowerState(str, Enum):
    ON = "poweredOn"
    OFF = "poweredOff"
    SUSPENDED = "suspended"


def pct(used: Optional[float], total: Optional[float]) -> Optional[float]:
    """Percentage of `used` over `total`, rounded to 1 decimal, None if unknown."""
    if used is None or not total:
        return None
    return round(100.0 * used / total, 1)


class VM(BaseModel):
    moid: str
    name: str
    uuid: Optional[str] = None
    power_state: str
    num_cpu: Optional[int] = None
    memory_mb: Optional[int] = None
    guest_os: Optional[str] = None
    ip_address: Optional[str] = None
    tools_status: Optional[str] = None
    host: Optional[str] = None
    cpu_usage_mhz: Optional[int] = None
    memory_usage_mb: Optional[int] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def memory_usage_pct(self) -> Optional[float]:
        return pct(self.memory_usage_mb, self.memory_mb)


class Host(BaseModel):
    moid: str
    name: str
    connection_state: str
    power_state: str
    version: Optional[str] = None
    cpu_cores: Optional[int] = None
    cpu_total_mhz: Optional[int] = None
    cpu_usage_mhz: Optional[int] = None
    memory_total_mb: Optional[int] = None
    memory_usage_mb: Optional[int] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpu_usage_pct(self) -> Optional[float]:
        return pct(self.cpu_usage_mhz, self.cpu_total_mhz)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def memory_usage_pct(self) -> Optional[float]:
        return pct(self.memory_usage_mb, self.memory_total_mb)


class Datastore(BaseModel):
    moid: str
    name: str
    type: Optional[str] = None
    accessible: bool = True
    capacity_gb: Optional[float] = None
    free_gb: Optional[float] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usage_pct(self) -> Optional[float]:
        if self.capacity_gb is None or self.free_gb is None:
            return None
        return pct(self.capacity_gb - self.free_gb, self.capacity_gb)


class Alert(BaseModel):
    severity: str  # "critical" | "warning"
    kind: str
    resource_type: str  # "vm" | "host" | "datastore"
    resource_id: str
    resource_name: str
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None


class Summary(BaseModel):
    vms_total: int
    vms_by_power_state: dict
    hosts_total: int
    hosts_connected: int
    datastores_total: int
    datastore_capacity_gb: float
    datastore_free_gb: float


class AlertReport(BaseModel):
    total: int
    critical: int
    warning: int
    alerts: List[Alert]
