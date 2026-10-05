#!/usr/bin/env bash
# Post-deploy check of the Payment endpoint against the live API, plus an
# S3-landing check (handler -> CloudWatch -> shipper -> raw/service=payment-service/)
# that re-validates the landed record against the shared telemetry schema.
#
# Stateless: no DB writes, nothing to clean up. The fault-injection knobs
# (PAYMENT_LATENCY_MS / PAYMENT_FAILURE_RATE / PAYMENT_TIMEOUT) are env vars driven
# by the M3 incident simulator, not here, so this covers the default happy path.
# Run after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 2; }

PROFILE="${1:?usage: $0 <aws-profile>}"
REGION=us-east-2
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
API="${API%/}"
BUCKET=$(aws ssm get-parameter --name /anomalypulse/telemetry/bucket/name \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")

LAYER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../layers/telemetry/python" && pwd)"
S3_PREFIX="raw/service=payment-service"
BODY=/tmp/payment_smoke_body
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
HDRS="$TMP/headers"

START_EPOCH=$(( $(date +%s) - 5 ))
fmt_utc() { date -u -r "$1" +"$2" 2>/dev/null || date -u -d "@$1" +"$2"; }  # BSD, then GNU
START_ISO=$(fmt_utc "$START_EPOCH" '%Y-%m-%dT%H:%M:%S+00:00')
START_TS=$(fmt_utc "$START_EPOCH" '%Y-%m-%dT%H:%M:%S.000Z')
START_DT=$(fmt_utc "$START_EPOCH" '%Y-%m-%d')

fail=0

# 1. live happy path. -D captures the response headers so we can read the
# x-amzn-RequestId API Gateway returns; the handler stamps that same id on the
# telemetry record, so check_s3 can match the landed record to this request.
code=$(curl -s -D "$HDRS" -o "$BODY" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"smoke-test-order","amount":19.99}' "$API/payments")
RID=$(tr -d '\r' < "$HDRS" | awk -F': *' 'tolower($1)=="x-amzn-requestid"{print $2}' | tail -1)
if [[ "$code" == "200" ]] && grep -q '"outcome": "success"' "$BODY"; then
  echo "PASS  POST /payments -> $code (outcome success, request_id=${RID:-<none>})"
else
  echo "FAIL  POST /payments -> $code (expected 200 + outcome success)"; cat "$BODY"; echo
  fail=1
fi

s3_list_keys() {
  aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --output json \
      --profile "$PROFILE" --region "$REGION" \
    | jq -r --arg s "$START_ISO" '.Contents[]? | select(.LastModified >= $s) | .Key'
}

check_s3() {
  echo "-- S3 landing check ($S3_PREFIX/) --"
  local err deadline dt key got

  if ! err=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$S3_PREFIX/" \
               --max-items 1 --profile "$PROFILE" --region "$REGION" 2>&1 >/dev/null); then
    echo "FAIL  cannot read s3://$BUCKET/$S3_PREFIX/ (needs s3:ListBucket + s3:GetObject; ask Jake):"
    echo "      $err"
    fail=1; return
  fi

  # CloudWatch -> shipper has a few seconds of lag; poll until our record appears
  # (matched by request_id when we have one) or the deadline passes.
  deadline=$(( $(date +%s) + ${S3_WAIT_SECS:-120} ))
  while :; do
    : > "$TMP/all.ndjson"
    for dt in $(printf '%s\n' "$START_DT" "$(date -u +%F)" | sort -u); do
      for key in $(s3_list_keys "$S3_PREFIX/dt=$dt/" || true); do
        aws s3 cp "s3://$BUCKET/$key" - --profile "$PROFILE" --region "$REGION" \
          2>/dev/null >> "$TMP/all.ndjson" || true
        echo >> "$TMP/all.ndjson"
      done
    done
    jq -cR --arg s "$START_TS" \
      'fromjson? | select(.service=="payment-service" and .timestamp >= $s)' \
      "$TMP/all.ndjson" > "$TMP/run.ndjson"
    if [[ -n "$RID" ]]; then
      jq -e --arg r "$RID" 'select(.request_id==$r)' "$TMP/run.ndjson" >/dev/null 2>&1 && break
    else
      [[ -s "$TMP/run.ndjson" ]] && break
    fi
    [[ $(date +%s) -ge $deadline ]] && break
    sleep 5
  done

  if [[ -n "$RID" ]]; then
    got=$(jq -r --arg r "$RID" 'select(.request_id==$r) | "\(.http_method) \(.status_code)"' \
      "$TMP/run.ndjson" | paste -sd, -)
    if [[ "$got" == "POST 200" ]]; then
      echo "PASS  S3: one record for POST -> 200 (request_id=$RID)"
    else
      echo "FAIL  S3: request_id=$RID expected 'POST 200', got '${got:-<none>}'"; fail=1
    fi
  elif [[ -s "$TMP/run.ndjson" ]]; then
    echo "PASS  S3: payment-service record(s) landed ($(wc -l < "$TMP/run.ndjson" | tr -d ' ') from this run)"
  else
    echo "FAIL  no payment-service record landed in S3"
    echo "      (confirm the handler is deployed; check CloudWatch /aws/lambda/anomalypulse-payment"
    echo "       for a telemetry_error line, and errors/service=payment-service/ for unparseable lines)"
    fail=1; return
  fi

  # Re-validate the landed records against the shared schema. jsonschema is
  # required: a missing lib is a FAIL (not a silent skip) so an un-validated run
  # can't look clean -- pip install -r requirements-dev.txt (or use the venv).
  if ! python3 -c 'import jsonschema' 2>/dev/null; then
    echo "FAIL  schema: jsonschema not installed for $(command -v python3); landed records were"
    echo "      not validated locally. pip install -r requirements-dev.txt (or use the venv), re-run."
    fail=1; return
  fi
  local out
  if out=$(python3 - "$LAYER_DIR" "$TMP/run.ndjson" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from schema_validator import validate

n = bad = 0
for line in open(sys.argv[2]):
    if not line.strip():
        continue
    n += 1
    ok, errors = validate(json.loads(line))
    if not ok:
        bad += 1
        print(f"INVALID: {errors[0].message}: {line.strip()}")
print(f"{n} records, {bad} invalid")
sys.exit(1 if bad else 0)
PY
  ); then
    echo "PASS  schema: $out"
  else
    echo "FAIL  schema: $out"; fail=1
  fi
}

# 2. the request above must have produced telemetry that landed in S3 and validates
check_s3

exit $fail
