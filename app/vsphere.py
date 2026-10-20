"""Thin pyvmomi wrapper that turns vSphere managed objects into plain models.

Works against a real vCenter or ESXi host and against the vcsim simulator,
because both speak the same vSphere API.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, TypeVar

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from app.config import Settings
from app.models import VM, Datastore, Host

log = logging.getLogger(__name__)

T = TypeVar("T")
GB = 1024 ** 3

# Leaf property paths only. Never request an aggregate such as "summary" or "runtime".
#
# vcsim sends an empty <faultToleranceState/> inside a VM's runtime info, and
# pyvmomi cannot deserialize an empty enum value, so reading vm.summary or
# vm.runtime fails with "AttributeError: summary". Asking for the individual
# values below never touches that field. It is also much cheaper: one
# PropertyCollector call returns every VM, instead of several round trips per VM.
_VM_PROPS = [
    "summary.config.name",
    "summary.config.instanceUuid",
    "summary.config.numCpu",
    "summary.config.memorySizeMB",
    "summary.config.guestFullName",
    "summary.guest.ipAddress",
    "summary.guest.toolsRunningStatus",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.guestMemoryUsage",
    "runtime.powerState",
    "runtime.host",
]


class NotFoundError(Exception):
    """The requested managed object does not exist."""


class VCenterUnavailableError(Exception):
    """vCenter could not be reached or rejected the credentials."""


class InventoryProvider(Protocol):
    """What the API layer needs. VSphereClient implements it, tests fake it."""

    def list_vms(self) -> List[VM]: ...
    def get_vm(self, moid: str) -> VM: ...
    def list_hosts(self) -> List[Host]: ...
    def list_datastores(self) -> List[Datastore]: ...
    def close(self) -> None: ...


def _safe(fn: Callable[[], T], default: Optional[T] = None) -> Optional[T]:
    """Read a property that may be unset or unavailable on some objects
    (disconnected hosts have no hardware info, for example)."""
    try:
        value = fn()
    except (AttributeError, vmodl.fault.ManagedObjectNotFound, vim.fault.NoPermission):
        return default
    return default if value is None else value


def _is_not_found(fault) -> bool:
    """True if a PropertyCollector MissingProperty fault means the object is gone."""
    inner = getattr(fault, "fault", fault)
    return isinstance(inner, vmodl.fault.ManagedObjectNotFound)


@dataclass
class _Row:
    """One object returned by a PropertyCollector call."""

    ref: object  # the managed object reference
    props: dict = field(default_factory=dict)  # property path -> value
    missing: list = field(default_factory=list)  # MissingProperty entries

    @property
    def moid(self) -> str:
        return self.ref._moId


class VSphereClient:
    def __init__(self, host: str, port: int, user: str, password: str, verify_ssl: bool = False):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._verify_ssl = verify_ssl
        self._si = None
        # A single pyvmomi session is not safe to use from several threads at once.
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, s: Settings) -> "VSphereClient":
        return cls(s.vcenter_host, s.vcenter_port, s.vcenter_user, s.vcenter_password, s.vcenter_verify_ssl)

    # ------------------------------------------------------------------ session
    def _connect(self) -> None:
        self._disconnect_quietly()
        kwargs = dict(host=self._host, user=self._user, pwd=self._password, port=self._port)
        if not self._verify_ssl:
            kwargs["disableSslCertValidation"] = True
        try:
            self._si = SmartConnect(**kwargs)
        except vim.fault.InvalidLogin as exc:
            raise VCenterUnavailableError("vCenter rejected the configured credentials") from exc
        except (OSError, vmodl.MethodFault) as exc:
            raise VCenterUnavailableError(
                f"Cannot reach vCenter at {self._host}:{self._port}: {exc}"
            ) from exc
        log.info("Connected to vCenter at %s:%s", self._host, self._port)

    def _disconnect_quietly(self) -> None:
        if self._si is not None:
            try:
                Disconnect(self._si)
            except Exception:  # noqa: BLE001 - best effort during reconnect/shutdown
                pass
            self._si = None

    def _content(self):
        """Return the service content, (re)connecting if the session went stale."""
        if self._si is None:
            self._connect()
        else:
            try:
                self._si.CurrentTime()  # cheap call that fails when the session expired
            except Exception:  # noqa: BLE001
                log.warning("vCenter session looks stale, reconnecting")
                self._connect()
        return self._si.RetrieveContent()

    def close(self) -> None:
        with self._lock:
            self._disconnect_quietly()

    # --------------------------------------------------------------- retrieval
    def _objects(self, content, vimtype) -> list:
        """All managed objects of one type, as lazy references."""
        view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def _retrieve(self, content, vimtype, paths, ref=None) -> List[_Row]:
        """Fetch the given property paths in a single PropertyCollector call.

        With ref=None it returns every object of `vimtype`; with a reference it
        returns just that object. Follows result pages if the server paginates.
        """
        pc = vmodl.query.PropertyCollector
        prop_spec = pc.PropertySpec(type=vimtype, pathSet=list(paths), all=False)

        view = None
        try:
            if ref is None:
                view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
                traversal = pc.TraversalSpec(name="traverseView", path="view", skip=False, type=type(view))
                obj_spec = pc.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
            else:
                obj_spec = pc.ObjectSpec(obj=ref, skip=False)

            filter_spec = pc.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
            collector = content.propertyCollector
            result = collector.RetrievePropertiesEx(specSet=[filter_spec], options=pc.RetrieveOptions())

            rows: List[_Row] = []
            while result is not None:
                for oc in result.objects or []:
                    props = {dp.name: dp.val for dp in (oc.propSet or [])}
                    rows.append(_Row(oc.obj, props, list(oc.missingSet or [])))
                if not result.token:
                    break
                result = collector.ContinueRetrievePropertiesEx(token=result.token)
            return rows
        finally:
            if view is not None:
                view.Destroy()

    # ---------------------------------------------------------------- mapping
    @staticmethod
    def _vm_from_props(moid: str, props: dict, host_names: Optional[dict] = None) -> VM:
        host_ref = props.get("runtime.host")
        host_name = None
        if host_ref is not None:
            if host_names is not None and host_ref._moId in host_names:
                host_name = host_names[host_ref._moId]
            else:
                host_name = _safe(lambda: host_ref.name)

        tools = props.get("summary.guest.toolsRunningStatus")
        return VM(
            moid=moid,
            name=props.get("summary.config.name") or moid,
            uuid=props.get("summary.config.instanceUuid"),
            power_state=str(props.get("runtime.powerState", "unknown")),
            num_cpu=props.get("summary.config.numCpu"),
            memory_mb=props.get("summary.config.memorySizeMB"),
            guest_os=props.get("summary.config.guestFullName"),
            ip_address=props.get("summary.guest.ipAddress"),
            tools_status=str(tools) if tools is not None else None,
            host=host_name,
            cpu_usage_mhz=props.get("summary.quickStats.overallCpuUsage"),
            memory_usage_mb=props.get("summary.quickStats.guestMemoryUsage"),
        )

    @staticmethod
    def _host_to_model(host) -> Host:
        summary = host.summary
        hw = _safe(lambda: summary.hardware)
        stats = _safe(lambda: summary.quickStats)

        cores = _safe(lambda: hw.numCpuCores)
        mhz = _safe(lambda: hw.cpuMhz)
        mem_bytes = _safe(lambda: hw.memorySize)

        return Host(
            moid=host._moId,
            name=summary.config.name,
            connection_state=str(host.runtime.connectionState),
            power_state=str(host.runtime.powerState),
            version=_safe(lambda: summary.config.product.fullName),
            cpu_cores=cores,
            cpu_total_mhz=(cores * mhz) if cores and mhz else None,
            cpu_usage_mhz=_safe(lambda: stats.overallCpuUsage),
            memory_total_mb=int(mem_bytes / (1024 ** 2)) if mem_bytes else None,
            memory_usage_mb=_safe(lambda: stats.overallMemoryUsage),
        )

    @staticmethod
    def _datastore_to_model(ds) -> Datastore:
        s = ds.summary
        capacity = _safe(lambda: s.capacity)
        free = _safe(lambda: s.freeSpace)
        return Datastore(
            moid=ds._moId,
            name=s.name,
            type=_safe(lambda: s.type),
            accessible=bool(_safe(lambda: s.accessible, True)),
            capacity_gb=round(capacity / GB, 1) if capacity is not None else None,
            free_gb=round(free / GB, 1) if free is not None else None,
        )

    # -------------------------------------------------------------- public API
    def list_hosts(self) -> List[Host]:
        with self._lock:
            content = self._content()
            return [self._host_to_model(h) for h in self._objects(content, vim.HostSystem)]

    def list_datastores(self) -> List[Datastore]:
        with self._lock:
            content = self._content()
            return [self._datastore_to_model(d) for d in self._objects(content, vim.Datastore)]

    def list_vms(self) -> List[VM]:
        with self._lock:
            content = self._content()
            # Resolve host names once instead of one round trip per VM.
            host_names = {
                row.moid: row.props.get("name")
                for row in self._retrieve(content, vim.HostSystem, ["name"])
            }
            rows = self._retrieve(content, vim.VirtualMachine, _VM_PROPS)
            return [self._vm_from_props(r.moid, r.props, host_names) for r in rows]

    def get_vm(self, moid: str) -> VM:
        with self._lock:
            content = self._content()
            ref = vim.VirtualMachine(moid, self._si._stub)
            try:
                rows = self._retrieve(content, vim.VirtualMachine, _VM_PROPS, ref=ref)
            except vmodl.fault.ManagedObjectNotFound as exc:
                raise NotFoundError(f"VM {moid} not found") from exc

            if not rows or any(_is_not_found(m.fault) for m in rows[0].missing):
                raise NotFoundError(f"VM {moid} not found")
            return self._vm_from_props(rows[0].moid, rows[0].props)