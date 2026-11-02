"""The diagnosis contract (M6-1): what Bedrock is given, and what it must give back.

Input: the classified incident as structured evidence, never raw logs or ground
truth. M8-3 builds it from the classifier's reply and the incident's feature
windows; Docs/DIAGNOSIS_CONTRACT.md is the reference.

Output: a diagnosis in the dashboard contract's `diagnosis` shape (severity,
summary, root_cause, evidence, recommended_actions), plus which input metrics
each evidence bullet rests on.
"""
from datetime import datetime

CONTRACT_VERSION = 1

# The classifier never reports NORMAL as an incident (Docs/DASHBOARD_CONTRACT.md).
INCIDENT_TYPES = ("TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION",
                  "DEPENDENCY_FAILURE", "CASCADING_FAILURE")
SERVICES = ("product-service", "cart-service", "order-service", "payment-service")
SEVERITIES = ("LOW", "MEDIUM", "HIGH")

# A metric is a feature column without its service prefix (Docs/FEATURE_MATRIX.md),
# so M8-3 reads `<service>_<metric>` from the windows. Every one is Tier-1.
# name -> (unit, what it measures)
METRICS = {
    "request_rate": ("rps", "requests per second to the service"),
    "p50_latency_ms": ("ms", "median end-to-end latency"),
    "p95_latency_ms": ("ms", "95th percentile end-to-end latency"),
    "p99_latency_ms": ("ms", "99th percentile end-to-end latency"),
    "error_rate_5xx": ("share", "share of requests answered 500-599, from 0 to 1"),
    "error_rate_4xx": ("share", "share of requests answered 400-499, from 0 to 1"),
    "lambda_duration_ms_mean": ("ms", "mean CPU time of the handler body"),
    "lambda_duration_ms_p95": ("ms", "95th percentile CPU time of the handler body"),
    "cold_start_count": ("count/min", "requests that paid a Lambda cold start"),
    "lambda_throttle_count": ("count/min", "invocations Lambda throttled (CloudWatch)"),
    "db_latency_ms_mean": ("ms", "mean time in the DynamoDB call, SDK retries included"),
    "db_latency_ms_p95": ("ms", "95th percentile time in the DynamoDB call, SDK retries included"),
    "db_consumed_read_capacity": ("RCU/min", "DynamoDB read capacity consumed (CloudWatch)"),
    "db_consumed_write_capacity": ("WCU/min", "DynamoDB write capacity consumed (CloudWatch)"),
    "throttled_requests": ("count/min", "throttled DynamoDB requests on the table (CloudWatch)"),
    "read_throttle_events": ("count/min", "throttled DynamoDB reads on the table (CloudWatch)"),
    "write_throttle_events": ("count/min", "throttled DynamoDB writes on the table (CloudWatch)"),
    "dependency_latency_ms_mean": ("ms", "mean time Order spent calling Payment"),
    "dependency_latency_ms_p95": ("ms", "95th percentile time Order spent calling Payment"),
    "dependency_error_count": ("count/min", "Order's calls to Payment that failed or timed out"),
}

# Fields that would leak the answer (Tier-2). An input carrying any of them is refused.
FORBIDDEN_KEYS = {"ground_truth", "manifest", "experiment_id", "fault_injection_params", "params",
                  "error_type", "db_throttled", "injector", "band", "intensity", "label"}


class BadInput(ValueError):
    """An input that breaks the contract."""


def metric_ref(service: str, metric: str) -> str:
    """How evidence names an input metric: 'order-service.p95_latency_ms'."""
    return f"{service}.{metric}"


def _time(value, what):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BadInput(f"{what} must be a UTC time ending in Z, got {value!r}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BadInput(f"{what} is not a valid time: {value!r}") from None


def _number(value, what, *, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        raise BadInput(f"{what} must be a number{' or null' if nullable else ''}, got {value!r}")


def _all_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _all_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _all_keys(inner)


def validate_input(evidence: dict) -> dict:
    """Return `evidence` if it meets the input contract; raise BadInput naming the first problem."""
    if not isinstance(evidence, dict):
        raise BadInput("the input must be an object")
    leaked = sorted(FORBIDDEN_KEYS & set(_all_keys(evidence)))
    if leaked:
        raise BadInput(f"ground truth must never reach Bedrock; remove {', '.join(leaked)}")
    required = {"contract_version", "incident_id", "incident_type", "confidence", "started_at",
                "window_start", "duration_minutes", "affected_services", "metrics"}
    allowed = required | {"observed_sequence"}
    missing, extra = required - set(evidence), set(evidence) - allowed
    if missing or extra:
        raise BadInput("input fields: " + "; ".join(
            part for part in (f"missing {', '.join(sorted(missing))}" if missing else "",
                              f"not in the contract {', '.join(sorted(extra))}" if extra else "") if part))
    if evidence["contract_version"] != CONTRACT_VERSION:
        raise BadInput(f"contract_version must be {CONTRACT_VERSION}")
    if not isinstance(evidence["incident_id"], str) or not evidence["incident_id"]:
        raise BadInput("incident_id must be a non-empty string")
    if evidence["incident_type"] not in INCIDENT_TYPES:
        raise BadInput(f"incident_type must be one of {', '.join(INCIDENT_TYPES)}")
    _number(evidence["confidence"], "confidence")
    if not 0 <= evidence["confidence"] <= 1:
        raise BadInput("confidence must be from 0 to 1")
    started = _time(evidence["started_at"], "started_at")
    if _time(evidence["window_start"], "window_start") < started:
        raise BadInput("window_start can't be before started_at")
    _number(evidence["duration_minutes"], "duration_minutes")

    services = evidence["affected_services"]
    if not isinstance(services, list) or not services or any(s not in SERVICES for s in services) \
            or len(set(services)) != len(services):
        raise BadInput(f"affected_services must be distinct services from {', '.join(SERVICES)}")

    metrics = evidence["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise BadInput("metrics must be a non-empty list")
    seen = set()
    for i, m in enumerate(metrics):
        if not isinstance(m, dict) or set(m) != {"service", "metric", "baseline", "current", "unit"}:
            raise BadInput(f"metrics[{i}] must have exactly service, metric, baseline, current, unit")
        if m["service"] not in SERVICES:
            raise BadInput(f"metrics[{i}].service {m['service']!r} is not a service")
        if m["metric"] not in METRICS:
            raise BadInput(f"metrics[{i}].metric {m['metric']!r} is not in the contract's metrics")
        if m["unit"] != METRICS[m["metric"]][0]:
            raise BadInput(f"metrics[{i}] {m['metric']} is in {METRICS[m['metric']][0]}, not {m['unit']!r}")
        _number(m["baseline"], f"metrics[{i}].baseline", nullable=True)
        _number(m["current"], f"metrics[{i}].current")
        ref = metric_ref(m["service"], m["metric"])
        if ref in seen:
            raise BadInput(f"metrics lists {ref} twice")
        seen.add(ref)

    previous = None
    for i, step in enumerate(evidence.get("observed_sequence", [])):
        if not isinstance(step, dict) or set(step) != {"at", "service", "metric", "observation"}:
            raise BadInput(f"observed_sequence[{i}] must have exactly at, service, metric, observation")
        at = _time(step["at"], f"observed_sequence[{i}].at")
        if metric_ref(step["service"], step["metric"]) not in seen:
            raise BadInput(f"observed_sequence[{i}] names {step['service']}.{step['metric']}, "
                           f"which is not in metrics")
        if previous is not None and at < previous:
            raise BadInput("observed_sequence must be in time order")
        previous = at
    return evidence


def output_schema(evidence: dict) -> dict:
    """The JSON schema Bedrock's answer is held to (structured outputs).

    Built per request: an evidence bullet can only cite metrics this input has,
    so a citation of a metric that isn't there can't be produced at all.
    """
    refs = [metric_ref(m["service"], m["metric"]) for m in evidence["metrics"]]
    return {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "summary": {"type": "string"},
            "root_cause": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "metrics": {"type": "array", "items": {"type": "string", "enum": refs}},
                    },
                    "required": ["statement", "metrics"],
                    "additionalProperties": False,
                },
            },
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["severity", "summary", "root_cause", "evidence", "recommended_actions"],
        "additionalProperties": False,
    }


def check_output_shape(raw: dict) -> list[str]:
    """Shape problems the schema can't express (counts, empty strings). Empty means well-formed."""
    problems = []
    for field in ("summary", "root_cause"):
        if not str(raw.get(field, "")).strip():
            problems.append(f"{field} is empty")
    evidence, actions = raw.get("evidence") or [], raw.get("recommended_actions") or []
    if not 1 <= len(evidence) <= 6:
        problems.append(f"evidence has {len(evidence)} bullets; 1 to 6 expected")
    for i, item in enumerate(evidence):
        if not item.get("metrics"):
            problems.append(f"evidence[{i}] cites no input metric")
        if not str(item.get("statement", "")).strip():
            problems.append(f"evidence[{i}] is empty")
    if not 1 <= len(actions) <= 6:
        problems.append(f"recommended_actions has {len(actions)} items; 1 to 6 expected")
    if any(not str(a).strip() for a in actions):
        problems.append("a recommended action is empty")
    return problems
