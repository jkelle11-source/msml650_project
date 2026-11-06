"""Smoke-level fidelity check of a diagnosis against its input (M6-1).

A dry run before M9-5's formal evaluation, not a replacement for it: cheap,
deterministic rules that flag obviously unsupported claims. Each finding names
the field and what is wrong. It checks:

- numbers: every number written must come from the input (a baseline or
  current value, or a ratio, percent change, difference, percent of a share, or
  ms in seconds derived from them; the confidence; the duration; the gap
  between two observed_sequence steps), rounded no further than it is written
  ("2950" must be 2945-2955, "2.9" 2.85-2.95). A hedge or bound before a
  number says it may be cut off rather than rounded, so its window is one
  written unit on the side the word gives: "roughly 15" is 14-16, "more than
  15" 15-16, "nearly 15" 14-15 (the second number of a range takes the first's
  word). An evidence bullet's numbers must come from the metrics it cites, so a
  real value put on the wrong metric is caught;
- severity: it must be the rubric's (contract.severity()), and no claim may
  mention a threshold;
- services: every service named must be in the input;
- times: a clock time written ("14:32") must be one of the input's times;
- metric families: a claim about throttling, cold starts, capacity, errors,
  latency, traffic, compute time, the database, timeouts or retries needs a
  metric of that kind in the input (retries: db_latency_ms, which includes the
  SDK's retries); deployments and how a table's capacity is set (provisioned or
  on-demand) have no metric, so any claim about either is unsupported;
- causation: no causal verbs ("caused", "led to", "due to", ...) outside
  root_cause, which is worded as most likely; and none in a cascade's
  root_cause either, whose faults are concurrent ("in turn", "produced", "put
  payment-service under load" are links the evidence can't show);
- ordering: a sentence that orders symptoms must say "observed sequence", and
  needs an observed_sequence in the input ("followed at the same scale" is two
  metrics moving together, not an order).

recommended_actions are checked for numbers and times only: they say what to
look at, which may be a component or service with no metric in the input
("inspect DynamoDB capacity", "check the payment-service call"). With a runbook
(M6-2), the numbers the runbook writes are allowed in recommended_actions too,
and nowhere else: summary, root cause and evidence still come only from the input.
"""
import re
from datetime import datetime
from itertools import combinations

from genai.contract import SERVICES, metric_ref, severity

_SERVICE_NAMES = {s.split("-")[0]: s for s in SERVICES}   # "order" -> "order-service"
# Where "order" is a plain word, not order-service: "in order to", "the order of", "order of magnitude".
_NOT_SERVICES = re.compile(r"\bin order (to|for|that)\b|\b(the|this|that|same|chronological|time) order\b|"
                           r"\border of\b", re.IGNORECASE)

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
    # Compute time, not the incident's duration ("over the 4-minute duration" is the input's own).
    r"\bcpu\b|compute time|\b(lambda|handler|function|execution|invocation) (duration|time)\b": (
        "lambda_duration_ms_mean", "lambda_duration_ms_p95"),
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
    # Nor how a table's capacity is set: a runbook says it (M6-2), the input never does.
    r"\bprovisioned\b|\bon-demand\b|\bpay[- ]per[- ]request\b": (),
}

# "Ruling out X" is a causal claim in the negative, whatever noun follows ("as the cause",
# "as an explanation").
CAUSAL = re.compile(r"\b(caus(e|ed|es|ing)|because|due to|led to|leads? to|leading to|"
                    r"result(ed|s|ing)? in|as a result|trigger(ed|s|ing)?|dr(o|i)ve[ns]?|driving|"
                    r"rul(e|es|ed|ing) out)\b",
                    re.IGNORECASE)
# A cascade's faults are concurrent, so its root_cause links nothing causally. The noun "cause"
# ("the most likely root cause", "root causes") is the one causal word it needs, so it isn't in
# this list.
CASCADE_LINKS = re.compile(r"\b(caus(ed|ing)|(?<!root )causes|because|due to|led to|leads? to|leading to|"
                           r"result(ed|s|ing)? in|as a result|trigger(ed|s|ing)?|dr(o|i)ve[ns]?|driving|"
                           r"produc(e|ed|es|ing)|in turn|push(ed|es|ing)|under (load|pressure|strain))\b",
                           re.IGNORECASE)
# "First" orders symptoms only when something moved first ("the first sign" is not an order).
ORDERING = re.compile(r"\b(after|before|later|earlier|followed|preceded|subsequently|then|prior to)\b|"
                      r"\b(rose|moved|changed|began|started|appeared|came|happened|occurred|spiked|jumped) "
                      r"first\b|\bfirst to\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")
# Tokens whose digits are names, not values: p95 and "the 95th percentile", 5xx, 4xx, 2xx,
# timestamps, ids, status codes ("HTTP 503", "500 responses", "status 502").
_NOT_VALUES = re.compile(r"\bp\d{2}\b|\b\d{2}th percentiles?\b|\b\dxx\b|\d{4}-\d\d-\d\dT[\d:.]+Z?|"
                         r"\b[a-z]+-\d+\b|\bHTTP ?\d+\b|"
                         r"\bstatus( code)? [1-5]\d\d\b|\b[1-5]\d\d (responses?|errors?|status)\b",
                         re.IGNORECASE)
# Clock times ("14:32", "17:04:05"): checked against the input's times, then not read as numbers.
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
# The baseline, not an order between two symptoms: "before the incident", and a "before" that
# closes a comparison ("15.1 rps against 15.0 before").
_BASELINE_PHRASES = re.compile(r"\b(before|prior to|since) (the|this) incident( began| started)?\b|"
                               r"\bbefore(?=\s*(?:[.,;:)]|$))", re.IGNORECASE)
# "Followed" meaning "moved with", not "came after": "end-to-end latency at both p50 and p95 followed at
# roughly the same scale" (Sonnet 4.6, Lambda-degradation sample, 2026-11-03). A bare "latency has
# followed" still orders.
_TRACKING_PHRASES = re.compile(r"\bfollowed (at|in) (roughly |about |nearly |much )?(the same|a similar) "
                               r"(scale|rate|pace|magnitude|proportion)\b|\bfollowed in (lock)?step\b",
                               re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")

# Words before a number that say it may be cut off rather than rounded, and which way: "roughly 15x"
# and "more than 15x" for 15.6x (Sonnet 4.6, Lambda-degradation sample, 2026-11-03 and 2026-11-05).
# Matched against the text just before the number.
_LOOSENERS = (
    ("either", re.compile(r"(\b(roughly|about|around|approximately|close to)\s+|~\s*)$", re.IGNORECASE)),
    ("up", re.compile(r"\b(more than|over|above|exceeding|at least|upwards of|in excess of)\s+$", re.IGNORECASE)),
    ("down", re.compile(r"\b(nearly|almost|under|below|less than|at most|up to)\s+$", re.IGNORECASE)),
)
# Between the two numbers of a range ("15x to 21x", "17–24 ms"): the second takes the first's word.
_RANGE_GAP = re.compile(r"^\s*[a-z%]{0,3}\s*(to|-|–)\s*$", re.IGNORECASE)


def _forms(values, unit) -> set[float]:
    """Values as they may be written: as is, a share as a percent or "1 in N", ms as seconds,
    rps per minute, and a per-minute count or capacity per second ("11,900 RCU/min is 198 RCU/s",
    the unit DynamoDB provisions capacity in)."""
    out = set()
    for v in values:
        out.add(float(v))
        if unit == "share":
            out.add(v * 100)
            if v:
                out.add(1 / v)       # "1 in 6" for a share of 0.167
        if unit == "ms":
            out.add(v / 1000)
        if unit == "rps":
            out.add(v * 60)
        if unit.endswith("/min"):
            out.add(v / 60)
    return out


def _metric_numbers(m: dict) -> set[float]:
    """One metric's baseline and current, and the changes derived from them."""
    b, c = m["baseline"], m["current"]
    values = [v for v in (b, c) if v is not None]
    if b is not None:
        values += [c - b, abs(c - b)]
        if b:
            values += [c / b, (c - b) / b * 100, abs(c - b) / b * 100]
    return _forms(values, m["unit"])


def _between(metrics: list[dict]) -> set[float]:
    """Comparisons between two cited metrics in the same unit: the gap and ratio of their currents."""
    out = set()
    for a, b in combinations(metrics, 2):
        if a["unit"] == b["unit"]:
            values = [abs(a["current"] - b["current"])]
            values += [x / y for x, y in ((a["current"], b["current"]), (b["current"], a["current"])) if y]
            out |= _forms(values, a["unit"])
    return out


def _general_numbers(evidence: dict) -> set[float]:
    """Numbers any field may write: 0, 1, 100, the confidence, the duration, the number of affected
    services, and the gaps between observed_sequence steps."""
    out = {0.0, 1.0, 100.0, float(evidence["duration_minutes"]), evidence["confidence"],
           evidence["confidence"] * 100, float(len(evidence["affected_services"]))}
    times = [datetime.fromisoformat(s["at"].replace("Z", "+00:00"))
             for s in evidence.get("observed_sequence", [])]
    for i, a in enumerate(times):
        for later in times[i + 1:]:
            gap = (later - a).total_seconds()
            out |= {gap, gap / 60}
    return out


def _input_clocks(evidence: dict) -> set[str]:
    """Every input time as HH:MM and HH:MM:SS (UTC), the forms a sentence quotes them in."""
    stamps = [evidence["started_at"], evidence["window_start"]]
    stamps += [s["at"] for s in evidence.get("observed_sequence", [])]
    clocks = set()
    for stamp in stamps:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        clocks |= {t.strftime("%H:%M"), t.strftime("%H:%M:%S"), f"{t.hour}:{t.minute:02d}"}
    return clocks


def _matches(raw: str, allowed, loosened: str | None = None) -> bool:
    """Whether a number as written rounds from an allowed value: within half a unit of its last
    significant digit ("2.9" +/-0.05, "2950" +/-5, "26" +/-0.5, "2900" +/-50). A loosened number
    may be cut off instead, so its window is one unit on its word's side: "either" (roughly 15:
    14-16), "up" (more than 15: 15-16) or "down" (nearly 15: 14-15)."""
    digits = raw.replace(",", "").lstrip("-")
    if "." in digits:
        half_unit = 0.5 * 10 ** -len(digits.split(".")[1])
    else:
        zeros = len(digits) - len(digits.rstrip("0"))
        half_unit = 0.5 * 10 ** zeros if digits.rstrip("0") else 0.5
    number = float(digits) * (-1 if raw.startswith("-") else 1)
    below, above = {None: (half_unit, half_unit), "either": (2 * half_unit, 2 * half_unit),
                    "up": (0, 2 * half_unit), "down": (2 * half_unit, 0)}[loosened]
    # The margin only absorbs float error (2.95 - 2.9 is 0.0500000000000003).
    margin = half_unit * 1e-9
    return any(number - below - margin <= v <= number + above + margin for v in allowed)


def _numbers(text: str) -> list[tuple[str, str | None]]:
    """(number as written, the side its hedge or bound loosens it to, or None) for each value in a
    text, after times and names ("p95", "5xx") are taken out."""
    clean = _NOT_VALUES.sub(" ", _CLOCK.sub(" ", text))
    out, previous = [], None
    for match in _NUMBER.finditer(clean):
        before = clean[max(0, match.start() - 40):match.start()]
        loosened = next((side for side, words in _LOOSENERS if words.search(before)), None)
        if loosened is None and previous and _RANGE_GAP.match(clean[previous[0]:match.start()]):
            loosened = previous[1]
        out.append((match.group(0), loosened))
        previous = (match.end(), loosened)
    return out


# A numbered list's markers ("1. Check ...") are not values.
_LIST_MARKERS = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


def _runbook_numbers(runbook: str | None) -> set[float]:
    """The values a runbook writes ("1,769 MB", "7 attempts", "a 6 s read timeout"), parsed the way
    a diagnosis's numbers are, so the written-precision rule applies to them too."""
    if not runbook:
        return set()
    text = _NOT_VALUES.sub(" ", _CLOCK.sub(" ", _LIST_MARKERS.sub(" ", runbook)))
    return {float(raw.replace(",", "")) for raw in _NUMBER.findall(text)}


def _texts(diagnosis: dict):
    """(field, text, is_claim, cited refs or None) for every string in the diagnosis."""
    yield "summary", diagnosis.get("summary", ""), True, None
    yield "root_cause", diagnosis.get("root_cause", ""), True, None
    for i, item in enumerate(diagnosis.get("evidence", [])):
        yield f"evidence[{i}]", item.get("statement", ""), True, item.get("metrics", [])
    for i, action in enumerate(diagnosis.get("recommended_actions", [])):
        yield f"recommended_actions[{i}]", action, False, None


def check(evidence: dict, diagnosis: dict, runbook: str | None = None) -> list[str]:
    """Findings for a raw diagnosis (evidence items with `statement` and `metrics`). Empty means none.
    `runbook` is the text the diagnosis was given, if any: its numbers are allowed in actions."""
    findings = []
    by_ref = {metric_ref(m["service"], m["metric"]): m for m in evidence["metrics"]}
    general = _general_numbers(evidence)
    anywhere = general.union(*(_metric_numbers(m) for m in evidence["metrics"]))
    in_actions = anywhere | _runbook_numbers(runbook)
    services = set(evidence["affected_services"]) | {m["service"] for m in evidence["metrics"]}
    present = {m["metric"] for m in evidence["metrics"]}
    has_sequence = bool(evidence.get("observed_sequence"))
    clocks = _input_clocks(evidence)

    expected = severity(evidence)
    if diagnosis.get("severity") != expected:
        findings.append(f"severity: {diagnosis.get('severity')}, but the rubric gives {expected}")

    for field, text, is_claim, cites in _texts(diagnosis):
        own = None
        if cites is not None:
            cited = [by_ref[r] for r in cites if r in by_ref]
            own = general.union(*(_metric_numbers(m) for m in cited), _between(cited))
        for clock in _CLOCK.finditer(text):
            if clock.group(0) not in clocks:
                findings.append(f"{field}: the time {clock.group(0)} is not in the input")
        for raw, loosened in _numbers(text):
            if own is not None and _matches(raw, own, loosened):
                continue
            if not _matches(raw, anywhere if is_claim else in_actions, loosened):
                findings.append(f"{field}: the number {raw} is not in or derivable from the input")
            elif own is not None:
                findings.append(f"{field}: the number {raw} is not from the metrics it cites")
        if not is_claim:
            # An action may point at any part of the stack the prompt describes (Order calls
            # Payment on every POST /orders); it states no fact, so services aren't checked. Nor
            # thresholds: "approaching throttling thresholds" is DynamoDB's capacity, not the rubric.
            continue
        if re.search(r"\bthresholds?\b", text, re.IGNORECASE):
            findings.append(f"{field}: mentions a threshold; the severity rubric's are never quoted")
        named = re.findall(r"\b(product|cart|order|payment)(?:-service)?\b", _NOT_SERVICES.sub(" ", text),
                           re.IGNORECASE)
        for word in named:
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
        elif evidence["incident_type"] == "CASCADING_FAILURE":
            for link in CASCADE_LINKS.finditer(text):
                findings.append(f"root_cause: a cascade's faults are concurrent, but it links them causally "
                                f"({link.group(0)!r})")
        for sentence in _SENTENCES.split(text):
            ordered = ORDERING.search(_TRACKING_PHRASES.sub(" ", _BASELINE_PHRASES.sub(" ", sentence)))
            if ordered and "observed sequence" not in sentence.lower():
                findings.append(f"{field}: orders symptoms without saying 'observed sequence': "
                                f"{sentence.strip()!r}")
            elif ordered and not has_sequence:
                findings.append(f"{field}: makes an ordering claim, but the input has no observed_sequence")

    cited = {ref for item in diagnosis.get("evidence", []) for ref in item.get("metrics", [])}
    for ref in sorted(cited - set(by_ref)):
        findings.append(f"evidence cites {ref}, which is not in the input")
    return sorted(set(findings), key=findings.index)
