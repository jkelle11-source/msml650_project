# Milestone 2 — Observability

> **GitHub Milestone**
> **Title:** `M2 — Observability`
> **Timeframe:** Week 2
> **Depends on:** `M1`
> **Description:**
> Turn the working application into an **observable** one: every request emits structured telemetry (not unstructured text), the telemetry lands centrally in S3, and we prove it's rich enough to train ML on. Scope is deliberately narrow — structured logs + CloudWatch metrics → S3, plus the frozen Tier-1/Tier-2 schema. The full event-driven pipeline (EventBridge → SQS → Aggregator Lambda) and one-minute-window aggregation are **out of scope here** — aggregation is M4, and the async pipeline is wired in M8. Each service owner instruments their own Lambda (sticky ownership from M1); Jake owns the shared schema contract and the S3 landing zone.

## Assignment summary
- **Jake** — Telemetry schema contract (Tier-1/Tier-2), JSON-schema validator, S3 raw landing zone + partitioning
- **Josh** — Instrument Product Service
- **Linu** — Instrument Cart Service
- **Melisa** — Instrument Order Service (dependency fields)
- **Rachel** — Instrument Payment Service **+** the CloudWatch **metrics** extractor (throttle counts, Lambda duration/throttles/cold starts)

## Coordination notes
- The **Feature/Label Boundary** (README §3) starts here, not in M4: Jake's schema explicitly tags every field **Tier-1 (observable, may become a feature)** or **Tier-2 (ground-truth/diagnostic, never a feature)**. Instrumenting services must never fold a Tier-2 field (e.g. a synthesized `db_throttled` boolean) into the observable record. Rachel's metrics extractor pulls the **count** of `ThrottledRequests` from CloudWatch — a legitimate Tier-1 signal — not a synthesized flag.
- Every record carries a **millisecond-precision timestamp** and the upstream `request_id`, so cross-service ordering can be reconstructed by correlation (required in MVP; X-Ray tracing stays a stretch goal).

---

## Issues

### `M2-1` — Telemetry schema contract, validator & S3 landing zone
- **Assignee:** @jake
- **Labels:** `observability`, `telemetry`, `infra`, `data`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** `M2-2`, `M2-3`, `M2-4`, `M2-5`

**Context.** Formalize the frozen field list from M1 into an enforced contract, and give the telemetry somewhere to land. This is the spine every ML decision downstream rests on, so the Tier-1/Tier-2 split is defined and machine-checkable from day one.

**Deliverables**
- `telemetry_schema.json` extended into a **validating** schema, with every field tagged `tier: 1` (observable) or `tier: 2` (ground-truth/diagnostic).
- A shared validation helper (importable by every service Lambda) that rejects/records malformed telemetry.
- S3 raw landing zone with a documented partitioning scheme (e.g. `s3://.../raw/service=<svc>/dt=<date>/…`) suited to later batch aggregation.
- A CloudWatch log subscription (or direct-write) path that delivers structured log records into the S3 landing zone.
- A one-page "telemetry contract" doc: field list, tiers, timestamp/`request_id` discipline, S3 layout.

**Definition of Done**
- [ ] Schema validates a known-good record and rejects a record missing `timestamp`/`request_id`.
- [ ] Every Tier-2 field is explicitly marked and documented as *never a feature*.
- [ ] Records from at least one deployed service are visible in the S3 landing zone under the documented partitions.
- [ ] All four service owners have imported the shared validation helper.

---

### `M2-2` — Instrument Product Service
- **Assignee:** @josh
- **Labels:** `observability`, `telemetry`, `service:product`, `mvp`
- **Blocked by:** `M2-1`
- **Blocks:** —

**Context.** Emit full Tier-1 telemetry for both product endpoints against the enforced schema. Independent of the other services.

**Deliverables**
- Per-request structured record with traffic/latency/error/Lambda fields populated (`request_rate` context, `latency_ms`, `status_code`, `lambda_duration_ms`, `cold_start`, DynamoDB `ConsumedCapacity`).
- Millisecond timestamp + `request_id` on every record.
- Records passing Jake's validator and landing in S3.

**Definition of Done**
- [ ] Sample of Product records validates clean against the schema.
- [ ] No Tier-2 field present in the emitted record.
- [ ] Records observable in S3 under the Product partition.

---

### `M2-3` — Instrument Cart Service
- **Assignee:** @linu
- **Labels:** `observability`, `telemetry`, `service:cart`, `mvp`
- **Blocked by:** `M2-1`
- **Blocks:** —

**Context.** Same Tier-1 instrumentation for the three cart endpoints; independent.

**Deliverables**
- Per-request structured record for all three endpoints in the agreed schema.
- Millisecond timestamp + `request_id` on every record.
- Records passing the validator and landing in S3.

**Definition of Done**
- [ ] Sample of Cart records validates clean against the schema.
- [ ] No Tier-2 field present in the emitted record.
- [ ] Records observable in S3 under the Cart partition.

---

### `M2-4` — Instrument Order Service (dependency fields)
- **Assignee:** @melisa
- **Labels:** `observability`, `telemetry`, `service:order`, `dependency`, `mvp`
- **Blocked by:** `M2-1`
- **Blocks:** —

**Context.** Order's records carry the dependency signal (`dependency`, `dependency_latency_ms`, dependency error/timeout counts) that dependency-failure and cascade detection lean on. Correlating Order records with Payment records via `request_id` is what makes the "observed sequence" narration possible later.

**Deliverables**
- Per-request structured record including `dependency`, `dependency_latency_ms`, and dependency error/timeout signals from the Payment call.
- Millisecond timestamp + `request_id`, with the same `request_id` propagated to the Payment call so records can be correlated.
- Records passing the validator and landing in S3.

**Definition of Done**
- [ ] A single order request can be traced across Order → Payment records by shared `request_id`.
- [ ] Dependency fields populated with real measured values.
- [ ] No Tier-2 field present; records validate clean and land in S3.

---

### `M2-5` — Instrument Payment + CloudWatch metrics extractor
- **Assignee:** @rachel
- **Labels:** `observability`, `telemetry`, `service:payment`, `cloudwatch`, `mvp`
- **Blocked by:** `M2-1`
- **Blocks:** —

**Context.** Two jobs: instrument the Payment Lambda (recording injected vs organic delay distinctly), and build the shared **metrics** collector that pulls the non-log CloudWatch signals every service needs — the Tier-1 side of DynamoDB and Lambda health. This is Rachel's because those throttle counts feed the DB-throttling work she's adjacent to in M3.

**Deliverables**
- Payment per-request record in the agreed schema, distinguishing injected delay from organic latency.
- A boto3 metrics-extraction module pulling from CloudWatch: DynamoDB `ConsumedCapacity` and **`ThrottledRequests` counts**, Lambda `Duration`, `Throttles`, `ColdStarts` / init duration.
- Extracted metrics aligned to the same timestamp discipline and landed in S3 alongside the log records.

**Definition of Done**
- [ ] Payment records validate clean; injected delay is separately identifiable.
- [ ] Metrics extractor returns real DynamoDB throttle **counts** (not a synthesized boolean) and Lambda health metrics.
- [ ] Extracted metrics land in S3 and are joinable to log records by timestamp/service.
- [ ] Milestone exit check: a reviewer confirms the combined telemetry is *sufficient to train ML on* (traffic, latency percentiles, errors, Lambda, DynamoDB, dependency signals all present).
