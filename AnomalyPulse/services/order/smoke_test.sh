#!/usr/bin/env bash
# Post-deploy check of the Order endpoints against the live API.
#
# Self-cleaning: POST /orders creates a row whose order_id is server-generated,
# and there is no DELETE /orders route, so this script removes the row it creates
# straight from DynamoDB. The delete is registered as an EXIT trap, so cleanup
# runs on every exit path (assertion failure, `set -e` abort, Ctrl-C), and
# DynamoDB's delete-item is idempotent so it is harmless when nothing was created.
#
# Covers the happy path + a 404. The payment-failure path needs payment's env
# knobs flipped (M3 incident simulator), so it is out of scope here. POST invokes
# payment for real, but payment is stateless (no residue to clean).
#
# Requires: jq, and an AWS profile with dynamodb:DeleteItem on the Orders table.
# Run after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 2; }

PROFILE="${1:?usage: $0 <aws-profile>}"
REGION=us-east-2
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
API="${API%/}"
TABLE=$(aws ssm get-parameter --name /anomalypulse/tables/orders/name \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")

BODY=/tmp/order_smoke_body
ORDER_ID=""

cleanup() {
  # Remove the order this run created, wherever the script exits. delete-item is
  # idempotent, so a missing key (e.g. the POST failed) is not an error.
  if [[ -n "$ORDER_ID" ]]; then
    if aws dynamodb delete-item --table-name "$TABLE" \
         --key "{\"order_id\": {\"S\": \"$ORDER_ID\"}}" \
         --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
      echo "CLEAN order $ORDER_ID deleted from $TABLE"
    else
      echo "WARN  could not delete order $ORDER_ID from $TABLE (check dynamodb:DeleteItem)"
    fi
  fi
}
trap cleanup EXIT

fail=0
check() {
  # check <method> <path> <expected-code> [json-body]
  local method="$1" path="$2" expected="$3" data="${4:-}" code
  if [[ -n "$data" ]]; then
    code=$(curl -s -o "$BODY" -w '%{http_code}' -X "$method" \
      -H 'Content-Type: application/json' -d "$data" "$API$path")
  else
    code=$(curl -s -o "$BODY" -w '%{http_code}' -X "$method" "$API$path")
  fi
  if [[ "$code" == "$expected" ]]; then
    echo "PASS  $method $path -> $code"
  else
    echo "FAIL  $method $path -> $code (expected $expected)"; cat "$BODY"; echo
    fail=1
  fi
}

# 1. create an order (invokes payment for real; payment is stateless)
check POST /orders 201 \
  '{"customer_id":"smoke-test-customer","items":[{"product_id":"prod-001","price":19.99,"quantity":2}]}'
ORDER_ID=$(jq -r '.data.order_id // empty' "$BODY")
if [[ -z "$ORDER_ID" ]]; then
  echo "FAIL  POST /orders did not return data.order_id"; fail=1
fi

# 2. the created order should be retrievable by id
if [[ -n "$ORDER_ID" ]]; then
  check GET "/orders/$ORDER_ID" 200
fi

# 3. an unknown id returns a structured 404, not a stack trace
check GET "/orders/does-not-exist" 404

# 4. delete the row we created, then confirm it is really gone over the API
if [[ -n "$ORDER_ID" ]]; then
  cleanup
  check GET "/orders/$ORDER_ID" 404
  ORDER_ID=""   # cleaned already; disarm the EXIT trap's re-delete
fi

exit $fail
