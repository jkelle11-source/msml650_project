"""The stored diagnosis as the dashboard feed shows it (M8-4). Pure, except main(), which reads the table.

M8-3 stores Bedrock's diagnosis in the dashboard's shape already, so the mapping is record.to_incident:
the five sections as stored, or None while the diagnosis is held back (record.HOLD_KINDS: causal or
ordering wording, or a bullet that cites no input metric) or was made for another type than the
incident's current one (record.for_another_type). check() verifies the hand-off for one record against
what the dashboard returned for it, after the JSON round trip the read API makes:

1. every section intact: severity, summary, root cause, the evidence bullets and the actions in order,
   exactly as stored, and nothing else in the diagnosis;
2. no ordering claim rendered as causation: the shown text, checked again against the input it was made
   from (`diagnosis_input`) by genai.self_check, has no "wording" finding;
3. every evidence bullet still traces to an input metric: one `evidence_metrics` entry per bullet, none
   empty, each a metric of the input, and no "citation" finding.

Other findings (a number, a severity disagreement) are notes: they are shown, and M9-5 scores them.

A record stored before #102 kept no input (`diagnosis_input`, nor `retried_for`), so for it items 2 and 3 are
checked only as far as they can be without the input, as live_check allows those records: a note, not a failure.

    cd AnomalyPulse
    python3 -m incidents.feed --profile <profile>              # the 10 newest stored incidents
    python3 -m incidents.feed --profile <profile> --limit 3
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from genai import contract, runbooks, self_check
from incidents import live_check, record

SECTIONS = ("severity", "summary", "root_cause", "evidence", "recommended_actions")
# Findings that break the Definition of Done (items 2 and 3); every other kind is a note. The same kinds
# as record.HOLD_KINDS (test_feed checks): what the feed holds back is what it must never show.
FAILING_KINDS = {"wording": "an ordering claim rendered as causation",
                 "citation": "a bullet that traces to no input metric"}


def raw_diagnosis(diagnosis, evidence_metrics):
    """The shown diagnosis as Bedrock answered it: each evidence bullet with the metrics it cites."""
    return {**{k: diagnosis[k] for k in SECTIONS if k != "evidence"},
            "evidence": [{"statement": s, "metrics": list(m)}
                         for s, m in zip(diagnosis["evidence"], evidence_metrics or [])]}


def _runbook_text(name):
    return runbooks.runbook_for(name.upper())[1] if name else None


def check(rec, shown):
    """(problems, notes) for one stored record and the dashboard's `incident` object for it."""
    problems, notes = [], []
    if shown is None:
        return ["the dashboard shows no incident for the record"], notes
    if shown.get("incident_id") != rec["incident_id"] or shown.get("prediction") != rec["prediction"]:
        problems.append("the dashboard's incident_id or prediction is not the stored one")

    stored, diagnosis = rec.get("diagnosis"), shown.get("diagnosis")
    if record.held_back(rec):
        if diagnosis is not None:
            problems.append("a held-back diagnosis reached the dashboard")
        kinds = sorted(record.finding_kinds(rec.get("self_check")) & set(record.HOLD_KINDS))
        notes.append(f"diagnosis held back ({', '.join(kinds)}): the console shows Severity pending")
        return problems, notes
    if record.for_another_type(rec):
        if diagnosis is not None:
            problems.append("a diagnosis made for another type reached the dashboard")
        notes.append(f"diagnosis made for {rec.get('diagnosis_for_type')}, not "
                     f"{rec['prediction']['incident_type']}: the console shows Severity pending")
        return problems, notes
    if stored is None:
        if diagnosis is not None:
            problems.append("the dashboard shows a diagnosis the record doesn't have")
        return problems, notes
    if diagnosis is None:
        return problems + ["the stored diagnosis did not reach the dashboard"], notes

    # 1. Every section intact.
    if set(diagnosis) != set(SECTIONS):
        problems.append(f"the diagnosis's sections are {sorted(diagnosis)}, not {sorted(SECTIONS)}")
    problems += [f"{key} is not as stored" for key in SECTIONS if diagnosis.get(key) != stored.get(key)]
    if problems:
        return problems, notes

    # 3. Every bullet traces to an input metric, by its citations.
    cited = rec.get("evidence_metrics")
    if cited is None or len(cited) != len(diagnosis["evidence"]):
        return problems + [f"{len(cited or [])} evidence_metrics entries for "
                           f"{len(diagnosis['evidence'])} evidence bullets"], notes
    given = rec.get("diagnosis_input")
    if given is None and "retried_for" not in rec:
        # Stored before #102: there is no input to check the citations and the wording against.
        problems += [f"evidence[{i}] cites no metric" for i, metrics in enumerate(cited) if not metrics]
        notes.append("stored before #102 (no diagnosis_input): citations and wording not checked against the input")
        return problems, notes
    if given is None:
        return problems + ["no diagnosis_input: traceability and wording can't be checked again"], notes
    refs = {contract.metric_ref(m["service"], m["metric"]) for m in given["metrics"]}
    for i, metrics in enumerate(cited):
        if not metrics:
            problems.append(f"evidence[{i}] cites no metric")
        problems += [f"evidence[{i}] cites {ref}, which is not in the input" for ref in metrics if ref not in refs]

    # 2 and 3 again: the shown text, checked against the input it was made from.
    raw = raw_diagnosis(diagnosis, cited)
    findings = contract.check_output_shape(raw) + self_check.check(given, raw, runbook=_runbook_text(rec.get("runbook")))
    for finding in findings:
        kind = self_check.kind(finding)
        if kind in FAILING_KINDS:
            problems.append(f"{FAILING_KINDS[kind]}: {finding}")
        else:
            notes.append(finding)
    return problems, notes


def feed_view(rec, now):
    """The dashboard's `incident` object for a record, after the read API's JSON round trip."""
    builder = live_check._dashboard_builder()
    result = builder.build_dashboard(live_check.quiet_window(now), record.to_incident(rec), None, now)
    return json.loads(json.dumps(result))["incident"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="anomalypulse")
    parser.add_argument("--limit", type=int, default=10, help="how many of the newest incidents (default 10)")
    args = parser.parse_args(argv)

    import boto3
    from boto3.dynamodb.conditions import Key

    aws = boto3.Session(profile_name=args.profile, region_name="us-east-2")
    name = aws.client("ssm").get_parameter(Name="/anomalypulse/tables/incidents/name")["Parameter"]["Value"]
    items = aws.resource("dynamodb").Table(name).query(
        KeyConditionExpression=Key("pk").eq(record.PK), ScanIndexForward=False, Limit=args.limit)["Items"]

    failed = 0
    now = datetime.now(timezone.utc)
    for rec in (record.from_dynamodb(item) for item in items):
        problems, notes = check(rec, feed_view(rec, now))
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'PASS'}  {rec['incident_id']}  {rec['prediction']['incident_type']}, "
              f"{rec['status']}, {len((rec.get('diagnosis') or {}).get('evidence') or [])} bullets")
        for line in problems:
            print(f"      ! {line}")
        for line in notes:
            print(f"      note: {line}")
    print(f"\n{len(items) - failed} of {len(items)} pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
