# AnomalyPulse Telemetry Contract

**Status:** frozen in M2-1 · **Schema version:** 1.0 · **Owner:** Jake (infra)

The single agreement every service Lambda emits against. If you are instrumenting a
service (M2-2…M2-5), this page plus the layer is all you need.

---

## Canonical sources (don't copy field lists by hand)

| Artifact | Path | Role |
|---|---|---|
| **Record schema** (enforced) | `AnomalyPulse/layers/telemetry/python/telemetry_schema.json` | Validates every emitted line. `additionalProperties: false`. |
| **Validator** | `AnomalyPulse/layers/telemetry/python/schema_validator.py` | `validate(record) -> (ok, errors)`; `validate_or_raise(record)`. |
| **Field catalog** (all tiers) | `AnomalyPulse/infra/shared/field_catalog.json` | Registry of every field incl. Tier-2. The only home for Tier-2 definitions. |

Both schema and validator ship in the **`anomalypulse-telemetry` Lambda layer** (attached to
every function via `Globals`). **Import the layer — never transcribe the field list.**

---

## How to emit

1. `print()` **one JSON object per request** to stdout. That's it — the shipper collects it
   from CloudWatch Logs asynchronously, off the request path. Don't write to S3 from a handler.
2. **Emit all 14 fields on every record.** If a field doesn't apply to your service, emit
   `null` — **never omit it.** (`additionalProperties: false` also means a stray or misspelled
   field gets the whole record **rejected**.)
3. **`timestamp`** — millisecond precision, UTC, `...Z` (e.g. `2026-09-30T22:12:42.081Z`).
   Enforced by regex `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$`.
4. **`request_id`** — propagate the **same** id into any downstream call, so a request can be
   reconstructed across services by correlation. (This is what makes the "observed sequence"
   narration possible later; full X-Ray tracing stays a stretch goal.)

---

## Fields & tiers

**Correlation** — identity & timing. Emitted by every service. Used for window aggregation and
ordering **only — never a model feature.**

| Field | Type | Notes |
|---|---|---|
| `timestamp` | string | ms-precision UTC, `...Z` |
| `request_id` | string | propagated across service hops |
| `service` | string | `product-service` \| `cart-service` \| `order-service` \| `payment-service` |
| `endpoint` | string | e.g. `/products` |
| `http_method` | string | `GET` \| `POST` \| `DELETE` |

**Tier-1 observable** — what a real monitor could see without knowing the cause. **May become
ML features.** Emit on every event; `null` when not applicable.

| Field | Type | Notes |
|---|---|---|
| `status_code` | integer | 100–599 |
| `latency_ms` | number | end-to-end wall-clock as seen by the handler |
| `lambda_duration_ms` | number | compute time of the Lambda body itself |
| `cold_start` | boolean | true if this invocation paid a cold-start penalty |
| `db_latency_ms` | number \| null | DynamoDB call time; `null` for services with no DB call (Payment) |
| `db_consumed_capacity` | number \| null | consumed capacity units; `null` when no DB call |
| `dependency` | string \| null | downstream service called; `null` when none (Product, Cart, Payment) |
| `dependency_latency_ms` | number \| null | measured call time to the dependency; `null` when none |
| `dependency_error` | boolean \| null | true if the dependency call failed or timed out |

**Tier-2 ground-truth / diagnostic** — encodes or reveals the injected cause. **NEVER emitted on
the request path.** Used only for labeling and evaluation. The M4 feature-engineering build guard
fails the build if any Tier-2 name reaches the training matrix.

| Field | Type | Notes |
|---|---|---|
| `error_type` | string | encodes the cause (e.g. `DynamoDBThrottle`) — diagnostic only |
| `db_throttled` | boolean | **distinct** from the observable CloudWatch `ThrottledRequests` **count** (a legitimate Tier-1 aggregate) |
| `incident_type` | string | the classification label (`NORMAL`, `TRAFFIC_SPIKE`, `DATABASE_THROTTLING`, `LAMBDA_DEGRADATION`, `DEPENDENCY_FAILURE`, `CASCADING_FAILURE`) |
| `severity` | string | `LOW` \| `MEDIUM` \| `HIGH` |
| `fault_injection_params` | object | exact simulator knobs for the experiment |

> **The one rule that matters most:** a Tier-2 field in an emitted record is a data leak. The
> record schema has no Tier-2 fields and rejects unknowns, so the leak is caught at emit time —
> keep it that way. In particular, never synthesize a `db_throttled` boolean into telemetry;
> the observable signal is the CloudWatch `ThrottledRequests` **count**, collected separately.

---

## Where it lands (S3)

Bucket name is published to SSM at `/anomalypulse/telemetry/bucket/name`
(`anomalypulse-telemetry-<account>-<region>`).

```text
raw/service=<svc>/dt=<YYYY-MM-DD>/part-<request_id>.json   ← NDJSON, one object per shipper batch
errors/service=<svc>/dt=<YYYY-MM-DD>/part-<request_id>.json ← lines that failed to parse (quarantine)
aggregated/                                                 ← reserved for M4 (one-minute windows)
```

- `service=…/dt=…` are **Hive-style key segments** — a naming convention that lets Athena/Glue
  discover partitions later. S3 itself is a flat key store; `dt` is **date** granularity.
- `raw/` objects **expire after 90 days** (lifecycle rule `expire-raw-telemetry`).

---

## Pipeline (how a record gets from `print()` to S3)

```text
service Lambda  print(json)
      │
      ▼
CloudWatch Logs  /aws/lambda/anomalypulse-<svc>
      │   SubscriptionFilter  { $.request_id = "*" }   ← forwards only telemetry lines,
      ▼                                                   not START/END/REPORT noise
shipper Lambda  (anomalypulse-telemetry-shipper)
      │   gunzip + unwrap → parse each line → good ⇒ raw/, unparseable ⇒ errors/
      ▼
S3  raw/service=<svc>/dt=<date>/…
```

Service owners only ever `print()` a valid JSON line. The CloudWatch subscription, the shipper,
and the S3 layout are Jake's infra — invisible to the services and swappable later (M8) without
changing this contract, because the `raw/service=/dt=/` layout is the durable interface.
