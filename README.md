# AnomalyPulse

**Serverless AI-Powered Incident Detection and Root-Cause Analysis**

AnomalyPulse is a serverless AI-powered observability platform that monitors a
distributed e-commerce application, detects anomalous behavior using machine
learning, classifies the underlying incident, and uses generative AI to explain
the likely root cause and recommend remediation. We deliberately inject known
failures to build a labeled dataset, so detection and classification can be
evaluated quantitatively rather than by demo alone.

This is a five-person course project (UMD MSML650). The full design — telemetry
schema, ML approach, evaluation integrity, and the reasoning behind each
decision — lives in **[Docs/PROJECT_PLAN.md](Docs/PROJECT_PLAN.md)**. This README
is the front door: what the repo contains, its current status, and how to plug in.

---

## Status

**Milestone 2 — Observability** (in progress). Turn the working application into an
*observable* one: every request emits structured telemetry against an enforced
schema, and that telemetry lands centrally in S3. See
[Milestones/M02_Observability.md](Milestones/M02_Observability.md).

| Piece | Owner | Status |
|---|---|---|
| **M2-1** Telemetry schema contract + validator, shared Lambda layer, S3 landing zone, CloudWatch→S3 shipper | Jake | 🚧 In review (branch `m2-1_jake`) |
| **M2-2** Instrument Product Service | Josh | ⬜ Pending (blocked on M2-1) |
| **M2-3** Instrument Cart Service | Linu | ⬜ Pending (blocked on M2-1) |
| **M2-4** Instrument Order Service *(dependency fields)* | Melisa | ⬜ Pending (blocked on M2-1) |
| **M2-5** Instrument Payment Service *(+ CloudWatch metrics extractor)* | Rachel | ⬜ Pending (blocked on M2-1) |

M2-1 freezes the contract every service emits against: a validating JSON Schema
(Tier-1 observable vs Tier-2 ground-truth fields, enforced by
`additionalProperties: false`), a shared validator packaged as the
`anomalypulse-telemetry` Lambda layer, an S3 raw landing zone
(`raw/service=<svc>/dt=<date>/…`), and the CloudWatch Logs → S3 shipper that
carries each printed record into the bucket. The full contract lives in
**[Docs/TELEMETRY_CONTRACT.md](Docs/TELEMETRY_CONTRACT.md)**. Service owners
(M2-2…M2-5) wire their handlers to the enforced validator once M2-1 merges; the
handlers already emit an initial Tier-1 telemetry line from M1 alignment.

**Milestone 1 — Serverless Application** (complete, merged to `main`). See
[Milestones/M01_Serverless_Application.md](Milestones/M01_Serverless_Application.md).

| Piece | Owner | Status |
|---|---|---|
| **M1-1** Shared infrastructure (API Gateway, DynamoDB tables, IAM roles, SSM config) | Jake | ✅ Merged |
| **M1-2** Product Service | Josh | ✅ Merged |
| **M1-3** Cart Service | Linu | ✅ Merged |
| **M1-4** Order Service *(invokes real Payment)* | Melisa | ✅ Merged |
| **M1-5** Payment Service *(+ fault-injection hooks)* | Rachel | ✅ Merged |

All four services are implemented, unit-tested, and merged to `main`; the four
Lambdas plus their routes, tables, and roles are defined in the single shared
stack (`AnomalyPulse/infra/shared/template.yaml`). The API is deployed to
**`us-east-2`** (stack `anomalypulse-shared`, stage `dev`). A catch-all
`StubFunction` answers `501 Not Implemented` for any route that isn't yet wired.

---

## Repository layout

| Path | What's there |
|---|---|
| [Docs/PROJECT_PLAN.md](Docs/PROJECT_PLAN.md) | The full project plan and design spec — source of truth for all design decisions. |
| [Docs/TELEMETRY_CONTRACT.md](Docs/TELEMETRY_CONTRACT.md) | The telemetry contract (M2): field list, Tier-1/Tier-2 split, timestamp/`request_id` discipline, and S3 layout. |
| [Docs/Summary.md](Docs/Summary.md) | Short project summary draft. |
| [Milestones/](Milestones/) | The ten milestone specs (M1–M10), each with issues and Definition of Done. |
| [Handoffs/](Handoffs/) | Onboarding and handoff docs — [M1-1_Handoff.md](Handoffs/M1-1_Handoff.md) (stack + team conventions) and [M2-1_Handoff.md](Handoffs/M2-1_Handoff.md) (how to emit telemetry). |
| [AnomalyPulse/infra/shared/](AnomalyPulse/infra/shared/) | The SAM stack — `template.yaml` defines the four Lambdas, their API routes, DynamoDB tables, per-service IAM roles, the telemetry layer, the S3 landing bucket, the log groups + subscription filters, and the SSM parameters. Also holds `field_catalog.json` (the full field registry incl. Tier-2) and `shipper/` (the CloudWatch Logs → S3 shipper Lambda). |
| [AnomalyPulse/layers/telemetry/](AnomalyPulse/layers/telemetry/) | The `anomalypulse-telemetry` Lambda layer — the frozen `telemetry_schema.json` + `schema_validator.py`, attached to every function and imported by service handlers and tests. |
| [AnomalyPulse/services/](AnomalyPulse/services/) | The service code: one folder per service (`product/`, `cart/`, `order/`, `payment/`), each with `handler.py` and `test_handler.py`. |

---

## Shared infrastructure reference

Everything the shared stack publishes is in **AWS Systems Manager Parameter
Store**, so that services look values up by a fixed path instead of hardcoding
anything that changes on redeploy. Values can be read with:

```bash
aws ssm get-parameter --name <path> --query Parameter.Value --output text --profile <your-profile>
```

### Published parameters

| Parameter path | Value | You need it for |
|---|---|---|
| `/anomalypulse/api/base-url` | API invoke URL (`https://…/dev`) | Calling the API in tests and the dashboard |
| `/anomalypulse/api/rest-api-id` | REST API ID | Referencing the shared API |
| `/anomalypulse/tables/products/name` | Products table name | Product Lambda's `TABLE_NAME` |
| `/anomalypulse/tables/carts/name` | Carts table name | Cart Lambda's `TABLE_NAME` |
| `/anomalypulse/tables/orders/name` | Orders table name | Order Lambda's `TABLE_NAME` |
| `/anomalypulse/telemetry/bucket/name` | Telemetry S3 bucket name | Reading raw telemetry landed by the shipper |

The Lambda functions, tables, and IAM roles now live in the same shared template,
so functions reference each other directly (via `!Ref`/`!GetAtt`) rather than
looking values up from SSM. The parameters above are for external consumers —
tests, scripts, and later milestones (dashboard, incident simulator).

List them all: `aws ssm get-parameters-by-path --path /anomalypulse --recursive --query "Parameters[].Name"`

### DynamoDB tables

Only key attributes are fixed; everything else is schemaless and written by the
service Lambdas. All tables are on-demand (`PAY_PER_REQUEST`).

| Table | Partition key | Sort key | Notes |
|---|---|---|---|
| `anomalypulse-products` | `id` (S) | — | `GET /products/{id}` by key; `GET /products` scans. |
| `anomalypulse-carts` | `user_id` (S) | `item_id` (S) | Composite key: `GET /cart/{user}` is one Query; `DELETE /cart/{user}/{item}` is one delete. |
| `anomalypulse-orders` | `order_id` (S) | — | `GET /orders/{id}` by key. |

> **M3 note:** the DynamoDB-throttling incident needs a table in `PROVISIONED`
> mode with low capacity to produce *real* throttling. On-demand is correct for
> M1; revisit the Products table's billing mode in M3.

### Per-service IAM roles (least privilege)

Each role grants only what its service does, plus CloudWatch Logs. The roles are
defined in the shared template and attached to each function there.

| Role | DynamoDB access | Extra |
|---|---|---|
| Product | Read-only on Products (`GetItem`, `Query`, `Scan`, `BatchGetItem`) | — |
| Cart | Read/write on Carts | — |
| Order | Read/write on Orders | `lambda:InvokeFunction` on `anomalypulse-payment` |
| Payment | none | Logs only (stores no data) |
| Shipper | none | `s3:PutObject` on the telemetry bucket (`raw/*`, `errors/*`) |

### Telemetry landing zone (S3)

Structured telemetry lands in `anomalypulse-telemetry-<account>-<region>` (name
published to SSM at `/anomalypulse/telemetry/bucket/name`). A service handler only
`print()`s one JSON record per request to stdout — the rest is automatic:

```text
service Lambda  print(json)
   → CloudWatch Logs  /aws/lambda/anomalypulse-<svc>
   → SubscriptionFilter { $.request_id = "*" }   (forwards telemetry lines only)
   → shipper Lambda  (anomalypulse-telemetry-shipper)
   → S3  raw/service=<svc>/dt=<YYYY-MM-DD>/…      (unparseable lines → errors/…)
```

Objects under `raw/` expire after 90 days. The `service=…/dt=…` key segments are
Hive-style partitions for later batch aggregation (M4). Handlers never write to S3
directly. Full detail: [Docs/TELEMETRY_CONTRACT.md](Docs/TELEMETRY_CONTRACT.md).

---

## Interface contracts

These keep five people's code compatible. Full detail in
[Handoffs/M1-1_Handoff.md](Handoffs/M1-1_Handoff.md) §7.

- **Response envelope** — every endpoint returns `{ "data": …, "error": null }`
  on success and `{ "data": null, "error": { "code": …, "message": … } }` on
  failure. Return structured errors, never stack traces.
- **Routes** — Product: `GET /products`, `GET /products/{id}` · Cart:
  `POST /cart`, `GET /cart/{user}`, `DELETE /cart/{user}/{item}` · Order:
  `POST /orders`, `GET /orders/{id}` · Payment: `POST /payments`.
- **Function names** — `anomalypulse-<service>` (fixed; Order invokes
  `anomalypulse-payment` by this exact name).
- **Telemetry** — every service emits one structured JSON log line per request,
  validated against the frozen schema in the `anomalypulse-telemetry` Lambda layer
  ([telemetry_schema.json](AnomalyPulse/layers/telemetry/python/telemetry_schema.json),
  `additionalProperties: false`). Tier-1 (observable) and Tier-2 (ground-truth)
  fields never mix — this protects the ML from data leakage. Full contract:
  [Docs/TELEMETRY_CONTRACT.md](Docs/TELEMETRY_CONTRACT.md).

---

## Deploying the stack (Jake)

The single shared stack builds and deploys all four service functions along with
the API, tables, roles, and SSM parameters:

```bash
cd AnomalyPulse/infra/shared
sam build
sam deploy
```

`sam build` also builds the `anomalypulse-telemetry` layer (via its `Makefile`) and
the shipper Lambda. Config (stack name, region, capabilities) is saved in
`samconfig.toml`, so routine deploys are just `sam deploy`.

## Running the tests

Each service has unit tests next to its handler. Test-only dependencies live in
`requirements-dev.txt` (kept out of `requirements.txt` so they're never bundled
into the Lambda). Install them first, then run `pytest`:

```bash
cd AnomalyPulse/services/<service>   # product | cart | order | payment
pip install -r requirements-dev.txt
python -m pytest
```

`requirements.txt` in each service folder holds only what the deployed Lambda
needs at runtime. `boto3`/`botocore` are provided by the AWS Lambda Python
runtime, so they're intentionally not bundled there — the tests pull them in via
`requirements-dev.txt` instead.

The telemetry layer has its own schema/validator tests (they need `jsonschema` +
`pytest`):

```bash
cd AnomalyPulse/layers/telemetry
pip install -r requirements.txt pytest
python -m pytest
```
