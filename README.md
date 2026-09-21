# CloudSentinel
## Serverless AI-Powered Incident Detection and Root-Cause Analysis

### Project Concept

**Core question:**

> Can a serverless AI system automatically detect, classify, and diagnose failures in a distributed cloud application using application telemetry?

CloudSentinel is a proposed cloud-computing project for a five-person team. The system will monitor a small serverless e-commerce application, detect abnormal behavior using machine learning, and use generative AI to explain incidents and recommend remediation.

The project combines:

1. A **serverless distributed application** that generates realistic telemetry.
2. An **ML-based anomaly detection and incident classification system**.
3. A **generative-AI diagnosis layer** that turns telemetry and ML predictions into human-readable incident reports.

The goal is to make this a **cloud-computing project with meaningful AI**, rather than simply an AI application deployed on AWS.

---

# 1. High-Level Architecture

```text
                         ┌──────────────────┐
                         │    Web Client    │
                         │ React / JS / HTML│
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │   API Gateway    │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
                    ▼             ▼             ▼
               ┌─────────┐  ┌─────────┐  ┌─────────┐
               │ Lambda  │  │ Lambda  │  │ Lambda  │
               │ Product │  │ Cart    │  │ Order   │
               └────┬────┘  └────┬────┘  └─────┬───┘
                    │            │             │
                    ▼            ▼             ▼
               DynamoDB       DynamoDB       Payment
                                             Service
                                                │
                                                ▼
                                         Simulated/Other
                                           Lambda

                         ╔══════════════════════╗
                         ║     TELEMETRY        ║
                         ║                      ║
                         ║ CloudWatch Logs      ║
                         ║ CloudWatch Metrics   ║
                         ║ X-Ray (optional)     ║
                         ╚══════════╤═══════════╝
                                    │
                                    ▼
                              ┌───────────┐
                              │EventBridge│
                              └─────┬─────┘
                                    │
                                    ▼
                              ┌───────────┐
                              │    SQS    │
                              │ Telemetry │
                              └─────┬─────┘
                                    │
                                    ▼
                              ┌───────────┐
                              │  Lambda   │
                              │ Aggregator│
                              └─────┬─────┘
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                    ┌──────────┐          ┌──────────┐
                    │    S3    │          │ DynamoDB │
                    │ Raw Data │          │ Incidents│
                    └────┬─────┘          └──────────┘
                         │
                         ▼
                  ┌───────────────┐
                  │   SageMaker   │
                  │  (training +  │
                  │    tuning)    │
                  └───────┬───────┘
                          │  model artifact → S3
                          ▼
                   ┌────────────┐
                   │   Lambda   │
                   │ Diagnosis  │
                   │ (loads     │
                   │  model +   │
                   │  runs      │
                   │ inference) │
                   └──────┬─────┘
                          │
                          ▼
                    ┌──────────┐
                    │ Bedrock  │
                    │   LLM    │
                    └────┬─────┘
                         │
                         ▼
                  ┌──────────────┐
                  │ CloudSentinel│
                  │   Dashboard  │
                  └──────────────┘
```

Not every component needs to be implemented in the first version. The architecture should have a clearly defined **MVP** and a set of optional stretch goals.

Note the inference path: SageMaker is used to **train and tune** the models; the trained artifact is stored in S3 and loaded by the **Diagnosis Lambda**, which runs inference. This keeps the runtime path fully serverless (see Section 9).

---

# 2. The Monitored Application

Build a deliberately small serverless **e-commerce application**. The purpose is not to create a complete online store; it is to create a realistic distributed system that produces useful telemetry and can experience controlled failures.

## Proposed Services

### Product Service

```text
GET /products
GET /products/{id}
```

Uses DynamoDB.

### Cart Service

```text
POST /cart
GET /cart/{user}
DELETE /cart/{user}/{item}
```

Uses DynamoDB.

### Order Service

```text
POST /orders
GET /orders/{id}
```

Uses DynamoDB and calls the payment service.

### Payment Service

```text
POST /payments
```

Can initially be another Lambda. It should support configurable artificial latency and failures so that incidents can be injected.

The application should remain intentionally simple. The interesting part is the **observability and AI system surrounding it**.

---

# 3. Telemetry

Telemetry is one of the most important parts of the project.

Every request should generate structured telemetry, rather than relying exclusively on unstructured text logs.

Example raw record:

```json
{
  "timestamp": "2026-09-14T15:42:18.123Z",
  "service": "order-service",
  "endpoint": "/orders",
  "request_id": "abc123",
  "status_code": 500,
  "latency_ms": 1842,
  "lambda_duration_ms": 1820,
  "cold_start": false,
  "db_latency_ms": 32,
  "db_throttled": true,
  "dependency": "payment-service",
  "dependency_latency_ms": 1500,
  "error_type": "DynamoDBThrottle"
}
```

Every record must carry a **synchronized, high-resolution timestamp** (millisecond precision) and the upstream `request_id`, so cross-service ordering can be reconstructed by correlation. This timestamp/correlation discipline is required in the MVP; full distributed tracing (X-Ray) remains a stretch goal. Any statement about the *ordering* of symptoms (e.g. "order failures began ~40 seconds after payment latency rose") is reported as an **observed sequence** derived from correlated timestamps, not as proven causation.

## Candidate Features

### Traffic

- Requests/sec
- Requests/min
- Endpoint frequency

### Latency

- Average latency
- p50 latency
- p95 latency
- p99 latency

### Errors

- 4xx rate
- 5xx rate
- Timeout rate
- Exception rate

### Lambda

- Duration
- Memory utilization
- Invocation count
- Throttles
- Cold starts

### DynamoDB

- Read/write activity
- Throttled-request counts (from CloudWatch)
- Latency
- Consumed capacity

### Dependencies

- Dependency latency
- Dependency error rate
- Timeout count

### Temporal Features

- Hour
- Rolling averages
- Rolling standard deviation
- Rate of change

## Feature/Label Boundary

A subtle but critical form of data leakage is a feature vector that already contains the answer. Some fields in the raw telemetry record are effectively near-labels — if they enter the model input, the classifier is solved by construction and the reported accuracy is meaningless. We therefore split telemetry into two explicit tiers.

**Tier 1 — Observable features (may enter the model):** fields a real monitoring system could see without knowing the cause. These include `request_rate`, `latency_ms` and its percentiles (p50/p95/p99), `status_code` counts and derived error rates, `lambda_duration_ms`, `cold_start`, DynamoDB `ConsumedCapacity`, DynamoDB `ThrottledRequests` **counts** (as emitted by CloudWatch), `dependency_latency_ms`, dependency error/timeout counts, and all derived temporal features.

**Tier 2 — Ground-truth / diagnostic (never a feature):** fields that encode or strongly reveal the injected cause. These include `error_type` (e.g. `"DynamoDBThrottle"`), the `db_throttled` boolean, all fault-injection parameters, and the incident label itself. Tier-2 fields are used only for labeling and evaluation.

The distinction between Tier 1 and Tier 2 for DynamoDB is deliberate: the **count** of throttled requests is a legitimate observable signal, whereas a synthesized `db_throttled` boolean flag is effectively the label and is forbidden as a feature.

The feature-engineering code must include an automated guard that fails the build if any Tier-2 field name appears in the training feature matrix.

## Window Labeling Policy

Features are aggregated into one-minute windows (see below), but experiments have onset, steady-state, and recovery phases (Section 7). Without an explicit rule, boundary windows introduce systematic label noise. The policy is:

- **Activity threshold.** A window is labeled with an incident class only if the injected incident is active for **≥ 50%** of the window's duration. Windows below that threshold are labeled `NORMAL`.
- **Cascades are labeled by observable state.** A window is labeled `CASCADING_FAILURE` only when **two or more injected faults are concurrently active** within it. The early windows of a cascade — when only the initial fault (e.g. a traffic spike) is active — are legitimately labeled by that observable single fault (e.g. `TRAFFIC_SPIKE`). Cascade performance is therefore reported as **time-to-recognize the multi-fault state**, not as per-window accuracy against a `CASCADING_FAILURE` label that would otherwise be applied before the cascade is observable.
- **Recovery windows are excluded** from the training set for the MVP, to avoid introducing a fuzzy sixth class. (Adding an explicit `RECOVERING` class is a stretch goal.)

The ML dataset should use these **aggregated one-minute windows** rather than treating every individual request as a separate ML observation.

---

# 4. Incident Types

Start with five primary incident classes plus a normal state.

| Incident | Observable Symptoms | Difficulty |
|---|---|---:|
| Normal | Stable metrics | — |
| Traffic Spike | Requests increase sharply | Easy |
| Database Throttling | DB throttles + latency increases | Medium |
| Lambda Degradation | Lambda duration increases | Medium |
| Dependency Failure | Downstream errors/timeouts | Medium |
| Cascading Failure | Multiple correlated symptoms | Hard |

## Cascading Failure

This should be one of the most interesting demonstrations.

Example:

```text
Traffic spike
     │
     ▼
DynamoDB throttling
     │
     ▼
Lambda latency increases
     │
     ▼
Requests timeout
     │
     ▼
Users retry
     │
     ▼
Traffic increases further
     │
     └───────────────► feedback loop
```

This gives CloudSentinel an opportunity to reason about correlated signals rather than simply applying individual threshold rules. Note that early cascade windows are labeled by their observable single fault per the Window Labeling Policy (Section 3); `CASCADING_FAILURE` applies only once two or more faults are concurrently active.

---

# 5. Incident Simulator

Build an **Incident Simulator** that can deliberately create known failures.

Conceptual interface:

```text
Incident Simulator
────────────────────────────────

[✓] Traffic Spike
[✓] DynamoDB Throttling
[✓] Lambda Slowdown
[✓] Payment Timeout
[ ] Cascading Failure

Duration: 5 minutes
Intensity: 0.8

             [ START INCIDENT ]
```

The simulator can be implemented as a Python/Boto3-based workload and fault-injection tool.

## Examples

### Traffic Spike

Increase request rate by a controlled factor.

```text
Normal:     20 requests/sec
Incident:  200 requests/sec
```

### Lambda Degradation

Introduce additional computation or controlled delay into a Lambda function.

```text
Normal:     ~100 ms
Incident:  ~1000 ms
```

### Payment Failure

Configure the simulated payment service to introduce:

- High latency
- Intermittent 5xx errors
- Timeouts

### Database Throttling

Produce **real** DynamoDB throttling by controlling the ratio of offered load to provisioned capacity: set a low provisioned RCU/WCU and drive the workload above it. The controllable parameter is the **offered-load-to-capacity ratio**, not a target throttle percentage. The resulting throttle rate is an *emergent outcome* that is measured and recorded per experiment, not something we set directly. We do **not** synthesize a `db_throttled` flag into the telemetry features (see the Feature/Label Boundary, Section 3); the model reads the observable CloudWatch `ThrottledRequests` counts only.

The exact mechanism should be designed carefully so that experiments remain reproducible and don't create unnecessary AWS costs.

---

# 6. Dataset Generation

Rather than relying entirely on a public cloud-log dataset, generate a **controlled synthetic-but-realistic dataset** from the application.

The advantage is that we know the ground truth.

For example:

```text
experiment_id: 042

start: 15:00
end:   15:10

incident:
    type = DATABASE_THROTTLING
    severity = HIGH
```

A possible initial dataset:

```text
200 normal experiments
200 traffic spike
200 database throttling
150 Lambda degradation
150 dependency failure
100 cascading failure
--------------------------------
1000 total experiments
```

The exact number can be adjusted based on compute cost and available time.

Each experiment should record:

- Incident type
- Start time
- End time
- Severity
- Fault-injection parameters
- Application telemetry
- Ground-truth label

This gives us a proper evaluation dataset.

---

# 6b. Evaluation Integrity

Because both the training and test datasets are generated by the same incident simulator, data leakage is a genuine risk: a model could learn to recognize the simulator's specific parameter fingerprint rather than learning generalizable failure patterns. Three structural controls address this.

We are careful about what these controls do and do not license us to claim. They let us argue that reported metrics reflect **generalization across held-out regions of the simulator's own parameter space** rather than memorization of a fixed set of simulator configurations. They do **not** establish generalization to real-world production incidents — validating that would require telemetry from real systems, which is out of scope.

## Control 1 — Parameter-Space Partitioning (Interleaved Bands)

Define each incident type in terms of a continuous parameter space (intensity, duration, severity), and partition that space into **interleaved bands** between training and test. The test bands sit *inside* the trained range rather than beyond it, so the test is genuine interpolation into unseen gaps — not monotonic extrapolation into more extreme (and typically easier-to-detect) territory.

Example partitions:

| Incident | Parameter | Training Bands | Test Bands (held-out gaps) |
|---|---|---|---|
| Traffic Spike | Multiplier | 2×–4×, 6×–8× | 4×–6×, 8×–10× |
| Lambda Slowdown | Added latency | 200–500ms, 800–1100ms | 500–800ms, 1100–1400ms |
| DB Throttling | Offered-load-to-capacity ratio | 1.5×–2.5×, 3.5×–4.5× | 2.5×–3.5×, 4.5×–5.5× |
| Dependency Failure | Timeout duration | 500–1500ms, 2500–3500ms | 1500–2500ms, 3500–4500ms |

This ensures that test-set incidents represent unseen parameter regions interleaved with the training regions, rather than interpolations that overlap the training distribution.

**Separate extreme-extrapolation stress slice.** In addition, generate a small slice at the extreme end of each parameter axis (e.g. traffic multiplier > 10×, added latency > 1500ms, offered-load ratio > 6×, timeout > 5000ms). Because more extreme incidents are generally *easier* to detect, this slice is reported **separately** as an extrapolation stress test and is not used to support the core generalization argument.

## Control 2 — Temporal Train/Test Split

Do not randomly shuffle experiments into train and test sets. Instead, use a strict temporal split:

```text
Experiments 1–700:    training + validation   (generated first)
Experiments 701–1000: test set                (generated on a separate day)
```

Generating the test set on a separate day introduces natural variability in AWS environment state — Lambda cold-start behavior, background DynamoDB latency, network conditions — that a random split would obscure. This makes the test set a more honest evaluation of generalization.

## Control 3 — Simulator Diversity Protocol

Before generating the test set, vary at least two simulator parameters beyond their training defaults:

- **Incident duration**: if training incidents run 5 minutes, test set should include 2-minute and 12-minute incidents.
- **Endpoint request distribution**: shift the baseline traffic mix (e.g., change the 60/15/10/10/5 split) to test robustness to workload variation.
- **Confounder incidents**: include a small number of experiments where two incident types co-occur without being labeled as a cascading failure (e.g., a traffic spike and a Lambda slowdown occurring simultaneously). This tests classifier robustness to confounders.

## Evaluation Integrity Statement

> Because the training and test datasets are both generated by the same incident simulator, we explicitly control for data leakage using three mechanisms: (1) interleaved parameter-space partitioning between train and test regimes, (2) strict temporal splitting with test data collected on a separate day, and (3) a simulator diversity protocol that varies incident duration, request distribution, and co-occurrence patterns in the test set. In addition, a strict Feature/Label Boundary (Section 3) prevents near-label fields from entering the model input. These controls allow us to claim that reported metrics reflect generalization across held-out regions of the simulator's parameter space rather than memorization of simulator artifacts. We do not claim generalization to real-world production incidents; that would require real-system telemetry and is out of scope.

---

# 7. Workload Generator

Create a Python workload generator that produces realistic request distributions.

Example:

```text
60% GET /products
15% GET /products/{id}
10% POST /cart
10% GET /orders
 5% POST /orders
```

The workload generator should support configurable:

- Request rate
- Request distribution
- Duration
- User count
- Incident type
- Incident intensity

Example experiment:

```text
0–10 min     normal
10–15 min    traffic spike
15–20 min    recovery
20–25 min    database throttling
25–30 min    recovery
```

This makes the dataset generation reproducible.

---

# 8. ML Approach

A key architectural decision:

> **Do not make the LLM the primary anomaly detector.**

Instead, separate detection/classification from explanation.

```text
Telemetry
    │
    ▼
Feature Engineering
    │
    ▼
ML Anomaly Detection
    │
    ▼
Incident Classification
    │
    ▼
Bedrock
    │
    ▼
Human-readable Diagnosis
```

This gives the project a measurable ML component.

## Model A — Anomaly Detection

Input (Tier-1 observable features only):

```text
[request_rate,
 p95_latency,
 error_rate,
 lambda_duration,
 db_latency,
 db_throttled_request_count,
 dependency_latency,
 ...]
```

Output:

```text
NORMAL
ANOMALY
```

Possible initial algorithms:

- Isolation Forest
- One-Class SVM
- Autoencoder as a stretch goal

Start with **Isolation Forest** because it is relatively simple, interpretable, and appropriate for an initial anomaly-detection experiment.

We include an unsupervised detector (Isolation Forest) **deliberately**, to model the realistic case where labels are unavailable in production and to provide a detection signal independent of the incident taxonomy. Because we do have labels in this project, we additionally train a **supervised binary anomaly detector** (NORMAL vs ANOMALY) as a comparison point, so the labeled-data case is also represented and the cost of going unsupervised is quantified.

## Model B — Incident Classification

For anomalous windows:

```text
NORMAL
TRAFFIC_SPIKE
DATABASE_THROTTLING
LAMBDA_DEGRADATION
DEPENDENCY_FAILURE
CASCADING_FAILURE
```

Possible models:

- XGBoost
- Random Forest
- Logistic Regression baseline
- Neural network as a stretch goal

A useful progression would be:

1. Simple statistical baseline
2. Isolation Forest
3. Supervised classifier
4. Compare performance

This gives the team a stronger experimental story.

## Model 0 — Threshold Baseline

Before training any ML model, implement a trivially simple rule-based detector as a sanity-check baseline:

```text
Flag ANOMALY if:
  p95_latency  > 2× rolling mean
  OR
  error_rate   > 5%
```

The threshold baseline sets a **performance floor**: any ML model that does not substantially outperform it has not learned useful multivariate structure. This is especially informative on cascading failures, where no single metric crosses a threshold cleanly — a large ML margin there is positive evidence of genuine multivariate learning.

**Overfitting to simulator artifacts is a separate question, detected separately.** It shows up as a **train/test performance gap** on the held-out interleaved parameter regions (Section 6b), *not* as the size of the margin over the baseline. A small margin over the baseline does not by itself imply overfitting — it may simply mean the synthetic incidents are easy, or that there is little multivariate structure to exploit.

Report threshold baseline performance alongside all ML results in the evaluation section.

---

# 9. SageMaker

Use SageMaker for the ML **training lifecycle only**, not for runtime inference.

Conceptual pipeline:

```text
S3
 │
 │ training data
 ▼
SageMaker Training + Hyperparameter Tuning
 │
 ▼
Model Artifact
 │
 ▼
S3 (artifact store)
 │
 ▼
Diagnosis Lambda  (loads artifact, runs inference)
```

Why inference lives in Lambda, not on a SageMaker endpoint: the models here (Isolation Forest, XGBoost) run against a few thousand one-minute windows and are tiny. A persistent SageMaker real-time endpoint is billed per hour whether or not it is serving traffic, which would be the single most expensive idle component in an otherwise serverless design and would undercut the serverless-economics analysis in Section 16. Packaging the trained artifact and loading it in the **Diagnosis Lambda** (a lightweight artifact loaded from S3 at cold start, or a container Lambda) keeps the runtime path genuinely serverless and makes "cost per inference" a meaningful number. This also respects the anti-logo principle in Section 12: SageMaker earns its place for training and tuning, not as decoration on the inference path.

SageMaker responsibilities (training-time):

- Training
- Hyperparameter tuning
- Model artifact storage
- Model evaluation

Inference responsibility (runtime):

- The Diagnosis Lambda loads the artifact from S3 and runs prediction.

(If the team later wants a managed inference path for pedagogical reasons, prefer **SageMaker Serverless Inference** over a real-time endpoint, and include an explicit idle-cost comparison against the Lambda approach in the Section 16 cost analysis.)

---

# 10. Bedrock

Bedrock should perform a different job from SageMaker.

> **SageMaker (via the Diagnosis Lambda) tells us what is happening. Bedrock explains why it is happening and what a human should do about it.**

Example input to Bedrock:

```json
{
  "incident": "DATABASE_THROTTLING",
  "confidence": 0.94,
  "duration_minutes": 7,
  "metrics": {
    "request_rate": "+310%",
    "p95_latency": "+420%",
    "db_throttle_rate": "+870%",
    "lambda_duration": "+260%",
    "error_rate": "+18%"
  },
  "affected_services": [
    "product-service",
    "order-service"
  ]
}
```

Expected output:

```text
Incident Summary
────────────────

Severity: HIGH
Confidence: 94%

Root Cause:
DynamoDB throttling is the most likely root cause.

Evidence:
• Database throttling increased 8.7×.
• p95 API latency increased 4.2×.
• Lambda execution time increased 2.6×.
• The affected endpoints depend directly on DynamoDB.

Recommended Actions:
1. Inspect DynamoDB capacity.
2. Check for an abnormal request burst.
3. Review application retry behavior.
4. Verify whether other tables are affected.
```

The LLM should receive **structured evidence**, not simply a giant dump of raw logs. Note that Bedrock is handed the classified incident type and metric deltas as input; it is a constrained *explanation generator*, evaluated on evidence fidelity rather than on independently diagnosing the incident (see Sections 16 and 20).

---

# 11. Optional RAG / Knowledge Base

This is a potential stretch goal — and one of the better-justified ones. Because Bedrock is otherwise handed the incident label and metrics (Section 10), RAG is the mechanism that gives it genuinely *new* information beyond its structured input, and it comes with its own measurable axis (retrieval quality).

Create a knowledge base containing:

- CloudSentinel troubleshooting guides
- Internal runbooks
- Historical incidents
- Architecture documentation
- Known failure modes
- Remediation procedures

Then:

```text
ML detector
     │
     ▼
Incident classification
     │
     ▼
Retrieve relevant runbooks
     │
     ▼
Bedrock
     │
     ▼
Diagnosis + recommended remediation
```

For example:

```text
Detected:
DYNAMODB_THROTTLING
```

The retrieval system finds a relevant runbook describing:

- Consumed capacity
- Traffic distribution
- Hot partitions
- Retry behavior
- Capacity configuration

Bedrock incorporates that information into its response.

This would make the GenAI component more useful and give us a concrete way to evaluate retrieval quality.

---

# 12. AWS Services

## Core Services

| AWS Service | Purpose |
|---|---|
| API Gateway | Public REST API |
| Lambda | Serverless microservices, processing, and ML inference |
| DynamoDB | Application state and incident metadata |
| S3 | Raw telemetry, ML datasets, and model artifacts |
| CloudWatch | Logs and metrics |
| SQS | Buffering and decoupling |
| EventBridge | Event routing |
| SageMaker | ML training and tuning (inference runs in Lambda) |
| Bedrock | Generative AI diagnosis |
| IAM | Security and permissions |
| CloudFormation/CDK | Infrastructure deployment |

## Stretch Services

| Service | Potential Use |
|---|---|
| Step Functions | Orchestrating incident/ML workflows |
| Athena | Querying historical telemetry |
| Glue | ETL |
| X-Ray | Distributed tracing |
| Cognito | Authentication |
| CloudWatch Dashboards | Native monitoring |
| Bedrock Knowledge Bases | RAG/runbooks |
| SageMaker Serverless Inference | Managed inference path (if desired later) |

Important principle:

> **Don't add AWS services simply to increase the number of AWS logos. Every service should solve a real architectural problem.**

---

# 13. Boto3

Boto3 should be used heavily throughout the project.

Potential uses:

- Calling API Gateway
- Invoking Lambda
- Reading/writing DynamoDB
- Uploading telemetry to S3
- Publishing EventBridge events
- Sending/receiving SQS messages
- Loading model artifacts from S3 for inference
- Calling Bedrock inference APIs
- Running experiments
- Starting/stopping infrastructure where appropriate

Infrastructure itself can be defined using CDK/CloudFormation rather than trying to manually provision everything through Boto3.

The goal should be to demonstrate that the team understands AWS programmatically without making the code unnecessarily complicated.

---

# 14. Dashboard

The dashboard should look more like an **SRE/operations console** than a generic chatbot.

Conceptual design:

```text
┌─────────────────────────────────────────────────────────────┐
│ CLOUDSENTINEL                              ● SYSTEM HEALTHY │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ REQUESTS       ERROR RATE       P95 LATENCY     INCIDENTS   │
│  1,284/min        0.7%             184ms           0        │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                    SERVICE HEALTH                           │
│                                                             │
│ Product       ████████████████████  Healthy                 │
│ Cart          ████████████████████  Healthy                 │
│ Orders        ███████████████░░░░░  Degraded                │
│ Payments      ███████████░░░░░░░░░  Degraded                │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                 ACTIVE INCIDENT                             │
│                                                             │
│  HIGH: Dependency Failure                                   │
│                                                             │
│  Confidence: 93%                                            │
│  Started: 14:32                                             │
│  Affected: Order Service, Payment Service                   │
│                                                             │
│  AI DIAGNOSIS                                               │
│  ─────────────────────────────────────────────────────────  │
│  Payment service latency increased from 110ms to 2.4s.      │
│  Order failures began approximately 40 seconds later        │
│  (observed sequence from correlated timestamps).            │
│                                                             │
│  ROOT CAUSE                                                 │
│  Payment dependency timeout                                 │
│                                                             │
│  RECOMMENDED ACTIONS                                        │
│  • Inspect payment service health                           │
│  • Review timeout/retry configuration                       │
│  • Check recent deployment                                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Copy that describes symptom ordering (e.g. "began approximately 40 seconds later") is presented as an **observed sequence** from correlated timestamps, not as an assertion of proven causation.

---

# 15. Final Demo

The strongest demo should be a **live incident injection**.

## Demo Part 1 — Healthy System

Start with:

```text
SYSTEM HEALTHY

Error Rate: 0.4%
P95 Latency: 170 ms
Active Incidents: 0
```

Show normal application activity.

---

## Demo Part 2 — Inject Database Incident

Someone selects:

```text
Inject:
DATABASE THROTTLING
```

The application continues receiving traffic.

Telemetry begins changing:

```text
DynamoDB throttles ↑
        ↓
Lambda latency ↑
        ↓
API 5xx ↑
```

CloudSentinel detects the incident.

Dashboard:

```text
HIGH-SEVERITY INCIDENT DETECTED

Incident:
DATABASE_THROTTLING

Confidence:
94%
```

Then Bedrock generates an explanation.

---

## Demo Part 3 — Show Ground Truth

Show that the simulator actually injected:

```text
Ground Truth:
DATABASE_THROTTLING
```

Model prediction:

```text
DATABASE_THROTTLING
Probability: 94%
```

AI diagnosis:

```text
DynamoDB throttling is the most likely root cause...
```

This is powerful because the audience can see that the system isn't simply generating a plausible story; the ML prediction can be quantitatively evaluated.

---

## Demo Part 4 — Cascading Failure

For the most ambitious demonstration, inject a cascading failure:

```text
Traffic spike
       ↓
Database throttling
       ↓
Lambda latency
       ↓
API errors
       ↓
Retries
       ↓
More traffic
```

CloudSentinel should identify the incident and **narrate the correlated signals** in the order they were observed. The narrated ordering comes from correlated timestamps across services, not from causal inference by the LLM — the demo caption should make this explicit so the audience does not read the narration as proven causation.

---

# 16. Evaluation

This should be a major part of the final report.

## Detection

Measure:

- Precision
- Recall
- F1
- False-positive rate
- Detection latency

Question:

> Did CloudSentinel correctly identify anomalous behavior?

## Classification

Measure:

- Accuracy
- Macro F1
- Confusion matrix
- Per-class precision/recall

Question:

> Did CloudSentinel identify the correct incident type?

## Operational Performance

Measure:

- Detection latency
- Diagnosis latency
- End-to-end latency
- Lambda duration
- API latency
- Throughput
- SQS backlog

**Detection latency is dominated by the one-minute aggregation window** and is reported as such; sub-minute detection is out of scope by design. As a stretch experiment, re-run detection with 15-second windows to characterize the tradeoff between detection latency and per-window signal stability.

## GenAI Quality

Bedrock is handed the classified incident type and the metric deltas, so "correct root cause" is near-tautological and is **not** used as the primary GenAI metric. Bedrock is evaluated as a constrained *explanation generator*, on the axes where it can actually be wrong:

- **Evidence fidelity**: does every claim in the diagnosis trace to a metric present in the structured input? (Report the percentage of unsupported claims.)
- **Hallucination rate**: mentions of services, metrics, or values not present in the input.
- **Actionability**: are the recommended actions correct and specific for the given incident type? (Human rubric.)
- **Completeness**: does the diagnosis cover the salient evidence?

If RAG is implemented, additionally evaluate **retrieval quality** (whether the retrieved runbook matches the incident type).

## Cloud Cost

Measure:

- Cost per experiment
- Cost per incident
- Cost per inference
- Approximate monthly cost at different request volumes

This allows us to discuss the **serverless economics** of the architecture. (Because inference runs in Lambda rather than on a persistent endpoint, cost-per-inference is a meaningful, near-zero-idle number — see Section 9.)

## Evaluation Integrity

Report the following to demonstrate that results generalize across the held-out regions of the simulator's parameter space (not to real-world incidents — see Section 6b):

- All metrics computed on the temporally held-out test set (experiments 701–1000), not on the training or validation sets.
- Threshold baseline performance reported alongside every ML metric, so the margin of improvement is explicit.
- Per-parameter-band breakdown for key incident types, confirming that performance does not degrade sharply on the held-out interleaved bands from Section 6b.
- The extreme-extrapolation stress slice reported **separately** from the core generalization metrics.
- Confounder accuracy: how often the classifier correctly handles co-occurring incidents that are not labeled as cascading failures.

These results, taken together, allow us to argue that CloudSentinel generalizes across held-out regions of the simulator's parameter space rather than fitting a fixed set of simulator configurations.

---

# 17. Experimental Methodology

A potential formal experiment, structured to enforce the leakage controls defined in Section 6b.

## Phase 1 — Training Set (Experiments 1–700)

Generated first, using the training bands (interleaved with the test bands per Section 6b):

```text
140 normal
140 traffic spikes         (multiplier bands 2×–4×, 6×–8×)
140 database throttling     (offered-load-to-capacity 1.5×–2.5×, 3.5×–4.5×)
105 Lambda degradation      (added latency 200–500ms, 800–1100ms)
105 dependency failures     (timeout 500–1500ms, 2500–3500ms)
 70 cascading failures
---
700 total
```

Split into training (experiments 1–560) and validation (experiments 561–700) for hyperparameter tuning.

## Phase 2 — Test Set (Experiments 701–1000)

Generated on a separate day, using the held-out interleaved bands and the simulator diversity protocol:

```text
 60 normal
 60 traffic spikes         (multiplier bands 4×–6×, 8×–10×)
 60 database throttling     (offered-load-to-capacity 2.5×–3.5×, 4.5×–5.5×)
 45 Lambda degradation      (added latency 500–800ms, 1100–1400ms)
 45 dependency failures     (timeout 1500–2500ms, 3500–4500ms)
 30 cascading failures
 30 confounder incidents    (co-occurring types, not labeled cascading)
 20 varied-duration         (2-min and 12-min incidents)
---
350 total
```

A small **extreme-extrapolation stress slice** (traffic > 10×, added latency > 1500ms, offered-load ratio > 6×, timeout > 5000ms) is generated in addition and reported separately from the core generalization metrics.

The test set is generated with a shifted endpoint request distribution and is not inspected until all training and validation work is complete.

## Per-Experiment Procedure

For each experiment in both phases:

1. Start the application.
2. Generate baseline workload.
3. Inject an incident using phase-appropriate parameter bands.
4. Collect telemetry (Tier-1 features + Tier-2 ground truth kept separate).
5. Aggregate features into one-minute windows and apply the Window Labeling Policy.
6. Run threshold baseline detector.
7. Run anomaly detector (Isolation Forest; plus supervised binary detector for comparison).
8. Run incident classifier (XGBoost).
9. Generate Bedrock diagnosis.
10. Compare all predictions to ground truth.
11. Record latency and cost.

## Final Reporting

Report threshold baseline, Isolation Forest, and supervised classifier metrics side by side on the held-out test set. Include per-parameter-band breakdowns, the separate extreme-extrapolation slice, and confounder accuracy. This creates a reproducible ML/cloud experiment with explicit controls for generalization, rather than a one-off demo.

---

# 18. MVP vs. Stretch Goals

## MVP — Thin End-to-End Slice

The true minimum viable project is the **thinnest possible slice that proves the full loop end-to-end**:

```text
API Gateway
    ↓
One Lambda service (Product)
    ↓
DynamoDB
    ↓
CloudWatch  →  structured telemetry
    ↓
S3
    ↓
Threshold detector (Model 0)
    ↓
Bedrock explanation
    ↓
Minimal dashboard
```

With exactly **one incident type (traffic spike)** proven end-to-end: injected → detected by the threshold baseline → explained by Bedrock → shown on the dashboard against ground truth.

The point of this slice is that *something demonstrable exists early*, and that every subsequent addition is an increment on a working system rather than a prerequisite for the first working system.

## Ordered Growth Path

Each tier builds on a working previous tier:

- **MVP+1 — Real ML.** Swap the threshold detector for the Isolation Forest anomaly detector and add the XGBoost incident classifier. Introduce SageMaker training + the Diagnosis Lambda inference path.
- **MVP+2 — Full incident taxonomy.** Add database throttling, Lambda degradation, and dependency failure, plus the remaining application services (Cart, Order, Payment).
- **MVP+3 — Cascades and integration.** Add cascading-failure injection, the full telemetry pipeline (SQS/EventBridge/aggregator), and the complete evaluation suite.

## Target Deliverable

The intended full deliverable (beyond the MVP) contains:

```text
API Gateway → Lambda → DynamoDB → CloudWatch → S3
→ SageMaker (training) → Diagnosis Lambda (inference) → Bedrock → Dashboard
```

With at least:

- Normal workload
- Traffic spike
- Database throttling
- Lambda degradation
- Dependency failure
- Basic anomaly detection
- Incident classification
- Bedrock-generated diagnosis

## Stretch Goals

Potential additions:

- SQS
- EventBridge
- Step Functions
- X-Ray
- RAG
- Historical incident retrieval
- Automatic remediation
- Cognito authentication
- Advanced causal inference
- Cost optimization
- Multi-model comparison
- Real-time CloudWatch dashboard
- Model drift monitoring
- `RECOVERING` class for recovery-phase windows
- 15-second detection windows (latency/stability tradeoff)

---

# 19. Proposed Development Milestones

## Milestone 1 — Serverless Application

Build:

```text
API Gateway
    ↓
Lambda
    ↓
DynamoDB
```

Get a functioning e-commerce API.

---

## Milestone 2 — Observability

Add:

```text
CloudWatch
    ↓
Structured telemetry (Tier-1 features / Tier-2 ground truth separated)
    ↓
S3
```

Verify that telemetry is sufficient for ML.

---

## Milestone 3 — Incident Simulator

Implement controlled:

- Traffic spikes
- Database throttling (via offered-load-to-capacity ratio)
- Lambda slowdown
- Payment failures
- Cascading failures

---

## Milestone 4 — Dataset

Generate labeled experiments and build the training/evaluation dataset, applying the Window Labeling Policy and the Feature/Label Boundary.

---

## Milestone 5 — ML

Train and evaluate:

- Statistical (threshold) baseline
- Isolation Forest (plus supervised binary detector for comparison)
- Supervised incident classifier

Train the selected model using SageMaker; deploy the artifact to S3 for the Diagnosis Lambda.

---

## Milestone 6 — GenAI

Add Bedrock for:

- Incident summaries
- Root-cause explanations
- Evidence synthesis
- Recommended remediation

---

## Milestone 7 — Dashboard

Build the CloudSentinel web interface.

---

## Milestone 8 — Integration

Connect:

```text
Telemetry
→ ML
→ Incident classification
→ Bedrock
→ Dashboard
```

---

## Milestone 9 — Evaluation

Run the full experiment suite and collect:

- ML metrics
- Detection latency
- AI quality
- Cloud performance
- Cost

---

## Milestone 10 — Final Demo

Prepare a live:

1. Healthy application
2. Injected incident
3. ML detection
4. AI diagnosis
5. Ground-truth comparison
6. Cascading-failure demonstration

---

# 20. Key Architectural Principle

The cleanest way to explain the project is:

> **SageMaker (via the Diagnosis Lambda) tells us what is happening. Bedrock tells us why it is happening and what a human should do about it.**

The ML system should be responsible for **measurable detection and classification**.

The GenAI system should be responsible for **interpretation, explanation, retrieval of relevant knowledge, and remediation recommendations**. Bedrock is a *constrained explanation generator*: it is handed the classified incident and evidence, so it is evaluated on evidence fidelity, hallucination rate, and actionability (Section 16), not on independently diagnosing the incident.

This separation makes the project substantially stronger than a generic "LLM reads cloud logs" application.

---

# 21. Potential Project Title

### CloudSentinel
**Serverless AI-Powered Incident Detection and Root-Cause Analysis**

Alternative titles:

- CloudSentinel: Intelligent Observability for Serverless Applications
- CloudSentinel: ML-Based Anomaly Detection and GenAI Incident Diagnosis
- CloudSentinel: An AI-Powered Serverless SRE Platform

---

# 22. The Core Pitch

A concise version for the team:

> **CloudSentinel is a serverless AI-powered observability platform that monitors a distributed e-commerce application, detects anomalous behavior using machine learning, classifies the underlying incident, and uses generative AI to explain the likely root cause and recommend remediation.**
>
> We will intentionally inject known failures into the application to create a labeled dataset, allowing us to quantitatively evaluate detection and classification performance. The system will demonstrate AWS serverless architecture, event-driven processing, NoSQL data modeling, ML deployment, generative AI, observability, fault tolerance, and cloud cost/performance considerations.

---

# 23. Open Questions for the Team

The following are genuinely open scoping decisions. (Questions the plan already commits to — SageMaker + Bedrock, the e-commerce domain, MVP incident scope, and whether to pursue RAG — have been resolved in the sections above and removed from this list.)

1. **How many AWS services are reasonable for the course?**
2. **How much emphasis should we put on ML versus GenAI?**
3. **Should automatic remediation be a stretch goal?**
4. **How much of the infrastructure should be deployed using CDK/CloudFormation versus Boto3?**
5. **What scale can we realistically test within the course's AWS budget?**
6. **What metrics will the professor likely value most: cloud architecture, ML performance, scalability, cost, or all of the above?**

---

## Recommended Direction

If the team agrees on the general concept, the next step should be to **lock down the MVP architecture before writing code**.

The most important decisions are:

1. Define the four application services.
2. Define the telemetry schema, including the Tier-1/Tier-2 Feature/Label Boundary.
3. Define the five incident types.
4. Define how each incident will be injected (including the offered-load-to-capacity mechanism for DynamoDB throttling).
5. Define the ML task, the Window Labeling Policy, and evaluation metrics.
6. Decide which AWS services are core versus stretch.
7. Break each weekly milestone into parallel sprint tasks and assign them across the five team members.

Once those are agreed upon, the project can be implemented incrementally without requiring the entire architecture to work on day one.
