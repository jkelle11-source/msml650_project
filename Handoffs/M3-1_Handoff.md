# M3-1 Handoff — Incident Simulator Framework & Experiment Manifest

> **The one-sentence version:** The simulator that runs an experiment now exists — it takes a lock, records a manifest, starts and stops injectors on schedule, and cleans up after itself — and your M3 job is to add **one file containing one injector class** that says how your fault is parameterized and how it is switched on and off. Jake has built the framework, the manifest, the fault control plane and a no-op injector to copy from. The supporting tables and the simulator role are **live** in `us-east-2` (stack `anomalypulse-shared`); the simulator itself is a local package, `AnomalyPulse/simulator/`.

**Where it stands (2026-10-12):** the whole package is tested against a mocked copy of the stack (332 tests), and a no-op run has gone through the CLI on the deployed stack and left a valid manifest. One Definition-of-Done item is still open: **your confirmation that the interface is buildable against** (see §9).

---

## 1. Mental model (read this first)

**An experiment is a timed run with a known answer.** Baseline traffic, then a fault for a set window, then recovery. The simulator writes down exactly what it injected and when. M4 turns the telemetry from that window into labeled training data, so *what the simulator writes down is the ground truth for the whole project*.

**An injector is the part that knows one kind of fault.** Traffic spike, DB throttling, Lambda slowdown, payment failure. The framework handles everything around it: the lock, the schedule, the manifest, the cleanup. You never write a `main()`.

**The framework calls your injector in four steps:**

```text
configure   intensity + band + seeded RNG  ->  the params to apply      (pure, no AWS)
start       begin injecting
stop        stop injecting                                              (also on failure / Ctrl-C)
observe     after the run: what you measured, if anything               (optional)
```

**Magnitudes come from bands, never from your code.** You are handed an intensity from 0 to 1 and a band (say `timeout_ms` 2500–3500) and you resolve a value inside it. M4 keeps training bands and test bands apart; a hardcoded magnitude would break that.

**Everything you resolve or measure is Tier-2.** It goes in the manifest and nowhere else. It must never appear in a telemetry record, and a handler must never log the fault it was told to apply. Same reason as in M2: if the cause leaks into the model's input, every accuracy number is meaningless.

**One experiment at a time on the shared stack.** Telemetry is tied to an experiment by *time alone* (requests are not tagged — the tag would be the label). Two overlapping runs contaminate each other. A lock stops a second simulator run, but it can't see anything else: **don't curl the API or run smoke tests while someone's experiment is running.**

---

## 2. What you do: add one injector file

Create `AnomalyPulse/simulator/injectors/<your_injector>.py`. That's the only file the framework needs; the registry finds it by itself, so nobody edits a shared list.

**If a handler applies the fault to itself** (payment failure, Lambda slowdown), subclass `ControlPlaneInjector`. Start and stop are already written — you supply the attributes and `configure()`:

```python
from simulator.injectors import ControlPlaneInjector, register


@register
class PaymentFailure(ControlPlaneInjector):
    name = "payment-failure"                 # what you pass to --type
    incident_type = "DEPENDENCY_FAILURE"     # the label a run of this injector gets
    parameter = "timeout_ms"                 # what its band is expressed in
    targets = ("payment-service",)           # whose handler applies the fault

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"timeout_ms": round(cls.native_value(intensity, band))}
```

**If the simulator applies the fault from outside** (a traffic spike, extra load on a table), subclass `Injector` and write `start()` and `stop()` yourself:

```python
from simulator.injectors import Injector, register


@register
class TrafficSpike(Injector):
    name = "traffic-spike"
    incident_type = "TRAFFIC_SPIKE"
    parameter = "multiplier"
    # targets stays (): a spike has no single service behind it
    needs_workload = True   # its fault goes through self.ctx.workload

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"multiplier": cls.native_value(intensity, band)}

    def start(self):
        # self.ctx.workload is the workload driver (simulator/workload.py).
        self.ctx.workload.set_rate_multiplier(self.activation.params["multiplier"])

    def stop(self):
        self.ctx.workload.set_rate_multiplier(1)
```

(The real one, built in M3-2, is `simulator/injectors/traffic_spike.py`.)

Four rules that trip people up (full details in [`Docs/SIMULATOR_INTERFACE.md`](../Docs/SIMULATOR_INTERFACE.md)):

1. **`configure()` is pure.** No AWS calls, no clock, no `random` module — only the `rng` you are handed. Same seed must give the same params; that is what makes a manifest reproducible. `cls.native_value(intensity, band)` does the usual linear mapping (0.8 in 2500–3500 → 3300).
2. **`stop()` must be safe at any time.** It is called when your window closes, *and* whenever a run fails or someone presses Ctrl-C — possibly before `start()` ran, possibly twice.
3. **`start()` and `stop()` return quickly.** If your fault needs work for the whole window (sending requests, say), run it in a thread you start in `start()` and end in `stop()`.
4. **Don't change persistent settings** (table capacity, Lambda configuration). Nothing would put them back if the simulator crashed.

`@register` checks your class the moment the file is imported — a missing step, a misspelled incident type, a target that isn't a service — and tells you what's wrong before any experiment runs.

---

## 3. Try it without AWS

```bash
cd AnomalyPulse
pip install -r simulator/requirements-dev.txt

# Works today. Prints the manifest a NORMAL run would start with; touches nothing:
python -m simulator start-incident --type noop --duration 60 --dry-run

# Once your file exists: what would a run of YOUR injector record?
python -m simulator start-incident --type <your-injector> --intensity 0.8 \
    --band <low> <high> --duration 300 --seed 7 --dry-run

# The framework's tests, which also check your injector automatically:
cd simulator && python -m pytest -q
```

Run the dry run twice with the same `--seed`: the `params` must be identical. `test_injectors.py` has contract tests that run against every registered injector, so yours is covered as soon as the file exists. Put your own tests in `AnomalyPulse/simulator/test_<your_injector>.py` — **not** inside `injectors/`.

---

## 4. Running a real experiment

You need AWS access (same one-time setup as [`M1-1_Handoff.md`](M1-1_Handoff.md) §5). The simulator runs under its own role, which can only touch what the framework needs — so a buggy injector can't delete a table. Add this profile once to `~/.aws/config`:

```ini
[profile sim]
role_arn = <value of /anomalypulse/simulator/role-arn in SSM>
source_profile = <your-profile>
region = us-east-2
```

```bash
aws ssm get-parameter --name /anomalypulse/simulator/role-arn --query Parameter.Value --output text --profile <your-profile>
aws sts get-caller-identity --profile sim     # should show assumed-role/anomalypulse-simulator
```

Then, from `AnomalyPulse/`:

```bash
python -m simulator start-incident --type noop --duration 60 --baseline 30 --recovery 30   # a NORMAL run
python -m simulator start-incident --type <your-injector> --intensity 0.8 --band <low> <high> --duration 300
```

- `--duration` is the fault's window in **seconds**. `--baseline` and `--recovery` default to 300 seconds each, so the second command above is a 15-minute run.
- **Every run sends baseline traffic** for its whole length (added in M3-2): 20 requests per second in the plan's 60/15/10/10/5 endpoint mix. `--rps`, `--users` and `--mix` change it.
- To see a run's effect in landed telemetry: `python -m simulator.telemetry_report <experiment_id> --profile <your-profile>` (your own profile, not `sim`), about two minutes after the run ends.
- The run prints its experiment id and where the manifest is. Ctrl-C is safe: the run winds down and records itself as `ABORTED`.
- The role's session lasts an hour and is renewed automatically when the profile is set up as above, so long runs are fine.
- **If your injector needs an AWS permission the role doesn't have** (reading a CloudWatch metric, describing a table), add exactly that to `SimulatorRole` in `template.yaml` in your PR. Jake reviews and deploys.

---

## 5. What gets recorded

One manifest per experiment at `experiments/<experiment_id>/manifest.json` in the telemetry bucket. The part that comes from your injector:

```json
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
```

- **`params`** is what `configure()` returned: what was *asked for*. It repeats from the seed.
- **`observed`** is what `observe()` returned: what was *measured*. It doesn't repeat. This is where a throttle count goes.
- **`active_from` / `active_until`** are the planned window, unless `start()` or `stop()` returned a different real time.

Both `params` and `observed` must be plain JSON. A `Decimal` straight out of DynamoDB will be rejected — convert it to `int` or `float` first. The full manifest and its rules are in [`Docs/SIMULATOR_INTERFACE.md`](../Docs/SIMULATOR_INTERFACE.md).

---

## 6. Per-person quick start (your M3 ticket)

Everyone: branch per ticket (`m3-<n>-<name>`), add your injector file and its test file, open a PR.

- **Josh — Workload generator + traffic spike (M3-2).** Two pieces.
  1. **The workload generator.** The runner calls three methods on it: `describe()` (a plain dict of its settings, recorded in the manifest), `start()` and `stop()` (safe to call twice). Everything else on the object is yours to design. *(Done in M3-2: `simulator/workload.py`, and every `start-incident` run now sends it, at 20 rps by default.)*
  2. **The traffic-spike injector.** Like the second example in §2: `parameter = "multiplier"`, no targets, and `start()`/`stop()` change the request rate through `self.ctx.workload`. It is started exactly as its window opens. This is the MVP incident, so land it first.
- **Linu — DynamoDB throttling (M3-3).** Subclass `Injector`. Things to know before you design it:
  - **Don't change capacity per experiment.** DynamoDB allows a provisioned → on-demand switch only 4 times per 24 hours, and capacity *decreases* are capped near 27 per day; M4 needs 140 throttling runs. The workable design is a table that sits at a **fixed low provisioned capacity** while your injector varies the **offered load**. That is also what the plan asks for: the knob is the offered-load-to-capacity ratio.
  - Size that fixed capacity so a 10× traffic spike does **not** throttle on its own, or `TRAFFIC_SPIKE` runs will carry throttling symptoms.
  - DynamoDB banks up to 300 seconds of unused capacity as burst, so throttling can start minutes after you raise the load. That matters for a 5-minute incident.
  - Return the measured throttle count from **`observe()`**; it lands in `observed`. Read it from CloudWatch with `self.ctx.session`, and add the CloudWatch permission to the simulator role in your PR.
  - The Products table's billing-mode change is a `template.yaml` edit: tell Jake the capacity you want and he makes it.
  - Still true: no `db_throttled` flag anywhere in telemetry.
- **Rachel — Payment failure + Lambda degradation (M3-4).** Both are `ControlPlaneInjector`s, like the first example in §2. Lambda degradation lists several services in `targets`, and the run picks one with `--target`. The larger half of your ticket is on the **handler side**:
  - Each handler that can be a target calls `active_faults("<its-service>")` (from `fault_config`, already in the telemetry layer) on every request and acts on `fault["injector"]` and `fault["params"]`.
  - **Payment's knobs move off environment variables** (`PAYMENT_LATENCY_MS`, `PAYMENT_FAILURE_RATE`, `PAYMENT_TIMEOUT`). Changing an environment variable recycles every warm execution environment, so each incident would start and end with a cold-start burst that normal runs never have — and `cold_start` is a model feature.
  - A handler must **never print or log** the fault it read.
  - Faults are scheduled ahead, not switched on: the framework writes the item 15 seconds before the window, so your injector needs at least 15 seconds of baseline (the default is 300).
- **Melisa — Cascading failure (M3-5).** A cascade is **several activations with staggered, overlapping windows**, not an injector that wraps others. Build a `runner.Plan` with one `runner.Fault` per activation and `incident_type="CASCADING_FAILURE"`, and call `runner.run()` — there is a worked example under "Runs with several faults" in the interface doc. The overlapping windows in the manifest *are* the "which faults are concurrently active" record your M4 labeling needs. Retry amplification is yours to design, either as its own injector or as a workload behaviour you agree with Josh. **Pass a workload to `runner.run()`** (`workload=Workload(...)`; example in the interface doc): without one the run sends no traffic, and a plan with a `traffic-spike` fault is refused.

Two things for Melisa and Rachel from the Order/Payment path:

- **In a botocore `Config`, `retries={"max_attempts": N}` means N *retries*, so N+1 attempts.** "No retries" is `retries={"total_max_attempts": 1}`. Order's payment-invoke client had this wrong (one hidden retry); it is fixed in PR #62 and live. With the old setting a hung payment would have run past Order's 10-second timeout and killed it before it logged telemetry.
- **Consequence of that fix:** transient payment-invoke failures the retry used to hide now surface as 502s with `dependency_error=true`. When the payment-timeout injector is validated, please confirm on the deployed stack that Order returns that clean 502 — it has been reasoned through, not yet observed.

---

## 7. For M4 (nothing to do yet, but it shapes your M4 ticket)

- **Label windows from `activations`, but read `incident_type` first** (Melisa, Linu). A cascade and a confounder both have two or more overlapping activations. Only the top-level label tells them apart: a cascade is `CASCADING_FAILURE`; a confounder is a single-fault type with two activations.
- **Use only `COMPLETED` manifests.** `FAILED` and `ABORTED` ones keep the windows they had planned. A manifest still at `RUNNING` is a run that died.
- **`severity` is relative to the band.** 0.9 in a 2×–4× band is `HIGH`; 0.1 in a 6×–8× band is `LOW`. To compare magnitudes, read `params`.
- **Experiments run one at a time, so M4's runs are serial** (Josh, Linu). 700 training runs at about 10 minutes each is roughly 117 hours — five days back to back. At 15 minutes each (the CLI default with a 5-minute fault) it is about 175 hours, and the test set is on top of that. The per-run length is the main lever and nobody has decided it yet.
- **`experiment_id` is a timestamp, not a number.** M4's registry assigns the 1–1000 sequence.
- **Project plan §17 lists 350 Phase 2 experiments but numbers them 701–1000 (300).** Unresolved.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `cannot register 'X': …` on import | Your class breaks the interface | The message names the attribute or the missing step. Fix it and re-run. |
| `no injector named 'x'; registered: …` | Typo in `--type`, or your file isn't in `simulator/injectors/` | Check the name against the list in the message. |
| Every `start-incident` fails with an `ImportError` | *Some* injector file is broken — maybe not yours | By design: a batch must never run without one of its injectors. Fix or remove the broken file. |
| `band is for 'x', but this injector's parameter is 'y'` | The band belongs to a different injector | Pass a band in your injector's own `parameter`. |
| `needs at least 15 s between the start of the run and its window` | A control-plane fault with too little baseline | Use `--baseline 15` or more. |
| `experiment … is already running, started by …` | Someone holds the lock | Wait for it. If that run is dead, see the next row. |
| A run was killed and the next one is refused | The dead run's lock hasn't expired (5 minutes after its planned end) | `python -m simulator cleanup`; add `--force` only if you are sure it is dead. |
| `invalid manifest: not JSON-serializable` | A `Decimal` or NaN in `params` / `observed` | Convert to `int` / `float` before returning it. |
| `could not reach the stack with AWS profile 'sim'` | The profile isn't set up, or your SSO session expired | §4; then `aws sso login --profile <your-profile>`. |
| The handler never applies the fault | The handler doesn't call `active_faults`, or checks a different `injector` name | The `injector` string in the item is your class's `name`. |
| `AccessDenied` from inside your injector | The simulator role lacks that permission | Add it to `SimulatorRole` in your PR (§4). |

**Golden rule:** run it with `--dry-run` first. Everything that can be wrong with a request is caught there, for free.

---

## 9. Your M3 checklist

- [ ] Pulled `main`; created your ticket branch `m3-<n>-<name>`; read `simulator/injectors/base.py` (the docstrings are the contract) and `noop.py`
- [ ] **Told Jake the interface works for your injector, or what it's missing** — this closes M3-1
- [ ] One injector file in `simulator/injectors/`, with `name`, `incident_type`, `parameter`, `targets`
- [ ] `configure()` is pure; the same `--seed` gives the same `params` in a dry run
- [ ] The band is taken from the arguments — **no hardcoded magnitude**
- [ ] `stop()` is safe before `start()`, after a failed `start()`, and twice
- [ ] Nothing resolved or measured appears in telemetry or in a handler's logs
- [ ] Your own tests in `simulator/test_<name>.py`; `python -m pytest -q` passes in `AnomalyPulse/simulator/`
- [ ] Any extra simulator-role permission is in your PR's `template.yaml` change
- [ ] (Josh) Workload has `describe()` / `start()` / `stop()` and is wired into `start-incident`
- [ ] (Linu) Measured throttle count returned from `observe()`; capacity sizing sent to Jake
- [ ] (Rachel) Handlers call `active_faults`; Payment's knobs are off environment variables
- [ ] (Melisa) Cascade built as a `runner.Plan` with overlapping windows
- [ ] After a real run: your activation is in the manifest and the shift is visible in landed telemetry

---

*Questions → ping Jake. The reference of record is [`Docs/SIMULATOR_INTERFACE.md`](../Docs/SIMULATOR_INTERFACE.md); the code that enforces it is in `AnomalyPulse/simulator/`.*
