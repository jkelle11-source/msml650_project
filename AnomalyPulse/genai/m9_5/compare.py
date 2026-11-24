"""The runbook comparison (M9-5, from M6-2): the same test-set incidents with and without runbooks.

    cd AnomalyPulse
    python -m genai.m9_5.compare --profile <profile> --dry-run    # the sample and what it would cost
    python -m genai.m9_5.compare --profile <profile>              # calls Bedrock, about 4 cents a call

Without runbooks is the stored diagnosis: what the live store kept for the test set, retry included, from
the request the switch off sends (byte-for-byte M6-1's, Docs/DIAGNOSIS_CONTRACT.md). With runbooks is a new
call on the stored input (`diagnosis_input`), through the store's own retry rule: one retry with the
findings when the self-check finds a RETRY_KINDS kind, kept by record.retry_kept. Same model and effort as
the live Lambda.

The sample is SAMPLE_PER_TYPE incidents of each classified type, drawn with SEED. Results go to
results/with_runbooks/<incident_id>.json; an incident already there is skipped, so a stopped run resumes.
"""
import argparse
import json
import os
import random
import sys

from genai.m9_5 import score
from incidents import record

SAMPLE_PER_TYPE = 6
SEED = 20261119
OUT = score.RESULTS / "with_runbooks"
COST_PER_CALL = 0.038     # The 38 with-runbooks calls' own tokens, at Sonnet 4.6's $3 in / $15 out per million


def sample(records):
    """SAMPLE_PER_TYPE test incidents of each classified type, in incident order."""
    rng, picked = random.Random(SEED), []
    for t in score.TYPES:
        pool = sorted((r for r in records if r["phase"] == "test" and r["diagnosis_input"]["incident_type"] == t),
                      key=lambda r: r["incident_id"])
        picked += rng.sample(pool, SAMPLE_PER_TYPE)
    return sorted(picked, key=lambda r: r["incident_id"])


def with_runbooks(evidence, diagnose):
    """The diagnosis the store would keep with runbooks on: first call, then its one retry if flagged."""
    first = diagnose(evidence, runbooks=True)
    calls = [first]
    stored = {"diagnosis": first["diagnosis"], "evidence_metrics": first["evidence_metrics"],
              "self_check": first["self_check"]}
    if record.finding_kinds(first["self_check"]) & set(record.RETRY_KINDS):
        retry = diagnose(evidence, runbooks=True, revise=record.revision(stored))
        calls.append(retry)
        if record.retry_kept(stored, retry):
            stored = {"diagnosis": retry["diagnosis"], "evidence_metrics": retry["evidence_metrics"],
                      "self_check": retry["self_check"]}
    return {**stored, "runbook": first["runbook"], "calls": calls}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="AWS profile allowed to call Bedrock")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    picked = sample(score.load())
    todo = [r for r in picked if not (OUT / f"{r['incident_id']}.json").exists()]
    print(f"{len(picked)} incidents, {len(todo)} to run: about {len(todo) * 1.3:.0f} calls, "
          f"${len(todo) * 1.3 * COST_PER_CALL:.2f}")
    if args.dry_run:
        for r in picked:
            print(f"  {r['incident_id']}  {r['diagnosis_input']['incident_type']}")
        return 0
    os.environ["AWS_PROFILE"] = args.profile
    from genai import explain
    OUT.mkdir(parents=True, exist_ok=True)
    for r in todo:
        result = with_runbooks(r["diagnosis_input"], explain.diagnose)
        (OUT / f"{r['incident_id']}.json").write_text(json.dumps(result, indent=1) + "\n")
        kinds = sorted(record.finding_kinds(result["self_check"]))
        print(f"  {r['incident_id']}  {r['diagnosis_input']['incident_type']:<20} "
              f"{len(result['calls'])} call(s)  {kinds or 'clean'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
