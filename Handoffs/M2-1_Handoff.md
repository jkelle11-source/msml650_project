# M2-1 Handoff — Telemetry Schema Contract, Validator, Layer & S3 Landing Zone (in progress)

> **For the next Claude session (and Jake).** This is a *working-session* handoff, not a team handoff. It captures (1) the **way Jake wants to work**, (2) the M2-1 deliverables and their status, and (3) exactly where things stand so you don't re-derive or re-litigate anything. The contract + validator + layer + S3 bucket are **done and deployed**; the remaining work is the **CloudWatch → S3 delivery path** and the **one-page contract doc**.

---

## 0. How to work with Jake on this — READ FIRST

**This is a Socratic, learning-by-doing session. Jake writes the code himself; you guide.**

The loop:

1. Take the ticket **one deliverable at a time** (see §2).
2. **Jake proposes** an approach or writes a draft.
3. **You give feedback** — point out errors, ask leading questions, explain *why* something bites later, surface trade-offs. Lead him to the answer; don't hand him the finished answer.
4. Repeat until that deliverable is right, then move to the next.

Guardrails:
- **No `Write`/`Edit` on his deliverables.** Reading files, running read-only checks (`git status`, `sam validate --lint`, `make -n`), and validating/running *his* work are fine and encouraged.
- **One explicit relaxation Jake made this session:** for **pure SAM/CloudFormation syntax**, he's fine with you just handing him the correct syntax ("asking you is pretty much the same as Googling the syntax"). This applies to *mechanical YAML/CFN syntax only* — keep the Socratic approach for design decisions, architecture, and the Python. Still flag correctness/design issues in whatever he writes.
- Keep feedback **tight and prioritized** — lead with the blocker, not a wall of nits. (Jake gets frustrated when feedback is too dense; rank issues.)
- When he asks "did I fix it?", **actually verify** against the files / by running it before answering.
- **Writing this handoff is the sanctioned exception** to the no-write rule; he explicitly requested it.

---

## 1. Context — the sources of truth

- **`Docs/PROJECT_PLAN.md`** — the whole AnomalyPulse plan. For M2-1 the load-bearing parts are **§3 (Telemetry; the Tier-1/Tier-2 Feature/Label Boundary; the ms-timestamp + `request_id` correlation discipline)** and **§12 (AWS service list + the "don't add services for logo-count" principle)**.
- **`Milestones/Milestone_02_Observability.md`** — the milestone. **`M2-1` is Jake's issue** and it *blocks* M2-2..M2-5 (the four service owners' instrumentation). Scope is deliberately narrow: structured logs + metrics → S3. The async pipeline (EventBridge→SQS→Aggregator) is **M8**; one-minute-window aggregation is **M4**. Both out of scope here.
- **`Handoffs/M1-1_Handoff.md`** — background on the deployed stack and team conventions.

One-line frame: services emit structured telemetry → ML detects/classifies incidents → Bedrock explains them. M2-1 makes the telemetry **structured, enforced, and landable in S3**.

---

## 2. M2-1 deliverables & status

| # | Deliverable | Status |
|---|---|---|
| 1 | Validating **record schema** (`telemetry_schema.json`) | ✅ **Done** |
| 1b | **Field catalog** with per-field tiers (all fields incl. Tier-2) | ✅ **Done** |
| — | **Lambda layer** packaging (delivery mechanism for the helper) | ✅ **Done, build-proven & deployed** |
| 2 | Shared **validation helper** (`schema_validator.py`) + tests | ✅ **Done, 36 tests pass** |
| 3 | **S3 raw landing zone** + partitioning (`raw/service=<svc>/dt=<date>/…`) | ✅ **Done & deployed** (bucket only; nothing writes to it yet) |
| 4 | **CloudWatch-logs → S3** delivery path | 🚧 **NEXT — design locked (see §4), not yet built** |
| 5 | One-page **telemetry-contract doc** | ⬜ Not started |

**Definition of Done — remaining:**
- [x] Schema validates a known-good record and rejects a record missing `timestamp`/`request_id` *(proven: 36 tests)*
- [x] Every Tier-2 field explicitly marked & documented as *never a feature* *(field catalog + `additionalProperties:false` rejects them)*
- [ ] Records from ≥1 deployed service visible in S3 under documented partitions *(blocked on deliverable 4)*
- [ ] All four service owners have imported the shared validation helper *(layer deployed; owners import it in M2-2..M2-5)*

---

## 3. Decisions already made — do NOT relitigate

**From the prior session (still standing):**
- **Standard JSON Schema (draft 2020-12) + a custom `tier` annotation.** Two artifacts on purpose: the **record schema** validates emitted lines (correlation + Tier-1 only, `additionalProperties:false` so any stray **Tier-2 field is rejected** — the leak-catcher); the **field catalog** is a registry listing *every* field with its tier (the only home for Tier-2 definitions).
- **`required` = all 14 observable fields.** Emission rule: "never omit, emit `null`."
- **Timestamp** enforced by a **`pattern` regex** pinning ms precision (`…\.\d{3}Z$`); `format: date-time` is advisory only (unenforced without a FormatChecker) — the regex does the real work.
- **Validator contract:** `validate(record)` returns `(bool, list_of_errors)` (uniform type both ways); `validate_or_raise(record)` raises the first error. Live handlers log-and-continue; tests/CI/the M4 feature-guard use the raising variant. `Draft202012Validator.check_schema(schema)` runs at **import** so a malformed schema fails loudly at cold start.
- **Sharing mechanism = a Lambda layer.** Single source of truth for the schema lives *inside the layer* (`layers/telemetry/python/telemetry_schema.json`); field catalog stays in `infra/shared/` (build-time artifact, doesn't ship to runtime).

**New this session:**
- **Layer build is no-Docker + cross-arch.** The Makefile copies `python/.` then `python3 -m pip install -r requirements.txt --platform manylinux2014_aarch64 --python-version 3.13 --only-binary=:all:`. Verified the built `rpds` extension is `…aarch64-linux-gnu.so` (Linux arm64 — correct for Graviton), not a macOS wheel. **Gotcha for the future:** that `--platform` pin is now the *only* thing keeping the layer arm64 (the `Globals.Function.Architectures: arm64` does **not** apply to LayerVersions — hence the harmless `BuildArchitecture x86_64` warning). Don't drop the pin.
- **`requirements.txt` = `jsonschema` only.** `json`/`pathlib` are stdlib and were (correctly) removed — never pip-install stdlib (`pathlib` on PyPI is a broken Py2 backport).
- **`pip` vs `python3 -m pip`:** this machine (Apple Silicon, Homebrew) has no bare `pip`, only `pip3` / `python3 -m pip`. The Makefile uses `python3 -m pip`.
- **Use `sam validate --lint`, not plain `sam validate`.** Plain validate is only a schema smoke test; it passed a bad `$( )` Sub and a mis-indented `SSEAlgorithm` that `--lint` (cfn-lint) caught. Make `--lint` the default for this template.
- **S3 landing zone = ONE bucket, prefix zones.** Bucket name is **zone-neutral** (`anomalypulse-telemetry-${AWS::AccountId}-${AWS::Region}`); `raw/` is this milestone's zone, `aggregated/` is reserved for M4. Zone isolation is done via **prefixes** (IAM scoping + prefix-scoped lifecycle), not separate buckets. Bucket is hardened: all 4 Block-Public-Access flags, SSE-S3 (AES256), `OwnershipControls: BucketOwnerEnforced`, and a lifecycle rule `expire-raw-telemetry` scoped to `Prefix: raw/` at 90 days. Name published to SSM at `/anomalypulse/telemetry/bucket/name`.
- **Partition layout is a NAMING CONVENTION, not real S3 partitioning.** S3 is a flat key store; `service=…/dt=…` are Hive-style key segments that *downstream query engines* (Athena/Glue) interpret as partition columns. The `key=value` form (not `raw/<svc>/<date>/`) is deliberate — it's what enables automatic partition discovery later. `dt` = **date** granularity (not hour/minute); the one-minute ML windows live *inside* a day's data, and date aligns with the §6b separate-day train/test split.

- **🚩 DE-SCOPED FIREHOSE — DO NOT RE-INTRODUCE IT.** We first sketched CloudWatch Logs → **Kinesis Firehose** → S3, then sanity-checked and rejected it for M2. Reasons: (1) Firehose/Kinesis is **not in PROJECT_PLAN §12's service list**, and §12 forbids adding services for logo-count; its real value (managed batching, file-size optimization, Parquet, backpressure) only pays off at volume / in M4/M8. (2) M2 scope is deliberately narrow. (3) Firehose does **not** avoid the gzip-unwrap step (that's inherent to a CloudWatch Logs *source*), so it's pure addition — extra service, second IAM role, buffering + dynamic-partition config — on top of an unwrap you need anyway. Lock-in is low because the **S3 `raw/service=/dt=/` layout is the durable interface**; the writer behind it can be upgraded in M8 without touching M4. **If the next session is tempted by Firehose, re-read this bullet first.**

---

## 4. NEXT STEP — CloudWatch subscription → shipper Lambda → S3 (deliverable 4)

**The chosen shape (simpler than Firehose, same decoupling):**

```
service Lambda  →  prints structured JSON telemetry line to stdout
      │
      ▼
CloudWatch Logs  (/aws/lambda/<fn>)
      │   ← SubscriptionFilter forwards matching events
      ▼
ONE shipper Lambda (Jake owns)  — decompress + unwrap + build key + put_object
      │
      ▼
S3   raw/service=<svc>/dt=<date>/part-….json
```

Why this and not direct-write-from-handler: an in-handler S3 write adds I/O + a failure mode **to the request path**, which would contaminate the very latency the incident experiments measure. Staying async/off-path is the reason for the one hop. Service owners still just `print()` a JSON line — the shipper is Jake's infra, invisible to them.

**Resources to build (suggested order, each deploy-and-verify before the next):**

1. **Explicit log groups** (`AWS::Logs::LogGroup`), one per service (`/aws/lambda/anomalypulse-product`, `-cart`, `-order`, `-payment`). *Gotcha:* Lambda auto-creates its log group on first invoke; if you don't declare it, the SubscriptionFilter at deploy time has nothing to attach to (race/failure). Declaring them also lets you set retention.
2. **Shipper Lambda (+ execution role)** — Jake owns it. It must:
   - **Decompress + unwrap** the CloudWatch Logs payload. CW Logs does *not* send clean JSON — it sends a **gzip-compressed** envelope wrapping many events: `{ "messageType","logGroup","logStream","logEvents":[ {"id","timestamp","message"}, … ] }`. The Lambda gunzips, iterates `logEvents`, and takes each `message` (that's our telemetry JSON line). (AWS blueprint: `kinesis-firehose-cloudwatch-logs-processor` is a reference, but here it writes to S3 directly.)
   - **Build the partition key** from each record: `raw/service=<record.service>/dt=<YYYY-MM-DD>/…`. Start `dt` from arrival/ingest date (simple); only switch to the record's own `timestamp` field (event-time) if the §6b separate-day split needs it.
   - **`put_object`** newline-delimited JSON to the bucket (look it up from SSM `/anomalypulse/telemetry/bucket/name` or pass as env var). Execution role needs `s3:PutObject` on `arn:…:<bucket>/raw/*` + basic execution (logs). Consider a distinct `errors/` prefix for malformed/undecodable records so a bad batch isn't silently dropped.
3. **SubscriptionFilters** (`AWS::Logs::SubscriptionFilter`), one per log group. `DestinationArn` = shipper Lambda ARN. `FilterPattern` should restrict to *our* telemetry lines (e.g. a JSON pattern like `{ $.request_id = * }`) so Lambda's own `START`/`END`/`REPORT`/init noise isn't shipped. **IAM note:** a filter → Lambda destination does **not** use a `RoleArn`; instead grant CloudWatch Logs permission to invoke the Lambda via an `AWS::Lambda::Permission` (principal `logs.<region>.amazonaws.com`, `SourceArn` the log group). This is why the role count stays at one.
4. **Deploy, then fire a request** at a deployed service and confirm an object appears under `raw/service=…/dt=…/`. That closes the DoD box "records from ≥1 service visible in S3 under the documented partitions."

**Then deliverable 5 (contract doc):** one page — field list, tiers, the ms-timestamp + `request_id` discipline, the S3 layout, and a note that **the canonical schema lives in the layer** (`layers/telemetry/python/telemetry_schema.json`) so teammates don't rely on word-of-mouth.

---

## 5. File locations & how to run things

```
AnomalyPulse/
├─ infra/shared/
│  ├─ template.yaml          ← TelemetryLayer + TelemetryBucket + Globals attach + SSM params
│  ├─ field_catalog.json     ← DONE (all fields, tier-tagged)
│  └─ samconfig.toml
└─ layers/telemetry/
   ├─ Makefile               ← copies python/. then python3 -m pip install (arm64 cross-build)
   ├─ requirements.txt       ← jsonschema ONLY
   ├─ test_schema_validator.py  ← 36 tests; lives HERE (layer root), NOT in python/, so the
   │                             Makefile's `cp -r python/.` doesn't ship it into the runtime layer
   └─ python/
      ├─ telemetry_schema.json   ← DONE (record schema; canonical copy)
      └─ schema_validator.py     ← DONE (check_schema at import; validate; validate_or_raise)
```

- **Run the validator tests:** they need `jsonschema` + `pytest`. There's no project venv; the prior session used a throwaway venv (`python3 -m venv … && pip install jsonschema pytest`) then `python -m pytest AnomalyPulse/layers/telemetry/test_schema_validator.py -q`. The test adds `python/` to `sys.path` itself, so it runs from anywhere. Make sure `jsonschema` is in whatever dev/CI env runs tests.
- **Validate infra:** `cd AnomalyPulse/infra/shared && sam validate --lint` (always `--lint`).
- **Build/verify the layer:** `sam build`, then check `.aws-sam/build/TelemetryLayer/python/` has `schema_validator.py`, `telemetry_schema.json`, **and** `jsonschema`/`rpds` (with `rpds*.so` = `aarch64-linux-gnu`).

---

## 6. Git state

- Branch **`m2-1_jake`**, based on `main`, **clean working tree, fully pushed** to `origin/m2-1_jake` (0 ahead / 0 behind at handoff).
- Recent commits this session: `3f27db3 Provisioning S3 bucket to hold telemetry`, `371ec66 Adjusting Makefile error`, `b611c79 Building and testing telemetry schema validator`, `6dc863d Defining telemetry schema and making schema validator usable by Lambdas`.
- **Convention:** branch-per-ticket → PR to `main`. Jake owns `infra/` and deploys the single shared stack; teammates never run `sam deploy`. Bucket + layer are already **deployed** to AWS.

*Remember: guide, don't do (except pure SAM/CFN syntax). Jake writes it; you make him get it right — and actually verify before you claim it works.*
