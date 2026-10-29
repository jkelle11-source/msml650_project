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

**Milestone 4 — Dataset** (complete, 2026-10-25; [Milestones/M04_Dataset.md](Milestones/M04_Dataset.md)).

| Piece | Owner | Status |
|---|---|---|
| **M4-1** Batch runner, experiment registry, S3 dataset layout, runner host | Jake | ✅ Done: `python -m simulator run-batch`, the `datasets/<name>/` layout (`tier1/` apart from `tier2/`), the `anomalypulse-runner` stack ([Docs/DATASET.md](Docs/DATASET.md)). Validated on the runner host: capacity check, an 11-run batch, and DB throttling from the runner (fixed: 16 clients, the surge ends on 10 s of served reads) |
| **M4-2** Generate training + validation set (1–700) | Josh | ✅ Done: 700 of 700 accepted (2026-10-20 to 25 UTC): training (1–560) in `v1-train-1` to `v1-train-4`, validation (561–700) in `v1-val`. Every class on target, 2 reruns, 1 interrupted run redone, none exhausted; every spot check passed; 4,438 training rows (3,553 train, 885 val). Queues, spot check and runbook: [Docs/TRAINING_SET.md](Docs/TRAINING_SET.md) (PR #75) |
| **M4-3** Bands, temporal split, diversity protocol | Linu | ✅ Done: `simulator/integrity.py` (interleaved bands, temporal split, diversity protocol, stress slice, cascade templates, the `v1` queue guard), validated on the runner ([Docs/EVALUATION_INTEGRITY.md](Docs/EVALUATION_INTEGRITY.md), PR #74) |
| **M4-4** Window labeling | Melisa | ✅ Done: `python -m labels`, one label or one logged exclusion per one-minute window ([Docs/LABELING.md](Docs/LABELING.md), PR #73) |
| **M4-5** Features + Feature/Label Boundary guard | Rachel | ✅ Done: `python -m features`, 106 Tier-1 columns per window, and the guard that fails the build on a Tier-2 name ([Docs/FEATURE_MATRIX.md](Docs/FEATURE_MATRIX.md), PR #76) |

Every M4 run is 10 minutes (3 baseline, 5 fault, 2 recovery) at 20 rps, from the runner host.
After each daily batch: the spot check, then labels, then features ([Docs/TRAINING_SET.md](Docs/TRAINING_SET.md), "Running it").

**The shared stack (`anomalypulse-shared`) is not deployed until M9-1 has generated `v1`'s test set.** The
test set must come from the same system as the training set, or its scores partly measure the change. `v1`'s
pin records only the service functions, the metrics extractor and the Products table, but a deploy can change
far more that shapes the telemetry without touching a service resource: `Globals` (they apply to every
function), environment variables, the shipper and its subscription filters, API Gateway, the other tables. The
stack was last updated 2026-10-16 03:17 UTC, before `v1` was pinned, so all 700 runs came from an undeployed
stack. M9-1 checks it still is:

```bash
aws cloudformation describe-stacks --stack-name anomalypulse-shared --query "Stacks[0].LastUpdatedTime" --output text --profile dev
# 2026-10-16T03:17:23.356000+00:00
```

**Everything M5–M8 add goes in its own stack** (the Diagnosis Lambda, the Incidents table, the event pipeline,
the dashboard, SageMaker roles), reading the shared stack's SSM parameters below. Read the telemetry from S3,
triggered by an EventBridge schedule or by a second subscription filter on a service's log group (each uses 1
of its 2); never S3 event notifications, which change the shared bucket. Nothing new writes under `raw/`,
`metrics/`, `experiments/` or `datasets/`, and nothing calls the app's API or its tables except the simulator.
No edits in the console either: they bypass CloudFormation and the timestamp.

**The simulator's run-time code is frozen too.** M9-1 runs from a commit newer than `0557121` (the one `v1`'s
training and validation ran from), because it adds the test set's queue generator. Before it starts, diff
`AnomalyPulse/simulator/` against `0557121`: only the queue generator, its queue files and docs may differ, not
the workload, the injectors or the batch's checks.

With slots 1–700 done, other experiments can run again, one at a time, but never during a batch. The runner
host stays stopped between batches and is terminated only after M9-1.

**Milestone 3 — Incident Simulator** (complete, merged to `main` and validated on the deployed
stack, 2026-10-15). M3 built the tool that deliberately
creates *known* failures against the live application, so M4 can generate a labeled
dataset with real ground truth. See
[Milestones/M03_Incident_Simulator.md](Milestones/M03_Incident_Simulator.md).

| Piece | Owner | Status |
|---|---|---|
| **M3-1** Simulator framework + CLI, experiment manifest, fault control plane, experiment lock | Jake | ✅ Done: every injector builds on it; runs fail as `FellBehind` if the machine sleeps (PRs #63, #66) |
| **M3-2** Workload generator + traffic-spike injector *(MVP incident)* | Josh | ✅ Done: steady baseline, a 3× spike, and a 10× spike at 20 rps validated in landed telemetry (PR #66) |
| **M3-3** DynamoDB throttling injector | Linu | ✅ Done: real throttling at 1.75×–8× on a new provisioned Products table, validated in landed telemetry |
| **M3-4** Payment-failure + Lambda-degradation injectors | Rachel | ✅ Done: Order's clean 502s and the `lambda_duration_ms` shift validated in landed telemetry (PRs #65, #68) |
| **M3-5** Cascading-failure orchestration | Melisa | ✅ Done: a four-stage cascade validated in landed telemetry; retry amplification needs a failing stage (`Docs/CASCADE.md`) |

M3-1 is the harness the injectors plug into: a local Python CLI
(`python -m simulator start-incident …`) that takes a lock, records an experiment
manifest (pure Tier-2 ground truth), starts and stops injectors on schedule, and
cleans up after itself. Each injector is one class in its own file behind a common
interface. Faults that a Lambda applies to itself are delivered through a DynamoDB
control plane rather than environment variables. The interface, the manifest and
the teardown story are in **[Docs/SIMULATOR_INTERFACE.md](Docs/SIMULATOR_INTERFACE.md)**;
injector owners start from **[Handoffs/M3-1_Handoff.md](Handoffs/M3-1_Handoff.md)**.

M3-2 added the baseline workload and the MVP incident. Every run sends open-loop
Poisson traffic, 20 requests per second in the plan's 60/15/10/10/5 endpoint mix
by default, and the `traffic-spike` injector multiplies that rate by a band-driven
factor for its window. `python -m simulator.telemetry_report <experiment_id>` shows
a run's effect in landed telemetry, phase by phase.

**Running an experiment** (details in the interface doc's "Running an experiment"):

- **Run from `AnomalyPulse/` under `caffeinate -is`:**
  `caffeinate -is python -m simulator start-incident …`. A sleeping laptop stops the
  run, and the runner then records it as `FAILED`.
- **Keep the 20 rps default.** M4 uses it for every run, test set included.
- **M4's batches run on an EC2 instance in us-east-2**, not a laptop (M4-1 in
  [Milestones/M04_Dataset.md](Milestones/M04_Dataset.md)).
- **Run one experiment at a time.** Don't send other traffic to the API during a run.
- **Leave the stack's configuration alone.** M4 keeps it as of PR #70 (the list is in
  [Milestones/M04_Dataset.md](Milestones/M04_Dataset.md)), and `anomalypulse-shared` isn't
  deployed at all until M9-1 has generated the test set (Status, above).

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
| [Docs/DATASET.md](Docs/DATASET.md) | The dataset (M4): batches and their checks, the experiment registry, the `datasets/` S3 layout, the runner host. |
| [Docs/EVALUATION_INTEGRITY.md](Docs/EVALUATION_INTEGRITY.md) | The leakage controls (M4-3): interleaved bands, the temporal split, the diversity protocol, cascade templates, how a queue is built, and the Evaluation Integrity Statement. |
| [Docs/TRAINING_SET.md](Docs/TRAINING_SET.md) | `v1`'s training and validation set (M4-2): the train/val cut, what the queues hold, the runbook for each daily batch, and its status. |
| [Docs/LABELING.md](Docs/LABELING.md) | Window labels (M4-4): the labeling decisions and the file format. |
| [Docs/FEATURE_MATRIX.md](Docs/FEATURE_MATRIX.md) | The feature matrix (M4-5): how to build it, and every column with its definition and tier (generated from the code). |
| [Docs/ML_INFERENCE.md](Docs/ML_INFERENCE.md) | Model packages and the Diagnosis Lambda (M5-1): the package format, promoting a model, calling the Lambda, cold starts, and the cost-per-inference hook for M9. |
| [Docs/Summary.md](Docs/Summary.md) | Short project summary draft. |
| [Milestones/](Milestones/) | The ten milestone specs (M1–M10), each with issues and Definition of Done. |
| [Handoffs/](Handoffs/) | Onboarding and handoff docs — [M1-1_Handoff.md](Handoffs/M1-1_Handoff.md) (stack + team conventions), [M2-1_Handoff.md](Handoffs/M2-1_Handoff.md) (how to emit telemetry), [M3-1_Handoff.md](Handoffs/M3-1_Handoff.md) (how to write an injector) and [M5-1_Handoff.md](Handoffs/M5-1_Handoff.md) (how to train, package and promote a model). |
| [AnomalyPulse/infra/shared/](AnomalyPulse/infra/shared/) | The SAM stack — `template.yaml` defines the four Lambdas, their API routes, DynamoDB tables, per-service IAM roles, the telemetry layer, the S3 landing bucket, the log groups + subscription filters, and the SSM parameters. Also holds `field_catalog.json` (the full field registry incl. Tier-2) and `shipper/` (the CloudWatch Logs → S3 shipper Lambda). |
| [AnomalyPulse/layers/telemetry/](AnomalyPulse/layers/telemetry/) | The `anomalypulse-telemetry` Lambda layer — the frozen `telemetry_schema.json` + `schema_validator.py`, plus `fault_config.py` (the reader each handler uses to pick up injected faults, M3), attached to every function and imported by service handlers and tests. |
| [AnomalyPulse/services/](AnomalyPulse/services/) | The service code: one folder per service (`product/`, `cart/`, `order/`, `payment/`), each with `handler.py` and `test_handler.py`. |
| [AnomalyPulse/simulator/](AnomalyPulse/simulator/) | The incident simulator (M3) — a local Python package, not deployed to Lambda. The runner and CLI, the experiment manifest schema, the experiment lock, the fault control-plane writer, the baseline workload (`workload.py`), the per-run telemetry report (`telemetry_report.py`), and `injectors/` (the interface, the registry, and one file per injector). M4's batch runner (`batch.py`), registry and layout (`dataset.py`), export (`export.py`), frozen-configuration check (`stack_config.py`), evaluation-integrity controls (`integrity.py`), queue generator (`build_queues.py`), telemetry spot check (`spot_check.py`) and queue files (`queues/`). |
| [AnomalyPulse/labels/](AnomalyPulse/labels/) | M4-4's window labeler (`python -m labels`): the labeling policy and the labels it writes under `tier2/labels/`. |
| [AnomalyPulse/features/](AnomalyPulse/features/) | M4-5's feature matrix (`python -m features`): the per-window Tier-1 features, the Feature/Label Boundary guard, and the join to the labels. |
| [AnomalyPulse/ml/](AnomalyPulse/ml/) | M5's training and inference: the SageMaker launcher (`python -m ml.launch smoke / train / tune / promote`), the job scripts (`job/`), the pinned library versions (`requirements-image.txt`), and the Diagnosis Lambda (`diagnosis/`: `handler.py` and its `Dockerfile`). |
| [AnomalyPulse/infra/ml/](AnomalyPulse/infra/ml/) | The `anomalypulse-ml` stack (M5–M8): the versioned model bucket, the training image's ECR copy, the SageMaker training and trainer roles, the Diagnosis Lambda, and its SSM parameters. |
| [AnomalyPulse/infra/runner/](AnomalyPulse/infra/runner/) | M4's runner host: its own CloudFormation stack (`anomalypulse-runner`: one EC2 instance, reached through Session Manager), `setup.sh` and the tmux helper. |

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
| Simulator (`anomalypulse-simulator`) | Experiments: `GetItem`, `PutItem`, `DeleteItem`. Fault-config: `Query`, `PutItem`, `DeleteItem` | `ssm:GetParameter(s)` on `/anomalypulse/*`; `s3:GetObject`/`PutObject` under `experiments/*`. Assumed by developers through the `sim` profile, and by the M4 runner host's role (`anomalypulse-runner`), not by a Lambda. Each injector's PR adds only the extra permissions it needs; M4-1 added `lambda:GetFunctionConfiguration`, reads of `raw/` and `metrics/`, and writes under `datasets/`. |

All four service roles also have `dynamodb:Query` on the fault-config table, and
nothing else on it, so a handler can read the faults aimed at it (M3).

### Lambda functions

Every function runs on `python3.13`, `arm64`, at **1,769 MB**, which is one full vCPU
(`Globals` in the template). Lambda gives CPU in proportion to memory: at the
128 MB default a function measured about 6% of a vCPU, so CPU-bound work ran ~16×
slower than its CPU time, and every request held its concurrency slot ~3× longer
than its telemetry showed. Product also builds its DynamoDB table while the
module loads, as Cart and Order do; built on the first request, it made each new
execution environment's first request take ~5.6 s.

The account allows **1,000 concurrent executions** (raised from 10 on 2026-10-14),
shared by every function, the shipper included.

Product, Cart and Order state their DynamoDB retries (legacy mode, 7 attempts)
rather than taking the runtime's default, which could wait 25 s, past their 10 s
timeout. The runtime itself is pinned to one version (`UpdateRuntimeOn: Manual` and a
`RuntimeVersionArn`), so its Python and boto3 change only when someone changes that ARN.
The telemetry layer builds the same bytes every time (pinned requirements, no
bytecode), so a deploy that doesn't change it doesn't republish it and update every
function.

The Products table is `anomalypulse-products-v2`, created provisioned at 200 RCU /
5 WCU for the DB-throttling injector. `GET /products` scans it, and a scan's cost
depends on the table's partitions, so the table must never switch billing mode or
change capacity during M4 ([Docs/DB_THROTTLING_INJECTOR.md](Docs/DB_THROTTLING_INJECTOR.md)).

**Don't change memory, capacity, retries or these handlers' timing until M9-1 has
generated `v1`'s test set.** They move every latency, duration and read-cost feature,
so runs before and after a change are not comparable.

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

**Deploy from `main`, or from a branch that is up to date with it.** A deploy
replaces the whole stack with that branch's template and code, so an older
branch rolls back whatever it lacks, such as the 1,769 MB memory setting. Merge
`main` into a branch before deploying from it.

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
