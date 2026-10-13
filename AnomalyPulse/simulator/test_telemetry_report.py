import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import telemetry_report as tr

T0 = datetime(2026, 10, 12, 4, 15, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _manifest(activations=True):
    acts = [{"injector": "traffic-spike", "params": {"multiplier": 3}, "observed": {},
             "active_from": _iso(T0 + timedelta(minutes=5)),
             "active_until": _iso(T0 + timedelta(minutes=10))}] if activations else []
    return {"experiment_id": "exp-x", "incident_type": "TRAFFIC_SPIKE", "status": "COMPLETED",
            "started_at": _iso(T0), "ended_at": _iso(T0 + timedelta(minutes=15)),
            "activations": acts}


def _records(rps, start_min, end_min, latency, service="product-service", status=200):
    out = []
    for i in range(int((end_min - start_min) * 60 * rps)):
        ts = T0 + timedelta(minutes=start_min, seconds=i / rps)
        out.append({"timestamp": _iso(ts), "service": service, "latency_ms": latency,
                    "status_code": status})
    return out


def test_phases_follow_the_manifest_windows():
    names = [name for name, _, _ in tr.phases(_manifest())]
    assert names == ["baseline", "fault: traffic-spike", "recovery"]
    assert [name for name, _, _ in tr.phases(_manifest(activations=False))] == ["normal"]


def test_the_spike_shows_as_a_rate_and_latency_shift():
    records = (_records(20, 0, 5, 40) + _records(60, 5, 10, 90, status=500)
               + _records(20, 10, 15, 42))
    report = tr.summarize(_manifest(), records)
    baseline, spike, recovery = report["phases"]
    assert baseline["client_rps"] == 20
    assert spike["client_rps"] == 60
    assert recovery["client_rps"] == 20
    assert spike["services"]["product-service"]["p95_ms"] == 90
    assert spike["services"]["product-service"]["5xx_share"] == 1
    assert baseline["services"]["product-service"]["5xx_share"] == 0


def test_payment_is_reported_but_not_counted_as_client_traffic():
    records = _records(20, 0, 5, 40) + _records(5, 0, 5, 30, service="payment-service")
    baseline = tr.summarize(_manifest(), records)["phases"][0]
    assert baseline["client_rps"] == 20
    assert baseline["services"]["payment-service"]["rps"] == 5


def test_steady_traffic_has_low_per_minute_variation():
    report = tr.summarize(_manifest(activations=False), _records(20, 0, 15, 40))
    assert report["per_minute"] == [1200] * 15
    assert report["per_minute_cv"] == 0


def test_records_outside_the_run_are_ignored():
    outside = _records(20, -2, 0, 40) + _records(20, 15, 16, 40)
    report = tr.summarize(_manifest(), outside)
    assert all(p["client_rps"] == 0 for p in report["phases"])
