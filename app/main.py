"""FastAPI app exposing vCenter inventory and simple health alerts as REST endpoints."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.models import VM, AlertReport, Datastore, Host, PowerState, Summary
from app.monitoring import Thresholds, evaluate
from app.vsphere import (
    InventoryProvider,
    NotFoundError,
    VCenterUnavailableError,
    VSphereClient,
)

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The client connects lazily on the first request, so the API starts even
    # when vCenter is still booting (as vcsim is in docker compose).
    app.state.client = VSphereClient.from_settings(get_settings())
    yield
    app.state.client.close()


app = FastAPI(
    title="VM Inventory API",
    version="1.0.0",
    description="REST service over the VMware vSphere API (pyvmomi): VMs, hosts, "
                "datastores and threshold based health alerts.",
    lifespan=lifespan,
)


# ------------------------------------------------------------------ dependencies
def get_client(request: Request) -> InventoryProvider:
    return request.app.state.client


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected is None:
        return  # auth disabled, intended for local demos only
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


Client = Depends(get_client)
Secured = [Depends(require_api_key)]


# -------------------------------------------------------------- error handling
@app.exception_handler(VCenterUnavailableError)
async def vcenter_unavailable_handler(_: Request, exc: VCenterUnavailableError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(NotFoundError)
async def not_found_handler(_: Request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# ------------------------------------------------------------------- endpoints
@app.get("/health", tags=["meta"])
def health():
    """Liveness only. Does not touch vCenter."""
    return {"status": "ok"}


@app.get("/vms", response_model=List[VM], dependencies=Secured, tags=["inventory"])
def list_vms(
    power_state: Optional[PowerState] = Query(None, description="Filter by power state"),
    host: Optional[str] = Query(None, description="Filter by ESXi host name"),
    name: Optional[str] = Query(None, description="Case-insensitive substring of the VM name"),
    client: InventoryProvider = Client,
):
    vms = client.list_vms()
    if power_state is not None:
        vms = [v for v in vms if v.power_state == power_state.value]
    if host is not None:
        vms = [v for v in vms if v.host == host]
    if name is not None:
        vms = [v for v in vms if name.lower() in v.name.lower()]
    return vms


@app.get("/vms/{moid}", response_model=VM, dependencies=Secured, tags=["inventory"])
def get_vm(moid: str, client: InventoryProvider = Client):
    return client.get_vm(moid)


@app.get("/hosts", response_model=List[Host], dependencies=Secured, tags=["inventory"])
def list_hosts(client: InventoryProvider = Client):
    return client.list_hosts()


@app.get("/datastores", response_model=List[Datastore], dependencies=Secured, tags=["inventory"])
def list_datastores(client: InventoryProvider = Client):
    return client.list_datastores()


@app.get("/summary", response_model=Summary, dependencies=Secured, tags=["inventory"])
def summary(client: InventoryProvider = Client):
    vms = client.list_vms()
    hosts = client.list_hosts()
    datastores = client.list_datastores()

    by_state: dict = {}
    for vm in vms:
        by_state[vm.power_state] = by_state.get(vm.power_state, 0) + 1

    return Summary(
        vms_total=len(vms),
        vms_by_power_state=by_state,
        hosts_total=len(hosts),
        hosts_connected=sum(1 for h in hosts if h.connection_state == "connected"),
        datastores_total=len(datastores),
        datastore_capacity_gb=round(sum(d.capacity_gb or 0 for d in datastores), 1),
        datastore_free_gb=round(sum(d.free_gb or 0 for d in datastores), 1),
    )


@app.get("/alerts", response_model=AlertReport, dependencies=Secured, tags=["monitoring"])
def alerts(
    host_cpu_pct: float = Query(80, ge=1, le=100, description="Host CPU usage alert threshold"),
    host_memory_pct: float = Query(85, ge=1, le=100, description="Host memory usage alert threshold"),
    datastore_pct: float = Query(85, ge=1, le=100, description="Datastore usage alert threshold"),
    vm_memory_pct: float = Query(90, ge=1, le=100, description="VM guest memory alert threshold"),
    client: InventoryProvider = Client,
):
    """Powered-off VMs, disconnected hosts, inaccessible datastores, and anything
    over the given usage thresholds. A breach at 95% or above is critical."""
    thresholds = Thresholds(host_cpu_pct, host_memory_pct, datastore_pct, vm_memory_pct)
    return evaluate(client.list_vms(), client.list_hosts(), client.list_datastores(), thresholds)
