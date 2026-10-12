# M3-1 Handoff — working notes (DRAFT, not finalized)

Raw notes collected while building M3-1 (simulator framework + manifest). To be cleaned up when the ticket is done. Last updated 2026-10-09.

Legend: [DECIDED] = agreed by Jake. [PROPOSED] = suggested, not yet confirmed. [TBD] = not designed yet.

## Build order / status

1. Experiments table (lock) — in template.yaml on branch `m3-1_jake`, lints clean, not yet confirmed deployed
2. Fault-config table + read access for the four service roles — in template.yaml, lints clean, not yet confirmed deployed. Reader module in the telemetry layer (`fault_config.py`) — in progress; test suite written (`layers/telemetry/test_fault_config.py`, 47 tests, needs pytest + boto3, no AWS access)
3. Simulator IAM role + `experiments/` prefix permissions + samconfig capability change — not started
4. Manifest schema — not started
5. Injector interface, registry, no-op injector — not started
6. Runner + CLI — not started
7. Tests, interface doc, teardown doc — not started

## Working assumptions [DECIDED]

- Everything stays in the single `anomalypulse-shared` stack (us-east-2). No separate simulator stack.
- Simulator is a local Python/boto3 CLI run from a laptop.
- `--intensity` is normalized 0–1 and mapped onto a band supplied by config. Bands are config, never hardcoded in an injector (Linu's M4-3 defines the real bands).
- Manifest records the normalized intensity, the resolved native magnitude (multiplier / ms / ratio), and the RNG seed. This is what makes "same params → same recorded config" true.

## How telemetry is tied to an experiment [DECIDED]

- Time-slicing. The manifest stores a pointer: bucket, prefixes, start/end timestamps. M4 slices `raw/` (and `metrics/`) by time.
- Requests are NOT tagged with `experiment_id`. Telemetry schema is frozen (`additionalProperties: false`) and `experiment_id` maps 1:1 to the label, so it would be a leak.
- Shipper still writes `raw/service=<svc>/dt=<date>/…` exactly as in M2. No pipeline change.
- Consequence: only ONE experiment can run at a time on the shared stack. Two overlapping runs put each other's traffic/faults in both windows and the labels are silently wrong.

## Experiment lock [DECIDED]

- DynamoDB table `anomalypulse-experiments`, `PAY_PER_REQUEST`, hash key `pk` (S). Name published to SSM at `/anomalypulse/tables/experiments/name`.
- Stays on-demand on purpose: a lock table must never throttle.
- Lock = one item with `pk = "LOCK"`. Acquire = create it, release = delete it.
- Acquire is a conditional put: `ConditionExpression = attribute_not_exists(pk) OR expires_at < :now`. DynamoDB checks and writes as one server-side operation, so exactly one of two simultaneous callers wins. Loser gets `ConditionalCheckFailedException` → CLI reports "experiment X already running, held by Y".
- Plain GetItem-then-PutItem is NOT safe (two laptops can both see nothing and both write).
- Item carries `experiment_id` and `expires_at` (planned end + margin) so a crashed run's lock can be taken over once stale.
- Release is a conditional delete (`experiment_id = :mine`) so you can't delete a lock someone else took after yours expired.
- The lock is advisory. It only stops simulator runs. Curling the API or running smoke tests during someone's experiment still pollutes their window — don't hit the deployed API while a run is in progress.
- Key is a generic `pk` (not `experiment_id`) because the lock row isn't an experiment. [PROPOSED] M4-1 registry rows live in the same table, e.g. `pk = EXP#<id>`.

## Manifests [DECIDED]

- Location: `experiments/<experiment_id>/manifest.json` in the existing telemetry bucket (`anomalypulse-telemetry-<account>-<region>`, name in SSM `/anomalypulse/telemetry/bucket/name`). Prefix-scoped IAM, same pattern as `ShipperRole`.
- Manifests are pure Tier-2. Nothing in them may ever reach the feature matrix.
- Shape: a LIST of fault activations (injector, resolved params, start, end) plus the top-level intended label.
  - single incident = list of one
  - cascade / confounder = overlapping entries
  - this is what gives M3-5 / M4-4 the "which faults are concurrently active over time" record without a schema change later
- Fields required by the milestone: `experiment_id`, incident type, start/end, severity, fault-injection params, ground-truth label, telemetry pointer.
- [PROPOSED] `experiment_id` is timestamp-based; M4's registry assigns the sequence number. Don't bake a 1–1000 integer into the id.
- Exact schema [TBD] (step 4).

## Fault control plane (affects Rachel / M3-4) [DECIDED]

- Handlers will read fault config from a control plane that the framework writes (cached a few seconds), instead of the simulator flipping Lambda env vars.
- Why not env vars: changing `PAYMENT_LATENCY_MS` / `PAYMENT_FAILURE_RATE` / `PAYMENT_TIMEOUT` means `UpdateFunctionConfiguration`, which recycles every warm execution environment. Every payment or Lambda-degradation incident would then start AND end with a cold-start burst that NORMAL runs never have. `cold_start` is a Tier-1 feature, so the classifier could learn that simulator fingerprint — exactly what §6b is meant to prevent.
- Second reason: a run that dies before teardown leaves the stack drifted from the template.
- Cost: more SAM (the resource + read grants on each service role) and a handler change in Rachel's M3-4 — payment's knobs move from env vars to the control-plane read.
- Still true: injected values are Tier-2. They go in the manifest only, never in a telemetry record. Handlers must never print/log the fault items they read.

Store:

- DynamoDB table `anomalypulse-fault-config`, `PAY_PER_REQUEST`. Name in SSM at `/anomalypulse/tables/fault-config/name` and in each service Lambda's env as `FAULT_CONFIG_TABLE`.
- Separate from the experiments table so service roles never touch the lock. Each of the four service roles gets `dynamodb:Query` on it and nothing else.
- Not SSM Parameter Store: its reads are capped at 40 TPS account-wide by default (checked in AWS docs), which a 200 rps spike with many execution environments refreshing could hit.

Item = one fault activation:

- `service` (S, hash key) — which Lambda applies it; same values as telemetry's `service` enum
- `fault_id` (S, range key) — unique per activation, e.g. `<experiment_id>#<n>`; matches the manifest entry
- `injector` (S) — name of the injector that wrote it
- `params` (M) — the resolved knobs; opaque to the framework. Rachel defines the vocabulary in M3-4.
- `active_from_ms`, `active_until_ms` (N) — epoch MILLISECONDS. Fault applies while `active_from_ms <= now < active_until_ms` (start inclusive, end exclusive).
- `expires_at` (N) — epoch SECONDS. DynamoDB TTL attribute; deletion is lazy (can take days), it is only garbage collection.

Why time windows instead of on/off:

- onset and recovery are exact and simultaneous across every execution environment, however old each one's cache is — PROVIDED the simulator writes the item at least one cache lifetime (default 5 s) before `active_from_ms`
- faults end on their own, so a crashed simulator cannot leave a service broken
- consequence for injector authors: a control-plane injector SCHEDULES a fault slightly in the future; it does not switch it on "now". Normal-path stop is a no-op; abort = delete the item (takes effect within one cache lifetime).
- two overlapping faults on one service = two items, no read-modify-write

Reader (in the telemetry layer, `fault_config.py`, ships to every Lambda automatically):

- `active_faults(service: str, now_ms: int | None = None) -> list[dict]`
- never raises; on a failed refresh serves the last cached items (they still carry their own windows), else `[]`
- module-level cache per execution environment, refreshed at most every `FAULT_CONFIG_TTL_SECONDS` (default 5); a FAILED refresh also resets the timer so a broken control plane costs one short call per interval, not one per request
- same code path whether or not a fault exists → identical overhead in NORMAL and incident runs (not a fingerprint)
- ~1 s timeouts, no retries on its DynamoDB client: `Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1})`
- returns plain int/float, never `Decimal` (`time.sleep(Decimal)` raises on 3.13 and `json.dumps` can't serialize it)
- `FAULT_CONFIG_TABLE` unset → `[]`, so local tests work without AWS
- [TBD] each handler needs a call to pick faults up; who adds it per service and what the per-`injector` behaviours are is M3-4.

## botocore retry setting gotcha (affects Melisa / Rachel, M3-4)

- In a botocore `Config`, `retries={"max_attempts": N}` means N RETRIES, i.e. N+1 total attempts. "No retries" is `retries={"total_max_attempts": 1}` (or `{"max_attempts": 0}`).
- Measured 2026-10-11 against a local server that accepts the connection and never replies (boto3 1.43): `max_attempts: 1` → 2 attempts, `total_max_attempts: 1` → 1 attempt. Same for the DynamoDB and Lambda clients. Read timeouts ARE retried.
- `services/order/handler.py` built its payment-invoke client with `read_timeout=6, retries={"max_attempts": 1}` and a comment saying "No retries". By the measurement above that was one retry. FIXED in PR #62 (merged to main 2026-10-12, also merged into `m3-1_jake`): now `retries={"total_max_attempts": 1}`, pinned by `test_payment_invoke_client_never_retries`. NOT live until the next deploy.
- INFERRED, not reproduced on the deployed stack: with a hung payment (the timeout fault) that is ~6 s + backoff + ~6 s, past Order's 10 s Lambda timeout. Order would be killed before it emits its telemetry line, instead of returning the clean 502 with `dependency_error=true` that the comment describes, and payment would be invoked twice per order. This is what the old setting would have done; still worth confirming the clean 502 on the deployed stack when the payment-timeout injector is validated (M3-4).
- Behaviour change from the fix: transient payment-invoke failures the retry used to hide (e.g. Lambda throttling during a traffic spike) now surface as 502s with `dependency_error=true`.

## Simulator IAM role [DECIDED in principle, details TBD]

- A dedicated simulator role defined in the template, which teammates assume to run the simulator.
- Teammates' `AnomalyPulseDeveloper` permission set is PowerUserAccess, which already allows `sts:AssumeRole`. No permission-set change needed.
- The role limits what the simulator PROCESS can do (a buggy injector can't delete a table). It does not stop a teammate doing those things by hand with their own PowerUser credentials.
- Needs a `samconfig.toml` capability change because the role is named.
- Will need at least: S3 put under `experiments/`, lock table read/write, fault-config writes, and (if the throttling injector changes capacity) `dynamodb:DescribeTable` + `dynamodb:UpdateTable` on the target table.

## Injector interface [TBD — step 5]

- Milestone asks for register / parameterize / start / stop. Injectors take `intensity` + `duration`, with bands as config.
- Requirements gathered so far:
  - must be able to record when a fault ACTUALLY became active, not just when start was called (some mechanisms are asynchronous)
  - framework saves any original settings before an injector changes them, so a cleanup command can restore after a crash
  - injected magnitudes recorded to the manifest as Tier-2

## DynamoDB throttling mechanism (affects Linu / M3-3)

Context: the `NOTE (M3)` above `ProductsTable` in template.yaml says the throttling incident needs a PROVISIONED table with low RCU/WCU.

- Quotas, checked against AWS docs on 2026-10-09:
  - provisioned → on-demand switch: max 4 times per rolling 24 hours (on-demand → provisioned any time). So flipping billing mode per experiment is OUT.
  - provisioned capacity decreases: start each UTC day with 4, +1 becomes available per hour, max 27 per day. So lowering capacity per incident caps near 27 throttling experiments/day. M4 needs 140 for training (+60 test).
- Workable reading of §5: the table sits at a FIXED low provisioned capacity permanently, and the injector varies OFFERED LOAD. Capacity never changes during a run → no quota problem, nothing to restore.
- Consequences to design around:
  - the fixed capacity must be high enough that a 10× traffic spike (and the >10× stress slice) does not throttle on its own, otherwise `TRAFFIC_SPIKE` runs carry throttling symptoms
  - DynamoDB banks up to 300 seconds of unused capacity as burst (best effort), so throttling onset can lag by minutes at low ratios — matters for a 5-minute incident
- If the injector does call `UpdateTable`: new settings only take effect once the operation completes, and they don't undo themselves if the simulator crashes.
- Plan: `ProductsTable` and that NOTE stay untouched in the M3-1 PR. The billing-mode change lands with M3-3 once Linu has sized the capacity. Jake makes the template edit (Jake owns the template / deploys).
- Still true: no `db_throttled` flag anywhere in telemetry. Throttle rate is a measured outcome from CloudWatch counts, not an input.

## Plan inconsistency to resolve

- PROJECT_PLAN §17 Phase 2 lists 350 experiments (60+60+60+45+45+30+30+20) but numbers them 701–1000, which is 300. Doesn't block M3-1.
