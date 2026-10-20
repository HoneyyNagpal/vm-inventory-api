"""Checks the pyvmomi layer without a vCenter: object -> model conversion, the
PropertyCollector request that fetches VMs, and parsing a real SOAP response.

Host and datastore tests use stand-ins shaped like vSphere managed objects,
including the awkward cases (disconnected hosts with no hardware info).
"""
import io
from types import SimpleNamespace as NS

import pytest
from pyVmomi import SoapAdapter, vim, vmodl

from app import vsphere
from app.vsphere import NotFoundError, VSphereClient

GB = 1024 ** 3
PC = vmodl.query.PropertyCollector


# ------------------------------------------------------------------ test doubles
class FakeStub:
    """Stands in for the SOAP stub so managed-object calls (view.Destroy) are recorded."""

    def __init__(self):
        self.calls = []

    def InvokeMethod(self, mo, info, args):
        self.calls.append(info.name)


class FakeCollector:
    """Returns canned PropertyCollector results, keyed by the requested object type."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.requests = []
        self.continued = []

    def RetrievePropertiesEx(self, specSet, options):
        if self.raises:
            raise self.raises
        spec = specSet[0]
        self.requests.append(spec)
        pages = self.responses.get(spec.propSet[0].type)
        return pages[0] if pages else None

    def ContinueRetrievePropertiesEx(self, token):
        self.continued.append(token)
        pages = next(iter(self.responses.values()))
        return pages[int(token)]


def make_content(collector, view_stub=None):
    stub = view_stub or FakeStub()
    view = vim.view.ContainerView("session[1]view", stub)
    content = NS(
        rootFolder=object(),
        viewManager=NS(CreateContainerView=lambda root, types, recursive: view),
        propertyCollector=collector,
    )
    return content, stub


def oc(moid, props, vimtype=vim.VirtualMachine, missing=None):
    return PC.ObjectContent(
        obj=vimtype(moid, None),
        propSet=[vmodl.DynamicProperty(name=k, val=v) for k, v in props.items()],
        missingSet=missing or [],
    )


def result(*objects, token=None):
    return PC.RetrieveResult(objects=list(objects), token=token)


def client_with(content, stub=None):
    client = VSphereClient("h", 1, "u", "p")
    client._si = NS(CurrentTime=lambda: None, RetrieveContent=lambda: content, _stub=stub or FakeStub())
    return client


# ------------------------------------------------------------------ VM mapping
def test_running_vm_is_mapped_with_host_name_and_usage():
    props = {
        "summary.config.name": "web-01",
        "summary.config.instanceUuid": "uuid-1",
        "summary.config.numCpu": 2,
        "summary.config.memorySizeMB": 4096,
        "summary.config.guestFullName": "Ubuntu Linux (64-bit)",
        "summary.guest.ipAddress": "10.0.0.5",
        "summary.guest.toolsRunningStatus": "guestToolsRunning",
        "summary.quickStats.overallCpuUsage": 250,
        "summary.quickStats.guestMemoryUsage": 2048,
        "runtime.powerState": "poweredOn",
        "runtime.host": vim.HostSystem("host-10", None),
    }
    model = VSphereClient._vm_from_props("vm-42", props, host_names={"host-10": "esx-01.lab"})
    assert model.moid == "vm-42"
    assert model.name == "web-01"
    assert model.host == "esx-01.lab"
    assert model.memory_usage_pct == 50.0
    assert model.ip_address == "10.0.0.5"
    assert model.tools_status == "guestToolsRunning"


def test_powered_off_vm_without_stats_still_maps():
    props = {
        "summary.config.name": "batch-01",
        "summary.config.numCpu": 1,
        "summary.config.memorySizeMB": 1024,
        "runtime.powerState": "poweredOff",
    }
    model = VSphereClient._vm_from_props("vm-7", props)
    assert model.power_state == "poweredOff"
    assert model.host is None
    assert model.cpu_usage_mhz is None
    assert model.memory_usage_pct is None


def test_vm_with_no_properties_falls_back_to_moid_and_unknown_state():
    model = VSphereClient._vm_from_props("vm-9", {})
    assert model.name == "vm-9"
    assert model.power_state == "unknown"


# ---------------------------------------------------- the empty-enum regression
def test_vm_properties_are_leaf_paths_only():
    """Aggregates like 'summary' or 'runtime' contain faultToleranceState, which vcsim
    leaves empty and pyvmomi cannot parse. Only leaf paths may be requested."""
    aggregates = {"summary", "runtime", "config", "guest", "summary.runtime", "summary.config",
                  "summary.guest", "summary.quickStats"}
    assert not aggregates & set(vsphere._VM_PROPS)
    assert all("." in p for p in vsphere._VM_PROPS)


SOAP_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/"
 xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<soapenv:Body><RetrievePropertiesExResponse xmlns="urn:vim25"><returnval><objects>
<obj type="VirtualMachine">vm-62</obj>
<propSet><name>runtime.powerState</name><val xsi:type="VirtualMachinePowerState">poweredOn</val></propSet>
<propSet><name>runtime.host</name><val type="HostSystem" xsi:type="ManagedObjectReference">host-21</val></propSet>
<propSet><name>summary.config.name</name><val xsi:type="xsd:string">DC0_H0_VM0</val></propSet>
<propSet><name>summary.config.numCpu</name><val xsi:type="xsd:int">2</val></propSet>
<propSet><name>summary.config.memorySizeMB</name><val xsi:type="xsd:int">32</val></propSet>
</objects></returnval></RetrievePropertiesExResponse></soapenv:Body></soapenv:Envelope>"""


def test_real_soap_response_with_leaf_properties_maps_to_a_vm():
    """Runs a SOAP reply through pyvmomi's real deserializer, then through our mapping."""
    stub = SoapAdapter.SoapStubAdapter("localhost", 1)
    parsed = SoapAdapter.SoapResponseDeserializer(stub).Deserialize(
        io.BytesIO(SOAP_ENVELOPE.encode()), PC.RetrieveResult
    )
    content = parsed.objects[0]
    props = {dp.name: dp.val for dp in content.propSet}

    model = VSphereClient._vm_from_props(content.obj._moId, props, host_names={"host-21": "DC0_H0"})
    assert model.moid == "vm-62"
    assert model.name == "DC0_H0_VM0"
    assert model.power_state == "poweredOn"
    assert model.host == "DC0_H0"
    assert model.num_cpu == 2


# -------------------------------------------------------------------- retrieval
def test_retrieve_builds_one_call_for_all_objects_and_destroys_the_view():
    collector = FakeCollector({vim.VirtualMachine: [result(oc("vm-1", {"runtime.powerState": "poweredOn"}))]})
    content, stub = make_content(collector)

    rows = VSphereClient("h", 1, "u", "p")._retrieve(content, vim.VirtualMachine, vsphere._VM_PROPS)

    assert [r.moid for r in rows] == ["vm-1"]
    assert rows[0].props == {"runtime.powerState": "poweredOn"}

    spec = collector.requests[0]
    assert list(spec.propSet[0].pathSet) == vsphere._VM_PROPS
    assert spec.propSet[0].all is False
    assert spec.objectSet[0].skip is True
    assert spec.objectSet[0].selectSet[0].path == "view"
    assert stub.calls == ["Destroy"]


def test_retrieve_follows_result_pages():
    pages = [
        result(oc("vm-1", {"summary.config.name": "a"}), token="1"),
        result(oc("vm-2", {"summary.config.name": "b"})),
    ]
    collector = FakeCollector({vim.VirtualMachine: pages})
    content, _ = make_content(collector)

    rows = VSphereClient("h", 1, "u", "p")._retrieve(content, vim.VirtualMachine, ["summary.config.name"])

    assert [r.moid for r in rows] == ["vm-1", "vm-2"]
    assert collector.continued == ["1"]


def test_retrieve_with_a_reference_skips_the_view():
    collector = FakeCollector({vim.VirtualMachine: [result(oc("vm-3", {"summary.config.name": "c"}))]})
    content, stub = make_content(collector)

    ref = vim.VirtualMachine("vm-3", None)
    rows = VSphereClient("h", 1, "u", "p")._retrieve(content, vim.VirtualMachine, ["summary.config.name"], ref=ref)

    assert rows[0].moid == "vm-3"
    assert collector.requests[0].objectSet[0].skip is False
    assert stub.calls == []  # no view was created, so nothing to destroy


def test_retrieve_destroys_the_view_even_when_the_call_fails():
    collector = FakeCollector(raises=RuntimeError("boom"))
    content, stub = make_content(collector)

    with pytest.raises(RuntimeError):
        VSphereClient("h", 1, "u", "p")._retrieve(content, vim.VirtualMachine, ["name"])
    assert stub.calls == ["Destroy"]


# ---------------------------------------------------------------- list / get VM
def test_list_vms_resolves_host_names_in_one_extra_call():
    collector = FakeCollector({
        vim.HostSystem: [result(oc("host-21", {"name": "DC0_H0"}, vim.HostSystem))],
        vim.VirtualMachine: [result(
            oc("vm-1", {"summary.config.name": "web", "runtime.powerState": "poweredOn",
                        "runtime.host": vim.HostSystem("host-21", None)}),
            oc("vm-2", {"summary.config.name": "db", "runtime.powerState": "poweredOff"}),
        )],
    })
    content, _ = make_content(collector)

    vms = client_with(content).list_vms()

    assert [(v.name, v.power_state, v.host) for v in vms] == [
        ("web", "poweredOn", "DC0_H0"),
        ("db", "poweredOff", None),
    ]
    assert len(collector.requests) == 2  # one for hosts, one for all VMs


def test_get_vm_returns_the_vm():
    collector = FakeCollector({vim.VirtualMachine: [result(oc("vm-5", {"summary.config.name": "app"}))]})
    content, _ = make_content(collector)

    assert client_with(content).get_vm("vm-5").name == "app"


def test_get_vm_when_server_raises_not_found():
    ref = vim.VirtualMachine("vm-99", None)
    collector = FakeCollector(raises=vmodl.fault.ManagedObjectNotFound(obj=ref))
    content, _ = make_content(collector)

    with pytest.raises(NotFoundError, match="vm-99"):
        client_with(content).get_vm("vm-99")


def test_get_vm_when_server_returns_nothing():
    content, _ = make_content(FakeCollector())

    with pytest.raises(NotFoundError, match="vm-99"):
        client_with(content).get_vm("vm-99")


def test_get_vm_when_object_is_reported_missing():
    ref = vim.VirtualMachine("vm-99", None)
    missing = PC.MissingProperty(
        path="summary.config.name",
        fault=vmodl.LocalizedMethodFault(fault=vmodl.fault.ManagedObjectNotFound(obj=ref)),
    )
    collector = FakeCollector({vim.VirtualMachine: [result(oc("vm-99", {}, missing=[missing]))]})
    content, _ = make_content(collector)

    with pytest.raises(NotFoundError, match="vm-99"):
        client_with(content).get_vm("vm-99")


# ------------------------------------------------------------- hosts, datastores
def test_host_totals_are_derived_from_cores_and_clock():
    host = NS(
        _moId="host-1",
        summary=NS(
            config=NS(name="esx-01", product=NS(fullName="VMware ESXi 8.0.2")),
            hardware=NS(numCpuCores=8, cpuMhz=2500, memorySize=64 * GB),
            quickStats=NS(overallCpuUsage=10000, overallMemoryUsage=32768),
        ),
        runtime=NS(connectionState="connected", powerState="poweredOn"),
    )
    model = VSphereClient._host_to_model(host)
    assert model.cpu_total_mhz == 20000
    assert model.cpu_usage_pct == 50.0
    assert model.memory_total_mb == 65536
    assert model.memory_usage_pct == 50.0


def test_disconnected_host_without_hardware_info_maps():
    host = NS(
        _moId="host-2",
        summary=NS(config=NS(name="esx-02")),   # no hardware / quickStats attributes
        runtime=NS(connectionState="disconnected", powerState="unknown"),
    )
    model = VSphereClient._host_to_model(host)
    assert model.connection_state == "disconnected"
    assert model.cpu_total_mhz is None
    assert model.cpu_usage_pct is None


def test_datastore_bytes_are_converted_to_gb():
    ds = NS(
        _moId="datastore-1",
        summary=NS(name="ds-fast", type="VMFS", accessible=True,
                   capacity=1000 * GB, freeSpace=250 * GB),
    )
    model = VSphereClient._datastore_to_model(ds)
    assert model.capacity_gb == 1000.0
    assert model.free_gb == 250.0
    assert model.usage_pct == 75.0