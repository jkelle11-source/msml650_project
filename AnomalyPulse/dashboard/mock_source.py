"""The mock source (M7-2): the contract's six scenarios, as the inputs build_dashboard takes.

Each fixture takes `at`, which turns an offset in seconds from the latest complete minute into a time
string, so every scenario is current whenever it is read. The numbers are illustrative, on the
workload's baseline of 20 requests a second: Product 15, Cart 2, Order 3 (Payment's 1 a second is
reached through Order). Nothing here comes from the validation or test sets.
"""

from datetime import datetime, timedelta
from builder import iso

MODEL = {"estimator": "logistic_regression", "job": "anomalypulse-hpo-261028173159-003-28af89d1"}


def _window(at, start_s, product, cart, order, payment, system_p95_ms):
    """A feature window starting start_s after the latest minute; each service is (per second, 5xx share, p95 ms)."""
    window = {"window_start": at(start_s), "system_p95_latency_ms": system_p95_ms}
    for prefix, (rate, errors, p95) in (("product", product), ("cart", cart), ("order", order), ("payment", payment)):
        window |= {f"{prefix}_request_rate": rate, f"{prefix}_error_rate_5xx": errors, f"{prefix}_p95_latency_ms": p95}
    return window


def _baseline(at, start_s=0):
    return _window(at, start_s, product=(15.0, 0.0, 41.0), cart=(2.0, 0.0, 38.0), order=(3.0, 0.0, 160.0),
                   payment=(1.0, 0.0, 110.0), system_p95_ms=64.0)


def _payment_failure(at):
    """The contract's example: Payment fails half its calls and slows to 2.9 s; Order fails the checkouts that hit it."""
    window = _window(at, 0, product=(15.0, 0.0, 41.0), cart=(2.0, 0.0, 38.0), order=(3.0, 30 / 180, 2950.0),
                     payment=(1.0, 0.5, 2900.0), system_p95_ms=260.0)
    incident = {
        "incident_id": "inc-mock-payment-failure",
        "prediction": {"incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93,
                       "started_at": at(-60), "detected_at": at(4),
                       "affected_services": ["order-service", "payment-service"], "model": MODEL},
        "diagnosis": {
            "severity": "HIGH",
            "summary": "Payment latency rose from 110 ms to 2.9 s. Order failures began about 40 seconds later "
                       "(observed sequence from correlated timestamps).",
            "root_cause": "Payment dependency timeout",
            "evidence": ["Payment p95 latency rose 26x.", "Order 5xx share rose from 0% to 16.7%."],
            "recommended_actions": ["Inspect payment service health", "Review timeout/retry configuration",
                                    "Check recent deployment"]}}
    manifest = {
        "experiment_id": "exp-mock-payment-failure", "status": "RUNNING",
        "incident_type": "DEPENDENCY_FAILURE", "severity": "HIGH", "started_at": at(-185), "ended_at": None,
        "activations": [
            {"fault_id": "exp-mock-payment-failure#0", "injector": "payment-failure", "target": "payment-service",
             "intensity": 0.8, "band": None, "params": {}, "observed": None,
             "active_from": at(-65), "active_until": at(235)}]}
    return window, incident, manifest


def _healthy(at):
    return _baseline(at), None, None


def _incident(at):
    return _payment_failure(at)


def _diagnosing(at):
    window, incident, manifest = _payment_failure(at)
    return window, {**incident, "diagnosis": None}, manifest


def _no_ground_truth(at):
    window, incident, _ = _payment_failure(at)
    return window, incident, None


def _cascade_early(at):
    """A traffic spike that a payment failure will join in two minutes: only the spike is active yet."""
    window = _window(at, 0, product=(45.0, 0.01, 180.0), cart=(6.0, 0.0, 150.0), order=(9.0, 0.0, 420.0),
                     payment=(3.0, 0.0, 300.0), system_p95_ms=210.0)
    incident = {
        "incident_id": "inc-mock-cascade",
        "prediction": {"incident_type": "TRAFFIC_SPIKE", "confidence": 0.88,
                       "started_at": at(-60), "detected_at": at(4),
                       "affected_services": ["product-service", "cart-service", "order-service"], "model": MODEL},
        "diagnosis": {
            "severity": "MEDIUM",
            "summary": "Requests to Product, Cart and Order rose to about three times baseline, and Product p95 "
                       "latency rose from 41 ms to 180 ms (observed sequence from correlated timestamps).",
            "root_cause": "Traffic surge beyond the baseline load",
            "evidence": ["Product requests rose from 900 to 2,700 a minute.", "Product p95 latency rose 4.4x."],
            "recommended_actions": ["Check the traffic source", "Review Lambda concurrency limits",
                                    "Watch downstream services for errors"]}}
    manifest = {
        "experiment_id": "exp-mock-cascade", "status": "RUNNING",
        "incident_type": "CASCADING_FAILURE", "severity": "HIGH", "started_at": at(-185), "ended_at": None,
        "activations": [
            {"fault_id": "exp-mock-cascade#0", "injector": "traffic-spike", "target": None,
             "intensity": 0.7, "band": None, "params": {}, "observed": None,
             "active_from": at(-65), "active_until": at(535)},
            {"fault_id": "exp-mock-cascade#1", "injector": "payment-failure", "target": "payment-service",
             "intensity": 0.8, "band": None, "params": {}, "observed": None,
             "active_from": at(120), "active_until": at(420)}]}
    return window, incident, manifest


def _no_data(at):
    # The pipeline stopped ten minutes ago: its last window is still there, and too old to show.
    return _baseline(at, start_s=-600), None, None


FIXTURES = {"healthy": _healthy, "diagnosing": _diagnosing, "incident": _incident,
            "cascade_early": _cascade_early, "no_ground_truth": _no_ground_truth, "no_data": _no_data}
SCENARIOS = tuple(FIXTURES)

def read(scenario: str, now: datetime) -> tuple[dict | None, dict | None, dict | None]:
    """(window, incident, manifest) for one scenario, with every time placed relative to now."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; use one of: {', '.join(SCENARIOS)}")
    
    minute = now.replace(second = 0, microsecond = 0) - timedelta(minutes = 1)
    
    def at(offset_s: int) -> str:
        return iso(minute + timedelta(seconds = offset_s))
    
    return FIXTURES[scenario](at)
