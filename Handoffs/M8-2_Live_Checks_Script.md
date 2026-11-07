# M8-2 Live Checks — The Check Script

The script [`M8-2_Live_Checks.md`](M8-2_Live_Checks.md) has you run after each live run. Save the code below as
`AnomalyPulse/m8_2_check.py` (don't commit it), then run it from `AnomalyPulse/`:

```bash
python m8_2_check.py <experiment_id> --profile dev
```

**What it does, for one run** (it reads only; it changes nothing):

1. Reads the run's manifest from the telemetry bucket (`experiments/<experiment_id>/manifest.json`): when the run
   and its fault started and ended, and the run's type. It refuses until 5 minutes after the run ends, so the run's
   last minutes have been diagnosed.
2. Reads every minute the Diagnosis Lambda answered for the run, from its metric lines in
   `/aws/lambda/anomalypulse-diagnosis` (Logs Insights).
3. Runs `ml/diagnosis/incident.py` over those minutes in order, and prints each minute and the incidents it opens
   and closes.
4. Reads the Lambda's health over the run: `Errors`, `DestinationDeliveryFailures` and `AsyncEventsDropped`, the
   `anomalypulse-diagnosis-failed` queue and its alarm.
5. Prints **PASS** or **FAIL** with the reason. A `noop` run passes with no incident. A fault run passes with exactly
   one incident that closes as the run's type. Every run also needs a clean health line.

**It needs** boto3, and a profile that can read SSM, S3, CloudWatch Logs, CloudWatch metrics and SQS
(AnomalyPulseDeveloper). It was tested on the traffic spike `exp-20261106T234446Z-809e`, where it printed PASS, and
with the expected type deliberately swapped, where it printed FAIL.

```python
"""M8-2's live check for one run: each diagnosed minute, incident.py over them, the Lambda's
health over the run, and PASS or FAIL. Read-only.

    cd AnomalyPulse
    python m8_2_check.py <experiment_id> [--profile dev]

Run it at least 5 minutes after the run ends, so its last windows have been diagnosed.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path("ml/diagnosis").resolve()))
import incident  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("experiment_id")
parser.add_argument("--profile", default="dev")
args = parser.parse_args()
aws = boto3.Session(profile_name=args.profile, region_name="us-east-2")


def t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


bucket = aws.client("ssm").get_parameter(Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
key = f"experiments/{args.experiment_id}/manifest.json"
m = json.loads(aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
start, end = t(m["started_at"]), t(m["ended_at"] or m["started_at"])
if m["status"] != "COMPLETED":
    sys.exit(f"{args.experiment_id} is {m['status']}, not COMPLETED")
if datetime.now(timezone.utc) < end + timedelta(minutes=5):
    sys.exit("too early: wait until 5 minutes after the run's end, so its last windows are diagnosed")
acts = m["activations"]
fault = (min(t(a["active_from"]) for a in acts), max(t(a["active_until"]) for a in acts)) if acts else None
want = m["incident_type"]
print(f"{args.experiment_id}: {want}, run {start:%H:%M:%S}-{end:%H:%M:%S}Z"
      + (f", fault {fault[0]:%H:%M:%S}-{fault[1]:%H:%M:%S}Z" if fault else ", no fault"))

# Every window the Diagnosis Lambda answered for this run, from its metric lines.
logs = aws.client("logs")
qid = logs.start_query(
    logGroupName="/aws/lambda/anomalypulse-diagnosis",
    startTime=int(start.timestamp()), endTime=int((end + timedelta(minutes=10)).timestamp()),
    queryString='filter event = "diagnosis" | fields window_start, anomalous, label, incident_type, confidence, '
                'SinceWindowEndSeconds | sort window_start asc | limit 500')["queryId"]
while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
    time.sleep(1)
rows = [{f["field"]: f["value"] for f in r} for r in res["results"]]
rows = [r for r in rows if start - timedelta(minutes=1) < t(r["window_start"]) < end]

state, incidents, lags = None, [], []
for r in rows:
    p = {"window_start": r["window_start"], "anomalous": r.get("anomalous") in ("1", "true"),
         "incident_type": r.get("incident_type") or None,
         "confidence": float(r["confidence"]) if r.get("confidence") else None}
    before = state
    state, changes = incident.step(state, p)
    if "closed" in changes and incidents:
        # The incident that closed; when a gap closes one and the same window opens the next, `state` is the new one.
        incidents[-1] = state if state["status"] == "CLOSED" else incident._closed(before)
    if "opened" in changes:
        incidents.append(state)
    elif state and state["status"] == "OPEN" and incidents:
        incidents[-1] = state
    lags.append(float(r["SinceWindowEndSeconds"]))
    w = t(r["window_start"])
    phase = "fault" if fault and w + timedelta(minutes=1) > fault[0] and w < fault[1] else ""
    print(f"  {w:%H:%M} {phase:5} {'flagged' if p['anomalous'] else 'cleared'} {r.get('label', ''):20} "
          f"incident_type {p['incident_type'] or '-':20} {(r.get('confidence') or '')[:5]:5} "
          f"diagnosed {float(r['SinceWindowEndSeconds']):5.1f} s after the minute  {' '.join(changes)}")

print(f"\n{len(rows)} windows diagnosed; SinceWindowEndSeconds {min(lags, default=0):.0f}-{max(lags, default=0):.0f} s")
for i in incidents:
    print(f"incident: {i['incident_type']} ({i['confidence']:.3f}), {i['started_at']} -> {i['ended_at'] or 'still open'}")

# The Lambda's health over the run: errors, failed reply deliveries, the failure queue and its alarm.
cw = aws.client("cloudwatch")
span = {"StartTime": start, "EndTime": end + timedelta(minutes=10), "Period": 86400}
health = {name: sum(d["Sum"] for d in cw.get_metric_statistics(
              Namespace="AWS/Lambda", MetricName=name, Statistics=["Sum"],
              Dimensions=[{"Name": "FunctionName", "Value": "anomalypulse-diagnosis"}], **span)["Datapoints"])
          for name in ("Errors", "DestinationDeliveryFailures", "AsyncEventsDropped")}
sqs = aws.client("sqs")
queue = sqs.get_queue_url(QueueName="anomalypulse-diagnosis-failed")["QueueUrl"]
health["failure queue"] = int(sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["ApproximateNumberOfMessages"])
                              ["Attributes"]["ApproximateNumberOfMessages"])
alarm = cw.describe_alarms(AlarmNames=["anomalypulse-diagnosis-failed"])["MetricAlarms"][0]["StateValue"]
print(f"health: {health}, alarm {alarm}")

problems = []
if not rows:
    problems.append("no diagnosed windows: is the pipeline running, and did the run send traffic?")
if want == "NORMAL":
    if incidents:
        problems.append(f"a NORMAL run opened {len(incidents)} incident(s)")
else:
    if len(incidents) != 1:
        problems.append(f"expected exactly 1 incident, got {len(incidents)}")
    elif incidents[0]["incident_type"] != want:
        problems.append(f"the incident is {incidents[0]['incident_type']}, expected {want}")
    if incidents and incidents[-1]["status"] == "OPEN":
        problems.append("the incident never closed")
if any(health[k] for k in health) or alarm != "OK":
    problems.append("the Lambda had errors, failed deliveries or failed windows (see health)")
print("\nPASS" if not problems else "\nFAIL: " + "; ".join(problems))
```
