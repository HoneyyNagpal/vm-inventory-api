"""Alert rules. Pure functions over the inventory models, so they are easy to test
and have no dependency on vCenter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from app.models import VM, Alert, AlertReport, Datastore, Host

CRITICAL_FLOOR = 95.0  # at or above this a threshold breach becomes critical


@dataclass(frozen=True)
class Thresholds:
    host_cpu_pct: float = 80.0
    host_memory_pct: float = 85.0
    datastore_pct: float = 85.0
    vm_memory_pct: float = 90.0


def _severity(value: float) -> str:
    return "critical" if value >= CRITICAL_FLOOR else "warning"


def evaluate(
    vms: Iterable[VM],
    hosts: Iterable[Host],
    datastores: Iterable[Datastore],
    thresholds: Thresholds = Thresholds(),
) -> AlertReport:
    alerts: List[Alert] = []

    for vm in vms:
        if vm.power_state == "poweredOff":
            alerts.append(Alert(
                severity="warning", kind="vm_powered_off", resource_type="vm",
                resource_id=vm.moid, resource_name=vm.name,
                message=f"VM {vm.name} is powered off",
            ))
        usage = vm.memory_usage_pct
        if usage is not None and usage >= thresholds.vm_memory_pct:
            alerts.append(Alert(
                severity=_severity(usage), kind="vm_memory_high", resource_type="vm",
                resource_id=vm.moid, resource_name=vm.name,
                message=f"VM {vm.name} guest memory at {usage}%",
                value=usage, threshold=thresholds.vm_memory_pct,
            ))

    for host in hosts:
        if host.connection_state != "connected":
            alerts.append(Alert(
                severity="critical", kind="host_not_connected", resource_type="host",
                resource_id=host.moid, resource_name=host.name,
                message=f"Host {host.name} is {host.connection_state}",
            ))
            continue  # stats of a disconnected host are meaningless
        cpu = host.cpu_usage_pct
        if cpu is not None and cpu >= thresholds.host_cpu_pct:
            alerts.append(Alert(
                severity=_severity(cpu), kind="host_cpu_high", resource_type="host",
                resource_id=host.moid, resource_name=host.name,
                message=f"Host {host.name} CPU at {cpu}%",
                value=cpu, threshold=thresholds.host_cpu_pct,
            ))
        mem = host.memory_usage_pct
        if mem is not None and mem >= thresholds.host_memory_pct:
            alerts.append(Alert(
                severity=_severity(mem), kind="host_memory_high", resource_type="host",
                resource_id=host.moid, resource_name=host.name,
                message=f"Host {host.name} memory at {mem}%",
                value=mem, threshold=thresholds.host_memory_pct,
            ))

    for ds in datastores:
        if not ds.accessible:
            alerts.append(Alert(
                severity="critical", kind="datastore_inaccessible", resource_type="datastore",
                resource_id=ds.moid, resource_name=ds.name,
                message=f"Datastore {ds.name} is not accessible",
            ))
            continue
        usage = ds.usage_pct
        if usage is not None and usage >= thresholds.datastore_pct:
            alerts.append(Alert(
                severity=_severity(usage), kind="datastore_usage_high", resource_type="datastore",
                resource_id=ds.moid, resource_name=ds.name,
                message=f"Datastore {ds.name} is {usage}% full",
                value=usage, threshold=thresholds.datastore_pct,
            ))

    alerts.sort(key=lambda a: (a.severity != "critical", a.resource_type, a.resource_name))
    critical = sum(1 for a in alerts if a.severity == "critical")
    return AlertReport(
        total=len(alerts), critical=critical, warning=len(alerts) - critical, alerts=alerts
    )
