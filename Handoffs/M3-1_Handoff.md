# M3-1 Handoff — working notes (DRAFT, not finalized)

Raw notes collected while building M3-1 (simulator framework + manifest). To be cleaned up when the ticket is done. Last updated 2026-10-12.

Legend: [DECIDED] = agreed by Jake. [PROPOSED] = suggested, not yet confirmed. [TBD] = not designed yet.

## Build order / status

1. Experiments table (lock) — DONE, deployed
2. Fault-config table + read access for the four service roles — DONE, deployed. Reader module in the telemetry layer (`fault_config.py`) — DONE; test suite `layers/telemetry/test_fault_config.py` (47 tests, all passing; needs pytest + boto3, no AWS access)
3. Simulator IAM role + `experiments/` prefix permissions + samconfig capability change — DONE, deployed. The first deploy was missing the fault-config permissions (typo when the policies were typed in); fixed in commit `2f7a002`, redeployed 2026-10-12, and verified on the deployed role by policy simulation the same day.
4. Manifest schema — DONE. Schema file `AnomalyPulse/simulator/manifest_schema.json`, the build / validate / write code `AnomalyPulse/simulator/manifest.py`, and `AnomalyPulse/simulator/test_manifest.py` (149 tests, all passing; no AWS access).
5. Injector interface, registry, no-op injector — WRITTEN 2026-10-12, with the simulator side of the fault control plane: `simulator/control_plane.py`, `simulator/injectors/` (`base.py`, `__init__.py`, `noop.py`), `test_control_plane.py` (21 tests), `test_injectors.py` (74 tests). All passing. Design is [PROPOSED] until Jake and the injector owners confirm it (see "Injector interface").
6. Runner + CLI — WRITTEN 2026-10-12: `simulator/lock.py`, `stack.py`, `runner.py`, `cli.py`, `__main__.py`, with `test_lock.py` (13), `test_runner.py` (44), `test_cli.py` (31). Whole package: 332 tests, all passing, none reaching AWS. NOT YET RUN AGAINST THE DEPLOYED STACK — the no-op end-to-end run on the real stack (first Definition-of-Done item) is still to do.
7. Interface doc, teardown doc, final handoff — not started (tests were written alongside each step)

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
- Implemented in `simulator/lock.py` [written 2026-10-12]: the item also carries `held_by` (the manifest's `created_by`); `expires_at` is epoch SECONDS = planned end of the run + 300 s. A lock is still held at the second it expires and can be taken over from the next second. `release()` returns False instead of deleting when the lock has changed hands, and the runner then warns that two experiments overlapped.
- The lock tests run on moto, which evaluates the condition expressions; a stub would only compare request parameters.

## Manifests [DECIDED]

- Location: `experiments/<experiment_id>/manifest.json` in the existing telemetry bucket (`anomalypulse-telemetry-<account>-<region>`, name in SSM `/anomalypulse/telemetry/bucket/name`). Prefix-scoped IAM, same pattern as `ShipperRole`.
- Manifests are pure Tier-2. Nothing in them may ever reach the feature matrix.
- Shape: a LIST of fault activations (injector, resolved params, start, end) plus the top-level intended label.
  - single incident = list of one
  - cascade / confounder = overlapping entries
  - this is what gives M3-5 / M4-4 the "which faults are concurrently active over time" record without a schema change later
- Fields required by the milestone: `experiment_id`, incident type, start/end, severity, fault-injection params, ground-truth label, telemetry pointer.
- `experiment_id` is timestamp-based: `exp-<YYYYMMDD>T<HHMMSS>Z-<4 hex>`. M4's registry assigns the sequence number. Don't bake a 1–1000 integer into the id.
- Schema file: `AnomalyPulse/simulator/manifest_schema.json` (JSON Schema draft 2020-12, same style as `telemetry_schema.json`).

Manifest shape [DECIDED 2026-10-12]:

```json
{
  "schema_version": "1.0",
  "experiment_id": "exp-20261012T041500Z-3f9a",
  "status": "COMPLETED",
  "incident_type": "DEPENDENCY_FAILURE",
  "severity": "HIGH",
  "seed": 20261012,
  "created_by": "jake",
  "started_at": "2026-10-12T04:15:00.000Z",
  "ended_at": "2026-10-12T04:30:00.000Z",
  "workload": { "request_rate_rps": 20 },
  "activations": [
    {
      "fault_id": "exp-20261012T041500Z-3f9a#0",
      "injector": "payment-failure",
      "target": "payment-service",
      "intensity": 0.8,
      "band": { "parameter": "timeout_ms", "low": 2500, "high": 3500 },
      "params": { "timeout_ms": 3300 },
      "observed": {},
      "active_from": "2026-10-12T04:20:00.000Z",
      "active_until": "2026-10-12T04:25:00.000Z"
    }
  ],
  "telemetry": {
    "bucket": "anomalypulse-telemetry-<account>-us-east-2",
    "prefixes": ["raw/", "metrics/"]
  }
}
```

- NORMAL run: `incident_type: "NORMAL"`, `severity: null`, empty `activations`. Telemetry slice = everything between `started_at` and `ended_at` under those prefixes.
- timestamps are ISO strings in the same format as telemetry (ms precision, UTC, `Z`). The control plane keeps epoch ms; the framework converts at that one boundary.
- written twice: once at start with `status: "RUNNING"`, again at the end with the final status, so a crashed run leaves evidence (M4 resume needs it)
- `severity` derived from intensity, not typed in; highest across activations. Thirds of the 0–1 range: LOW below 1/3, MEDIUM below 2/3, HIGH otherwise.
  - [DECIDED 2026-10-12] severity stays band-relative. Intensity is a position WITHIN a band, so 0.9 in a 2×–4× band is HIGH and 0.1 in a 6×–8× band is LOW. Don't use severity to compare magnitude across bands; read `params` for that.
  - why not the native value: the framework would need a full parameter range per incident type on top of the band, and nothing downstream uses severity as a label or a feature.
- each activation records the band it was drawn from, so M4 can prove from the manifest alone that no test-band value was used in training
- `incident_type` is the INTENDED experiment-level label (affects Melisa / M4-4, Linu / M4-3):
  - a confounder run (§6b Control 3) is a single-fault type with two activations; a cascade is `CASCADING_FAILURE`
  - the activations alone cannot tell those apart (both are ≥2 overlapping entries), so window labeling must read `incident_type` before applying "≥2 concurrent faults → `CASCADING_FAILURE`"
  - the validator deliberately does NOT require a single-fault type to have exactly one activation
- enforced like telemetry: JSON Schema file with `additionalProperties: false`, validated before every write. `params` and `workload` are the only open objects (owned by injector / workload authors).
- code lives in a new `AnomalyPulse/simulator/` package

Schema details — choices made while writing the file [PROPOSED until Jake confirms]:

- `status` values: `RUNNING`, `COMPLETED`, `FAILED` (the runner hit an error), `ABORTED` (the operator interrupted it)
- `ended_at` is null while `RUNNING` and a timestamp for every final status (schema-enforced both ways)
- `NORMAL` ⇔ `severity: null` and no activations; every other type needs a severity and at least one activation (schema-enforced)
- `target` is one of telemetry's four `service` values, or null when the fault has no single service target (a traffic spike)
- `seed` is an integer from 0 to 2^32−1 (numpy's limit)
- `telemetry.prefixes` entries must end in `/`
- every key is required; `ended_at`, `severity` and `target` are nullable but must be present (same convention as telemetry)
- [PROPOSED, added in step 5] each activation has an open `observed` object next to `params`. `params` is what was asked for and repeats from the seed; `observed` is what the injector measured (e.g. M3-3's throttle count) and does not. Empty until the run ends. Without it the measured throttle rate would have to go in `params`, and two runs with the same seed would stop recording the same config.

Checked in `manifest.py` because the schema cannot express them:

- `ended_at` after `started_at`; `active_from` before `active_until`; `active_from` not before `started_at`
- `active_until` not after `ended_at` — for `COMPLETED` runs only. A `FAILED` or `ABORTED` run keeps the windows it planned, which can run past `ended_at`; otherwise an interrupted run could not write its final manifest. [PROPOSED]
- each `fault_id` starts with this manifest's `experiment_id`, and no two are the same
- `band.low <= band.high`
- `severity` matches the highest intensity
- every timestamp is a real date and time (the schema's pattern only checks the shape)
- the whole manifest is JSON-serializable: a `Decimal` read back from DynamoDB, or NaN / Infinity, inside `params` or `workload` is a validation error instead of a crash at write time

`manifest.py` functions (needs `jsonschema` installed locally):

- `to_iso(epoch_ms)` / `to_epoch_ms(iso)` — the one place epoch ms and ISO strings meet
- `new_experiment_id(now_ms=None)` — the 4-hex suffix comes from `secrets`, so it never draws from the seeded RNG
- `severity_for(intensities)`
- `fault_id(experiment_id, index)` — `<experiment_id>#<index>`; `INCIDENT_TYPES` — the six labels, read from the schema
- `build_activation(experiment_id, index, *, injector, target, intensity, band, params, active_from_ms, active_until_ms, observed=None)`
- `build_manifest(*, experiment_id, incident_type, seed, created_by, started_at_ms, workload, activations, bucket, prefixes=("raw/", "metrics/"))` — returns the `RUNNING` manifest; severity is derived, never passed in
- `finalize(manifest, status, ended_at_ms)` — returns a copy with the final status and end time
- `validate(manifest)` → `(valid, [messages])`; `validate_or_raise(manifest)` raises `ManifestError`
- `write_manifest(s3_client, bucket, manifest)` — validates, then `PutObject` to `experiments/<experiment_id>/manifest.json`; returns the key. The S3 client is passed in, so the runner builds it from the `sim` profile's session.

Package layout and tests:

- `AnomalyPulse/simulator/` is a Python package (it has an `__init__.py`), unlike the service directories. Import by name from `AnomalyPulse/`: `from simulator import manifest`. Injectors will live in a sub-package, which is why it is a package.
- `requirements.txt` = runtime (`boto3`, `jsonschema`); the simulator is a local CLI, so boto3 has to be installed. `requirements-dev.txt` = those plus `pytest` and `moto`.
- run the tests from `AnomalyPulse/simulator/`: `pip install -r requirements-dev.txt`, then `python -m pytest -q`. No test reaches AWS or needs credentials: calls are stubbed with botocore's `Stubber`, or answered by moto for the lock, runner and CLI tests.

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
- consequence for injector authors: a control-plane injector SCHEDULES a fault slightly in the future; it does not switch it on "now". Stop deletes the item: on the normal path the window has already closed and this only tidies the table; on abort it is what ends the fault (within one cache lifetime).
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

Writer (simulator side, `simulator/control_plane.py`) [written 2026-10-12]:

- `ControlPlane(table)` over a boto3 Table resource: `schedule(service=, fault_id=, injector=, params=, active_from_ms=, active_until_ms=)`, `cancel(service, fault_id)`, `cancel_experiment(experiment_id)`, `cancel_all()`
- `schedule()` REFUSES (ValueError, nothing written) a fault that starts less than `MIN_LEAD_MS` = 10 s from now, and a `service` that is not one of the four. Either would give a run whose manifest describes a fault the application did not see that way.
  - why 10 s: the reader's cache lives 5 s; the rest covers its eventually consistent read, one failed refresh, and laptop/Lambda clock skew
- floats in `params` are stored as Decimals (boto3 refuses floats); the reader's `_plain()` turns them back. A contract test feeds the writer's item to the real reader and checks the window and the number types.
- `expires_at` = end of window + 1 hour, in epoch seconds
- `cancel_experiment` / `cancel_all` query the four service partitions (the role has no Scan) with a consistent read; they are what a cleanup command will use

## botocore retry setting gotcha (affects Melisa / Rachel, M3-4)

- In a botocore `Config`, `retries={"max_attempts": N}` means N RETRIES, i.e. N+1 total attempts. "No retries" is `retries={"total_max_attempts": 1}` (or `{"max_attempts": 0}`).
- Measured 2026-10-11 against a local server that accepts the connection and never replies (boto3 1.43): `max_attempts: 1` → 2 attempts, `total_max_attempts: 1` → 1 attempt. Same for the DynamoDB and Lambda clients. Read timeouts ARE retried.
- `services/order/handler.py` built its payment-invoke client with `read_timeout=6, retries={"max_attempts": 1}` and a comment saying "No retries". By the measurement above that was one retry. FIXED in PR #62 (merged to main 2026-10-12, also merged into `m3-1_jake`): now `retries={"total_max_attempts": 1}`, pinned by `test_payment_invoke_client_never_retries`. LIVE since the 2026-10-12 deploy.
- INFERRED, not reproduced on the deployed stack: with a hung payment (the timeout fault) that is ~6 s + backoff + ~6 s, past Order's 10 s Lambda timeout. Order would be killed before it emits its telemetry line, instead of returning the clean 502 with `dependency_error=true` that the comment describes, and payment would be invoked twice per order. This is what the old setting would have done; still worth confirming the clean 502 on the deployed stack when the payment-timeout injector is validated (M3-4).
- Behaviour change from the fix: transient payment-invoke failures the retry used to hide (e.g. Lambda throttling during a traffic spike) now surface as 502s with `dependency_error=true`.

## Simulator IAM role [DECIDED]

- Role name `anomalypulse-simulator`, defined in template.yaml (`SimulatorRole`). ARN also in SSM at `/anomalypulse/simulator/role-arn`.
- Teammates assume it to run the simulator. The `AnomalyPulseDeveloper` permission set already allows `sts:AssumeRole` (confirmed by policy simulation), so no permission-set change is needed.
- The role limits what the simulator PROCESS can do (a buggy injector can't delete a table). It does not stop a teammate doing those things by hand with their own credentials.
- Trust policy: account root as principal + `ArnLike` on `aws:PrincipalArn` = `…:role/aws-reserved/sso.amazonaws.com/us-east-2/AWSReservedSSO_AnomalyPulseDeveloper_*`. Pattern (not exact ARN) because Identity Center re-creates the role with a new random suffix if the permission set loses all its assignments; this is the construction AWS's Identity Center guide recommends. Don't name another permission set `AnomalyPulseDeveloper_<something>` — it would match.
- `samconfig.toml` now has `capabilities = "CAPABILITY_NAMED_IAM"` (required because the role has a custom name).
- What the role may do (M3-1 framework only):
  - `ssm:GetParameter` / `GetParameters` on `/anomalypulse/*`
  - experiments table: `GetItem`, `PutItem`, `DeleteItem` (the lock)
  - fault-config table: `Query`, `PutItem`, `DeleteItem`
  - S3: `GetObject`, `PutObject` under `experiments/` only
  - nothing else. The API is public, so calling it needs no permission.
- Verified on the deployed role 2026-10-12 with `iam simulate-principal-policy` (nothing is executed): everything in the list above is allowed; S3 `GetObject`/`PutObject` under `raw/`, `Scan`/`GetItem` on the products table, and `DeleteTable` on the fault-config table are denied.
- RULE for injector owners: each injector's PR adds exactly the extra permissions it needs to this role (e.g. `dynamodb:DescribeTable` / `UpdateTable` or direct table reads for M3-3, `cloudwatch:GetMetricStatistics` to record the measured throttle rate). Jake reviews and deploys.
- No lifecycle rule on `experiments/` on purpose: manifests are tiny and are the ground truth, so they must not expire.

How to use it (one-time, in `~/.aws/config`):

```ini
[profile sim]
role_arn = arn:aws:iam::<account-id>:role/anomalypulse-simulator
source_profile = <your-profile>
region = us-east-2
```

- check: `aws sts get-caller-identity --profile sim` shows `assumed-role/anomalypulse-simulator`
- check: `aws dynamodb scan --table-name anomalypulse-products --profile sim` is denied
- sessions assumed this way last one hour; the CLI and boto3 renew them automatically when the profile is set up as above (matters for long M4 batches only if someone exports credentials by hand)

## Injector interface [written 2026-10-12; PROPOSED until Jake and the injector owners confirm]

The contract is the docstrings in `simulator/injectors/base.py`. `simulator/injectors/noop.py` is the smallest example. What follows is the summary.

Adding an injector:

- one new file in `simulator/injectors/`, containing an `Injector` subclass under `@register`. No shared file to edit: the registry imports every module in the package the first time it is asked for a name.
- its tests go in `simulator/test_<name>.py`, NOT inside `simulator/injectors/`
- `@register` checks the class when the module is imported (name, incident type, parameter, targets, all steps implemented) and raises there, not mid-run. A file that fails to import fails every lookup, so a batch can't quietly run without an injector.
- `test_injectors.py` has contract tests that run against every registered injector automatically (same seed → same params; params are plain JSON; the activation fits the manifest)

Class attributes:

- `name` — registry name, lowercase with hyphens (`payment-failure`); also what the manifest and the fault-config item record as `injector`
- `incident_type` — the label a run of this injector alone gets
- `parameter` — the native parameter its band is expressed in (`timeout_ms`, `multiplier`, …). A band for another parameter is rejected.
- `targets` — the services it can be aimed at; `()` when there is no single service (traffic spike → `target: null`). With one target it is used automatically; with several the run must name one.
- `lead_ms` — how long before the window opens `start()` must be called. 0 = called as the window opens.

The steps (milestone wording: register / parameterize / start / stop):

- `configure(cls, intensity, band, rng) -> params` — class method, PURE: no AWS, no clock, no randomness except `rng`. This is what makes manifests reproducible. `native_value(intensity, band)` gives the linear position in the band.
- `start()` — begin injecting for `self.activation`'s window. Returns the real start time in epoch ms if it differed from the plan, else None.
- `stop()` — stop and undo. Called when the window closes AND on failure/abort, possibly before `start()` ran, possibly twice. Must be safe every time. Returns the real end time if it differed, else None.
- `observe() -> dict` — optional; called once after the recovery period. What was measured, recorded as `observed` in the manifest.
- the framework's half is `Injector.plan(...)`: checks intensity, band, target and window, calls `configure`, returns the `Activation`. One injector instance handles one activation; it gets `self.ctx` (`experiment_id`, `session`, `control_plane`, `workload`) and `self.activation`.

Rules for every injector (also in the `Injector` docstring):

- never hardcode a magnitude; it comes from the band
- resolved and measured values go in the manifest only (Tier-2); never into telemetry, never logged by a handler
- do not change persistent infrastructure settings (table capacity, Lambda configuration)
- `start()` / `stop()` return quickly; anything that lasts the whole window runs in a thread the injector owns

Per owner:

- Rachel (M3-4): subclass `ControlPlaneInjector`. It already implements `start` (schedule the fault-config item) and `stop` (cancel it), so payment-failure and Lambda-degradation are class attributes plus `configure`. The handler side (call `active_faults`, act on `injector` + `params`) is still M3-4.
- Josh (M3-2): traffic spike is `targets = ()`, `parameter = "multiplier"`, `lead_ms = 0`; `start`/`stop` change the request rate through `self.ctx.workload`. The workload driver's own API is Josh's to define; M3-1 only passes the object through.
- Linu (M3-3): `observe()` is where the measured throttle count goes; `self.ctx.session` is the boto3 session for CloudWatch / DescribeTable (add the permissions to the simulator role in that PR).
- Melisa (M3-5): a cascade is several activations with staggered windows, one injector instance each. No injector wraps another. The runner (step 6) will take a list of activations.

Decisions taken while writing it:

- [DECIDED 2026-10-12] a no-op run records no activation: its manifest is `incident_type: NORMAL`, `severity: null`, empty `activations`. Nothing was injected, so the manifest claims nothing.
  - consequence: a no-op run does not exercise the fault-config write path. That path has its own tests (`test_control_plane.py`) and gets its first real use in M3-4.
- [PROPOSED] the no-op injector is the `NORMAL` injector (`incident_type = "NORMAL"`). The runner still takes it through every step but leaves any `NORMAL` injector out of `activations`.
- [PROPOSED] real start/end times are reported as return values of `start()` / `stop()`; this covers the earlier requirement to record when a fault ACTUALLY became active.
- [PROPOSED] the earlier requirement "framework saves original settings so a cleanup command can restore after a crash" is DROPPED and replaced by the rule that injectors do not change persistent settings. The mechanisms chosen since then need no restore: control-plane faults expire by their window, the throttling table stays at a fixed capacity, and generated load stops when the simulator stops. An injector that really must change a setting needs a framework change first.
- No change to the simulator role or the template in this step: the control plane uses the `Query` / `PutItem` / `DeleteItem` the role already has.

## Runner and CLI [written 2026-10-12; PROPOSED until Jake confirms; not yet run on the deployed stack]

How to run it (from `AnomalyPulse/`, with the `sim` profile from "Simulator IAM role" and `pip install -r simulator/requirements.txt`):

```bash
python -m simulator start-incident --type noop --duration 60                      # a NORMAL run
python -m simulator start-incident --type <injector> --intensity 0.8 \
    --duration 300 --band <low> <high> [--target <service>]
python -m simulator start-incident ... --dry-run                                  # no AWS at all
python -m simulator cleanup                                                       # after a run that died
```

- `--type` is an injector's registry name; the label comes from the injector (`noop` → `NORMAL`). An unknown name lists the registered ones.
- `--duration` is the fault's window in SECONDS. `--baseline` and `--recovery` (seconds, default 300 each) go before and after it, so the default single-incident run is 5 + duration + 5 minutes.
- `--band LOW HIGH` is in the injector's own parameter (its name is taken from the injector). There are no default bands: M4-3 owns the real ones.
- `--seed` defaults to a random one that is recorded in the manifest, so every run can be repeated. `--created-by` defaults to the local username. `--profile` defaults to `sim`, `--region` to `us-east-2`.
- `--dry-run` prints the manifest the run would start with and opens no AWS session. This is how an injector owner checks `configure()` (same seed → same params) without spending a real run.
- exit codes: 0 completed, 1 refused (bad request, lock held, stack unreachable), 130 Ctrl-C. A failure during the run itself shows its traceback.

What one run does (`runner.run`):

1. resolves the plan into activations and builds the `RUNNING` manifest; everything that can be wrong (inputs, band, target, label vs faults, not enough baseline for a fault's lead) is refused HERE, before anything is touched
2. takes the lock (or stops: "experiment X is already running, started by Y")
3. writes the `RUNNING` manifest, starts the workload
4. calls each injector's `start()` and `stop()` on schedule: an injector with `lead_ms == 0` is started as its window opens; a control-plane injector is started 15 s before (its 10 s minimum lead + 5 s slack), which is why a control-plane fault needs at least 15 s of baseline
5. waits out the recovery, calls each `observe()`, stops the workload
6. writes the final manifest (`COMPLETED`, with real times and `observed`), releases the lock

If anything fails after the lock is taken, or on Ctrl-C, the same wind-down still runs: `stop()` on every injector not yet stopped, the workload stopped, every fault-config item of the experiment deleted (a backstop that does not rely on any injector's `stop()`), the manifest rewritten as `FAILED` / `ABORTED` with the windows it had planned, the lock released. One wind-down step failing is logged and does not stop the others.

For a run with several faults (cascade, confounder) the CLI is not enough; build a `runner.Plan` with one `runner.Fault` per activation (injector, offset, duration, intensity, band, target) and an explicit `incident_type`, and call `runner.run(plan, Stack.connect(session))`. That is the entry point for M3-5 and for M4's batch runner. Each activation gets its own RNG derived from the seed and its index, so adding a fault to a plan does not change what another one resolves.

Workload (affects Josh / M3-2):

- the runner calls three methods on the workload object: `describe()` (recorded as the manifest's `workload`), `start()`, `stop()` (safe to repeat). `runner.NoWorkload` is the stand-in that sends nothing. Everything else on the object is between the workload and the injectors, which get it as `self.ctx.workload`.
- the CLI has no way to choose a workload yet; it always runs with none. Wiring M3-2's generator into `start-incident` is part of M3-2.

Teardown / cost [to be turned into the teardown doc in step 7]:

- the simulator creates no infrastructure. A run leaves behind one manifest object (kept on purpose) and nothing else: the lock item and fault-config items are deleted at the end of every run, including failed and aborted ones.
- if the process is killed outright: fault-config items stop applying when their window ends and are garbage-collected by TTL; the lock expires 5 minutes after the run's planned end. `python -m simulator cleanup` removes both immediately. It refuses while a lock is still live (that run may be going) unless `--force`.
- the manifest of a killed run stays at `RUNNING`, which is how it is recognised as dead.

Choices made while writing it [PROPOSED]:

- default baseline and recovery of 300 s each (the per-run length is still an open M4 question, see "M4 runtime estimate")
- `--type` takes injector names (`payment-failure`), not incident-type labels
- failed and aborted runs keep their planned windows in the manifest and are told apart by `status`
- the CLI handles one injector per run; multi-fault runs go through `runner.Plan`
- no simulator role or template change was needed

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

## M4 runtime estimate (affects Josh / M4-2, Linu / M4-3)

- One experiment at a time (see "How telemetry is tied to an experiment") means M4's runs are serial. Running in parallel would need a second, separate deployment of the stack, which the single-stack decision rules out for now.
- 700 training + validation runs × ~10 minutes each ≈ 117 hours, about 5 days of back-to-back running.
- At 15 minutes each (5 baseline + 5 incident + 5 recovery, as in the manifest example above) it is ≈ 175 hours.
- The test set (§17 Phase 2 lists 350 runs) is another ≈ 58 hours at 10 minutes each, and is meant to be generated on a separate day.
- The per-run length is an assumption here, not a decision, and it is the main lever on the total. [TBD] who sets it (M4-1 / M4-3).

## Plan inconsistency to resolve

- PROJECT_PLAN §17 Phase 2 lists 350 experiments (60+60+60+45+45+30+30+20) but numbers them 701–1000, which is 300. Doesn't block M3-1.
