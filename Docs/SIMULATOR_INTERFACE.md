# AnomalyPulse Incident Simulator — Injector Interface, Manifest & Teardown

**Status:** built in M3-1; the workload and traffic spike landed in M3-2 · **Manifest schema version:** 1.0 · **Owner:** Jake (infra)

The reference for anyone writing an injector (M3-2…M3-5) or reading a manifest (M4). If you
just want to get your injector going, start with
[`Handoffs/M3-1_Handoff.md`](../Handoffs/M3-1_Handoff.md) and come back here for the details.

---

## Canonical sources (don't copy from this page into code)

| Artifact | Path | Role |
|---|---|---|
| **Injector interface** | `AnomalyPulse/simulator/injectors/base.py` | `Injector`, `ControlPlaneInjector`, `Activation`, `Context`. The docstrings are the contract. |
| **Registry** | `AnomalyPulse/simulator/injectors/__init__.py` | `@register`, `get(name)`, `names()`. |
| **Smallest example** | `AnomalyPulse/simulator/injectors/noop.py` | The no-op injector, used for NORMAL runs. |
| **Manifest schema** (enforced) | `AnomalyPulse/simulator/manifest_schema.json` | Validates every manifest before it is written. |
| **Manifest code** | `AnomalyPulse/simulator/manifest.py` | Build, validate, write; the checks the schema cannot express. |
| **Fault control plane** | `AnomalyPulse/simulator/control_plane.py` (writer), `AnomalyPulse/layers/telemetry/python/fault_config.py` (reader in every Lambda) | How a fault reaches a handler. |
| **Runner / CLI** | `AnomalyPulse/simulator/runner.py`, `cli.py` | One run from lock to final manifest. |
| **Workload** | `AnomalyPulse/simulator/workload.py` | The baseline traffic every CLI run sends (M3-2). |
| **Telemetry report** | `AnomalyPulse/simulator/telemetry_report.py` | One run's effect in landed telemetry, phase by phase. |

---

## Running an experiment

Run from **`AnomalyPulse/`** (one level above `simulator/`), on a branch that has the simulator
code, with `pip install -r simulator/requirements.txt` and the `sim` AWS profile (setup is in
the handoff). "No module named simulator" means you are in the wrong directory.

```bash
caffeinate -is python -m simulator start-incident --type noop --duration 60      # a NORMAL run
caffeinate -is python -m simulator start-incident --type <injector> --intensity 0.8 \
    --duration 300 --band <low> <high> [--target <service>]
python -m simulator start-incident ... --dry-run                 # no AWS at all; check it first
python -m simulator cleanup                                      # after a run that died
```

Four things M3-2's validation runs taught us:

- **Keep the machine awake: run every real experiment under `caffeinate -is`** (macOS; it
  stops idle sleep while on power). A sleeping laptop stops the simulator, whose timers count
  only time awake. One validation run slept through its recovery and finished 25 minutes late.
  Its traffic had stopped, but the manifest still said `COMPLETED`. The runner now fails a run
  whose steps come more than 10 s late (`FellBehind`), but a run that fails that way is lost.
  Closing the lid still sleeps the Mac, `caffeinate` or not.
- **Baseline traffic is 20 rps** (`--rps` defaults to it). M4 uses 20 rps for every run, test set
  included. Don't change the rate partway through a dataset: request rate is a feature, and a
  shift would track the generation date.
- **The stack must have PR #67's configuration** (1,769 MB functions, Product building its table at
  load). It is deployed. Until PR #67 is merged, deploying any other branch would roll it back
  and change every latency feature.
- **After a run, wait about two minutes** for the shipper, then check it with
  `python -m simulator.telemetry_report <experiment_id> --profile <your-profile>` (below).

| Option | Meaning |
|---|---|
| `--type` | An injector's registry name (`noop`, `payment-failure`, …). The run's label comes from the injector. |
| `--intensity` | 0–1, the position within the band. Not for `noop`. |
| `--band LOW HIGH` | The band, in the injector's own parameter (`timeout_ms`, `multiplier`, …). Required for every injector except `noop`; there are no default bands. |
| `--duration` | How long the fault is active, in **seconds**. |
| `--baseline`, `--recovery` | Seconds before and after the fault. Default 300 each. |
| `--target` | The service to aim at, for an injector that supports more than one. |
| `--seed` | RNG seed. Default: a random one, recorded in the manifest. The workload's request sequence uses it too. |
| `--rps` | Baseline traffic, in requests per second, sent for the whole run. Default `20`, the plan's normal rate. Every CLI run sends traffic: a run without it would have no telemetry. |
| `--users` | How many simulated users the traffic is spread over. Default `50`. |
| `--mix W W W W W` | Endpoint weights, in the order `GET /products`, `GET /products/{id}`, `POST /cart`, `GET /orders/{id}`, `POST /orders`. Default `60 15 10 10 5` (plan §7). |
| `--created-by` | Recorded in the manifest. Default: your username. |
| `--dry-run` | Print the manifest the run would start with. Opens no AWS session. |
| `--profile`, `--region` | Default `sim` and `us-east-2`. |

Exit codes: `0` completed · `1` refused (bad request, lock held, stack unreachable), or the run
fell behind schedule because the machine slept (recorded as `FAILED`) · `130` Ctrl-C. Any other
failure *during* a run shows its traceback.

**One experiment at a time.** Telemetry is tied to an experiment by time alone, so two runs
that overlap contaminate each other's labels. A lock in the `anomalypulse-experiments` table
enforces this between simulator runs: a second run is refused with the name of whoever holds
it. The lock cannot see anything else, so **don't curl the API or run smoke tests while
someone's experiment is running.**

**Runs with several faults** (a cascade, a confounder) don't go through the CLI. Build a plan
in Python:

Run the script under `caffeinate -is` too (`caffeinate -is python my_cascade.py`).

```python
import boto3

from simulator.runner import Fault, Plan, run
from simulator.stack import Stack
from simulator.workload import Workload, api_base_url

# `db-throttling` and its band parameter are placeholders for whatever M3-3 registers.
plan = Plan(
    incident_type="CASCADING_FAILURE", recovery_s=300, seed=7, created_by="melisa",
    faults=(
        Fault("traffic-spike", offset_s=300, duration_s=420, intensity=0.6,
              band={"parameter": "multiplier", "low": 2, "high": 4}),
        Fault("db-throttling", offset_s=360, duration_s=360, intensity=0.6,
              band={"parameter": "load_ratio", "low": 1.5, "high": 2.5}),
    ),
)
session = boto3.Session(profile_name="sim", region_name="us-east-2")
load = Workload(rps=20, seed=plan.seed, base_url=api_base_url(session))
manifest = run(plan, Stack.connect(session), workload=load)
```

`offset_s` is from the start of the run to that fault's window opening. Each fault gets its own
RNG, derived from the seed and its position in the list, so adding a fault never changes what
another one resolves. `runner.preview(plan, workload=load)` returns the starting manifest without
touching AWS.

**Pass a workload.** Unlike the CLI, `run()` sends no traffic unless it is given a `workload`, and
it refuses a plan containing an injector with `needs_workload` (the traffic spike) when none is
given. Give the workload the plan's seed so its request sequence repeats with the run.

### Checking a run in landed telemetry

```bash
python -m simulator.telemetry_report <experiment_id> --profile <your-profile>   # not sim
```

It prints request rate, p50/p95 latency and 4xx and 5xx share per service for each phase, plus
client-facing requests per minute. Phases are cut wherever a fault starts or stops, so an overlap
of faults in a cascade is its own phase. A run still at `RUNNING` is refused: it has no end time.
Wait about two minutes after the run ends, because the shipper lands records in batches. It needs
a profile that can read `raw/`, which the simulator role cannot.

**Check the per-minute counts.** In a healthy run they hold steady through each phase and rise
by the spike's multiplier in its window. Minutes of zeros mean the client stopped sending, and
the usual cause is that the machine slept.

### What one run does

1. Resolves the plan into activations and builds the starting manifest. **Everything that can be
   wrong is refused here, before anything is touched:** bad intensity or band, a missing target,
   a label that contradicts the faults, too little baseline for a fault's lead time, a traffic
   spike with no workload.
2. Takes the lock, or stops with "experiment X is already running, started by Y".
3. Writes the manifest with `status: RUNNING`; starts the workload.
4. Calls each injector's `start()` and `stop()` on schedule. A step that comes more than 10 s late
   (`runner.MAX_LATE_MS`) fails the run with `FellBehind`: the machine slept, and the windows in
   the manifest are no longer what happened.
5. Waits out the recovery period (the same lateness check applies); calls each `observe()`; stops
   the workload.
6. Rewrites the manifest as `COMPLETED`, with real times and what was observed; releases the lock.

If anything fails after the lock is taken, or you press Ctrl-C, the same wind-down still runs:
every injector is stopped, the workload is stopped, the experiment's fault-config items are
deleted (whatever any `stop()` did or didn't do), the manifest is rewritten as `FAILED` or
`ABORTED`, and the lock is released.

---

## The injector interface

An injector is **one file** in `AnomalyPulse/simulator/injectors/` containing a subclass of
`Injector` under `@register`. No shared file is edited: the registry imports every module in
that package the first time a name is looked up. Put tests beside the package as
`AnomalyPulse/simulator/test_<name>.py`, never inside `injectors/`.

One injector instance handles **one activation**: one fault, on one target, for one time window.
A cascade is several activations with staggered windows, each with its own instance. No
injector wraps another.

### Class attributes

| Attribute | Meaning |
|---|---|
| `name` | Registry name, lowercase with hyphens (`payment-failure`). Also what the manifest and the fault-config item record as `injector`. |
| `incident_type` | The label a run of this injector alone gets: one of the six in the project plan. An injector whose type is `NORMAL` injects nothing and is left out of the manifest. |
| `parameter` | The native parameter its band is expressed in (`timeout_ms`, `multiplier`, …). A band for a different parameter is rejected. `None` only for `NORMAL`. |
| `targets` | The services it can be aimed at. `()` when there is no single service (a traffic spike → `target: null`). With one target it is used automatically; with several, the run must name one. |
| `lead_ms` | How long before the window opens `start()` must be called. `0` (the default) means "as the window opens". |
| `needs_workload` | `True` when the fault is applied through `self.ctx.workload`, as the traffic spike's is. A run with such an injector and no workload is refused before anything is touched. Default `False`. |

`@register` checks all of this **when the file is imported** and raises there, so a mistake
shows up before any experiment runs. A file that fails to import fails *every* lookup, so a
batch can never quietly run without one of its injectors.

### The steps

| Step | When the framework calls it | What it must do |
|---|---|---|
| `configure(cls, intensity, band, rng)` | Before the run, with no AWS involved | Return the native params as a plain dict. **Pure:** no AWS calls, no clock, no randomness except `rng`. Same arguments and same seed must give the same dict. `cls.native_value(intensity, band)` gives the linear position in the band. |
| `start()` | `lead_ms` (plus 5 s slack) before the window opens; at the window's opening when `lead_ms` is 0 | Begin injecting for `self.activation`'s window. Return the epoch ms the fault *really* became active if that differs from the plan, else `None`. |
| `stop()` | When the window closes, **and** on any failure or Ctrl-C | Stop and undo. Can be called before `start()` ran, after it raised, or twice: it must be safe every time. Return the real end time if it differed, else `None`. |
| `observe()` *(optional)* | Once, after the recovery period | Return what was measured (e.g. a throttle count) as a plain dict. Recorded as `observed`. |

Inside an instance: `self.activation` (`experiment_id`, `fault_id`, `target`, `intensity`, `band`,
`params`, `active_from_ms`, `active_until_ms`) and `self.ctx` (`experiment_id`, `session` — a
boto3 session under the simulator role, `control_plane`, `workload`).

### Rules every injector follows

- **Never hardcode a magnitude.** The value comes from the band, so M4 can keep training and
  test bands apart (project plan §6b).
- **Resolved and measured values go in the manifest only.** They are Tier-2. Never put them in a
  telemetry record, and never have a handler log the fault it read.
- **Don't change persistent infrastructure settings** (table capacity, Lambda configuration).
  Nothing would restore them if the simulator crashed. The mechanisms chosen for M3 don't need
  to; if yours does, talk to Jake first.
- **`start()` and `stop()` return quickly.** Work that lasts the whole window runs in a thread the
  injector starts in `start()` and ends in `stop()`.
- **Need an AWS permission the simulator role lacks?** Add exactly that permission to
  `SimulatorRole` in `template.yaml` in your PR. Jake reviews and deploys.

### Control-plane injectors (faults a handler applies to itself)

For "make this Lambda slow or fail", subclass `ControlPlaneInjector`. It already implements
`start()` and `stop()`, so the injector is the class attributes plus `configure()`:

```python
from simulator.injectors import ControlPlaneInjector, register


@register
class PaymentFailure(ControlPlaneInjector):
    name = "payment-failure"
    incident_type = "DEPENDENCY_FAILURE"
    parameter = "timeout_ms"
    targets = ("payment-service",)

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"timeout_ms": round(cls.native_value(intensity, band))}
```

`start()` writes one item to the `anomalypulse-fault-config` table; `stop()` deletes it. The
target's handler picks it up through the reader that ships in the telemetry layer:

```python
from fault_config import active_faults

for fault in active_faults("payment-service"):
    if fault["injector"] == "payment-failure":
        ...  # act on fault["params"]
```

| Item attribute | Meaning |
|---|---|
| `service` (hash key) | Which Lambda applies it. Same values as telemetry's `service`. |
| `fault_id` (range key) | `<experiment_id>#<n>`. Matches the manifest entry. |
| `injector` | The injector's `name`. The handler decides what to do from this. |
| `params` | What `configure()` returned. |
| `active_from_ms`, `active_until_ms` | Epoch **milliseconds**. The fault applies while `active_from_ms <= now < active_until_ms`. |
| `expires_at` | Epoch **seconds**. DynamoDB TTL, for garbage collection only. |

Faults are **scheduled, not switched on**. Each Lambda execution environment caches its items
for 5 seconds, so the item is written at least 10 seconds before the window opens; by then it
is in every cache and the fault begins everywhere at the same instant. That is why a
control-plane fault needs at least 15 seconds of baseline, and why `schedule()` refuses a fault
that starts sooner. The window also closes on its own, so a crashed simulator cannot leave a
service broken.

`active_faults()` never raises, costs the same whether or not a fault exists (so its overhead
is not a fingerprint of incident runs), and returns plain numbers.

### The workload

The runner calls three methods on the workload object: `describe()` (a plain dict, recorded as
the manifest's `workload`), `start()` and `stop()` (safe to repeat). Everything else on the
object is between the workload and the injectors, which receive it as `self.ctx.workload`.
`runner.NoWorkload` is the stand-in that sends nothing; `run()` uses it when it is given no
workload.

The real one is `simulator.workload.Workload` (M3-2), and every `start-incident` run gets one.
It sends requests to the deployed API (URL from SSM, `/anomalypulse/api/base-url`) as an
**open-loop Poisson process**: arrivals keep coming at the configured rate whether or not earlier
requests have finished, so a slow service gets the same offered load, not less. The request
sequence (routes, users, items) comes from an RNG seeded with the plan's seed. If more than
`max_in_flight` (256) requests are outstanding, new arrivals are dropped and counted, not queued.

| On the workload | For |
|---|---|
| `describe()` | `generator`, `request_rate_rps`, `users`, `mix` (normalized to fractions), `max_in_flight` |
| `set_rate_multiplier(m)` | Injectors: scale the arrival rate; `1` is baseline. The traffic spike uses it. |
| `outcomes()` | Injectors: what the client has seen so far, `{"sent", "dropped", "responses": {"2xx": …, "5xx": …, "error": …}}`. A response counts when it comes back; `error` is no HTTP response at all. The traffic spike records the difference across its window. |
| `requests_sent()`, `current_rps` | Injectors: how much has been sent, and the rate right now |
| `set_retry_policy(r)`, `retry_stats()` | Injectors: resend failed requests (5xx or no response) `r` times on average, a second apart, until one succeeds; `0` turns retries off. Retry amplification (M3-5) uses them; `Docs/CASCADE.md` has the details. |

`GET /orders/{id}` reads back orders the run created. Until the first one exists (about a second
into a run) it gets a 404, as a stale link would. Which order a read picks is the one thing in
the sequence that depends on the API's answers.

A traffic spike's `observed` is the client's view of its window: `requests_sent`,
`achieved_rps`, `requests_dropped`, and `responses` by class.

---

## The manifest

One JSON object per experiment at `experiments/<experiment_id>/manifest.json` in the telemetry
bucket. **Every field is Tier-2:** nothing in a manifest may reach the feature matrix.

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

| Field | Notes |
|---|---|
| `experiment_id` | `exp-<UTC start>-<4 hex>`. Not a sequence number; M4's registry assigns those. |
| `status` | `RUNNING` when the run starts, then `COMPLETED`, `FAILED` (the runner hit an error) or `ABORTED` (Ctrl-C). **A manifest still at `RUNNING` after its run is a run that died.** |
| `incident_type` | The **intended** experiment-level label. See "Reading a manifest" below. |
| `severity` | `LOW` / `MEDIUM` / `HIGH` from the highest activation intensity, by thirds; `null` for `NORMAL`. |
| `seed` | 0 to 2³²−1. Same plan and same seed resolve the same `params`. |
| `started_at`, `ended_at` | Telemetry's timestamp format (UTC, millisecond precision, `Z`). `ended_at` is `null` while `RUNNING`. |
| `workload` | Open object: whatever the workload's `describe()` returned. |
| `activations` | One entry per injected fault; empty for `NORMAL`. |
| `activations[].band` | The band the intensity was mapped onto. M4 can prove from the manifest alone that no test-band value was used in training. |
| `activations[].params` | Open object: what was **asked for**. Repeats from the seed. |
| `activations[].observed` | Open object: what the injector **measured**. Does not repeat. Empty until the run ends. |
| `activations[].active_from`, `active_until` | The fault's window; start inclusive, end exclusive. |
| `telemetry` | Where the run's telemetry is: everything under those prefixes between `started_at` and `ended_at`. |

The schema is closed (`additionalProperties: false`) everywhere except `workload`, `params` and
`observed`, and every key is required (nullable keys are present with `null`). On top of the
schema, `manifest.validate()` checks: the run ends after it starts; each window has a positive
length and does not open before the run; a `COMPLETED` run's windows end inside it; each
`fault_id` belongs to this experiment and is unique; `band.low <= band.high`; `severity`
matches the intensities; a `CASCADING_FAILURE` run has two or more faults active at the same
time; every timestamp is a real date; and the whole thing is JSON-serializable
(a `Decimal` straight from DynamoDB, or NaN, is a validation error rather than a crash mid-write).

### Reading a manifest (M4)

- **Slice telemetry by time.** Requests are not tagged with `experiment_id`: the telemetry schema
  is frozen, and the id maps 1:1 to the label, so it would be a leak.
- **Label windows from `activations`, but read `incident_type` first.** A cascade and a
  confounder (project plan §6b, Control 3) both have two or more overlapping activations. They
  differ only in the top-level label: a cascade is `CASCADING_FAILURE`; a confounder is a
  single-fault type with two activations. Apply "two or more concurrent faults →
  `CASCADING_FAILURE`" only when `incident_type` is `CASCADING_FAILURE`.
- **Use only `COMPLETED` runs.** A `FAILED` or `ABORTED` manifest keeps the windows it had
  *planned*, which may run past `ended_at`.
- **Use only runs M4's registry launched.** Every manifest written before M4 is a test or
  validation run. None of them ran on M4's settings (20 rps, PR #67's configuration, the
  machine kept awake), so none belong in the dataset:

  | Manifest | Why it is excluded |
  |---|---|
  | `exp-20261012T054951Z-ec18`, `exp-20261013T162018Z-bdcd` | `workload` is `{}`: no traffic, so no telemetry behind the label |
  | `exp-20261013T025625Z-a826`, `exp-20261013T030821Z-9aed` | Before PR #67: 128 MB functions, slow Product cold starts, an account limit of 10 |
  | `exp-20261014T230012Z-2427` | 5 rps; the laptop slept through its recovery (it still says `COMPLETED`); 10× is a test-band value |
  | `exp-20261015T000130Z-85b4` | 10× is a test-band value; it was the 20 rps capacity check |

  A run whose `workload` is `{}` never belongs, whenever it was made.
- **Check every run's `workload.request_rate_rps` is 20.**
- **Deduplicate records by `(service, request_id)`.** The shipper delivers at least once: one
  M3-2 validation run landed 43 records twice, byte for byte.
- **A run's first minute differs from the rest of its baseline.** For the first second or so,
  `GET /orders/{id}` gets 404s (no order exists yet). After an idle gap, the run also starts on
  cold functions: at 20 rps the first minute had 9 Product cold starts and peaked at 18
  concurrent executions, against 5–9 later. Back-to-back runs start warm. Dropping each run's
  first minute (M4-4) costs one of its five baseline windows and keeps every run's baseline alike.
- **`severity` is relative to the band**, not to the native value: 0.9 in a 2×–4× band is `HIGH`,
  0.1 in a 6×–8× band is `LOW`. To compare magnitudes across bands, read `params`.
- **A `NORMAL` run has no activations**, including a run of the `noop` injector.

---

## Teardown and cost

The simulator creates **no infrastructure**. It uses two small on-demand tables and one S3
prefix that the shared stack already owns.

| What a run writes | What happens to it |
|---|---|
| The manifest, `experiments/<id>/manifest.json` | **Kept on purpose.** It is the ground truth, it is tiny, and the `experiments/` prefix has no lifecycle rule. |
| The lock item | Deleted at the end of every run, including failed and aborted ones. |
| Fault-config items | Deleted at the end of every run, including failed and aborted ones. |

So after a run that ended on its own, in any status, **there is nothing to tear down.**

If the process was killed outright (`kill -9`, a closed laptop):

- Fault-config items stop applying when their window ends, and DynamoDB's TTL removes them
  later. No service stays broken.
- The lock expires 5 minutes after the run's planned end; until then new runs are refused.
- The manifest stays at `RUNNING`, which marks the run as dead. Don't use its telemetry.

To clear both immediately:

```bash
python -m simulator cleanup
```

It deletes every fault-config item and releases the dead run's lock. It **refuses while a lock
is still live**, because that run may be going and deleting its faults would silently ruin its
labels. `--force` overrides that; only use it when you know the run is dead.

**Cost of the simulator itself** is a handful of DynamoDB and S3 requests per run. The real
cost of an experiment is the traffic the workload sends and whatever an injector drives
(Lambda invocations, DynamoDB capacity), which each injector owner documents with their
injector.

**The workload's traffic** is the larger part. A default 15-minute run at 20 rps sends about
18,000 requests. At list price, API Gateway ($3.50 per million REST requests) makes up most of
the roughly $0.07 per run, and Lambda, CloudWatch Logs and DynamoDB add about a cent between
them. A traffic spike multiplies the traffic in its window: the 10× capacity check sent 72,000
requests, about $0.30. By estimate, all of M4's runs come to roughly $100. Nothing keeps sending
after a run ends: the workload lives in the simulator process.

### Capacity

**The account allows 1,000 concurrent Lambda executions** (raised from 10 on 2026-10-14), shared
by every function, the shipper included. With PR #67's configuration, the 20 rps capacity check
(`exp-20261015T000130Z-85b4`, a 10× spike to 200 rps) had:

- **No throttles anywhere.** Peak concurrency was 55 during the spike and 5–9 at baseline.
- **A client that kept up:** it sent 200.04 rps, and 200.01 rps landed, with nothing dropped and
  no errors.
- **A clean telemetry signature:** Product's p95 latency went from 11 to 22 ms during the spike.
  Product's 42 cold starts took 35 ms at the median and 87 ms at most.

**Not tested above 200 rps.** Only the extreme-extrapolation slice (more than 10×) goes higher.
Check the client keeps up before generating it: compare a spike's `observed.achieved_rps` with
what was asked for.

**What throttling looks like, if it ever comes back.** A client request that Lambda throttles
never reaches its handler, so it leaves **no telemetry record**. Only the client's 5xx count
sees it, in a spike's `observed.responses`. A throttled Order-to-Payment invoke is worse for
labels: Order records a 502 with `dependency_error: true`, which reads like a dependency
failure. At the old limit of 10, a cold 20 rps run lost 110 requests in its first minute, and a
3× spike lost about 0.3% of its window.

---

## Tests

```bash
cd AnomalyPulse/simulator
pip install -r requirements-dev.txt
python -m pytest -q
```

No test reaches AWS or needs credentials. `test_injectors.py` includes contract tests that run
against **every registered injector** (same seed → same params; params are plain JSON; the
activation fits the manifest), so a new injector file is checked as soon as it exists.

---

## Why it is built this way

- **Faults come from a table, not from Lambda environment variables.** Changing an environment
  variable recycles every warm execution environment, so each incident would begin and end with
  a cold-start burst that normal runs never have. `cold_start` is a Tier-1 feature; the model
  could learn that fingerprint instead of the incident.
- **The table is DynamoDB, not SSM Parameter Store.** Parameter Store reads default to 40 per
  second account-wide, which a traffic spike across many execution environments could hit.
- **Faults are time windows, not on/off switches.** Onset is simultaneous everywhere, recovery
  is exact, and a crash cannot leave a fault on.
- **`params` and `observed` are separate.** A measured throttle rate stored with the inputs
  would make two runs of the same seed record different "config".
- **Telemetry is tied to a run by time, not by a tag,** for the leak reason above. The price is
  one experiment at a time on the shared stack.
- **Injectors don't change persistent settings.** That is what lets teardown be "nothing to do".
