# AnomalyPulse Live Demo Runbook (M10-1)

How to drive the live demo from a presenter's laptop: the checks before it, the one command that runs the
injected incidents, what the console shows minute by minute, how to reset or stop a take, and what to do when
something goes wrong. Written so a presenter who didn't build the system can drive it with the `dev` profile.
Every command and time below was run and measured on the deployed stack on 2026-11-26 ("Evidence", at the end).

The demo's live part is two injected incidents on one stretch of traffic: **database throttling**, then a
**cascading failure** (§15 Parts 2 and 4). The traffic comes from the runner host, an EC2 instance in AWS; the
laptop only sends it commands through `AnomalyPulse/infra/runner/drive.sh` and shows the console.

## Rules that always hold

| Rule | Why |
|---|---|
| **All traffic from the runner, through `drive.sh`.** Never run the simulator on a laptop. | With laptop traffic the detector flagged about half of the healthy minutes ([`DETECTION.md`](DETECTION.md), "Where live traffic comes from"). |
| **Never pass `--mix`.** | In another endpoint mix the detector flagged every healthy minute ([`M9-2_REPORT.md`](M9-2_REPORT.md)). |
| **Never press Ctrl-C in a run's tmux session.** Stop a run with `bash drive.sh abort <session>`. | Ctrl-C leaves the experiment lock held, and the next run is refused ("Stopping a take early"). |
| **One run at a time.** | The experiment lock refuses a second run while one is going. |
| **Traffic at least 6 minutes before the healthy segment, and about 6 minutes after a fault before calling the system healthy.** | The detector looks back over earlier minutes, so for a few minutes it still sees the idle time or the fault ([`PIPELINE.md`](PIPELINE.md), "On the minutes only the live path scores"). |
| **Keep each run's 5 minutes of recovery traffic** (the default; don't pass a shorter `--recovery`). | An incident closes only when later minutes arrive. With no traffic, it stays on screen ([`INCIDENT_STORE.md`](INCIDENT_STORE.md), "When an incident closes"). |
| **Never deploy or update the `anomalypulse-runner` stack.** | It would replace the instance every experiment ran on ([`DATASET.md`](DATASET.md), "The runner host"). |
| **Stop the runner when you're done.** | It costs money while it runs. |

## 1. Before demo day (once per laptop)

1. **AWS CLI v2 and the `dev` profile** (`AnomalyPulseDeveloper`, us-east-2). Check it:

   ```bash
   aws sts get-caller-identity --profile dev     # the Arn names AWSReservedSSO_AnomalyPulseDeveloper
   ```

   If it says the token has expired: `aws sso login --profile dev`. A login lasts about 8 hours.
2. **Python 3** (`python3 --version`). `drive.sh` uses it, and it serves the console.
3. **The repository on `main`:** `git clone https://github.com/jkelle11-source/msml650_project.git`, or `git pull`
   on `main` if you already have it.
4. **The console.** `cd AnomalyPulse/console && npm start` (without Node:
   `python3 -m http.server 4173 --bind 127.0.0.1`), then open <http://127.0.0.1:4173>. Only that address and
   port can read the API. With nothing running it reads **System healthy** with 0 active incidents.
   The offline fallback is <http://127.0.0.1:4173/?source=mock>: it needs no network and has a Scenario picker
   ([`console/README.md`](../AnomalyPulse/console/README.md)). M10-2 adds a recorded run as the other fallback.
5. **Try `drive.sh`:**

   ```bash
   cd AnomalyPulse/infra/runner
   export AWS_PROFILE=dev            # drive.sh uses $AWS_PROFILE, and dev when it's unset
   bash drive.sh status              # runner: i-... stopped, lock: free
   ```

## 2. Demo day: before the demo

Start about 45 minutes before the slot. All commands run from `AnomalyPulse/infra/runner`, with
`export AWS_PROFILE=dev` in that terminal.

| Step | Command | Expect |
|---|---|---|
| 1. Log in | `aws sso login --profile dev` | Log in again if the demo ends more than 8 hours later |
| 2. Nothing else is running | `bash drive.sh status` | `stopped` and `lock: free`. If the lock is held, someone's run is going: find out whose before starting anything |
| 3. The models are M9's | `aws ssm get-parameter --name /anomalypulse/ml/models/incident_type --query Parameter.Version --output text` and the same for `/anomalypulse/ml/models/anomaly_iforest` | `2`, then `1`. Anything else means a model was promoted since M9, which changes predictions without any deploy: stop and tell Jake |
| 4. The console | Open it | **System healthy**, 0 active incidents |
| 5. The budget | Jake: no `anomalypulse-daily` alert email today | If one came, talk to Jake before running anything |
| 6. Start the runner | `bash drive.sh start` | `running, and taking commands` (about 1–2 minutes) |
| 7. Check out `main` there | `bash drive.sh setup main` | Ends with `checkout: <commit> (main)`. If the simulator changed on `main` since the last dry run, tell Jake |
| 8. Healthy check (M10-6) | below | Passes, about 23 minutes |

**The healthy check** sends 6 minutes of warm-up, 15 minutes of healthy traffic and 2 of recovery:

```bash
bash drive.sh run health start-incident --type noop --duration 900 --baseline 360 --recovery 120 \
    --created-by m10-health-check
bash drive.sh log health        # at the end: "<experiment_id> COMPLETED", then "exit 0"
```

- **Pass:** no incident opens at any point (the console's active incidents stays 0) and the log ends `exit 0`.
- **Record the detector's flagged share** (M10-6): in CloudWatch Logs Insights, on
  `/aws/lambda/anomalypulse-diagnosis` over the run's time range, run
  `fields window_start, anomalous, label, incident_type | filter event = "diagnosis" | sort window_start asc`, then
  count `anomalous = 1` over the 16 full minutes after the warm-up (from the first full minute after T+6 to the
  last full minute of the run). Above 10% doesn't fail the check, but the classifier is then the only safeguard
  that day: tell the team.
- **Fail** (an incident opens): don't present live. Use the fallback and tell Jake. Never change the detector's
  cut-off to get past it.

The runner can stay on between the check and the demo. The idle stop stops it after an hour without traffic, so
run `bash drive.sh status` before the take, and `bash drive.sh start` if it stopped (it keeps the same instance).

## 3. The demo take

One command runs both incidents. The cascade comes straight after the database throttling because its own
warm-up is only 5 minutes: the first run's recovery traffic covers the rest.

```bash
bash drive.sh run demo \
  start-incident --type db-throttling --intensity 0.7 --band 3.5 4.5 --duration 300 --baseline 360 --created-by m10-demo -- \
  start-cascade --spec simulator/cascades/example.json --created-by m10-demo
```

It prints `started tmux session demo`, then the first run's experiment id (`exp-... DATABASE_THROTTLING`) and
its workload (20 requests a second, the default mix). The two runs take 36 minutes. These are M10-1's default
strengths and timings; if M10-2 (Rachel) settles on different ones, use hers.

**What happens, and what the console shows.** T is when you pressed Enter. Measured on 2026-11-26; the
cascade's fault start times vary by up to about 15 seconds.

| Time | What happens | What the console shows |
|---|---|---|
| T+0 | Warm-up traffic starts | Healthy. The newest minute on the console is always about 2 minutes behind the clock |
| T+2 | | The first numbers: about 1,150–1,270 requests a minute, 0% errors, p95 about 10–17 ms |
| T+6:00 | Database throttling starts on the Products table | Still healthy (the console is 2 minutes behind) |
| T+8 | | Product turns **Degraded**: p95 about 3.2 s, errors still 0%. Throttled reads are retried, so they are slow rather than failed |
| T+9:50 | The incident opens | **Database throttling detected**, about 99% confidence, Product affected, AI severity pending |
| T+10:30 | Bedrock's diagnosis is stored (it took 33–40 s per call) | AI severity, the AI diagnosis page and the ground-truth match appear |
| T+11:00 | The throttling ends | Product recovers on screen about 2 minutes later |
| T+16:00 | The first run ends; the cascade's run starts, with 5 minutes of warm-up | |
| T+16:10 | The incident closes | **No active incident** |
| T+21:00 | The cascade starts with a traffic spike | |
| T+22:25 | Database throttling joins | |
| T+23:30 | Order slows down (200–500 ms added) | |
| T+24:25 | Retry amplification joins | |
| T+25:10 | The incident opens | **Traffic spike detected**: expected, because the spike ran alone in its first minutes |
| T+26:15 | The incident's type changes | **Cascading failure detected**, all four services affected, then its AI severity once the new diagnosis is stored (High on 2026-11-26) |
| T+31:00 | All the cascade's faults end | |
| T+36:00 | The cascade's run ends | |
| T+37:10 | The incident closes | **No active incident**, all services healthy |

**Timing to plan the narration around:**

- **The incident, AI diagnosis, ground-truth and timeline pages show an incident only while it is open:** about
  6 minutes for the database throttling (T+9:50 to T+16:10) and 12 for the cascade (T+25:10 to T+37:10). An
  incident closes about 5 minutes after its fault ends, so a longer `--duration` keeps it on screen longer.
- **The healthy segment** is the stretch after the 6-minute warm-up and before the fault reaches the console:
  T+6 to T+8. The numbers already look healthy from T+2. For a longer healthy segment, raise `--baseline`, which
  starts the fault later.
- **The cascade reads "Traffic spike detected" for about a minute** before it turns into "Cascading failure
  detected". Say so on stage, or wait for the change before showing it.

## 4. Between takes

**Normally there is nothing to reset.** Every run cancels its faults, releases the lock and leaves nothing
behind ([`SIMULATOR_INTERFACE.md`](SIMULATOR_INTERFACE.md), "Teardown and cost"), and its incident closes on
its own about 5 minutes after the fault, while the recovery traffic still runs. Before the next take:

```bash
bash drive.sh status            # lock: free, tmux: none
```

**Stopping a take early:**

```bash
bash drive.sh abort demo
```

It takes up to about 1½ minutes, and stops the cascade chained after it too. It ends like this:

```text
exp-20261126T171715Z-43d6 ABORTED
interrupted
exit 130
lock:    free
```

After an abort, the console keeps showing **Incident active** while the system is idle. The incident closes
within about 4 minutes of the next take's traffic starting (allow 5), recorded with its real end time, so start
the next take at least 5 minutes before you need a healthy console.

Why not Ctrl-C: in the tmux session, Ctrl-C also stops the program writing the run's log. The simulator then
fails on its first message while winding down, before it releases the lock, and every new run is refused until
5 minutes after the stopped run's planned end. `abort` interrupts only the simulator (`drive.sh`, "Why `abort`").

**Retakes.** Give each one a new session name, so its log stays separate:

- **The whole demo again:** the same command, as `bash drive.sh run demo2 ...`.
- **Only the database throttling:** the first `start-incident` line alone.
- **Only the cascade:** put a 2-minute run with no fault in front of it, so the traffic runs 7 minutes before the
  spike. This one has been checked with `--dry-run` only: try it in a dry run (M10-6) before relying on it.

  ```bash
  bash drive.sh run cascade \
    start-incident --type noop --duration 60 --baseline 60 --recovery 0 --created-by m10-demo -- \
    start-cascade --spec simulator/cascades/example.json --created-by m10-demo
  ```

**When something goes wrong:**

| What you see | What it means | What to do |
|---|---|---|
| `Token has expired` or another SSO error | Your login ran out | `aws sso login --profile dev`, then repeat the command. A run already going isn't affected |
| `the runner is stopped: run 'drive.sh start' first` | The idle stop, or someone, stopped it | `bash drive.sh start` |
| `the runner is running but Session Manager can't reach it yet` | It is still starting | Wait a minute, then `bash drive.sh status` |
| A run is refused because the lock is held, and `status` shows a tmux session | A run is still going | Wait for it (`bash drive.sh log <session>`), or `bash drive.sh abort <session>` |
| The lock is held, and `status` shows `tmux: none` | A run died without releasing it, for example after a Ctrl-C | `bash drive.sh run fix cleanup --force`: it deletes leftover faults and releases the lock (`deleted N fault(s)`, `released the lock of ...`). Only when no tmux session is listed: on a live run, `--force` breaks it. Otherwise the lock frees itself 5 minutes after the dead run's planned end |
| **Incident active** before any fault | An incident left open by an earlier run | It closes within about 5 minutes of traffic starting. Wait for it before the healthy segment |
| **No data**, or a stale connection chip, for more than a minute or two | The pipeline or the API is late | Press Refresh. If it stays, switch to the fallback |
| No incident by about T+12 | Detection didn't fire | Talk over the Overview, then switch to the fallback for the reveal |
| Anything else going wrong live | | Switch to the fallback (`?source=mock`, or M10-2's recording). Let the run finish on its own: there is nothing to clean up |

## 5. After the demo

```bash
bash drive.sh log demo          # wait for "exit 0", or abort the run
bash drive.sh status            # lock: free, tmux: none
bash drive.sh stop              # "stopped"
```

If the runner is left on, the idle stop stops it an hour after the traffic ends. `bash drive.sh log demo 40`
lists each run's experiment id, for the record.

## 6. Cost guardrails

What things cost (measured: [`COST_AND_PERFORMANCE.md`](COST_AND_PERFORMANCE.md), [`DATASET.md`](DATASET.md)):

| | Cost |
|---|---|
| The system idle, until the teardown | About $0.84 a day |
| One run's traffic | About $0.28 |
| One Bedrock diagnosis | About $0.035; 1 to 5 per incident (1 for the database throttling, 2 for the cascade on 2026-11-26) |
| The runner while it is on | About $0.07 an hour |
| The healthy check | About $0.50 |
| A take at its worst | About $1.30 |
| Two dry runs and the demo, each with its healthy check | About $5.40 |

**What keeps a take cheap is that everything is bounded before it starts**, not the budget alerts:

- Every run has a fixed length (16 and 20 minutes here), and the lock allows only one at a time. A cascade is
  cut off at 30 minutes whatever its spec says (`cascade.MAX_RUN_S`).
- Bedrock is called 1 to 5 times per incident, and the incident store handles one minute at a time (reserved
  concurrency 1), so a burst of minutes can't fan out into a burst of calls.
- The runner's idle stop stops it after an hour without traffic.
- **Budgets are a tripwire, not a cap.** `anomalypulse-daily` emails when a UTC day's actual cost passes $5, and
  the monthly `Student` budget alerts against $100. AWS reports costs hours late, so an alert comes after the
  money is spent: it catches a forgotten runner or a runaway day, not a take.
- No capacity caps were added to the shop (decided 2026-11-25), so the demo runs on the same system M9 evaluated.

## 7. For the owner: redeploying (Jake, `admin`)

Presenters skip this. The live stacks match `main` as of 2026-11-26 (`5fac754`). **Don't deploy anything between
the last passing dry run and the demo.**

- **Order:** `shared`, `genai`, `pipeline`, `incidents`, `dashboard`, because later stacks read earlier ones'
  SSM parameters. Each one: `cd AnomalyPulse/infra/<stack> && sam build && AWS_PROFILE=admin sam deploy`
  (README, "Deploying the stack").
- **"No changes to deploy" exits with an error, and it is a pass:** the stack already matches the code.
- **Don't rebuild `ml`.** The Diagnosis image builds on `public.ecr.aws/lambda/python:3.13`, which AWS rebuilt on
  2026-11-24, after ours was pushed (2026-11-06). A rebuild would change the image M9 evaluated, and it needs
  Docker.
- **The explanation Lambda's boto3 and botocore are pinned** (`genai/requirements.txt`, PR #120), so its package
  doesn't change with the build day.
- **After a pipeline deploy that creates the schedule rule,** check the second the window builder starts at
  ([`PIPELINE.md`](PIPELINE.md), "When a minute's window is ready"). It was :45 on 2026-11-26.

## 8. Teardown after the course (Jake, `admin`)

The idle cost runs until this is done.

1. The runner: `aws cloudformation delete-stack --stack-name anomalypulse-runner --profile admin`, then the same
   for `anomalypulse-runner-idle-stop`.
2. The application stacks, newest first: `anomalypulse-dashboard`, `anomalypulse-incidents`,
   `anomalypulse-pipeline`, `anomalypulse-genai`, `anomalypulse-ml`, `anomalypulse-shared`. Empty the telemetry
   bucket before deleting `anomalypulse-shared`, or the delete fails. It holds the datasets and every manifest:
   copy what you want to keep first.
3. Kept when their stacks are deleted, so delete them by hand: the Incidents table (from `pipeline`), the ML model
   bucket (versioned: delete every version) and the training-image ECR repository (both from `ml`).
4. SAM's own artifacts: the `aws-sam-cli-managed-default` stack's bucket, and the companion stack holding the
   Diagnosis image's repository (`resolve_image_repos`).
5. The `anomalypulse-daily` budget. Keep the monthly one until the last bill arrives.

## Evidence (2026-11-26)

**Fresh deploy.** `main` at `5fac754` redeployed onto the existing stacks in the order above: only `genai`
changed (the boto3 pin); the others reported no changes. All six templates and their parameters matched `main`,
and the window builder still started at :45.

**Healthy baseline** (§15 Part 1), `exp-20261126T041940Z-ec74`: a run with no fault, 04:19:40 to 04:42:40 UTC, from
the runner at `5fac754`. No incident and no Bedrock call (`incidents.live_check` passed); the classifier said
`NORMAL` on all 23 minutes; the detector flagged 0 of the 16 full minutes after the warm-up. Over those minutes:
1,192 requests a minute (median; 1,156 to 1,273), 0% errors, p95 13.4 ms (median; 9.7 to 17.1). The console at
04:33 UTC (1,217 requests a minute, 0%, 11 ms, 0 incidents) matched the health file exactly:
[`images/m10-1/M10-1_Healthy_Baseline.png`](images/m10-1/M10-1_Healthy_Baseline.png). The plan's example was
0.4% errors and 170 ms; the deployed system is faster and had no errors.

**Reset between takes**, driven with `dev` from a laptop (`--created-by m10-reset-drill`):

| Run | What it showed |
|---|---|
| `exp-20261126T171715Z-43d6`: the database throttling, aborted 3 min 50 s into the fault | `drive.sh abort` on the runner: `ABORTED`, exit 130, lock free, no fault-config items left. Its incident stayed open while idle, then closed at 17:35:48 UTC, from the next run's first window (about 4 minutes after that run started), with its end at 17:28, the abort's minute |
| `exp-20261126T173139Z-af48`: the database throttling, started right after | `live_check` passed: one incident, `DATABASE_THROTTLING`, opened 17:41:28 and closed 17:47:49 UTC, 1 Bedrock call, detection 189 s |
| `exp-20261126T174740Z-2207`: the cascade, chained after it | `live_check` passed: one incident, opened as `TRAFFIC_SPIKE` at 17:56:50, `CASCADING_FAILURE` from about 17:57:50, closed 18:08:49 UTC, 2 Bedrock calls, detection 250 s |

The runner was stopped at 18:13 UTC with the lock free.
