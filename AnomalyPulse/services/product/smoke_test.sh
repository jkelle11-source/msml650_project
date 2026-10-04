#!/usr/bin/env bash
# Post-deploy check of the Product service against the live stack. Covers:
#   - endpoint status codes
#   - the {data,error} response envelope
#   - CORS: Access-Control-Allow-Origin on a GET + the OPTIONS preflight
#   - end-to-end telemetry landing (handler -> CloudWatch -> shipper -> S3),
#     by chaining check_telemetry.py
# Run after seed.py and after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile> [region]
set -euo pipefail

PROFILE="${1:?usage: $0 <aws-profile> [region]}"
REGION="${2:-us-east-2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
API="${API%/}"

fail=0

check() {
  local path="$1" expected="$2" code
  code=$(curl -s -o /tmp/product_smoke_body -w '%{http_code}' "$API$path")
  if [[ "$code" == "$expected" ]]; then
    echo "PASS  GET $path -> $code"
  else
    echo "FAIL  GET $path -> $code (expected $expected)"; cat /tmp/product_smoke_body; echo
    fail=1
  fi
}

# --- Endpoint status codes ---
check /products 200
check /products/prod-001 200
check /products/does-not-exist 404

# --- Response envelope shape (last body above is the 404) ---
if grep -q '"data"' /tmp/product_smoke_body && grep -q '"error"' /tmp/product_smoke_body; then
  echo "PASS  404 body is the {data,error} envelope"
else
  echo "FAIL  404 body missing data/error envelope"; cat /tmp/product_smoke_body; echo; fail=1
fi

# --- CORS: GET response carries Access-Control-Allow-Origin ---
origin=$(curl -s -D - -o /dev/null "$API/products" | tr -d '\r' \
  | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
if [[ "$origin" == "*" ]]; then
  echo "PASS  GET /products -> Access-Control-Allow-Origin: *"
else
  echo "FAIL  GET /products missing Access-Control-Allow-Origin (got '${origin:-none}')"; fail=1
fi

# --- CORS: OPTIONS preflight answered at the API Gateway layer ---
preflight=$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS \
  -H 'Origin: https://example.com' -H 'Access-Control-Request-Method: GET' \
  "$API/products")
if [[ "$preflight" == "200" || "$preflight" == "204" ]]; then
  echo "PASS  OPTIONS /products preflight -> $preflight"
else
  echo "FAIL  OPTIONS /products preflight -> $preflight (expected 200/204)"; fail=1
fi

# --- End-to-end telemetry landing in S3 (handler -> shipper -> raw/) ---
echo "-- Verifying telemetry lands in S3 (check_telemetry.py) --"
if python3 -c 'import boto3, jsonschema' 2>/dev/null; then
  python3 "$HERE/check_telemetry.py" --profile "$PROFILE" --region "$REGION" || fail=1
else
  echo "SKIP  check_telemetry.py needs boto3 + jsonschema in the active env"
fi

exit $fail
