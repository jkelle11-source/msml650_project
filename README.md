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

**Milestone 3 — Incident Simulator** (in progress). Build the tool that deliberately
creates *known* failures against the live application, so M4 can generate a labeled
dataset with real ground truth. See
[Milestones/M03_Incident_Simulator.md](Milestones/M03_Incident_Simulator.md).

| Piece | Owner | Status |
|---|---|---|
| **M3-1** Simulator framework + CLI, experiment manifest, fault control plane, experiment lock | Jake | 🚧 Built and run on the deployed stack; awaiting the injector owners' sign-off |
| **M3-2** Workload generator + traffic-spike injector *(MVP incident)* | Josh | ⬜ Pending (blocked on M3-1) |
| **M3-3** DynamoDB throttling injector | Linu | ⬜ Pending (blocked on M3-1) |
| **M3-4** Payment-failure + Lambda-degradation injectors | Rachel | ⬜ Pending (blocked on M3-1) |
| **M3-5** Cascading-failure orchestration | Melisa | ⬜ Pending (blocked on M3-2…M3-4) |

M3-1 is the harness the injectors plug into: a local Python CLI
(`python -m simulator start-incident …`) that takes a lock, records an experiment
manifest (pure Tier-2 ground truth), starts and stops injectors on schedule, and
cleans up after itself. Each injector is one class in its own file behind a common
interface. Faults that a Lambda applies to itself are delivered through a DynamoDB
control plane rather than environment variables. The interface, the manifest and
the teardown story are in **[Docs/SIMULATOR_INTERFACE.md](Docs/SIMULATOR_INTERFACE.md)**;
injector owners start from **[Handoffs/M3-1_Handoff.md](Handoffs/M3-1_Handoff.md)**.

**Milestone 2 — Observability** (complete, merged to `main`). Every request emits
structured telemetry against an enforced schema, and that telemetry lands centrally
in S3. See [Milestones/M02_Observability.md](Milestones/M02_Observability.md).

| Piece | Owner | Status |
|---|---|---|
| **M2-1** Telemetry schema contract + validator, shared Lambda layer, S3 landing zone, CloudWatch→S3 shipper | Jake | ✅ Merged |
| **M2-2** Instrument Product Service | Josh | ✅ Merged |
| **M2-3** Instrument Cart Service | Linu | ✅ Merged |
| **M2-4** Instrument Order Service *(dependency fields)* | Melisa | ✅ Merged |
| **M2-5** Instrument Payment Service *(+ CloudWatch metrics extractor)* | Rachel | ✅ Merged |

M2-1 froze the contract every service emits against: a validating JSON Schema
(Tier-1 observable vs Tier-2 ground-truth fields, enforced by
`additionalProperties: false`), a shared validator packaged as the
`anomalypulse-telemetry` Lambda layer, an S3 raw landing zone
(`raw/service=<svc>/dt=<date>/…`), and the CloudWatch Logs → S3 shipper that
carries each printed record into the bucket. The full contract lives in
**[Docs/TELEMETRY_CONTRACT.md](Docs/TELEMETRY_CONTRACT.md)**.

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
| [Docs/SIMULATOR_INTERFACE.md](Docs/SIMULATOR_INTERFACE.md) | The incident simulator reference (M3): injector interface, experiment manifest, how to run an experiment, teardown. |
| [Docs/Summary.md](Docs/Summary.md) | Short project summary draft. |
| [Milestones/](Milestones/) | The ten milestone specs (M1–M10), each with issues and Definition of Done. |
| [Handoffs/](Handoffs/) | Onboarding and handoff docs — [M1-1_Handoff.md](Handoffs/M1-1_Handoff.md) (stack + team conventions), [M2-1_Handoff.md](Handoffs/M2-1_Handoff.md) (how to emit telemetry) and [M3-1_Handoff.md](Handoffs/M3-1_Handoff.md) (how to write an injector). |
| [AnomalyPulse/infra/shared/](AnomalyPulse/infra/shared/) | The SAM stack — `template.yaml` defines the four Lambdas, their API routes, DynamoDB tables, per-service IAM roles, the telemetry layer, the S3 landing bucket, the log groups + subscription filters, and the SSM parameters. Also holds `field_catalog.json` (the full field registry incl. Tier-2) and `shipper/` (the CloudWatch Logs → S3 shipper Lambda). |
| [AnomalyPulse/layers/telemetry/](AnomalyPulse/layers/telemetry/) | The `anomalypulse-telemetry` Lambda layer — the frozen `telemetry_schema.json` + `schema_validator.py`, plus `fault_config.py` (the reader each handler uses to pick up injected faults, M3), attached to every function and imported by service handlers and tests. |
| [AnomalyPulse/services/](AnomalyPulse/services/) | The service code: one folder per service (`product/`, `cart/`, `order/`, `payment/`), each with `handler.py` and `test_handler.py`. |
| [AnomalyPulse/simulator/](AnomalyPulse/simulator/) | The incident simulator (M3) — a local Python package, not deployed to Lambda. The runner and CLI, the experiment manifest schema, the experiment lock, the fault control-plane writer, and `injectors/` (the interface, the registry, and one file per injector). |

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
| `/anomalypulse/telemetry/bucket/name` | Telemetry S3 bucket name | Reading raw telemetry landed by the shipper; where experiment manifests are written |
| `/anomalypulse/tables/experiments/name` | Experiments table name | The simulator's experiment lock |
| `/anomalypulse/tables/fault-config/name` | Fault-config table name | The simulator's fault control plane |
| `/anomalypulse/simulator/role-arn` | Simulator role ARN | The `role_arn` of your `sim` AWS profile ([M3-1 handoff](Handoffs/M3-1_Handoff.md) §4) |

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
| `anomalypulse-experiments` | `pk` (S) | — | Simulator only. Holds the one experiment lock item (`pk = "LOCK"`). |
| `anomalypulse-fault-config` | `service` (S) | `fault_id` (S) | One item per injected fault, written by the simulator and queried by each service for its own partition. TTL on `expires_at`. |

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
| Simulator (`anomalypulse-simulator`) | Experiments: `GetItem`, `PutItem`, `DeleteItem`. Fault-config: `Query`, `PutItem`, `DeleteItem` | `ssm:GetParameter(s)` on `/anomalypulse/*`; `s3:GetObject`/`PutObject` under `experiments/*`. Assumed by developers through the `sim` profile, not by a Lambda. Each injector's PR adds only the extra permissions it needs. |

All four service roles also have `dynamodb:Query` on the fault-config table, and
nothing else on it, so a handler can read the faults aimed at it (M3).

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

Experiment manifests written by the simulator live under `experiments/<experiment_id>/`
in the same bucket and never expire: they are the ground truth.

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
- **Incident injectors** — each injector is one `Injector` subclass, registered by
  name, in its own file under `AnomalyPulse/simulator/injectors/`. Magnitudes come
  from the band passed in, never from the code, and everything an injector resolves
  or measures goes in the experiment manifest only (Tier-2), never into telemetry.
  One experiment runs at a time on the shared stack. Full contract:
  [Docs/SIMULATOR_INTERFACE.md](Docs/SIMULATOR_INTERFACE.md).

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

The incident simulator is a local package with its own runtime and test
dependencies. No test reaches AWS or needs credentials:

```bash
cd AnomalyPulse/simulator
pip install -r requirements-dev.txt
python -m pytest
```
