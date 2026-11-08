# Dashboard Data Contract (M7-2)

**Owner:** Jake (the read API) · **Consumer:** the SRE console (M7-1, Linu) · **Stack:** `anomalypulse-dashboard` ·
**Status:** signed off by Linu (2026-10-31, #27); `diagnosis` matches M6-1's output (Josh, PR #86)

The one JSON object the console reads, what every field means, and where each value comes from once M8 makes it
live. The console renders only from this object. Until M8-5 the read API serves mock data in exactly this shape;
M8-5 switches its source to live data without changing the shape.

---

## The call

```text
GET https://ffc8nyi6jl.execute-api.us-east-2.amazonaws.com/dashboard
```

The URL is the `anomalypulse-dashboard` stack's `DashboardUrl` output, also published in SSM at
`/anomalypulse/dashboard/url`. It stays the same across deploys of the stack.

- **200** with the object below, `Content-Type: application/json`.
- **503** with `{"error": "<what failed>"}` when the API can't read its source. The console keeps showing the
  last good reply, marked stale.
- **Poll every 10–15 s** (the console polls every 15 s). The numbers describe one-minute windows, so they change
  at most once a minute; the diagnosis can arrive at any time.
- **Mock only:** `GET /dashboard?scenario=<name>` returns one of the fixed scenarios below, so every state of the
  console can be built and checked; no `scenario` gives `healthy`. An unknown name returns **400** with
  `{"error": "unknown scenario '<name>'; use one of: ..."}`. The live API ignores `scenario`.
- **CORS:** any origin while the API serves mock data. M8-5 narrows it to the console's addresses,
  `http://127.0.0.1:4173` and `http://localhost:4173` (a browser treats the two as different origins).

## Example

The `incident` scenario: a payment failure, diagnosed. The numbers are illustrative.

```json
{
  "contract_version": 1,
  "generated_at": "2026-10-30T14:34:05.000Z",
  "health": {
    "window_start": "2026-10-30T14:33:00.000Z",
    "status": "INCIDENT",
    "requests_per_min": 1200,
    "error_rate": 0.025,
    "p95_latency_ms": 260,
    "active_incidents": 1,
    "services": [
      {"service": "product-service", "status": "HEALTHY",  "requests_per_min": 900, "error_rate": 0.0,   "p95_latency_ms": 41},
      {"service": "cart-service",    "status": "HEALTHY",  "requests_per_min": 120, "error_rate": 0.0,   "p95_latency_ms": 38},
      {"service": "order-service",   "status": "DEGRADED", "requests_per_min": 180, "error_rate": 0.167, "p95_latency_ms": 2950},
      {"service": "payment-service", "status": "DEGRADED", "requests_per_min": 60,  "error_rate": 0.5,   "p95_latency_ms": 2900}
    ]
  },
  "incident": {
    "incident_id": "inc-20261030T143200Z",
    "prediction": {
      "incident_type": "DEPENDENCY_FAILURE",
      "confidence": 0.93,
      "started_at": "2026-10-30T14:32:00.000Z",
      "detected_at": "2026-10-30T14:33:04.000Z",
      "affected_services": ["order-service", "payment-service"],
      "model": {"estimator": "logistic_regression", "job": "anomalypulse-hpo-261028173159-003-28af89d1"}
    },
    "diagnosis": {
      "severity": "HIGH",
      "summary": "Payment latency rose from 110 ms to 2.9 s. Order failures began about 40 seconds later (observed sequence from correlated timestamps).",
      "root_cause": "Payment dependency timeout",
      "evidence": ["Payment p95 latency rose 26x.", "Order 5xx share rose from 0% to 16.7%."],
      "recommended_actions": ["Inspect payment service health", "Review timeout/retry configuration", "Check recent deployment"]
    },
    "ground_truth": {
      "experiment_id": "exp-20261030T143000Z-3f2a",
      "incident_type": "DEPENDENCY_FAILURE",
      "severity": "HIGH",
      "faults": [
        {"injector": "payment-failure", "target": "payment-service",
         "active_from": "2026-10-30T14:31:55.000Z", "active_until": "2026-10-30T14:36:55.000Z", "active_now": true}
      ]
    },
    "comparison": {"detection_latency_s": 69.0}
  }
}
```

## Conventions

- **Every key is always present.** A value that doesn't exist is `null`: never a missing key, `0` or `""`. Each
  field that can be `null` says what its `null` means.
- **Times** are UTC ISO 8601 with milliseconds and `Z` (`2026-10-30T14:32:00.000Z`), the same as `window_start` in
  the feature files.
- **Units are in the name:** `_ms` milliseconds, `_s` seconds, `_per_min` per minute. **Shares and probabilities
  are 0–1** (`0.025` is 2.5%); the console formats them.
- **Names are the system's own.** Services are `product-service`, `cart-service`, `order-service` and
  `payment-service`, as in the telemetry. Incident types are the classifier's classes: `NORMAL`, `TRAFFIC_SPIKE`,
  `DATABASE_THROTTLING`, `LAMBDA_DEGRADATION`, `DEPENDENCY_FAILURE`, `CASCADING_FAILURE`. Severities are `LOW`,
  `MEDIUM`, `HIGH`.
- **Versioning.** `contract_version` goes up when a field is removed, renamed or changes meaning. Adding a field
  doesn't change it, so the console ignores keys it doesn't know.

## Fields

### Top level

| Field | Type | Meaning |
|---|---|---|
| `contract_version` | int | `1` |
| `generated_at` | time | When the API built this reply |
| `health` | object | The latest minute. Always present |
| `incident` | object or `null` | The active incident. `null`: none is active |

### `health`

| Field | Type | `null` means | Meaning |
|---|---|---|---|
| `window_start` | time | `NO_DATA` | The minute every number below describes |
| `status` | `HEALTHY`, `INCIDENT`, `NO_DATA` | never `null` | `NO_DATA` when the latest window started more than 3 minutes before `generated_at`, or while telemetry is late (the pipeline's late alarm is on, decided 2026-11-05): the pipeline has stopped or fallen behind, its numbers may be missing requests, and the console must not say healthy. Otherwise `INCIDENT` when `incident` isn't `null`, else `HEALTHY` |
| `requests_per_min` | int | `NO_DATA` | Requests to Product, Cart and Order in the minute. Not Payment's: Payment is reached only through Order, so adding them would count each checkout twice |
| `error_rate` | 0–1 | no requests, or `NO_DATA` | Share of those requests that returned 5xx |
| `p95_latency_ms` | number | no requests, or `NO_DATA` | 95th percentile of end-to-end latency over all those requests. It is not computed from the services' own p95s: percentiles don't combine |
| `active_incidents` | int | never `null` | `0` or `1`: one experiment runs at a time |
| `services` | array | never `null` | Always the four services, in the order above |

Each item of `services`:

| Field | Type | `null` means | Meaning |
|---|---|---|---|
| `service` | service name | never `null` | |
| `status` | `HEALTHY`, `DEGRADED`, `IDLE`, `NO_DATA` | never `null` | `NO_DATA` when `health.status` is. Otherwise `DEGRADED` when the service is in `incident.prediction.affected_services`, else `IDLE` when it had no requests, else `HEALTHY`. It shows what the models concluded, not a threshold rule of the API's own (decided 2026-10-30): the service's own numbers are beside it |
| `requests_per_min` | int | `NO_DATA` | The service's requests in the minute |
| `error_rate` | 0–1 | no requests, or `NO_DATA` | Share of its requests that returned 5xx |
| `p95_latency_ms` | number | no requests, or `NO_DATA` | 95th percentile of its end-to-end latency |

### `incident`

| Field | Type | `null` means | Meaning |
|---|---|---|---|
| `incident_id` | string | never `null` | The same while the incident is active; a new id is a new incident |
| `prediction` | object | never `null` | What the models concluded |
| `diagnosis` | object or `null` | Bedrock hasn't answered yet | What Bedrock wrote |
| `ground_truth` | object or `null` | No injected fault is known for this time: a real incident, or no manifest was found | What the simulator injected |
| `comparison` | object or `null` | `ground_truth` is `null` | The two compared |

**The sections never borrow from each other.** `prediction` and `diagnosis` hold only what the models and Bedrock
produced. Nothing from `ground_truth` fills them, even while they are empty. Comparing them afterwards, on screen
or in `comparison`, is the point of showing them side by side.

`prediction`:

| Field | Type | Meaning |
|---|---|---|
| `incident_type` | incident type, never `NORMAL` | The incident's type: the class with the most of the incident's minutes so far, a tie going to the most recent (`ml/diagnosis/incident.py`; decided 2026-11-08, [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Why the incident's type"). It can change during one incident: a cascade is first classified as its first fault's type, and as `CASCADING_FAILURE` once that has as many of its minutes. A class the classifier gives only a fault's last minute or two doesn't replace it |
| `confidence` | 0–1 | The probability the classifier last gave `incident_type` |
| `started_at` | time | `window_start` of the incident's first window |
| `detected_at` | time | When that first window was classified |
| `affected_services` | array of service names, at least one | The services the incident shows in: those whose own metrics crossed a fixed line against their pre-incident baseline in the incident's opening minute or any later minute it counted (if none crossed at opening, the one that rose most). The list only grows while the incident is open. M8-3 computes it with `genai.affected` from the feature windows, never from the manifest (`Docs/INCIDENT_STORE.md`); each Bedrock input lists its own minute's services (`Docs/DIAGNOSIS_CONTRACT.md`, "affected_services") |
| `model` | object | Which model answered: `estimator` and `job`, from the Diagnosis Lambda's reply |

`diagnosis` (the fields follow PROJECT_PLAN §10 and §14, and are exactly M6-1's output: `Docs/DIAGNOSIS_CONTRACT.md`):

| Field | Type | Meaning |
|---|---|---|
| `severity` | severity | The severity rubric's, computed from Bedrock's input (`genai.contract.severity()`, `Docs/DIAGNOSIS_CONTRACT.md`). Bedrock is asked for one too; a different answer is a self-check finding, never shown. The manifest's severity is ground truth and never fills it |
| `summary` | string | Plain text. Any ordering of symptoms is worded as an **observed sequence from correlated timestamps**, never as causation |
| `root_cause` | string | The most likely root cause |
| `evidence` | array of strings | Each one traces to a metric in Bedrock's input |
| `recommended_actions` | array of strings | In the order to try them |

`ground_truth`:

| Field | Type | Meaning |
|---|---|---|
| `experiment_id` | string | The run |
| `incident_type` | incident type | The run's type, from its manifest. A cascade's is `CASCADING_FAILURE` for the whole run, including its first minutes, when the model is right to give the first fault's type. `faults[].active_now` shows which faults are active in the minute shown |
| `severity` | severity | From the injected intensity, the manifest's rule |
| `faults` | array | One per injected fault, in the manifest's order |

Each item of `faults`:

| Field | Type | `null` means | Meaning |
|---|---|---|---|
| `injector` | string | never `null` | The simulator's name: `traffic-spike`, `db-throttling`, `lambda-degradation`, `payment-failure` or `retry-amplification` |
| `target` | service name | the fault has no single target (a traffic spike) | The service the fault is applied to |
| `active_from`, `active_until` | time | never `null` | While the run is going, the planned times: until it ends, its manifest has only those. After it ends, the actual times |
| `active_now` | bool | `health.window_start` is `null` | Whether the fault is active for at least 30 s of the minute `health` describes: the Window Labeling Policy's threshold. `retry-amplification` is listed, but the policy never counts it as a fault |

`comparison`:

| Field | Type | `null` means | Meaning |
|---|---|---|---|
| `detection_latency_s` | number | `detected_at` is before every fault's `active_from`: an alarm before anything was injected | `prediction.detected_at` minus the earliest `active_from`. With one-minute windows it can't be much under a minute |

## Mock scenarios

| `scenario` | What it shows |
|---|---|
| `healthy` (the default) | No incident; the four services `HEALTHY` |
| `diagnosing` | A dependency failure just detected: `diagnosis` is `null` |
| `incident` | The example above |
| `cascade_early` | A cascade's first minutes: `prediction` says `TRAFFIC_SPIKE`; `ground_truth` says `CASCADING_FAILURE`, with only the spike `active_now` |
| `no_ground_truth` | An incident with nothing injected, as in production: `ground_truth` and `comparison` are `null` |
| `no_data` | The pipeline has stopped: `NO_DATA`, every number `null` |

Every time in a scenario is placed at the moment of the request: `health.window_start` is the latest complete
minute, and every other time keeps its offset from it, so the scenarios never go stale (and never turn into
`NO_DATA` by age). The `incident` scenario's ids are the mock's own, and it sends Order's error rate as 30/180,
which the example rounds to 0.167. The numbers are illustrative; none comes from the validation or test sets.

## Where each value comes from in M8

| Section | Now (M7) | Live (M8) | Who |
|---|---|---|---|
| `health` | the mock scenario | The Incidents table's health item (`pk = "HEALTH"`, `sk = "LATEST"`), which the pipeline's window builder rewrites every minute with the newest minute, in `builder.py`'s input shape. The system-wide p95 is computed from the request records themselves. The item's `telemetry_late` says whether the pipeline's late alarm is on. Its numbers come back from DynamoDB as `Decimal`s ([`PIPELINE.md`](PIPELINE.md), "What it writes") | M8-1 (Jake) |
| `prediction` | the mock scenario | The Diagnosis Lambda's reply, stored in the Incidents table | M8-2 (Melisa), M8-3 (Rachel) |
| `diagnosis` | the mock scenario | Bedrock's output, stored in the Incidents table | M8-3 (Rachel), M8-4 (Josh) |
| `ground_truth`, `comparison` | the mock scenario | The manifest of the experiment running in that minute, read from the shared bucket (read only) | Jake |

**The code** is in `AnomalyPulse/dashboard/`. `builder.py` applies every rule in this document to raw inputs:
the latest window, the incident record, the manifest and the time. `mock_source.py` supplies the scenarios'
inputs. `handler.py` picks the source from the `DASHBOARD_SOURCE` setting (anything but `mock`, including no
setting, answers 503, never mock data) and is the only code that reads the clock. M8-5 adds a live source beside
the mock one and changes nothing else.

## Open questions

1. **Linu: answered** (2026-10-31, #27). The console is plain HTML/CSS and JavaScript modules: static files, no
   build step. It runs from `http://127.0.0.1:4173` and `http://localhost:4173`, both while she develops and, for
   the demo, on the presenting laptop; it is not hosted in AWS (revisit in M9 if a shareable link matters). It
   polls every 15 s and keeps the last good reply on a 503 or a timeout. Nothing is missing. While `diagnosis`
   is `null`, the incident panel shows "Severity pending": the manifest's severity is ground truth and never
   fills it.
2. **Josh: answered** (PR #86). The `diagnosis` fields are exactly M6-1's output. Its `severity` is the fixed
   rubric's, computed from the input (since 2026-11-08, #99; before that it was Bedrock's own pick, which the
   self-check still compares with the rubric). Beside `diagnosis`, the
   explanation Lambda's reply carries `evidence_metrics` (the input metrics each evidence bullet cites) and
   `self_check` (`passed` and findings); M8-3 stores them, and M8-4 decides whether the console shows them.
