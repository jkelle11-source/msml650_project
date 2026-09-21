First let me read the milestone definitions from the proposal.Good — I have a clear picture of the full application shape. Here's how to split Milestone 1 cleanly across five people.

---

## Milestone 1 — Serverless Application: Division of Work

The core deliverable is a working API Gateway → Lambda → DynamoDB stack. The natural seam is **one person per service, plus one person owning the shared infrastructure that everyone plugs into.** With this split, each person can build and test their piece independently against a shared interface contract agreed on day one.

---

### Person 1 — Shared Infrastructure & API Gateway

**What they own:** The AWS account setup, CDK/CloudFormation project skeleton, API Gateway configuration, and the shared DynamoDB table definitions (or individual tables, depending on the team's preference). This person creates the skeleton that everyone else deploys into.

**Deliverables:**
- AWS account configured, IAM roles and least-privilege policies created for all five services
- CDK (or SAM) project initialized with a shared stack structure
- API Gateway deployed with route stubs: all routes defined, returning 501 Not Implemented until the Lambda behind them is wired
- DynamoDB tables created (Products, Carts, Orders) with partition keys and any GSIs agreed on upfront
- A shared `.env` / config file or SSM Parameter Store entries so other team members can reference table names, ARNs, and the API base URL without hardcoding

**Independence note:** Everyone else depends on this person's table names and API Gateway URL, so this work should be done — or at least the interface contract documented — before others start. This person's work is the only real sequencing dependency in Milestone 1, and it's small (a day or two of setup).

---

### Person 2 — Product Service

**What they own:** The two product endpoints and their DynamoDB interactions.

**Deliverables:**
- `GET /products` — scans or queries the Products table, returns a list
- `GET /products/{id}` — fetches a single product by partition key
- Lambda function with structured JSON logging (matching the telemetry schema from Section 3: `service`, `endpoint`, `latency_ms`, `status_code`, etc.)
- Seed script that populates the Products table with 20–30 realistic dummy products
- Local test script (e.g., `curl` or pytest) that validates both endpoints against the deployed API

**Independence note:** Only depends on Person 1's table name and API Gateway route being wired. No dependency on Cart, Order, or Payment.

---

### Person 3 — Cart Service

**What they own:** The three cart endpoints and their DynamoDB interactions.

**Deliverables:**
- `POST /cart` — adds an item to a user's cart (writes to Carts table keyed on `user_id`)
- `GET /cart/{user}` — retrieves a user's current cart
- `DELETE /cart/{user}/{item}` — removes a specific item
- Lambda function with structured JSON logging in the same schema as Person 2
- Local test script validating all three endpoints, including edge cases (empty cart, item not found)

**Independence note:** Cart doesn't call any other service. Only depends on Person 1's Carts table and API Gateway route.

---

### Person 4 — Order Service

**What they own:** The two order endpoints. This is the most connected service — it writes to DynamoDB *and* calls Payment — so it needs Person 5's Payment Lambda to exist (even as a stub) to complete.

**Deliverables:**
- `POST /orders` — creates an order in the Orders table, then calls the Payment Lambda synchronously (via `boto3 lambda.invoke` or direct invocation)
- `GET /orders/{id}` — retrieves an order by ID
- Lambda function with structured JSON logging, including `dependency`, `dependency_latency_ms`, and `dependency_error` fields populated from the Payment call
- Handles payment failure gracefully (returns a 402 or 500 with a structured error body)
- Local test script validating both happy-path and payment-failure scenarios

**Independence note:** Needs Person 5's Payment Lambda deployed (even returning a hardcoded success response) to run end-to-end tests. Everything else is independent.

---

### Person 5 — Payment Service

**What they own:** The payment Lambda and the fault-injection interface that the incident simulator will use in later milestones. Building this right now pays dividends in Milestone 3.

**Deliverables:**
- `POST /payments` — accepts a payment payload, returns success or failure
- **Configurable behavior via environment variables or request parameters:**
  - `PAYMENT_LATENCY_MS` — adds artificial sleep before responding (for Lambda slowdown injection later)
  - `PAYMENT_FAILURE_RATE` — returns a failure response with this probability (for dependency failure injection later)
  - `PAYMENT_TIMEOUT` — optionally hangs the response to simulate a timeout
- Structured JSON logging with `latency_ms`, `outcome` (success/failure), and any injected delay recorded
- Local test script covering success, injected failure, and injected latency scenarios

**Independence note:** Payment has no upstream dependencies — it doesn't call any other service. Person 4 depends on this stub existing, but Person 5 can deliver a hardcoded-success stub in a few hours to unblock Person 4, then iterate on the full configurable version.

---

## Coordination Protocol

Two things to agree on at kickoff so everyone can work in parallel:

**1. Telemetry schema contract** — agree on the JSON log fields (from Section 3) before anyone writes a Lambda. Every Lambda should emit the same fields even if some are null for that service. Person 1 can own a `telemetry_schema.json` file in the repo that everyone references.

**2. API response shapes** — agree on success and error JSON shapes upfront. A simple `{"data": ..., "error": null}` envelope is enough. This lets Person 4 stub the Payment call before Person 5 is done.