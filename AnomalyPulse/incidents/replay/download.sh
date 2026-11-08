#!/bin/bash
# Download what replay.py reads: datasets/v1's validation runs (slots 561-700) and the two models the
# deployed Diagnosis Lambda serves, each at its promoted pointer's version. Read-only, about 940 MB.
#
#     AnomalyPulse/incidents/replay/download.sh <dir>
#
# Needs the AWS CLI, python3 and a profile with AnomalyPulseDeveloper (default `dev`; set AWS_PROFILE).
set -euo pipefail
[ $# -eq 1 ] || { sed -n '5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
export AWS_PROFILE="${AWS_PROFILE:-dev}" AWS_REGION="${AWS_REGION:-us-east-2}"
DIR=$1
mkdir -p "$DIR/data" "$DIR/models/lr" "$DIR/models/if"

B=$(aws ssm get-parameter --name /anomalypulse/telemetry/bucket/name --query Parameter.Value --output text)
aws s3 cp "s3://$B/datasets/v1/tier2/experiments.jsonl" "$DIR/data/experiments.jsonl" --only-show-errors
IDS=$(python3 -c '
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
print(" ".join(r["experiment_id"] for r in rows if 561 <= r["slot"] <= 700))' "$DIR/data/experiments.jsonl")
INCLUDE=()
for id in $IDS; do INCLUDE+=(--include "experiment_id=$id/*"); done
PIDS=()
for part in tier1/requests tier1/metrics tier2/labels tier2/manifests; do
  aws s3 sync "s3://$B/datasets/v1/$part/" "$DIR/data/$part/" --exclude "*" "${INCLUDE[@]}" --only-show-errors &
  PIDS+=($!)
done
for pid in "${PIDS[@]}"; do wait "$pid"; done

fetch() {  # fetch <pointer parameter> <folder>: the promoted package's model.json and model.joblib
  aws ssm get-parameter --name "$1" --query Parameter.Value --output text > "$2/pointer.json"
  read -r bucket key version < <(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
bucket, key = p["uri"][len("s3://"):].split("/", 1)
print(bucket, key, p["version_id"])' "$2/pointer.json")
  aws s3api get-object --bucket "$bucket" --key "$key" --version-id "$version" "$2/model.tar.gz" > /dev/null
  tar -xzf "$2/model.tar.gz" -C "$2" model.json model.joblib
}
fetch /anomalypulse/ml/models/incident_type "$DIR/models/lr"
fetch /anomalypulse/ml/models/anomaly_iforest "$DIR/models/if"

echo "runs: $(echo $IDS | wc -w | tr -d ' ')"
du -sh "$DIR/data" "$DIR/models"
