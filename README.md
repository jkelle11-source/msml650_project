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

**Milestone 1 — Serverless Application** (in progress). See
[Milestones/Milestone_01_Serverless_Application.md](Milestones/Milestone_01_Serverless_Application.md).

| Piece | Owner | Status |
|---|---|---|
| **M1-1** Shared infrastructure (API Gateway, DynamoDB tables, IAM roles, SSM config) | Jake | ✅ Live |
| **M1-2** Product Service | Josh | ⏳ Unblocked |
| **M1-3** Cart Service | Linu | ⏳ Unblocked |
| **M1-4** Order Service *(needs Payment stub)* | Melisa | ⏳ Unblocked |
| **M1-5** Payment Service *(+ fault-injection hooks)* | Rachel | ⏳ Unblocked |

The shared API is deployed to **`us-east-2`** (stack `anomalypulse-shared`). Every
route currently answers `501 Not Implemented` until the service behind it is wired.

---

## Repository layout

| Path | What's there |
|---|---|
| [Docs/PROJECT_PLAN.md](Docs/PROJECT_PLAN.md) | The full project plan and design spec — source of truth for all design decisions. |
| [Docs/Summary.md](Docs/Summary.md) | Short project summary draft. |
| [Milestones/](Milestones/) | The ten milestone specs (M1–M10), each with issues and Definition of Done. |
| [Handoffs/](Handoffs/) | Onboarding and handoff docs. Start with [M1-1_Handoff.md](Handoffs/M1-1_Handoff.md). |
| [AnomalyPulse/infra/](AnomalyPulse/infra/) | SAM infrastructure. `shared/` is Jake's deployed stack; each service gets its own sibling folder. |

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
| `/anomalypulse/api/base-url` | API invoke URL (`https://…/dev`) | Calling the API in tests |
| `/anomalypulse/api/rest-api-id` | REST API ID | Attaching your routes (`RestApiId: !Ref SharedApiId`) |
| `/anomalypulse/api/root-resource-id` | API root resource ID | Some route setups need it |
| `/anomalypulse/tables/products/name` | Products table name | Product Lambda's `TABLE_NAME` |
| `/anomalypulse/tables/carts/name` | Carts table name | Cart Lambda's `TABLE_NAME` |
| `/anomalypulse/tables/orders/name` | Orders table name | Order Lambda's `TABLE_NAME` |
| `/anomalypulse/iam/product-role-arn` | Product execution role ARN | Product Lambda's `Role:` |
| `/anomalypulse/iam/cart-role-arn` | Cart execution role ARN | Cart Lambda's `Role:` |
| `/anomalypulse/iam/order-role-arn` | Order execution role ARN | Order Lambda's `Role:` |
| `/anomalypulse/iam/payment-role-arn` | Payment execution role ARN | Payment Lambda's `Role:` |

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

Each role grants only what its service does, plus CloudWatch Logs. Look up the
ARN from SSM above and reference it as your function's `Role:`.

| Role | DynamoDB access | Extra |
|---|---|---|
| Product | Read-only on Products (`GetItem`, `Query`, `Scan`, `BatchGetItem`) | — |
| Cart | Read/write on Carts | — |
| Order | Read/write on Orders | `lambda:InvokeFunction` on `anomalypulse-payment` |
| Payment | none | Logs only (stores no data) |

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
  matching the frozen field list in
  [AnomalyPulse/infra/shared/telemetry_schema.json](AnomalyPulse/infra/shared/telemetry_schema.json).
  Tier-1 (observable) and Tier-2 (ground-truth) fields never mix — this protects
  the ML from data leakage.

---

## Deploying the shared stack (Jake)

```bash
cd AnomalyPulse/infra/shared
sam build
sam deploy
```

Config (stack name, region, capabilities) is saved in `samconfig.toml`, so
routine deploys are just `sam deploy`.
