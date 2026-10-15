import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import manifest as mf
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


def _activation(injector, from_min, until_min):
    return {"injector": injector, "params": {}, "observed": {},
            "active_from": _iso(T0 + timedelta(minutes=from_min)),
            "active_until": _iso(T0 + timedelta(minutes=until_min))}


def test_phases_follow_the_manifest_windows():
    names = [name for name, _, _ in tr.phases(_manifest())]
    assert names == ["baseline", "fault: traffic-spike", "recovery"]
    assert [name for name, _, _ in tr.phases(_manifest(activations=False))] == ["normal"]


def test_overlapping_faults_get_their_own_phase():
    # A cascade: the second fault starts while the first is still active.
    manifest = {**_manifest(), "activations": [_activation("traffic-spike", 5, 9),
                                               _activation("db-throttling", 7, 11)]}
    spans = tr.phases(manifest)
    assert [(name, int((a - T0).total_seconds() // 60), int((b - T0).total_seconds() // 60))
            for name, a, b in spans] == [
        ("baseline", 0, 5), ("fault: traffic-spike", 5, 7),
        ("fault: traffic-spike + db-throttling", 7, 9), ("fault: db-throttling", 9, 11),
        ("recovery", 11, 15)]


def test_every_record_lands_in_exactly_one_phase_even_between_faults():
    manifest = {**_manifest(), "activations": [_activation("traffic-spike", 2, 4),
                                               _activation("traffic-spike", 8, 10)]}
    assert [name for name, _, _ in tr.phases(manifest)] == [
        "baseline", "fault: traffic-spike", "no fault", "fault: traffic-spike", "recovery"]
    report = tr.summarize(manifest, _records(20, 0, 15, 40))
    assert sum(p["services"]["product-service"]["records"] for p in report["phases"]) == 20 * 60 * 15
    # Two phases share a name; each keeps its own records and rate.
    assert [p["client_rps"] for p in report["phases"]] == [20] * 5


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


def test_4xx_is_reported_apart_from_5xx():
    # A request-shape bug answered with 400s must not look healthy.
    records = _records(20, 0, 5, 40) + _records(4, 0, 5, 30, service="cart-service", status=400)
    baseline = tr.summarize(_manifest(), records)["phases"][0]
    assert baseline["services"]["cart-service"]["4xx_share"] == 1
    assert baseline["services"]["cart-service"]["5xx_share"] == 0
    assert baseline["services"]["product-service"]["4xx_share"] == 0


def test_payment_is_reported_but_not_counted_as_client_traffic():
    records = _records(20, 0, 5, 40) + _records(5, 0, 5, 30, service="payment-service")
    baseline = tr.summarize(_manifest(), records)["phases"][0]
    assert baseline["client_rps"] == 20
    assert baseline["services"]["payment-service"]["rps"] == 5


def test_steady_traffic_has_low_per_minute_variation():
    report = tr.summarize(_manifest(activations=False), _records(20, 0, 15, 40))
    assert report["per_minute"] == [1200] * 15
    assert report["per_minute_cv"] == 0


def test_the_table_prints_every_phase_and_both_error_shares(capsys):
    manifest = {**_manifest(), "activations": [_activation("traffic-spike", 5, 9),
                                               _activation("db-throttling", 7, 11)]}
    records = _records(20, 0, 15, 40) + _records(5, 6, 6 + 1 / 60, 40, status=503)
    tr._print(manifest, tr.summarize(manifest, records))
    out = capsys.readouterr().out
    assert "fault: traffic-spike + db-throttling" in out
    assert "4xx" in out and "5xx" in out
    # Five 5xx among 2,400 requests show as 0.2%, instead of rounding to 0%.
    assert "0.0%" in out and "0.2%" in out


def test_a_run_that_died_is_refused_not_crashed_on(aws, monkeypatch, capsys):
    bucket = "anomalypulse-telemetry-000000000000-us-east-2"   # conftest's
    dead = {**_manifest(), "status": "RUNNING", "ended_at": None}
    aws.client("s3").put_object(Bucket=bucket, Key=mf.manifest_key("exp-x"), Body=json.dumps(dead))
    monkeypatch.setattr(tr.boto3, "Session", lambda **kwargs: aws)
    assert tr.main(["exp-x", "--profile", "dev"]) == 1
    assert "still RUNNING with no end time" in capsys.readouterr().err


def test_records_outside_the_run_are_ignored():
    outside = _records(20, -2, 0, 40) + _records(20, 15, 16, 40)
    report = tr.summarize(_manifest(), outside)
    assert all(p["client_rps"] == 0 for p in report["phases"])
