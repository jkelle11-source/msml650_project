# Milestone 3 — Incident Simulator

> **GitHub Milestone**
> **Title:** `M3 — Incident Simulator`
> **Timeframe:** Week 3
> **Depends on:** `M2`
> **Description:**
> Build the Python/boto3 tool that deliberately creates **known** failures against the live application, so M4 can generate a labeled dataset with real ground truth. Five injectors plus a framework: traffic spike, DynamoDB throttling, Lambda slowdown, payment failure, and cascading failure. Sticky ownership continues — Rachel owns payment/Lambda-degradation injection because she built the hooks in M1; the rest map to whoever owns the affected surface. **MVP-first sequencing:** Josh's **traffic-spike injector is the thin-slice incident** (README §18) — land and validate it end-to-end (inject → visible telemetry shift) *before* the medium/hard injectors, because it's the one already targeted for the MVP loop.

## Assignment summary
- **Jake** — Simulator framework/CLI + experiment manifest schema (the harness everyone plugs injectors into)
- **Josh** — Workload generator **+** Traffic-spike injector *(MVP incident — do first)*
- **Linu** — DynamoDB throttling injector (offered-load-to-capacity mechanism)
- **Rachel** — Payment-failure injector **+** Lambda-degradation injector
- **Melisa** — Cascading-failure orchestration *(composes the other injectors)*

## Coordination notes
- **Real throttling, not synthesized.** DynamoDB throttling is produced by driving offered load above a low provisioned RCU/WCU. The controllable knob is the **offered-load-to-capacity ratio**, not a target throttle percentage; the resulting throttle rate is an *emergent, measured* outcome recorded per experiment. No `db_throttled` flag is ever written into features (Feature/Label Boundary, §3).
- **Cascade labeling is observable-state-based.** Early cascade windows are legitimately labeled by their observable single fault (e.g. `TRAFFIC_SPIKE`); `CASCADING_FAILURE` applies only once **two or more faults are concurrently active**. The simulator records concurrency so M4 can apply this correctly.
- All injectors accept `intensity` and `duration` parameters drawn from the **band structure** in §6b — Linu's M4 eval-integrity work defines the exact bands, so injectors must take bands as config, not hardcode magnitudes.

---

## Issues

### `M3-1` — Incident Simulator framework & experiment manifest
- **Assignee:** @jake
- **Labels:** `simulator`, `infra`, `tooling`, `mvp`
- **Blocked by:** `M2-1`
- **Blocks:** `M3-2`, `M3-3`, `M3-4`, `M3-5`

**Context.** The shared harness: a CLI/config layer that starts an experiment, selects an incident + parameters, drives the workload, and records a manifest (incident type, start/end, severity, fault-injection params, ground-truth label) alongside the telemetry. Every injector is a plugin behind a common interface so the five can be built in parallel.

**Deliverables**
- Simulator CLI (`start-incident --type … --intensity … --duration …`) mirroring the conceptual interface in §5.
- A pluggable injector interface (register/parameterize/start/stop) the four injector owners implement against.
- Experiment **manifest** schema + writer: `experiment_id`, incident type, start/end, severity, fault-injection params, ground-truth label, telemetry pointer.
- Manifest and telemetry land in S3 keyed by `experiment_id` (feeds M4's registry).

**Definition of Done**
- [ ] A no-op/echo injector runs end-to-end through the CLI and writes a valid manifest.
- [ ] Injector interface documented; each owner has confirmed it's buildable against.
- [ ] Manifests are reproducible (same params → same recorded config) and cost-bounded (documented teardown).

---

### `M3-2` — Workload generator + Traffic-spike injector `[MVP incident]`
- **Assignee:** @josh
- **Labels:** `simulator`, `workload`, `incident:traffic-spike`, `service:product`, `mvp`
- **Blocked by:** `M3-1`
- **Blocks:** `M3-5`

**Context.** Two coupled pieces: the realistic workload generator (the baseline "normal" traffic) and the traffic-spike injector that scales it. This is the MVP incident, so it must be proven end-to-end first. Product ownership makes Josh the natural home (60% of traffic is `GET /products`).

**Deliverables**
- Workload generator with configurable request rate, distribution, duration, and user count; default mix `60/15/10/10/5` per §7.
- Traffic-spike injector: multiply request rate by a band-driven factor (e.g. `20→200 rps`).
- End-to-end validation: spike injected → telemetry shows the rate/latency shift in S3.

**Definition of Done**
- [ ] Baseline workload produces stable "normal" telemetry over a full run.
- [ ] Traffic-spike injector accepts a multiplier **band** as config (not hardcoded).
- [ ] **MVP loop demonstrated:** a traffic spike is injected and the shift is visible in landed telemetry — this is the thin slice §18 asks for.

---

### `M3-3` — DynamoDB throttling injector
- **Assignee:** @linu
- **Labels:** `simulator`, `incident:db-throttling`, `dynamodb`, `medium`
- **Blocked by:** `M3-1`
- **Blocks:** `M3-5`

**Context.** Produce *real* throttling by controlling the offered-load-to-capacity ratio: set low provisioned RCU/WCU on a target table and drive workload above it. Design carefully for reproducibility and AWS cost.

**Deliverables**
- Injector that sets/records provisioned capacity and drives a controlled offered-load-to-capacity ratio (band-driven).
- Per-experiment recording of the **emergent** throttle rate (measured, not set) from CloudWatch `ThrottledRequests` counts.
- Documented teardown that restores capacity to avoid lingering cost.

**Definition of Done**
- [ ] Injector reliably produces observable `ThrottledRequests > 0` at target ratios.
- [ ] Controllable parameter is the **ratio**; throttle rate is recorded as an outcome, not an input.
- [ ] No `db_throttled` flag written to telemetry features; only CloudWatch counts are used.
- [ ] Capacity is restored on teardown; cost impact documented.

---

### `M3-4` — Payment-failure + Lambda-degradation injectors
- **Assignee:** @rachel
- **Labels:** `simulator`, `incident:dependency-failure`, `incident:lambda-degradation`, `service:payment`, `medium`
- **Blocked by:** `M3-1`
- **Blocks:** `M3-5`

**Context.** Both injectors are "make a Lambda slow or fail," which is exactly the hook set Rachel built in M1 — payment failure turns on `PAYMENT_FAILURE_RATE`/`PAYMENT_TIMEOUT`; Lambda degradation generalizes `PAYMENT_LATENCY_MS` into added compute/delay on a target Lambda.

**Deliverables**
- Payment-failure injector: band-driven high latency, intermittent 5xx, and timeouts on the payment path.
- Lambda-degradation injector: band-driven added latency/compute (`~100ms → ~1000ms`) on a target Lambda.
- Both wired to the simulator interface and recording their injected parameters into the manifest.

**Definition of Done**
- [ ] Payment-failure injector produces observable dependency errors/timeouts on Order's telemetry.
- [ ] Lambda-degradation injector produces the intended `lambda_duration_ms` shift.
- [ ] Both accept latency/timeout **bands** as config.
- [ ] Injected magnitudes recorded to the manifest as Tier-2 ground truth (kept out of features).

---

### `M3-5` — Cascading-failure orchestration
- **Assignee:** @melisa
- **Labels:** `simulator`, `incident:cascading-failure`, `service:order`, `hard`
- **Blocked by:** `M3-2`, `M3-3`, `M3-4`
- **Blocks:** —

**Context.** Compose the primitives into the feedback loop from §4 (traffic spike → DB throttling → Lambda latency → timeouts → retries → more traffic). Order sits at the center of that retry loop, so Melisa owns the composition. This is the hard, high-value demo case — the one where no single threshold fires cleanly.

**Deliverables**
- Orchestrator that sequences the underlying injectors with realistic onset delays to reproduce the cascade.
- A retry-amplification step (timeouts drive user retries that further increase traffic).
- Manifest recording of **which faults are concurrently active over time**, so M4 can label `CASCADING_FAILURE` only during genuine multi-fault windows.

**Definition of Done**
- [ ] A cascade run reproduces correlated symptoms across ≥3 services in landed telemetry.
- [ ] Concurrency timeline recorded so early single-fault windows are distinguishable from true multi-fault windows.
- [ ] Run is reproducible and cost-bounded via documented teardown.
