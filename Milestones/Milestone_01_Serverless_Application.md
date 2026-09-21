# Milestone 1 — Serverless Application

> **GitHub Milestone**
> **Title:** `M1 — Serverless Application`
> **Timeframe:** Week 1
> **Depends on:** none
> **Description:**
> Stand up the working `API Gateway → Lambda → DynamoDB` stack: a deliberately small serverless e-commerce API with four services (Product, Cart, Order, Payment) behind a shared API Gateway, each backed by DynamoDB, plus the shared infrastructure skeleton everyone deploys into. This is the MVP substrate — every later milestone (telemetry, simulator, ML, GenAI, dashboard) plugs into the interface contract agreed here. The natural seam is **one person per service, plus one person owning the shared infrastructure**, so each person builds and tests independently against a contract fixed on day one.

## Assignment summary
- **Jake** — Shared infrastructure, IAM, CDK/SAM skeleton, API Gateway, DynamoDB tables *(the only real sequencing dependency)*
- **Josh** — Product Service
- **Linu** — Cart Service
- **Melisa** — Order Service *(needs Rachel's Payment stub)*
- **Rachel** — Payment Service *(+ fault-injection hooks that pay off in M3)*

## Coordination protocol (agree at kickoff, before anyone writes a Lambda)
1. **Telemetry schema contract** — agree the JSON log fields (README §3) up front. Every Lambda emits the same fields even if some are `null` for that service. Jake owns `telemetry_schema.json` in the repo; full instrumentation lands in M2, but the field list is frozen now so services don't diverge.
2. **API response shapes** — agree success/error envelopes up front. A simple `{"data": ..., "error": null}` envelope is enough, and it lets Melisa stub the Payment call before Rachel is finished.

---

## Issues

### `M1-1` — Shared infrastructure & API Gateway skeleton
- **Assignee:** @jake
- **Labels:** `infra`, `api-gateway`, `iam`, `mvp`
- **Blocked by:** none
- **Blocks:** `M1-2`, `M1-3`, `M1-4`, `M1-5`

**Context.** Own the AWS account setup, the CDK/SAM project skeleton, API Gateway configuration, and the shared DynamoDB table definitions. This is the skeleton everyone else deploys into, so the interface contract (table names, ARNs, base URL) must be documented before the others start. It's small — a day or two — but it's the one true sequencing dependency in this milestone.

**Deliverables**
- AWS account configured; IAM roles and least-privilege policies created for all five service tracks.
- CDK (or SAM) project initialized with a shared stack structure the four service stacks deploy into.
- API Gateway deployed with **route stubs**: all routes defined, returning `501 Not Implemented` until the Lambda behind them is wired.
- DynamoDB tables created (Products, Carts, Orders) with partition keys and any GSIs agreed upfront.
- Shared config surface — an `.env` / SSM Parameter Store entries — so others reference table names, ARNs, and the API base URL without hardcoding.

**Definition of Done**
- [ ] Every route returns `501` from a live, reachable API Gateway URL.
- [ ] Table names, ARNs, base URL, and IAM role ARNs are published to SSM/`.env` and documented in the repo README.
- [ ] `telemetry_schema.json` committed with the frozen Tier-1 field list (per M1 coordination protocol).
- [ ] A teammate can deploy their own service stack into the skeleton without editing Jake's stack.

---

### `M1-2` — Product Service
- **Assignee:** @josh
- **Labels:** `service:product`, `lambda`, `dynamodb`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** —

**Context.** The two product read endpoints and their DynamoDB interactions. Highest-traffic service in the workload mix (60% `GET /products`), so it doubles as the natural home for load generation later. Depends only on Jake's table + route being wired; independent of Cart, Order, Payment.

**Deliverables**
- `GET /products` — scans/queries the Products table, returns a list.
- `GET /products/{id}` — fetches a single product by partition key.
- Lambda with structured JSON logging matching the frozen schema (`service`, `endpoint`, `latency_ms`, `status_code`, …).
- Seed script populating Products with 20–30 realistic dummy products.
- Local test script (`curl`/pytest) validating both endpoints against the deployed API.

**Definition of Done**
- [ ] Both endpoints return `2xx` with correct data against the deployed API Gateway URL.
- [ ] Structured log line emitted per request in the agreed schema shape.
- [ ] Seed script is idempotent and re-runnable.
- [ ] Test script passes against the deployed stack, not just locally.

---

### `M1-3` — Cart Service
- **Assignee:** @linu
- **Labels:** `service:cart`, `lambda`, `dynamodb`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** —

**Context.** The three cart endpoints and their DynamoDB interactions. Cart calls no other service, so it's fully independent once Jake's Carts table and routes exist.

**Deliverables**
- `POST /cart` — adds an item to a user's cart (writes to Carts, keyed on `user_id`).
- `GET /cart/{user}` — retrieves a user's current cart.
- `DELETE /cart/{user}/{item}` — removes a specific item.
- Lambda with structured JSON logging in the same schema as Product.
- Local test script covering all three endpoints **including edge cases** (empty cart, item not found).

**Definition of Done**
- [ ] All three endpoints return correct `2xx`/`4xx` behavior against the deployed API.
- [ ] Edge cases (empty cart, missing item) return structured error envelopes, not stack traces.
- [ ] Structured log line emitted per request in the agreed schema.
- [ ] Test script passes against the deployed stack.

---

### `M1-4` — Order Service
- **Assignee:** @melisa
- **Labels:** `service:order`, `lambda`, `dynamodb`, `dependency`, `mvp`
- **Blocked by:** `M1-1`, `M1-5` *(Payment stub only)*
- **Blocks:** —

**Context.** The two order endpoints — the most connected service, because it writes to DynamoDB **and** calls Payment. It needs Rachel's Payment Lambda to exist (even a hardcoded-success stub) to complete its end-to-end test. The `dependency_*` telemetry fields it populates here become the raw signal for dependency-failure detection later.

**Deliverables**
- `POST /orders` — creates an order in Orders, then calls the Payment Lambda synchronously (`boto3 lambda.invoke` or direct invocation).
- `GET /orders/{id}` — retrieves an order by ID.
- Lambda with structured JSON logging including `dependency`, `dependency_latency_ms`, and `dependency_error` populated from the Payment call.
- Graceful payment-failure handling (return `402`/`500` with a structured error body).
- Local test script covering happy-path **and** payment-failure scenarios.

**Definition of Done**
- [ ] `POST /orders` succeeds end-to-end against Rachel's Payment stub.
- [ ] Payment failure produces a structured error body and a log line with `dependency_error` set.
- [ ] `dependency_latency_ms` reflects the real measured call time to Payment.
- [ ] Test script passes both happy-path and failure paths against the deployed stack.

---

### `M1-5` — Payment Service (+ fault-injection interface)
- **Assignee:** @rachel
- **Labels:** `service:payment`, `lambda`, `fault-injection`, `mvp`
- **Blocked by:** `M1-1`
- **Blocks:** `M1-4`

**Context.** The payment Lambda **and** the configurable fault-injection interface the incident simulator will drive in M3. Building the knobs now pays dividends later — payment-failure and Lambda-slowdown injection in M3 are just these hooks turned on. Payment has no upstream dependencies, so Rachel can ship a hardcoded-success stub in a few hours to unblock Melisa, then iterate on the full configurable version.

**Deliverables**
- `POST /payments` — accepts a payload, returns success or failure.
- Configurable behavior via env vars / request params:
  - `PAYMENT_LATENCY_MS` — artificial sleep before responding (Lambda-slowdown injection later).
  - `PAYMENT_FAILURE_RATE` — returns a failure with this probability (dependency-failure injection later).
  - `PAYMENT_TIMEOUT` — optionally hangs to simulate a timeout.
- Structured JSON logging with `latency_ms`, `outcome` (success/failure), and any injected delay recorded.
- Local test script covering success, injected failure, and injected latency.

**Definition of Done**
- [ ] Hardcoded-success stub deployed early and Melisa confirmed unblocked (link the confirmation).
- [ ] All three injection knobs demonstrably change response behavior in tests.
- [ ] Injected delay is recorded distinctly from organic latency in the log line.
- [ ] Test script passes success / injected-failure / injected-latency scenarios against the deployed stack.
