#!/usr/bin/env bash
# Post-deploy check of the Cart endpoints against the live API.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 2; }
python3 -c 'import jsonschema' 2>/dev/null \
  || { echo "jsonschema is required (pip install -r requirements-dev.txt)" >&2; exit 2; }

PROFILE="${1:?usage: $0 <aws-profile>}"
REGION=us-east-2
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
API="${API%/}"
BUCKET=$(aws ssm get-parameter --name /anomalypulse/telemetry/bucket/name \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")

USER="smoke-test-user"
ITEM="smoke-test-item"
BODY=/tmp/cart_smoke_body
LAYER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../layers/telemetry/python" && pwd)"
S3_PREFIX="raw/service=cart-service"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
HDRS="$TMP/headers"
SENT="$TMP/sent.tsv"   # one line per request sent: request_id <TAB> method <TAB> status
: > "$SENT"

START_EPOCH=$(( $(date +%s) - 5 ))
fmt_utc() { date -u -r "$1" +"$2" 2>/dev/null || date -u -d "@$1" +"$2"; }  # BSD, then GNU
START_ISO=$(fmt_utc "$START_EPOCH" '%Y-%m-%dT%H:%M:%S+00:00')
START_TS=$(fmt_utc "$START_EPOCH" '%Y-%m-%dT%H:%M:%S.000Z')
START_DT=$(fmt_utc "$START_EPOCH" '%Y-%m-%d')

fail=0
check() {
  local method="$1" path="$2" expected="$3" data="${4:-}" code rid
  if [[ -n "$data" ]]; then
    code=$(curl -s -D "$HDRS" -o "$BODY" -w '%{http_code}' -X "$method" \
      -H 'Content-Type: application/json' -d "$data" "$API$path")
  else
    code=$(curl -s -D "$HDRS" -o "$BODY" -w '%{http_code}' -X "$method" "$API$path")
  fi
  # API Gateway returns its request id in x-amzn-RequestId; the handler stamps the
  # same id on the telemetry record, so check_s3 can match records to requests.
  rid=$(tr -d '\r' < "$HDRS" | awk -F': *' 'tolower($1)=="x-amzn-requestid"{print $2}' | tail -1)
  printf '%s\t%s\t%s\n' "${rid:-<no-x-amzn-requestid>}" "$method" "$code" >> "$SENT"
  if [[ "$code" == "$expected" ]]; then
    echo "PASS  $method $path -> $code"
  else
    echo "FAIL  $method $path -> $code (expected $expected)"; cat "$BODY"; echo
    fail=1
  fi
}

s3_list_keys() {
  aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$1" --output json \
      --profile "$PROFILE" --region "$REGION" \
    | jq -r --arg s "$START_ISO" '.Contents[]? | select(.LastModified >= $s) | .Key'
}

check_s3() {
  echo "-- S3 landing check ($S3_PREFIX/) --"
  local err deadline want=("POST /cart" "GET /cart/{user}" "DELETE /cart/{user}/{item}")
  local dt key route missing=() rid method code got pending

  if ! err=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$S3_PREFIX/" \
               --max-items 1 --profile "$PROFILE" --region "$REGION" 2>&1 >/dev/null); then
    echo "FAIL  cannot read s3://$BUCKET/$S3_PREFIX/ (needs s3:ListBucket + s3:GetObject; ask Jake):"
    echo "      $err"
    fail=1; return
  fi

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
      'fromjson? | select(.service=="cart-service" and .timestamp >= $s)' \
      "$TMP/all.ndjson" > "$TMP/run.ndjson"
    jq -r '"\(.http_method) \(.endpoint)"' "$TMP/run.ndjson" | sort -u > "$TMP/routes.txt"

    missing=()
    for route in "${want[@]}"; do
      grep -qxF "$route" "$TMP/routes.txt" || missing+=("$route")
    done
    pending=0
    while IFS=$'\t' read -r rid method code; do
      jq -e --arg r "$rid" 'select(.request_id==$r)' "$TMP/run.ndjson" >/dev/null 2>&1 \
        || pending=$((pending + 1))
    done < "$SENT"
    [[ ( ${#missing[@]} -eq 0 && $pending -eq 0 ) || $(date +%s) -ge $deadline ]] && break
    sleep 5
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "FAIL  no record landed in S3 for: ${missing[*]}"
    echo "      (if none landed: confirm the new handler is deployed, check CloudWatch"
    echo "       /aws/lambda/anomalypulse-cart for a telemetry_error line, and look under"
    echo "       errors/service=cart-service/ for unparseable lines)"
    fail=1; return
  fi
  echo "PASS  S3: records for all 3 routes landed ($(wc -l < "$TMP/run.ndjson" | tr -d ' ') from this run)"

  # Every request this run sent must have exactly one record, carrying the
  # method and status the client actually saw.
  while IFS=$'\t' read -r rid method code; do
    got=$(jq -r --arg r "$rid" 'select(.request_id==$r) | "\(.http_method) \(.status_code)"' \
      "$TMP/run.ndjson" | paste -sd, -)
    if [[ "$got" == "$method $code" ]]; then
      echo "PASS  S3: one record for $method -> $code (request_id=$rid)"
    else
      echo "FAIL  S3: request_id=$rid expected one record '$method $code', got '${got:-<none>}'"
      fail=1
    fi
  done < "$SENT"

  local out
  if out=$(python3 - "$LAYER_DIR" "$TMP/run.ndjson" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from schema_validator import validate

n = bad = 0
for line in open(sys.argv[2]):
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

# 1. add an item to the throwaway user's cart
check POST   /cart          200 "{\"user_id\":\"$USER\",\"item_id\":\"$ITEM\",\"quantity\":3}"
# 2. it should now show up when we read the cart
check GET    "/cart/$USER"  200
if ! grep -q "$ITEM" "$BODY"; then
  echo "FAIL  GET /cart/$USER did not contain $ITEM after POST"; fail=1
fi
# 3. remove it again (self-clean)
check DELETE "/cart/$USER/$ITEM" 200
# 4. the cart should be empty now
check GET    "/cart/$USER"  200
if grep -q "$ITEM" "$BODY"; then
  echo "FAIL  GET /cart/$USER still contained $ITEM after DELETE"; fail=1
fi
# 5. edge case: deleting a missing item returns a structured 404, not a stack trace
check DELETE "/cart/$USER/$ITEM" 404

# 6. the requests above must have produced telemetry that landed in S3 and validates
check_s3

exit $fail
