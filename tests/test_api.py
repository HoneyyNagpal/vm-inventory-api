import pytest

from app import config
from app.vsphere import VCenterUnavailableError


def test_health_does_not_need_vcenter(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_list_vms_returns_all(client):
    body = client.get("/vms").json()
    assert [v["name"] for v in body] == ["web-01", "db-01", "batch-01"]


def test_vm_memory_usage_pct_is_computed(client):
    web = next(v for v in client.get("/vms").json() if v["name"] == "web-01")
    assert web["memory_usage_pct"] == 25.0


def test_filter_by_power_state(client):
    body = client.get("/vms", params={"power_state": "poweredOff"}).json()
    assert [v["name"] for v in body] == ["batch-01"]


def test_filter_by_host_and_name(client):
    body = client.get("/vms", params={"host": "esx-01", "name": "DB"}).json()
    assert [v["name"] for v in body] == ["db-01"]


def test_invalid_power_state_is_rejected(client):
    assert client.get("/vms", params={"power_state": "exploded"}).status_code == 422


def test_get_vm_by_moid(client):
    assert client.get("/vms/vm-2").json()["name"] == "db-01"


def test_get_unknown_vm_is_404(client):
    resp = client.get("/vms/vm-999")
    assert resp.status_code == 404
    assert "vm-999" in resp.json()["detail"]


def test_hosts_expose_usage_percentages(client):
    host = client.get("/hosts").json()[0]
    assert host["cpu_usage_pct"] == 85.0
    assert host["memory_usage_pct"] == 30.5


def test_datastore_usage_pct(client):
    ds = next(d for d in client.get("/datastores").json() if d["name"] == "ds-fast")
    assert ds["usage_pct"] == 97.0


def test_summary_counts(client):
    body = client.get("/summary").json()
    assert body["vms_total"] == 3
    assert body["vms_by_power_state"] == {"poweredOn": 2, "poweredOff": 1}
    assert body["hosts_total"] == 2
    assert body["hosts_connected"] == 1
    assert body["datastore_capacity_gb"] == 5000.0


def test_alerts_endpoint_uses_default_thresholds(client):
    report = client.get("/alerts").json()
    kinds = {a["kind"] for a in report["alerts"]}
    assert {"vm_powered_off", "vm_memory_high", "host_not_connected",
            "host_cpu_high", "datastore_usage_high"} <= kinds
    assert report["total"] == report["critical"] + report["warning"]


def test_alert_thresholds_are_configurable(client):
    report = client.get("/alerts", params={"host_cpu_pct": 90}).json()
    assert "host_cpu_high" not in {a["kind"] for a in report["alerts"]}


def test_threshold_out_of_range_is_rejected(client):
    assert client.get("/alerts", params={"datastore_pct": 150}).status_code == 422


def test_vcenter_down_returns_503(client, inventory):
    def boom():
        raise VCenterUnavailableError("Cannot reach vCenter at vcsim:8989")

    inventory.list_hosts = boom
    resp = client.get("/hosts")
    assert resp.status_code == 503
    assert "Cannot reach vCenter" in resp.json()["detail"]


class TestApiKey:
    @pytest.fixture(autouse=True)
    def enable_key(self, monkeypatch):
        settings = config.Settings(
            vcenter_host="x", vcenter_port=1, vcenter_user="u", vcenter_password="p",
            vcenter_verify_ssl=False, api_key="s3cret",
        )
        monkeypatch.setattr("app.main.get_settings", lambda: settings)

    def test_missing_key_is_401(self, client):
        assert client.get("/vms").status_code == 401

    def test_wrong_key_is_401(self, client):
        assert client.get("/vms", headers={"X-API-Key": "nope"}).status_code == 401

    def test_correct_key_is_allowed(self, client):
        assert client.get("/vms", headers={"X-API-Key": "s3cret"}).status_code == 200

    def test_health_stays_open(self, client):
        assert client.get("/health").status_code == 200
