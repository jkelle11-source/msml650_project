"""M8-3's live check for one run: the incident records it opened, the dashboard's read of them, the
Lambda's own log lines, and the Lambda's health. Read-only.

    cd AnomalyPulse
    python3 -m incidents.live_check <experiment_id> [--profile anomalypulse]

Run it at least 5 minutes after the run ends, so its last minutes have been diagnosed and stored.
The judging is in plain functions (tested offline); main() is the AWS reads around them.
"""
import argparse
import importlib.util
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from genai import contract
from incidents import record

FUNCTION = "anomalypulse-incidents"
LOG_GROUP = "/aws/lambda/anomalypulse-incidents"
QUEUE = "anomalypulse-incidents-failed"
ALARM = "anomalypulse-incidents-failed"
WAIT = timedelta(minutes=5)
SERVICES = ("product", "cart", "order", "payment")


def _t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_incident(rec, want, experiment_id):
    """(problems, notes) for one stored record of a run whose type is `want`."""
    problems, notes = [], []
    prediction = rec.get("prediction") or {}
    if rec.get("status") != "CLOSED" or not rec.get("ended_at"):
            problems.append("the incident never closed (it closes only when later minutes arrive: if traffic stopped "
                        "right after the fault, that is documented behavior, INCIDENT_STORE.md, "
                        "'When an incident closes'; otherwise the run's own quiet minutes were never stored)")
    if prediction.get("incident_type") != want:
        problems.append(f"the incident is {prediction.get('incident_type')}, expected {want}")
    if rec.get("experiment_id") != experiment_id:
        problems.append(f"experiment_id is {rec.get('experiment_id')!r}, expected {experiment_id!r}")
    if "experiment_id" in prediction or not contract.FORBIDDEN_KEYS.isdisjoint(set(prediction)):
        problems.append("the prediction holds a ground-truth key")
    if rec.get("diagnosis") is None:
        problems.append("no diagnosis was stored" + (f" ({rec['diagnosis_error']['type']})" if rec.get("diagnosis_error") else ""))
    else:
        if (rec.get("latency") or {}).get("diagnosis_to_stored_s") is None:
            problems.append("no round-trip latency was stored")
        if rec.get("diagnosis_for_type") != prediction.get("incident_type"):
            notes.append(f"the diagnosis was made for {rec.get('diagnosis_for_type')}, the incident ended as "
                         f"{prediction.get('incident_type')}")
        if (rec.get("self_check") or {}).get("passed") is False:
            notes.append("the diagnosis failed its self-check (stored flagged)")
    calls = rec.get("bedrock_calls", 0)
    if not 1 <= calls <= record.MAX_CALLS:
        problems.append(f"bedrock_calls is {calls}, expected 1 to {record.MAX_CALLS}")
    if rec.get("diagnosis_error"):
        notes.append(f"the last call failed: {rec['diagnosis_error']['type']}")
    if not prediction.get("affected_services"):
        problems.append("affected_services is empty")
    return problems, notes


def check_run(records, want, experiment_id):
    """(problems, notes) for every record the run opened."""
    if want == "NORMAL":
        return ([f"a NORMAL run opened {len(records)} incident(s)"] if records else []), []
    if len(records) != 1:
        return [f"expected exactly 1 incident, got {len(records)}"], []
    return check_incident(records[0], want, experiment_id)


def parse_lines(messages):
    """The JSON log lines among Logs Insights' @message values; anything else is skipped."""
    out = []
    for message in messages:
        try:
            line = json.loads(message)
        except ValueError:
            continue
        if isinstance(line, dict):
            out.append(line)
    return out


def summarize_steps(lines):
    steps = [line for line in lines if line.get("event") == "incident_step"]
    sources = {}
    for line in steps:
        sources[line.get("window_from")] = sources.get(line.get("window_from"), 0) + 1
    return {"steps": len(steps), "window_from": sources,
            "opened": sum("opened" in (line.get("changes") or []) for line in steps),
            "type_changed": sum("type_changed" in (line.get("changes") or []) for line in steps),
            "closed": sum("closed" in (line.get("changes") or []) for line in steps)}


def summarize_latency(lines):
    rows = [line for line in lines if line.get("event") == "incident_diagnosis"]
    out = {"diagnoses": len(rows)}
    for name in ("SinceWindowEndSeconds", "DiagnosisToStoredSeconds", "RoundTripSeconds", "BedrockMs"):
        values = [line[name] for line in rows if isinstance(line.get(name), (int, float))]
        out[name] = {"median": statistics.median(values), "max": max(values)} if values else None
    return out


def _dashboard_builder():
    path = Path(__file__).resolve().parent.parent / "dashboard" / "builder.py"
    spec = importlib.util.spec_from_file_location("dashboard_builder_for_live_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readback(rec, manifest, now):
    """(problems, detection_latency_s): the stored record through the dashboard's build_dashboard,
    on a quiet health window (this checks the record's shape, not live health)."""
    window = {"window_start": now.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
              "system_p95_latency_ms": 0.0}
    for service in SERVICES:
        window.update({f"{service}_request_rate": 1.0, f"{service}_error_rate_5xx": 0.0,
                       f"{service}_p95_latency_ms": 1.0})
    try:
        result = _dashboard_builder().build_dashboard(window, record.to_incident(rec), manifest, now)
    except Exception as err:  # noqa: BLE001 - any failure to read the record is the finding
        return [f"build_dashboard could not read the record: {type(err).__name__}: {err}"], None
    shown = result["incident"]
    problems = []
    if shown is None or shown["diagnosis"] != rec["diagnosis"] or shown["prediction"] != rec["prediction"]:
        problems.append("build_dashboard did not return the stored prediction and diagnosis")
    latency = None
    if manifest is not None and shown and shown.get("comparison"):
        latency = shown["comparison"]["detection_latency_s"]
    return problems, latency


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("experiment_id")
    parser.add_argument("--profile", default="anomalypulse")
    args = parser.parse_args(argv)

    import boto3
    from boto3.dynamodb.conditions import Key

    aws = boto3.Session(profile_name=args.profile, region_name="us-east-2")
    ssm = aws.client("ssm")
    bucket = ssm.get_parameter(Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
    table = aws.resource("dynamodb").Table(
        ssm.get_parameter(Name="/anomalypulse/tables/incidents/name")["Parameter"]["Value"])

    manifest = json.loads(aws.client("s3").get_object(
        Bucket=bucket, Key=f"experiments/{args.experiment_id}/manifest.json")["Body"].read())
    if manifest["status"] != "COMPLETED":
        sys.exit(f"{args.experiment_id} is {manifest['status']}, not COMPLETED")
    start, end = _t(manifest["started_at"]), _t(manifest["ended_at"])
    if datetime.now(timezone.utc) < end + WAIT:
        sys.exit("too early: wait until 5 minutes after the run's end, so its last minutes are stored")
    want = manifest["incident_type"]
    print(f"{args.experiment_id}: {want}, run {start:%H:%M:%S}-{end:%H:%M:%S}Z")

    items = table.query(KeyConditionExpression=Key("pk").eq(record.PK) & Key("sk").between(
        _iso(start - timedelta(minutes=1)), _iso(end + timedelta(minutes=10))), ConsistentRead=True)["Items"]
    records = [record.from_dynamodb(item) for item in items]
    for rec in records:
        p = rec["prediction"]
        print(f"incident {rec['incident_id']}: {p['incident_type']} ({p['confidence']:.3f}), {rec['status']}, "
              f"{rec['sk']} -> {rec['ended_at']}, calls {rec['bedrock_calls']}, diagnosis for "
              f"{rec['diagnosis_for_type']}, services {p['affected_services']}, experiment {rec.get('experiment_id')}")

    problems, notes = check_run(records, want, args.experiment_id)
    for rec in records:
        more, latency = readback(rec, manifest, datetime.now(timezone.utc))
        problems += more
        print(f"dashboard read-back of {rec['incident_id']}: "
              f"{'ok' if not more else 'FAILED'}, detection latency {latency} s")

    logs = aws.client("logs")
    qid = logs.start_query(
        logGroupName=LOG_GROUP, startTime=int((start - timedelta(minutes=1)).timestamp()),
        endTime=int((end + timedelta(minutes=10)).timestamp()),
        queryString="fields @message | filter @message like /incident_/ | sort @timestamp asc | limit 1000")["queryId"]
    while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
        time.sleep(1)
    messages = [f["value"] for row in res["results"] for f in row if f["field"] == "@message"]
    lines = parse_lines(messages)
    steps, latency = summarize_steps(lines), summarize_latency(lines)
    print(f"steps: {steps}")
    print(f"latency: {latency}")
    if not steps["steps"]:
        problems.append("no incident_step log lines: did the Lambda receive the run's events?")

    cw = aws.client("cloudwatch")
    span = {"StartTime": start, "EndTime": end + timedelta(minutes=10), "Period": 86400}
    health = {name: sum(d["Sum"] for d in cw.get_metric_statistics(
                  Namespace="AWS/Lambda", MetricName=name, Statistics=["Sum"],
                  Dimensions=[{"Name": "FunctionName", "Value": FUNCTION}], **span)["Datapoints"])
              for name in ("Errors", "DestinationDeliveryFailures", "AsyncEventsDropped")}
    sqs = aws.client("sqs")
    url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    health["failure queue"] = int(sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
    alarm = cw.describe_alarms(AlarmNames=[ALARM])["MetricAlarms"][0]["StateValue"]
    print(f"health: {health}, alarm {alarm}")
    if any(health.values()) or alarm != "OK":
        problems.append("the Lambda had errors, failed deliveries or failed minutes (see health)")

    for note in notes:
        print(f"note: {note}")
    print("\nPASS" if not problems else "\nFAIL: " + "; ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())