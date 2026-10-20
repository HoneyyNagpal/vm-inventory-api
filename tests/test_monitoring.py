from app.models import VM, Datastore, Host
from app.monitoring import Thresholds, evaluate


def host(**kw):
    base = dict(moid="h", name="esx", connection_state="connected", power_state="poweredOn",
                cpu_total_mhz=10000, memory_total_mb=10000)
    base.update(kw)
    return Host(**base)


def kinds(report):
    return [a.kind for a in report.alerts]


def test_healthy_inventory_has_no_alerts():
    vm = VM(moid="v", name="ok", power_state="poweredOn", memory_mb=1000, memory_usage_mb=100)
    ds = Datastore(moid="d", name="ds", capacity_gb=100.0, free_gb=80.0)
    report = evaluate([vm], [host(cpu_usage_mhz=1000, memory_usage_mb=1000)], [ds])
    assert report.total == 0


def test_powered_off_vm_is_a_warning():
    vm = VM(moid="v", name="idle", power_state="poweredOff")
    report = evaluate([vm], [], [])
    assert kinds(report) == ["vm_powered_off"]
    assert report.alerts[0].severity == "warning"


def test_threshold_is_inclusive():
    report = evaluate([], [host(cpu_usage_mhz=8000)], [], Thresholds(host_cpu_pct=80))
    assert kinds(report) == ["host_cpu_high"]


def test_just_below_threshold_is_quiet():
    report = evaluate([], [host(cpu_usage_mhz=7900)], [], Thresholds(host_cpu_pct=80))
    assert report.total == 0


def test_breach_at_95_percent_is_critical():
    report = evaluate([], [host(cpu_usage_mhz=9600)], [])
    assert report.alerts[0].severity == "critical"


def test_disconnected_host_is_critical_and_its_stats_are_ignored():
    h = host(connection_state="disconnected", cpu_usage_mhz=9900, memory_usage_mb=9900)
    report = evaluate([], [h], [])
    assert kinds(report) == ["host_not_connected"]
    assert report.critical == 1


def test_inaccessible_datastore_is_critical():
    ds = Datastore(moid="d", name="lost", accessible=False, capacity_gb=100.0, free_gb=50.0)
    report = evaluate([], [], [ds])
    assert kinds(report) == ["datastore_inaccessible"]


def test_missing_stats_do_not_crash_or_alert():
    vm = VM(moid="v", name="no-stats", power_state="poweredOn")
    report = evaluate([vm], [host()], [Datastore(moid="d", name="ds")])
    assert report.total == 0


def test_critical_alerts_sort_first():
    vm = VM(moid="v", name="idle", power_state="poweredOff")
    h = host(connection_state="disconnected")
    report = evaluate([vm], [h], [])
    assert [a.severity for a in report.alerts] == ["critical", "warning"]
