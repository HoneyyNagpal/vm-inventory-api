import pytest
from fastapi.testclient import TestClient

from app.main import app, get_client
from app.models import VM, Datastore, Host
from app.vsphere import NotFoundError


class FakeInventory:
    """In-memory stand-in for VSphereClient so API tests need no vCenter."""

    def __init__(self):
        self.vms = [
            VM(moid="vm-1", name="web-01", power_state="poweredOn", num_cpu=2, memory_mb=4096,
               host="esx-01", memory_usage_mb=1024, cpu_usage_mhz=300),
            VM(moid="vm-2", name="db-01", power_state="poweredOn", num_cpu=4, memory_mb=8192,
               host="esx-01", memory_usage_mb=7900),
            VM(moid="vm-3", name="batch-01", power_state="poweredOff", num_cpu=2, memory_mb=2048,
               host="esx-02"),
        ]
        self.hosts = [
            Host(moid="host-1", name="esx-01", connection_state="connected", power_state="poweredOn",
                 cpu_cores=8, cpu_total_mhz=20000, cpu_usage_mhz=17000,
                 memory_total_mb=65536, memory_usage_mb=20000),
            Host(moid="host-2", name="esx-02", connection_state="disconnected", power_state="poweredOn"),
        ]
        self.datastores = [
            Datastore(moid="ds-1", name="ds-fast", type="VMFS", capacity_gb=1000.0, free_gb=30.0),
            Datastore(moid="ds-2", name="ds-bulk", type="NFS", capacity_gb=4000.0, free_gb=3000.0),
        ]

    def list_vms(self):
        return self.vms

    def get_vm(self, moid):
        for vm in self.vms:
            if vm.moid == moid:
                return vm
        raise NotFoundError(f"VM {moid} not found")

    def list_hosts(self):
        return self.hosts

    def list_datastores(self):
        return self.datastores

    def close(self):
        pass


@pytest.fixture
def inventory():
    return FakeInventory()


@pytest.fixture
def client(inventory):
    app.dependency_overrides[get_client] = lambda: inventory
    yield TestClient(app)
    app.dependency_overrides.clear()
