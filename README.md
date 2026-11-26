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

**The shared stack (`anomalypulse-shared`) was not deployed while `v1` was generated**, from 2026-10-20 (slots
1–700) to 2026-11-16 (M9-1's test set and stress slice, 701–1070). The test set had to come from the same system
as the training set, or its scores would partly measure the change. `v1`'s pin records only the service
functions, the metrics extractor and the Products table, but a deploy can change far more that shapes the
telemetry without touching a service resource: `Globals` (they apply to every function), environment variables,
the shipper and its subscription filters, API Gateway, the other tables. The stack was last updated 2026-10-16
03:17 UTC, before `v1` was pinned, and still read that after M9-1's last batch:

```bash
aws cloudformation describe-stacks --stack-name anomalypulse-shared --query "Stacks[0].LastUpdatedTime" --output text --profile dev
# 2026-10-16T03:17:23.356000+00:00
```

**With `v1` complete, the shared stack can be deployed again.** A deploy can't change `v1`, whose 1,070 slots are
all filled. Anything that moves the telemetry does move live traffic away from what the models learned, so a live
measurement after such a deploy (M9-4, M9-6, the M10 demo) measures the changed system, not `v1`'s.

**Everything M5–M8 add goes in its own stack** (the Diagnosis Lambda, the Incidents table, the event pipeline,
the dashboard, SageMaker roles), reading the shared stack's SSM parameters below. Read the telemetry from S3,
triggered by an EventBridge schedule or by a second subscription filter on a service's log group (each uses 1
of its 2); never S3 event notifications, which change the shared bucket. Nothing new writes under `raw/`,
`metrics/`, `experiments/` or `datasets/`, and nothing calls the app's API or its tables except the simulator.
No edits in the console either: they bypass CloudFormation and the timestamp.

**The simulator's run-time code was frozen too.** M9-1 ran from `80d5932`, newer than `0557121` (the one `v1`'s
training and validation ran from), because it adds the test set's queue generator and, since M9-1's validation,
a start wait in `batch.py` for 2-minute throttling runs (when such a run starts, not what it sends or measures).
Its diff of `AnomalyPulse/simulator/` against `0557121` showed only those two, their tests, the queue files and
docs, not the workload, the injectors or the batch's checks.

With `v1` done, other experiments run one at a time. The runner host sends their traffic too, never a laptop,
stays stopped between sessions, and is terminated only after M10 ([Docs/DATASET.md](Docs/DATASET.md), "The
runner host").

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

- **Send every run's traffic from the runner host, never a laptop:**
  `bash AnomalyPulse/infra/runner/drive.sh run <session> start-incident …` from a laptop
  starts it there. With laptop traffic the Isolation Forest flagged about half of the
  healthy live minutes ([Docs/DETECTION.md](Docs/DETECTION.md), "Where live traffic comes
  from"); the runner also never sleeps, and its credentials renew themselves.
- **Keep the 20 rps default.** M4 uses it for every run, test set included.
- **M4's batches run on an EC2 instance in us-east-2**, not a laptop (M4-1 in
  [Milestones/M04_Dataset.md](Milestones/M04_Dataset.md)).
- **Run one experiment at a time.** Don't send other traffic to the API during a run.
- **The stack's configuration is `v1`'s**, as of PR #70 (the list is in
  [Milestones/M04_Dataset.md](Milestones/M04_Dataset.md)). `anomalypulse-shared` can be
  deployed again now that `v1` is complete, but a change moves live telemetry away from
  `v1`'s (Status, above).

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
| [Docs/TEST_SET.md](Docs/TEST_SET.md) | `v1`'s test set and stress slice (M9-1): what the queues hold, the checks before the first test batch (the training-job guard, the system pin, the validation queue), the runbook and its status. |
| [Docs/COST_AND_PERFORMANCE.md](Docs/COST_AND_PERFORMANCE.md) | Cloud cost and operational performance (M9-4), measured on the test set's batches: cost per inference, per experiment and per incident, what the system costs idle, monthly costs by request rate (the store's and AnomalyPulse's), SageMaker Serverless Inference against the Lambda on paper, and the live pipeline's throughput, latency, Lambda durations and queue backlog. |
| [Docs/LABELING.md](Docs/LABELING.md) | Window labels (M4-4): the labeling decisions and the file format. |
| [Docs/FEATURE_MATRIX.md](Docs/FEATURE_MATRIX.md) | The feature matrix (M4-5): how to build it, and every column with its definition and tier (generated from the code). |
| [Docs/ML_INFERENCE.md](Docs/ML_INFERENCE.md) | Model packages and the Diagnosis Lambda (M5-1): the package format, promoting a model, calling the Lambda, cold starts, and the cost-per-inference hook for M9. |
| [Docs/DETECTION.md](Docs/DETECTION.md) | Anomaly detection (M5-2): the Isolation Forest and the supervised binary detector, the forest's cut-off, healthy vs fault-start false alarms, and the validation results. |
| [Docs/M5-3_REPORT.md](Docs/M5-3_REPORT.md) | Incident classification (M5-3): Model 0 (the threshold baseline), logistic regression, XGBoost and Random Forest on validation, the cascade margin and time to recognize a cascade, the wrong windows, and the selected model. |
| [Docs/DIAGNOSIS_CONTRACT.md](Docs/DIAGNOSIS_CONTRACT.md) | The diagnosis contract (M6-1): what Bedrock is given (the classified incident as structured evidence, how `affected_services` is decided) and what it gives back, the wording rules, the severity rubric, the self-check, and the model choice. |
| [Docs/DASHBOARD_CONTRACT.md](Docs/DASHBOARD_CONTRACT.md) | The dashboard data contract (M7-2): the JSON object the SRE console reads, what every field means, the mock scenarios, where each value comes from in M8, and M8-5's live run (one incident of each type on the console, with screenshots in `Docs/images/m8-5/`). |
| [Docs/PIPELINE.md](Docs/PIPELINE.md) | The live telemetry pipeline (M8-1): the path from a service's log line to a one-minute window, what it writes (health, windows, the WindowReady event, the Incidents table's health item), when each window is ready, what late telemetry does, its alarms and load tests, the IAM at each hop, and what live windows do differently from training's, measured on validation. |
| [Docs/LIVE_DIAGNOSIS.md](Docs/LIVE_DIAGNOSIS.md) | Detect + classify on the live path (M8-2): each WindowReady window through the Isolation Forest, then the classifier, in the Diagnosis Lambda; the reply, the Tier-1 guard on the live path, the detection-latency metrics, the open/close rule proposed for M8-3 (`incident.py`) with its results on validation, deploying, and the live Definition of Done checks. |
| [Docs/INCIDENT_STORE.md](Docs/INCIDENT_STORE.md) | The incident store (M8-3): the path of each diagnosed minute through `anomalypulse-incidents` (grouping into incidents, Bedrock's input, when it is called, the stored record's layout and what the dashboard reads), the baseline and metrics decisions, round-trip latency, its IAM and failure queue, deploying, and what is not verified yet. |
| [Docs/INCIDENT_FEED.md](Docs/INCIDENT_FEED.md) | The incident feed (M8-4): how a stored diagnosis maps to the dashboard contract, which flagged diagnoses are held back, and the check that each one arrives intact, worded as an observed sequence and traced to its input metrics. |
| [Docs/GENAI_EVALUATION.md](Docs/GENAI_EVALUATION.md) | GenAI quality evaluation (M9-5): evidence fidelity, hallucination and completeness over the test set's stored diagnoses, why root-cause correctness isn't the headline, the stack errors in actions (capacity and X-Ray), and the blind with-and-without-runbooks comparison. |
| [Docs/M9-2_REPORT.md](Docs/M9-2_REPORT.md) | Detection and classification on the test set (M9-2): the deployed rule, the classifier, per-band results, the extreme slice, confounders, and why the shifted traffic mix breaks the Isolation Forest's cut-off and dependency-failure recall. |
| [Docs/M9-3_REPORT.md](Docs/M9-3_REPORT.md) | The threshold baseline (Model 0) beside every model on the test set (M9-3): paired margins, healthy-traffic false alarms, the cascade margin, every classifier M5-3 trained, the train/test gap as a separate signal, and the finalized Evaluation Integrity Statement with its evidence. |
| [Docs/M9-6_REPORT.md](Docs/M9-6_REPORT.md) | Latency on the live path and the report figures (M9-6): the fault-to-diagnosis chain split into its steps (the window, CloudWatch's 2-minute settle, the window builder's schedule, diagnosis), how incidents are matched to runs and which runs are left out, the comparison with M9-2's offline latency, and the six figures in `Docs/images/m9-6/`. |
| [Docs/Summary.md](Docs/Summary.md) | Short project summary draft. |
| [Milestones/](Milestones/) | The ten milestone specs (M1–M10), each with issues and Definition of Done. |
| [Handoffs/](Handoffs/) | Onboarding and handoff docs — [M1-1_Handoff.md](Handoffs/M1-1_Handoff.md) (stack + team conventions), [M2-1_Handoff.md](Handoffs/M2-1_Handoff.md) (how to emit telemetry), [M3-1_Handoff.md](Handoffs/M3-1_Handoff.md) (how to write an injector), [M5-1_Handoff.md](Handoffs/M5-1_Handoff.md) (how to train, package and promote a model), [M6-1_Handoff.md](Handoffs/M6-1_Handoff.md) (the diagnosis pipeline after review, and how to build M6-2's runbooks), [M8-2_Live_Checks.md](Handoffs/M8-2_Live_Checks.md) (M8-2's last live checks, one run per injector, with [the check script](Handoffs/M8-2_Live_Checks_Script.md)) and [M8-3_Handoff.md](Handoffs/M8-3_Handoff.md) (building M8-3 on M8-2's live diagnoses: the events, `incident.py`, Bedrock's input, the incident record, and the decisions left). |
| [AnomalyPulse/infra/shared/](AnomalyPulse/infra/shared/) | The SAM stack — `template.yaml` defines the four Lambdas, their API routes, DynamoDB tables, per-service IAM roles, the telemetry layer, the S3 landing bucket, the log groups + subscription filters, and the SSM parameters. Also holds `field_catalog.json` (the full field registry incl. Tier-2) and `shipper/` (the CloudWatch Logs → S3 shipper Lambda). |
| [AnomalyPulse/layers/telemetry/](AnomalyPulse/layers/telemetry/) | The `anomalypulse-telemetry` Lambda layer — the frozen `telemetry_schema.json` + `schema_validator.py`, plus `fault_config.py` (the reader each handler uses to pick up injected faults, M3), attached to every function and imported by service handlers and tests. |
| [AnomalyPulse/services/](AnomalyPulse/services/) | The service code: one folder per service (`product/`, `cart/`, `order/`, `payment/`), each with `handler.py` and `test_handler.py`. |
| [AnomalyPulse/simulator/](AnomalyPulse/simulator/) | The incident simulator (M3) — a local Python package, not deployed to Lambda. The runner and CLI, the experiment manifest schema, the experiment lock, the fault control-plane writer, the baseline workload (`workload.py`), the per-run telemetry report (`telemetry_report.py`), and `injectors/` (the interface, the registry, and one file per injector). M4's batch runner (`batch.py`), registry and layout (`dataset.py`), export (`export.py`), frozen-configuration check (`stack_config.py`), evaluation-integrity controls (`integrity.py`), queue generator (`build_queues.py`: training and validation, and since M9-1 the test set and stress slice), telemetry spot check (`spot_check.py`) and queue files (`queues/`). |
| [AnomalyPulse/labels/](AnomalyPulse/labels/) | M4-4's window labeler (`python -m labels`): the labeling policy and the labels it writes under `tier2/labels/`. |
| [AnomalyPulse/features/](AnomalyPulse/features/) | M4-5's feature matrix (`python -m features`): the per-window Tier-1 features, the Feature/Label Boundary guard, and the join to the labels. |
| [AnomalyPulse/ml/](AnomalyPulse/ml/) | M5's training and inference: the SageMaker launcher (`python -m ml.launch smoke / train / tune / promote`), the job scripts (`job/`), the pinned library versions (`requirements-image.txt`), and the Diagnosis Lambda (`diagnosis/`: `handler.py`, its `Dockerfile`, `forbidden_names.json`, the Tier-1 guard's list copied by `write_forbidden_names.py`, and `incident.py`, M8-2's open/close rule for M8-3). |
| [AnomalyPulse/infra/ml/](AnomalyPulse/infra/ml/) | The `anomalypulse-ml` stack (M5–M8): the versioned model bucket, the training image's ECR copy, the SageMaker training and trainer roles, the Diagnosis Lambda, and its SSM parameters; from M8-2, the rule that sends it each WindowReady event from the pipeline bus, its failure queue `anomalypulse-diagnosis-failed` and that queue's alarm. |
| [AnomalyPulse/genai/](AnomalyPulse/genai/) | M6-1's explanation layer: `contract.py` (the input contract and the severity rubric), `prompt.py`, `explain.py` (the Bedrock call), `self_check.py` (the smoke-level fidelity check), `affected.py` (which services an incident affects, for M8-3), `handler.py` (the `anomalypulse-explain` Lambda), `runbooks.py` and `runbooks/` (M6-2: one runbook per incident type for `recommended_actions`, behind `ANOMALYPULSE_RUNBOOKS`, on in the template since M9-5), `samples/` (one input per incident type and each model's saved diagnoses), `run_samples.py` (`--runbooks on|off`), `m9_5/` (M9-5's evaluation: `fetch.py`, `score.py`, `compare.py`, `sheet.py`; [Docs/GENAI_EVALUATION.md](Docs/GENAI_EVALUATION.md)) and the tests. |
| [AnomalyPulse/incidents/](AnomalyPulse/incidents/) | M8-3's incident store: `builder.py` (Bedrock's input), `record.py` (the stored record), `core.py` (one diagnosed minute in, records out), `handler.py` (the Lambda's AWS calls), the `Makefile` that bundles it with `genai/` and `incident.py`, `live_check.py` (the read-only check to run after each live run), `feed.py` (M8-4: checks that a stored diagnosis reaches the dashboard intact, worded as an observed sequence and traced to its input; [Docs/INCIDENT_FEED.md](Docs/INCIDENT_FEED.md)), `replay/` (validation's runs replayed as the live stream, offline: `download.sh`, `replay.py`, `analyze.py`), and the tests (`python3 -m pytest incidents`). Its stack is `infra/incidents/`. |
| [AnomalyPulse/infra/genai/](AnomalyPulse/infra/genai/) | The `anomalypulse-genai` stack (M6-1): the explanation Lambda, its role (`bedrock:InvokeModel` on Claude models only) and log group, and the function name's SSM parameter. |
| [AnomalyPulse/infra/incidents/](AnomalyPulse/infra/incidents/) | The `anomalypulse-incidents` stack (M8-3): the rule that sends it each diagnosed minute from the pipeline bus, the Lambda (reserved concurrency 1, 150 s timeout) and its least-privilege role, the failure queue `anomalypulse-incidents-failed` and its alarm. |
| [AnomalyPulse/dashboard/](AnomalyPulse/dashboard/) | M7-2's dashboard read API: `builder.py` (every rule of the dashboard contract), `mock_source.py` (the contract's six mock scenarios), `live_source.py` (M8-5: the health item, the open incident through `incidents.record.to_incident`, and its run's manifest), `handler.py` (the Lambda: picks the source, reads the clock), the `Makefile` that bundles it with `incidents/record.py` and its `genai` modules for the live source, and their tests. |
| [AnomalyPulse/console/](AnomalyPulse/console/) | M7-1's SRE console: static HTML, CSS and JavaScript modules (no build step) that render the dashboard contract, `mock/` (snapshots of the read API's six scenarios, the demo's offline fallback) and its tests. Run from the presenting laptop; see its [README](AnomalyPulse/console/README.md). Kept out of `dashboard/`, the read API's folder (its Lambda bundle is built by `dashboard/Makefile`). |
| [AnomalyPulse/infra/dashboard/](AnomalyPulse/infra/dashboard/) | The `anomalypulse-dashboard` stack (M7-2): the read API's Lambda, its HTTP API (`GET /dashboard`, CORS), role and log group, and the URL's SSM parameter. |
| [AnomalyPulse/pipeline/](AnomalyPulse/pipeline/) | M8-1's live telemetry pipeline, one folder per Lambda, each with `handler.py` and its tests: `forwarder/` (CloudWatch Logs → the pipeline bus), `aggregator/` (the telemetry queue → `pipeline/records/`) and `window_builder/` (every minute: health, the 106-column windows, WindowReady; its `Makefile` packages `features/` and the parts of `simulator/` the guard needs). See [Docs/PIPELINE.md](Docs/PIPELINE.md). |
| [AnomalyPulse/infra/pipeline/](AnomalyPulse/infra/pipeline/) | The `anomalypulse-pipeline` stack (M8-1): the second subscription filter on each service log group, the three Lambdas, the bus and its rule, the telemetry, dead-letter and undelivered queues, the Incidents table and its SSM parameter, and the alarms. |
| [AnomalyPulse/infra/runner/](AnomalyPulse/infra/runner/) | M4's runner host, which sends every batch's and every live run's traffic: its own CloudFormation stack (`anomalypulse-runner`: one EC2 instance, reached through Session Manager), `setup.sh`, the tmux helper, `drive.sh`, which drives it from a laptop, and `idle-stop.yaml` (stack `anomalypulse-runner-idle-stop`: a CloudWatch alarm that stops the runner after an hour without traffic). Never update `anomalypulse-runner`: an update re-resolves its AMI and would replace the instance ([Docs/DATASET.md](Docs/DATASET.md), "The runner host"). |
| [AnomalyPulse/cost/](AnomalyPulse/cost/) | M9-4's cost work. Measured: `collect_m9.py` gathers the test set's usage, bill and operational metrics from AWS (read-only) into `inputs/m9/`, and `measured.py` turns them into `results/` (`projections.json` is M9-6's cost curve); [Docs/COST_AND_PERFORMANCE.md](Docs/COST_AND_PERFORMANCE.md). Estimated before M9-1: `estimate.py` prices the plan run by run from October's usage, from `collect.py`'s `inputs/` ([README](AnomalyPulse/cost/README.md)). Tests: `python3 -m pytest cost`. |
| [AnomalyPulse/report/](AnomalyPulse/report/) | M9-6's report: `fetch.py` downloads the test set's and stress slice's manifests read-only into `data/`, `latency.py` builds the live latency chain from M9-5's exported incidents into `results/latency_m9_6.json`, and `make_figures.py` draws the six report figures into `Docs/images/m9-6/` from the result files; `requirements.txt` pins matplotlib. Tests: `python3 -m pytest report`. See [Docs/M9-6_REPORT.md](Docs/M9-6_REPORT.md). |

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
| `/anomalypulse/dashboard/url` | The dashboard read API's URL (`https://…/dashboard`), published by the `anomalypulse-dashboard` stack | The SRE console (M7-1) and M8 |
| `/anomalypulse/genai/explain-function` | The explanation Lambda's name (`anomalypulse-explain`), published by the `anomalypulse-genai` stack | M8-3, which invokes it with each classified incident |
| `/anomalypulse/tables/incidents/name` | The Incidents table's name (`anomalypulse-incidents`), published by the `anomalypulse-pipeline` stack | M8-2 and M8-3, which write incidents to it, and M8-5, which reads them and the health item |

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
| `anomalypulse-incidents` | `pk` (S) | `sk` (S) | The `anomalypulse-pipeline` stack's, kept if the stack is deleted. The window builder's health item (`pk = "HEALTH"`, `sk = "LATEST"`), and M8-3's incident records and running state, laid out in [Docs/INCIDENT_STORE.md](Docs/INCIDENT_STORE.md) ([Docs/PIPELINE.md](Docs/PIPELINE.md) describes the health item). |

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

**Memory, capacity, retries and these handlers' timing are `v1`'s.** They can change
now that `v1` is complete (2026-11-16), but they move every latency, duration and
read-cost feature, so runs after a change are not comparable with `v1`'s or with what
the models learned from it.

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

The live pipeline (M8-1) writes under `pipeline/` (each minute's records, health and
feature window) and reads `metrics/` ([Docs/PIPELINE.md](Docs/PIPELINE.md), "What it
writes"). Nothing under `pipeline/` expires: the bucket's lifecycle rules, in the
shared stack, cover `raw/` and `metrics/` only.

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
- **Dashboard read API** — `GET /dashboard`, on its own HTTP API in the
  `anomalypulse-dashboard` stack, returns the dashboard object itself, not the
  services' `data`/`error` envelope; its errors are `{ "error": "…" }`. The SRE
  console renders only from it. Full contract:
  [Docs/DASHBOARD_CONTRACT.md](Docs/DASHBOARD_CONTRACT.md).
- **Diagnosis (Bedrock)** — M8-3 invokes `anomalypulse-explain` directly with one
  classified incident as structured evidence (Tier-1 only: ground-truth fields are
  refused before Bedrock), and stores the reply, whose `diagnosis` is the dashboard
  contract's `diagnosis`. Any ordering of symptoms is worded as an observed sequence,
  never as causation. Full contract:
  [Docs/DIAGNOSIS_CONTRACT.md](Docs/DIAGNOSIS_CONTRACT.md).

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

### The dashboard stack

The dashboard read API (M7-2) is its own stack, `anomalypulse-dashboard`, so
deploying it never touches the shared stack:

```bash
cd AnomalyPulse/infra/dashboard
sam build
AWS_PROFILE=admin sam deploy
```

`admin`, because the stack creates an IAM role. `dashboard/Makefile` builds the
function: `AnomalyPulse/dashboard/`, with the incident store's `record.py` and the
modules it imports (boto3 comes with the Lambda runtime). Its URL is the stack's
`DashboardUrl` output and `/anomalypulse/dashboard/url`. With `DashboardSource=live`,
the stack's default since M8-5, it serves live data: the newest minute's health and
the open incident from the Incidents table, and that run's manifest from the
telemetry bucket. `sam deploy --parameter-overrides DashboardSource=mock` serves the
contract's mock scenarios instead, and `DashboardSource=live` switches it back.

### The explanation stack

The explanation Lambda (M6-1) is its own stack, `anomalypulse-genai`, which reads
nothing from the shared stack:

```bash
cd AnomalyPulse/infra/genai
sam build
AWS_PROFILE=admin sam deploy
```

`admin`, because the stack creates an IAM role. `sam build` runs the folder's
`Makefile`, which bundles `anthropic[bedrock]` as arm64 Linux wheels (pinned to 1.x:
2.x sends an API version Bedrock accepts only for Claude 4.7 and later). The
function calls Claude Sonnet 4.6 through a global cross-region inference profile;
the model, region and effort are environment variables in the template. Its name is
`/anomalypulse/genai/explain-function`. Check it with one sample:

```bash
aws lambda invoke --function-name anomalypulse-explain --cli-binary-format raw-in-base64-out \
  --payload file://AnomalyPulse/genai/samples/dependency_failure.json reply.json --profile dev
```

### The pipeline stack

The live telemetry pipeline (M8-1) is its own stack, `anomalypulse-pipeline`. It reads
the bucket's name from SSM and adds a second subscription filter to each service log
group, but never updates the shared stack:

```bash
cd AnomalyPulse/infra/pipeline
sam build
AWS_PROFILE=admin sam deploy
```

`admin`, because the stack creates IAM roles. `sam build` builds in the source folders
(`build_in_source` in `samconfig.toml`), because the window builder's `Makefile`
packages the repository's `features/` and the parts of `simulator/` the Feature/Label
Boundary guard needs, with `jsonschema` as arm64 Linux wheels. The Incidents table's
name is `/anomalypulse/tables/incidents/name`. After a deploy that creates the window
builder's schedule rule, such as a new stack, check which second its runs start at
([Docs/PIPELINE.md](Docs/PIPELINE.md), "When a minute's window is ready").

### The incident store stack

The incident store (M8-3) is its own stack, `anomalypulse-incidents`. It reads the pipeline bus, so
`anomalypulse-ml`, `anomalypulse-pipeline` and `anomalypulse-genai` are deployed first:

```bash
cd AnomalyPulse/infra/incidents
sam build
AWS_PROFILE=admin sam deploy
```

`admin`, because the stack creates an IAM role. `sam build` builds in the source folder (`build_in_source`
in `samconfig.toml`), because the `Makefile` in `incidents/` bundles `genai/` and `ml/diagnosis/incident.py`
from the repository. How it works, what it stores and what is not verified yet:
[Docs/INCIDENT_STORE.md](Docs/INCIDENT_STORE.md).

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

The dashboard read API's tests need only `pytest`, and reach no AWS. One of them
checks the `incident` mock scenario against the example in
`Docs/DASHBOARD_CONTRACT.md`, so a change to either shows up there:

```bash
cd AnomalyPulse/dashboard
python -m pytest
```

The explanation layer's tests run from `AnomalyPulse/` (the package is `genai`)
and reach no AWS: Bedrock is faked. `run_samples` does call Bedrock, five times,
for about 15¢:

```bash
cd AnomalyPulse
pip install -r genai/requirements.txt pytest
python -m pytest -q genai
python -m genai.run_samples --profile dev     # the five samples through Bedrock
```

The SRE console's tests need only Node 20 or later, and reach no network:

```bash
cd AnomalyPulse/console
npm test
```

The incident simulator is a local package with its own runtime and test
dependencies. No test reaches AWS or needs credentials:

```bash
cd AnomalyPulse/simulator
pip install -r requirements-dev.txt
python -m pytest
```

The incident store's tests (M8-3) reach no AWS: the table, S3, the experiment lock and the explanation
Lambda are faked, and one test reads a stored record through the dashboard's `build_dashboard`. They run
from `AnomalyPulse/` (the package is `incidents`):

```bash
cd AnomalyPulse
python3 -m pytest incidents -q
cfn-lint infra/incidents/template.yaml   # optional: pip install cfn-lint
```

The pipeline's three Lambdas have their own tests, which reach no AWS: S3, the bus and
the Incidents table are faked or stubbed. Run each folder on its own. All three test
files are named `test_handler.py`, so one pytest run over `pipeline/` stops on the
name clash before running any:

```bash
cd AnomalyPulse/pipeline/<lambda>   # forwarder | aggregator | window_builder
pip install -r requirements-dev.txt
python -m pytest
```

---

## Future Directions

M9-6 measured how long the live path takes from the start of a fault to its stored diagnosis: a median of 249.5 s
on the test set ([Docs/M9-6_REPORT.md](Docs/M9-6_REPORT.md)). This section explains where that time goes, what
could shorten it, and in what order to try. None of it has been built or tested yet. Figures marked as estimates
are arithmetic on M9-6's measurements, not measurements.

### Where the time goes

Take a fault whose first flagged minute ends at 12:01:00. The median run (284 timed test runs, runbooks off) goes
like this:

| Step | Median | What sets it |
|---|---|---|
| The fault's start to the end of its first flagged minute | 52 s | The one-minute window (plan §16) |
| That minute's end to the metrics extractor reading it, at 12:03:29 | 149 s | `METRICS_LAG_MINUTES = 2` (120 s), plus the extractor's schedule, which fires at :29 past each minute (29 s) |
| The snapshot to the window builder's next run, at 12:03:45 | about 15 s | The window builder's schedule, which fires at :45 |
| Building and classifying the full window | about 5 s | About 4 s to build, under 1 s to classify |
| Diagnosis: the Bedrock call and storing the result | 28 s | The explain Lambda's Bedrock call |

The middle three rows make up the incident record's `window_end_to_diagnosis_s`: a median of 169 s, with almost no
spread. The two schedule seconds come from the Lambdas' logs, and were the same during M9-1 (2026-11-14) as on
2026-11-26. Each second is drawn when its schedule rule is created, so a deploy that replaces the rule moves it
([Docs/PIPELINE.md](Docs/PIPELINE.md), "When a minute's window is ready").

**About 150 s of the 250 s is the pipeline waiting for CloudWatch.** The models read a full window of 106
columns, and 19 of them come from CloudWatch. A window with any empty value is refused, so nothing is classified
until the extractor has read that minute from CloudWatch.

### Option 1: give CloudWatch less time to settle

Setting `METRICS_LAG_MINUTES` to 1 (in `AnomalyPulse/infra/shared/template.yaml`) would make the extractor read a
minute 89 s after it ends instead of 149 s. That saves about 60 s (estimate: about 190 s end to end).

**The risk is silent.** For each minute, the extractor asks CloudWatch for a `Sum`: each table's consumed capacity
and throttle events, and each function's Lambda throttles. If CloudWatch has not finished publishing the minute,
it returns what it has so far, so the result is a smaller number, not an empty one. The window builder refuses only
empty values. The classifier would therefore see a quieter minute than any it was trained on, and nothing would
flag it. No one has measured how soon these metrics are complete; 2 minutes was a cautious choice. Moving the
extractor's schedule second earlier would also give CloudWatch less time, with the same risk.

Both of these checks are read-only and need no extra traffic:

- **Is the current 149 s enough?** Compare the extractor's snapshots in S3 with CloudWatch's final values for the
  same minutes. CloudWatch keeps 1-minute data for 15 days, so this only works for runs from the last two weeks.
- **Would 89 s be enough?** During live runs that happen anyway, such as a demo rehearsal, poll CloudWatch for each
  minute at 60, 75, 90 s and so on after it ends. Compare each answer with the value 10 minutes later.

### Option 2: build the full window as soon as the snapshot lands

This saves about 15 s and does not change the data (estimate: about 235 s end to end). Today the snapshot is
written at about :29.5 and then waits for the window builder's run at :45. A build started by the snapshot's
arrival would remove that wait.

It needs a new trigger. An S3 event notification on the shared bucket is ruled out (Status, above). Instead, the
extractor could put an event on the pipeline bus after it writes, which means a change to the shared stack. The
builder's own once-a-minute run has to stay, at a second between about :20 and :50, for the dashboard's health
deadlines.

### Option 3: detect from the request records alone

This takes CloudWatch out of detection altogether. Of the 106 feature columns, only 19 come from CloudWatch:

- each table's consumed read and write capacity;
- each table's read, write and request throttles;
- each function's Lambda throttles.

The other 87 come from the services' own request records: latency percentiles, error rates, request rates,
DynamoDB call latency, cold starts, and their changes and rolling statistics
([Docs/FEATURE_MATRIX.md](Docs/FEATURE_MATRIX.md)). The records for a minute are in about 45 s after it ends,
which is when the window builder already writes the dashboard's health from them. A classifier on those 87 columns
could name a fault about 2 minutes sooner (estimate: about 2 minutes end to end, made of the 52 s window, about
46 s to build and classify, and 28 s of diagnosis).

**Whether accuracy holds without the 19 columns is unknown.** What the evidence says so far:

- **Several CloudWatch columns never changed.** In M4-5's check on 123 validation rows, the Lambda throttles and
  Cart's and Order's table throttles stayed fixed.
- **The records already separate DB throttling.** The records' `product_db_latency_ms_p95` separated it from Lambda
  degradation in the same check (median 395 ms against 6 ms).
- **DB throttling is still the type to watch.** It is where the throttle counts would matter most, so its recall is
  the first number to check.

The 1-minute full window could keep running alongside the faster path, to confirm.

### Option 4: 15-second windows

This is the plan's stretch experiment (§16); it did not run in M9. On its own it saves only about 40 s (estimate:
about 210 s end to end): the window step falls from 52 s to roughly 10–20 s, but the 169 s wait for CloudWatch
stays. AWS publishes Lambda and DynamoDB metrics per minute, so the 19 CloudWatch columns cannot exist at 15 s.

It also brings costs:

- **A new trigger.** EventBridge schedules run at most once a minute, so 15-s windows need a different trigger.
- **Late records.** A record can wait 13 s in the telemetry queue ([Docs/PIPELINE.md](Docs/PIPELINE.md)), so each
  window would wait about that long after it ends before it is complete.
- **New features and labels.** Request rates, the `_change` columns and the 3-window rolling statistics are
  defined over minutes. The labeler's 30-second activity threshold
  ([Docs/LABELING.md](Docs/LABELING.md)) would need a new value, and the models would need retraining.
- **More false alarms.** A 15-s window holds about a quarter of a minute's requests, so its p95 and p99 latencies
  are noisier, and there are four times as many windows an hour. The incident store's rules (open on the first
  counted window, close after two that don't count in a row) would need retuning, which gives back some of the
  time.

After option 3, though, the window becomes the largest step left, and 15-s windows pay off (estimate: about 1 to
1.5 minutes end to end, of which Bedrock's 28 s is then the largest part).

### Suggested order of future work

1. **Offline: retrain on the 87 record-only columns.** This needs no new runs and no new features: drop the 19
   CloudWatch columns from `v1`'s existing feature matrix and train the same models as M5 on the rest. Score them
   on the test set with M9-2's evaluation (`AnomalyPulse/ml/m9_2_metrics.py`). Compare per-class recall
   (Database throttling first), macro-F1 and healthy-window false alarms with M9-2's numbers and their intervals.
   It costs a few short SageMaker jobs, and within about a day it shows whether options 3 and 4 are worth
   building.
2. **If accuracy holds: add a record-only fast path to the live system.** Build a record-only window in the window
   builder's existing run (it already writes health from the same records), classify it, and let the incident
   store open on it. Keep the full window as a check. Then measure on live runs with `report/latency.py` and M9-2's
   evaluation.
3. **Offline: try 15-s windows.** Rebuild features and labels at 15 s from `v1`'s raw request records
   (`tier1/requests`) and retrain. Measure false alarms per hour and latency against step 1's 1-minute model. Go
   live, with a sub-minute trigger, only if the false alarms stay acceptable.
4. **Only if the CloudWatch path stays the detection path:** measure how soon CloudWatch's minutes are complete
   (option 1's poller) before lowering `METRICS_LAG_MINUTES`, and consider the snapshot-triggered build (option 2).

Each of these changes the system M9 evaluated. Afterwards, re-measure latency with `report/latency.py` and accuracy
with M9-2's evaluation, and keep the new numbers apart from M9's. The live steps need traffic from a runner host,
like every live run so far (Status, above).
