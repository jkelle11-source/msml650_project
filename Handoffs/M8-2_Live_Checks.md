# M8-2 Handoff — The Live Checks Left After PR #93

> **The one-sentence version:** M8-2 is merged and deployed, and one live traffic spike passed, so Definition of
> Done items 1 and 3 are ticked. What's left is item 2: five live runs, one per injector plus a NORMAL run, about
> 1½ hours, each checked with the script in [`M8-2_Live_Checks_Script.md`](M8-2_Live_Checks_Script.md).

**Where it stands (2026-11-06):**
- **Merged and deployed.** PR #93 is merged, and `anomalypulse-ml` is deployed from it, with the forest promoted as
  `anomaly_iforest`.
- **Verified live.** One traffic spike passed (`exp-20261106T234446Z-809e`), which ticked items 1 and 3 in M08
  ([`Docs/LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md), "The live traffic spike").
- **Left:** item 2, *"Full taxonomy classifies correctly on a live run of each injector."*

## Before you start

1. **Pull `main`.** There's nothing to deploy or promote: the stack already runs this code.
2. **Log in to AWS SSO.** One login lasts about 8 hours, which is enough. You need two profiles:
   - one that assumes the simulator role. `sim` is the simulator's default; if yours has another name, add
     `--profile <yours>` to each run command.
   - one with read access (AnomalyPulseDeveloper, `dev` below) for the check script.
3. **Install the simulator's Python libraries:** `pip install -r AnomalyPulse/simulator/requirements.txt`
   (boto3 and jsonschema). The check script needs only boto3.
4. **Keep the machine awake for the whole 1½ hours.** On a Mac: AC power, lid open, and the `caffeinate -is` in each
   command. A machine that sleeps mid-run fails that run.
5. **Tell Jake before you start.** Only one experiment can run on the stack at a time. The simulator won't start
   while another run holds the lock, and these runs must not overlap M9-1.
6. **Save the check script** from [`M8-2_Live_Checks_Script.md`](M8-2_Live_Checks_Script.md) as
   `AnomalyPulse/m8_2_check.py`. Don't commit it.

## The runs

Run these from `AnomalyPulse/`, one at a time, each starting when the one before ends. Use exactly these bands:
they're the training bands, and the test bands must stay unseen until M9-1.

```bash
cd AnomalyPulse
caffeinate -is python -m simulator start-incident --type noop --duration 300 --baseline 360 --created-by melisa-m8-2-live
caffeinate -is python -m simulator start-incident --type db-throttling --intensity 0.7 --band 3.5 4.5 --duration 300 --baseline 360 --created-by melisa-m8-2-live
caffeinate -is python -m simulator start-incident --type lambda-degradation --intensity 0.7 --band 800 1100 --duration 300 --target order-service --baseline 360 --created-by melisa-m8-2-live
caffeinate -is python -m simulator start-incident --type payment-failure --intensity 0.7 --band 500 1500 --duration 300 --baseline 360 --created-by melisa-m8-2-live
caffeinate -is python -m simulator start-cascade --spec simulator/cascades/example.json --created-by melisa-m8-2-live
```

- **How long:** each `start-incident` run takes 16 minutes (6 of normal traffic, 5 of fault, 5 of recovery). The
  cascade takes 20.
- **Why the cascade goes last:** its spec has only 5 minutes of normal traffic first, so start it right after
  another run, whose traffic covers the warm-up.
- **The experiment id:** each run prints it when it starts and ends with `<experiment_id> COMPLETED`. Note each id.

## Check each run

Run the check **at least 5 minutes after the run ends**, so its last minutes have been diagnosed. You can check one
run while the next is going.

```bash
python m8_2_check.py <experiment_id> --profile dev
```

It prints:
- each minute's diagnosis;
- the incidents `incident.py` opens and closes;
- the Diagnosis Lambda's health over the run: errors, failed reply deliveries, the failure queue and its alarm;
- **PASS** or **FAIL** with the reason.

It reads only; it changes nothing.

**PASS means:**

| Run | Expected |
|---|---|
| `noop` | No incident, and no minute with an `incident_type` |
| `db-throttling` | Exactly one incident, `DATABASE_THROTTLING` when it closes |
| `lambda-degradation` | Exactly one, `LAMBDA_DEGRADATION` |
| `payment-failure` | Exactly one, `DEPENDENCY_FAILURE` |
| cascade | Exactly one, `CASCADING_FAILURE` when it closes. It may open as its first fault's type and change once |
| every run | The incident closes before the run ends; no errors, no failed deliveries, an empty failure queue, alarm `OK` |

**Normal, not a failure:**
- The first 1–2 minutes after idle time are *flagged* but classified `NORMAL`.
- The fault's first, partial minute is classified `NORMAL`.
- The incident's type lasts 1–2 minutes past the fault's end.
- Each minute is diagnosed about 165–180 s after it ends.

**Tell Jake about it, even with a PASS:** an `incident_type` well before the fault starts, or more than about 3 minutes
after it ends.

## If something goes wrong

- **A run dies** (sleep, network, Ctrl-C): run `python -m simulator cleanup`. It deletes the faults left active and
  releases the lock; it refuses while the lock is still live, and `--force` overrides that. Then rerun that one.
- **FAIL on the type or the incident count:** don't rerun to get a PASS. Stop, and send Jake the script's full
  output for that run.
- **The failure queue isn't empty, or the alarm is `ALARM`:** the Lambda refused or failed a window, and the error
  is in its log group, `/aws/lambda/anomalypulse-diagnosis`. Send Jake the error. Don't purge the queue: each
  message is the window's event, kept 14 days.

## When all five pass

Record it the way item 1 was, in one PR:

1. **`Milestones/M08_Integration.md`, M8-2:** tick item 2, and add an "Item 2" bullet to the "Done" note. Give one
   line per run: the experiment id, the type its incident closed as, and when it opened and closed relative to the
   fault.
2. **`Docs/LIVE_DIAGNOSIS.md`:** add a section "The live run of each injector (<date>)" after "The live traffic
   spike", with a table of the same lines plus each run's health line.
3. **Open the PR** with "Closes #29".
