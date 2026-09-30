# Milestone 1 — Serverless Application

> **GitHub Milestone**
> **Title:** `M1 — Serverless Application`
> **Timeframe:** Week 1
> **Depends on:** none
> **Description:**
> Stand up the working `API Gateway → Lambda → DynamoDB` stack: a deliberately small serverless e-commerce API with four services (Product, Cart, Order, Payment) behind a shared API Gateway, each backed by DynamoDB. Everything lives in **one shared CloudFormation stack that Jake owns and deploys** — the API, all routes, the tables, the IAM roles, and all four service Lambdas. This is the MVP substrate — every later milestone (telemetry, simulator, ML, GenAI, dashboard) plugs into the interface contract agreed here. The natural seam is **one person per service's application code, plus Jake owning the single shared stack**: each teammate implements their service's Lambda handler in `services/<service>/handler.py` and merges it by PR, while Jake owns the one stack everything deploys through.

## Assignment summary
- **Jake** — The single shared stack: IAM, SAM template, API Gateway + all routes, DynamoDB tables, and all four service Lambda resources *(the only real sequencing dependency)*
- **Josh** — Product Service handler code
- **Linu** — Cart Service handler code
- **Melisa** — Order Service handler code *(needs Rachel's Payment handler)*
- **Rachel** — Payment Service handler code *(+ fault-injection hooks that pay off in M3)*

## Coordination protocol (agree at kickoff, before anyone writes a handler)
1. **Telemetry schema contract** — agree the JSON log fields up front. Every Lambda emits the same fields even if some are `null` for that service. Jake owns `telemetry_schema.json` in the repo; full instrumentation lands in M2, but the field list is frozen now so services don't diverge.
2. **API response shapes** — agree success/error envelopes up front. A simple `{"data": ..., "error": null}` envelope is enough, and it lets Melisa stub the Payment call before Rachel is finished.
3. **Routes and function names are owned by the shared stack** — Jake defines every route and the `anomalypulse-<service>` function names in the one template. Teammates fill in the handler behind their route; a route change is a coordination decision, not a solo edit.

---

## Issues

### `M1-1` — Shared stack: API Gateway, tables, roles, and service Lambdas
- **Assignee:** @jake
- **Labels:** `infra`, `api-gateway`, `iam`, `mvp`
- **Blocked by:** none
- **Blocks:** `M1-2`, `M1-3`, `M1-4`, `M1-5`

**Context.** Own the AWS account setup and the one shared SAM stack that holds everything: the API Gateway, every route, the DynamoDB tables, the per-service IAM roles, and all four service Lambda resources (each pointing at a `services/<service>/handler.py` code folder). Teammates don't deploy — they fill in their handler code and merge by PR, and Jake redeploys the stack. The interface contract (routes, table names, base URL, function names, response envelope) must be documented before the others start. It's small — a day or two — but it's the one true sequencing dependency in this milestone.

**Deliverables**
- AWS account configured; per-service IAM roles with least-privilege policies (read-only Products; read/write Carts and Orders; Order may invoke Payment; Payment logs-only).
- One SAM stack (`anomalypulse-shared`) holding the API, all routes, the tables, the roles, and all four service Lambdas.
- Every route wired to its `anomalypulse-<service>` Lambda; a `/{proxy+}` catch-all returns `501 Not Implemented` for undefined paths. Service Lambdas ship as **stubs** (200 + envelope) until teammates fill in the logic.
- DynamoDB tables created (Products, Carts, Orders) with the agreed partition/sort keys.
- Shared config surface — SSM Parameter Store entries — so tools reference the API base URL and table names without hardcoding.
- Stub `services/<service>/handler.py` for each service so the stack builds and deploys.

**Definition of Done**
- [ ] Every defined route returns a stub `200` (envelope) from a live API Gateway URL; an undefined path returns `501`.
- [ ] API base URL and table names are published to SSM and reachable.
- [ ] `telemetry_schema.json` committed with the frozen Tier-1 field list (per M1 coordination protocol).
- [ ] A teammate can edit their `services/<service>/handler.py` and, after Jake redeploys, see their route return real data — without touching the template.

---

### `M1-2` — Product Service
- **Assignee:** @josh
- **Labels:** `service:product`, `lambda`, `dynamodb`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** —

**Context.** The two product read endpoints and their DynamoDB interactions, implemented in `services/product/handler.py`. Highest-traffic service in the workload mix (60% `GET /products`), so it doubles as the natural home for load generation later. Jake's stack already wires the route, table, and role; independent of Cart, Order, Payment.

**Deliverables**
- `GET /products` — scans/queries the Products table, returns a list.
- `GET /products/{id}` — fetches a single product by partition key.
- Handler (`services/product/handler.py`) that branches on `event["resource"]`/`event["httpMethod"]`, with structured JSON logging matching the frozen schema (`service`, `endpoint`, `latency_ms`, `status_code`, …).
- Seed script populating Products with 20–30 realistic dummy products.
- Test script (local unit tests, plus `curl` against the deployed API after merge) validating both endpoints.

**Definition of Done**
- [ ] Both endpoints return `2xx` with correct data against the deployed API Gateway URL (after Jake redeploys the merged PR).
- [ ] Structured log line emitted per request in the agreed schema shape.
- [ ] Seed script is idempotent and re-runnable.
- [ ] Tests pass locally and the endpoints are confirmed against the deployed stack.

---

### `M1-3` — Cart Service
- **Assignee:** @linu
- **Labels:** `service:cart`, `lambda`, `dynamodb`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** —

**Context.** The three cart endpoints and their DynamoDB interactions, implemented in `services/cart/handler.py`. Cart calls no other service, so it's fully independent once Jake's Carts table and routes exist (they do).

**Deliverables**
- `POST /cart` — adds an item to a user's cart (writes to Carts, keyed on `user_id`).
- `GET /cart/{user}` — retrieves a user's current cart.
- `DELETE /cart/{user}/{item}` — removes a specific item.
- Handler (`services/cart/handler.py`) with structured JSON logging in the same schema as Product.
- Test script (local unit tests + `curl` post-merge) covering all three endpoints **including edge cases** (empty cart, item not found).

**Definition of Done**
- [ ] All three endpoints return correct `2xx`/`4xx` behavior against the deployed API.
- [ ] Edge cases (empty cart, missing item) return structured error envelopes, not stack traces.
- [ ] Structured log line emitted per request in the agreed schema.
- [ ] Tests pass locally and against the deployed stack.

---

### `M1-4` — Order Service
- **Assignee:** @melisa
- **Labels:** `service:order`, `lambda`, `dynamodb`, `dependency`, `mvp`
- **Blocked by:** `M1-1`, `M1-5` *(Payment handler stub only)*
- **Blocks:** —

**Context.** The two order endpoints, implemented in `services/order/handler.py` — the most connected service, because it writes to DynamoDB **and** invokes Payment (`anomalypulse-payment`). It needs Rachel's Payment handler deployed (even a hardcoded-success stub) to complete its end-to-end test. The `dependency_*` telemetry fields it populates here become the raw signal for dependency-failure detection later.

**Deliverables**
- `POST /orders` — creates an order in Orders, then invokes the Payment Lambda synchronously (`boto3 lambda.invoke` on `anomalypulse-payment`).
- `GET /orders/{id}` — retrieves an order by ID.
- Handler (`services/order/handler.py`) with structured JSON logging including `dependency`, `dependency_latency_ms`, and `dependency_error` populated from the Payment call.
- Graceful payment-failure handling (return `402`/`500` with a structured error body).
- Test script covering happy-path **and** payment-failure scenarios.

**Definition of Done**
- [ ] `POST /orders` succeeds end-to-end against Rachel's Payment stub.
- [ ] Payment failure produces a structured error body and a log line with `dependency_error` set.
- [ ] `dependency_latency_ms` reflects the real measured call time to Payment.
- [ ] Tests pass both happy-path and failure paths against the deployed stack.

---

### `M1-5` — Payment Service (+ fault-injection interface)
- **Assignee:** @rachel
- **Labels:** `service:payment`, `lambda`, `fault-injection`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** `M1-4`

**Context.** The payment handler (`services/payment/handler.py`) **and** the configurable fault-injection interface the incident simulator will drive in M3. The three knobs are pre-declared as env vars (defaulting to off) in Jake's template; building the logic now pays dividends later — payment-failure and Lambda-slowdown injection in M3 are just these hooks turned on. Payment has no upstream dependencies and no table, so Rachel can ship a hardcoded-success handler in a few hours to unblock Melisa, then iterate on the full configurable version.

**Deliverables**
- `POST /payments` — accepts a payload, returns success or failure.
- Configurable behavior via the pre-declared env vars:
  - `PAYMENT_LATENCY_MS` — artificial sleep before responding (Lambda-slowdown injection later).
  - `PAYMENT_FAILURE_RATE` — returns a failure with this probability (dependency-failure injection later).
  - `PAYMENT_TIMEOUT` — optionally hangs to simulate a timeout.
- Structured JSON logging with `latency_ms` and `outcome` (success/failure). **Tier-1 only** — `latency_ms` reflects any injected slowdown, but the injected knob values themselves are Tier-2 and must **not** be emitted (the simulator records them separately in M3/M4).
- Test script covering success, injected failure, and injected latency.

**Definition of Done**
- [ ] Hardcoded-success handler merged early and Melisa confirmed unblocked (link the confirmation).
- [ ] All three injection knobs demonstrably change response behavior in tests.
- [ ] Payment's log line stays Tier-1 (no injected-parameter fields); `latency_ms` reflects injected delay organically.
- [ ] Tests pass success / injected-failure / injected-latency scenarios against the deployed stack.
