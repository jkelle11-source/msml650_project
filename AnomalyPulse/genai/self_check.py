"""Smoke-level fidelity check of a diagnosis against its input (M6-1).

A dry run before M9-5's formal evaluation, not a replacement for it: cheap,
deterministic rules that flag obviously unsupported claims. Each finding names
the field and what is wrong. It checks:

- numbers: every number written must come from the input (a baseline or
  current value, or a ratio, percent change, difference, percent of a share, or
  ms in seconds derived from them; the confidence; the duration; the gap
  between two observed_sequence steps);
- services: every service named must be in the input;
- times: a clock time written ("14:32") must be one of the input's times;
- metric families: a claim about throttling, cold starts, capacity, errors,
  latency, traffic, compute time, the database, timeouts or retries needs a
  metric of that kind in the input (retries: db_latency_ms, which includes the
  SDK's retries); deployments have no metric, so any claim of one is unsupported;
- causation: no causal verbs ("caused", "led to", "due to", ...) outside
  root_cause, which is worded as most likely;
- ordering: a sentence that orders symptoms must say "observed sequence", and
  needs an observed_sequence in the input.

recommended_actions are checked for numbers and times only: they say what to
look at, which may be a component or service with no metric in the input
("inspect DynamoDB capacity", "check the payment-service call").
"""
import re
from datetime import datetime

from genai.contract import SERVICES

_SERVICE_NAMES = {s.split("-")[0]: s for s in SERVICES}   # "order" -> "order-service"

# Keyword -> the input metrics that support a claim about it. Matched case-insensitively.
FAMILIES = {
    r"throttl": ("throttled_requests", "read_throttle_events", "write_throttle_events",
                 "lambda_throttle_count"),
    r"cold[ -]start": ("cold_start_count",),
    r"\bcapacity\b|\bRCU\b|\bWCU\b": ("db_consumed_read_capacity", "db_consumed_write_capacity"),
    r"\berrors?\b|\bfail(ed|ing|ures?|s)?\b|\b5xx\b|\b4xx\b": (
        "error_rate_5xx", "error_rate_4xx", "dependency_error_count"),
    r"latenc|\bslow": ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms", "db_latency_ms_mean",
                       "db_latency_ms_p95", "dependency_latency_ms_mean", "dependency_latency_ms_p95"),
    r"request rate|\btraffic\b|requests per second|\brps\b": ("request_rate",),
    r"\bduration\b|\bcpu\b|compute time": ("lambda_duration_ms_mean", "lambda_duration_ms_p95"),
    r"dynamodb|\bdatabase\b|\bdb\b|\btable\b": (
        "db_latency_ms_mean", "db_latency_ms_p95", "db_consumed_read_capacity",
        "db_consumed_write_capacity", "throttled_requests", "read_throttle_events",
        "write_throttle_events"),
    r"\btime(d)? ?out": ("dependency_error_count", "dependency_latency_ms_mean",
                         "dependency_latency_ms_p95"),
    # db_latency_ms is defined as including the SDK's retries; nothing else measures them.
    r"\bretr(y|ies|ied|ying)\b": ("db_latency_ms_mean", "db_latency_ms_p95"),
    # No metric says anything about deployments or releases.
    r"\bdeploy|\brollout\b|\brelease\b": (),
}

# "Ruling out X" is a causal claim in the negative, whatever noun follows ("as the cause",
# "as an explanation").
CAUSAL = re.compile(r"\b(caus(e|ed|es|ing)|because|due to|led to|leads? to|leading to|"
                    r"result(ed|s|ing)? in|as a result|trigger(ed|s|ing)?|dr(o|i)ve[ns]?|driving|"
                    r"rul(e|es|ed|ing) out)\b",
                    re.IGNORECASE)
ORDERING = re.compile(r"\b(after|before|later|earlier|followed|preceded|subsequently|then|first|"
                      r"prior to|once)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")
# Tokens whose digits are names, not values: p95, 5xx, 4xx, 2xx, timestamps, ids.
_NOT_VALUES = re.compile(r"\bp\d{2}\b|\b\dxx\b|\d{4}-\d\d-\d\dT[\d:.]+Z?|\b[a-z]+-\d+\b|\bHTTP ?\d+\b",
                         re.IGNORECASE)
# Clock times ("14:32", "17:04:05"): checked against the input's times, then not read as numbers.
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
# "Before the incident" is the baseline, not an order between two symptoms.
_BASELINE_PHRASES = re.compile(r"\b(before|prior to|since) (the|this) incident( began| started)?\b",
                               re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def _allowed_numbers(evidence: dict) -> list[float]:
    allowed = {0.0, 1.0, 100.0, float(evidence["duration_minutes"]),
               evidence["confidence"], evidence["confidence"] * 100, float(len(evidence["affected_services"]))}
    for m in evidence["metrics"]:
        values = [v for v in (m["baseline"], m["current"]) if v is not None]
        b, c = m["baseline"], m["current"]
        if b is not None:
            values += [c - b, abs(c - b)]
            if b:
                values += [c / b, (c - b) / b * 100, abs(c - b) / b * 100]
        for v in values:
            allowed.add(float(v))
            if m["unit"] == "share":
                allowed.add(v * 100)
                if v:
                    allowed.add(1 / v)       # "1 in 6" for a share of 0.167
            if m["unit"] == "ms":
                allowed.add(v / 1000)
            if m["unit"] == "rps":
                allowed.add(v * 60)
    times = [datetime.fromisoformat(s["at"].replace("Z", "+00:00"))
             for s in evidence.get("observed_sequence", [])]
    for i, a in enumerate(times):
        for later in times[i + 1:]:
            gap = (later - a).total_seconds()
            allowed |= {gap, gap / 60}
    return sorted(allowed)


def _input_clocks(evidence: dict) -> set[str]:
    """Every input time as HH:MM and HH:MM:SS (UTC), the forms a sentence quotes them in."""
    stamps = [evidence["started_at"], evidence["window_start"]]
    stamps += [s["at"] for s in evidence.get("observed_sequence", [])]
    clocks = set()
    for stamp in stamps:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        clocks |= {t.strftime("%H:%M"), t.strftime("%H:%M:%S"), f"{t.hour}:{t.minute:02d}"}
    return clocks


def _matches(number: float, allowed: list[float]) -> bool:
    for v in allowed:
        tolerance = max(abs(v) * 0.06, 0.06 if abs(v) < 10 else 0.6)
        if abs(number - v) <= tolerance:
            return True
    return False


def _texts(diagnosis: dict):
    """(field, text, is_claim) for every string in the diagnosis."""
    yield "summary", diagnosis.get("summary", ""), True
    yield "root_cause", diagnosis.get("root_cause", ""), True
    for i, item in enumerate(diagnosis.get("evidence", [])):
        yield f"evidence[{i}]", item.get("statement", ""), True
    for i, action in enumerate(diagnosis.get("recommended_actions", [])):
        yield f"recommended_actions[{i}]", action, False


def check(evidence: dict, diagnosis: dict) -> list[str]:
    """Findings for a raw diagnosis (evidence items with `statement` and `metrics`). Empty means none."""
    findings = []
    allowed = _allowed_numbers(evidence)
    services = set(evidence["affected_services"]) | {m["service"] for m in evidence["metrics"]}
    present = {m["metric"] for m in evidence["metrics"]}
    has_sequence = bool(evidence.get("observed_sequence"))

    clocks = _input_clocks(evidence)

    for field, text, is_claim in _texts(diagnosis):
        for clock in _CLOCK.finditer(text):
            if clock.group(0) not in clocks:
                findings.append(f"{field}: the time {clock.group(0)} is not in the input")
        for raw in _NUMBER.findall(_NOT_VALUES.sub(" ", _CLOCK.sub(" ", text))):
            if not _matches(float(raw.replace(",", "")), allowed):
                findings.append(f"{field}: the number {raw} is not in or derivable from the input")
        if not is_claim:
            # An action may point at any part of the stack the prompt describes (Order calls
            # Payment on every POST /orders); it states no fact, so services aren't checked.
            continue
        for word in re.findall(r"\b(product|cart|order|payment)(?:-service)?\b", text, re.IGNORECASE):
            if _SERVICE_NAMES[word.lower()] not in services:
                findings.append(f"{field}: names {_SERVICE_NAMES[word.lower()]}, which the input doesn't")
        for pattern, family in FAMILIES.items():
            hit = re.search(pattern, text, re.IGNORECASE)
            if hit and not present & set(family):
                findings.append(f"{field}: claims {hit.group(0)!r}, but the input has no metric for it")
        if field != "root_cause":
            for verb in CAUSAL.findall(text):
                findings.append(f"{field}: causal wording {verb[0]!r}; correlated timestamps are an "
                                f"observed sequence, not proof of cause")
        for sentence in _SENTENCES.split(text):
            ordered = ORDERING.search(_BASELINE_PHRASES.sub(" ", sentence))
            if ordered and "observed sequence" not in sentence.lower():
                findings.append(f"{field}: orders symptoms without saying 'observed sequence': "
                                f"{sentence.strip()!r}")
            elif ordered and not has_sequence:
                findings.append(f"{field}: makes an ordering claim, but the input has no observed_sequence")

    cited = {ref for item in diagnosis.get("evidence", []) for ref in item.get("metrics", [])}
    known = {f"{m['service']}.{m['metric']}" for m in evidence["metrics"]}
    for ref in sorted(cited - known):
        findings.append(f"evidence cites {ref}, which is not in the input")
    return sorted(set(findings), key=findings.index)
