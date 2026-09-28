#!/usr/bin/env bash
# Post-deploy check of the Order endpoints against the live API.
#
# Self-cleaning: POST /orders creates a row whose order_id is server-generated,
# and there is no DELETE /orders route, so this script removes the row it creates
# straight from DynamoDB. The delete is registered as an EXIT trap, so cleanup
# runs on every exit path (assertion failure, `set -e` abort, Ctrl-C), and
# DynamoDB's delete-item is idempotent so it is harmless when nothing was created.
#
# Covers the happy path + a 404, plus a live payment-failure path: it flips
# payment's PAYMENT_FAILURE_RATE knob (M3 incident simulator) to 1.0, confirms
# POST /orders surfaces a clean 502 (not a 500) from the dependency failure, and
# restores the knob afterwards. POST invokes payment for real, but payment is
# stateless (no residue to clean); the failure path persists no order.
#
# Requires: jq, and an AWS profile with dynamodb:DeleteItem on the Orders table
# plus lambda:GetFunctionConfiguration and lambda:UpdateFunctionConfiguration on
# the payment function. The payment knob is always restored on exit (EXIT trap),
# even on assertion failure or Ctrl-C.
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
PAYMENT_FN=anomalypulse-payment
PAYMENT_ENV_SAVED=""   # original payment env Variables JSON; set only while the knob is flipped

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

restore_payment() {
  # Put payment's env back exactly as it was before we flipped the failure knob.
  # Guarded so it is a no-op if we never flipped it (or already restored), which is
  # why the whole original Variables map is written back verbatim rather than just
  # zeroing PAYMENT_FAILURE_RATE (update-function-configuration replaces the map).
  [[ -n "$PAYMENT_ENV_SAVED" ]] || return 0

  local want got
  want=$(printf '%s' "$PAYMENT_ENV_SAVED" | jq -r '.PAYMENT_FAILURE_RATE // empty')

  if aws lambda update-function-configuration --function-name "$PAYMENT_FN" \
       --environment "{\"Variables\": $PAYMENT_ENV_SAVED}" \
       --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 \
     && aws lambda wait function-updated --function-name "$PAYMENT_FN" \
       --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
    # Verify the knob actually reads back as the saved value - a successful API call
    # is not proof the config took, and a silently-flipped payment would poison
    # every later order. Only clear the saved state once the read-back confirms it.
    got=$(aws lambda get-function-configuration --function-name "$PAYMENT_FN" \
      --query 'Environment.Variables.PAYMENT_FAILURE_RATE' --output text \
      --profile "$PROFILE" --region "$REGION" 2>/dev/null || echo "<read-failed>")
    if [[ "$got" == "$want" ]]; then
      echo "CLEAN payment env restored ($PAYMENT_FN, PAYMENT_FAILURE_RATE=$got)"
      PAYMENT_ENV_SAVED=""
      return 0
    fi
    echo "WARN  payment restore did NOT verify: PAYMENT_FAILURE_RATE reads '$got', expected '$want'"
  else
    echo "WARN  could not restore payment env on $PAYMENT_FN"
  fi
  # Leave PAYMENT_ENV_SAVED set so the EXIT-trap call retries once more, and make the
  # unclean state impossible to miss.
  echo "WARN  >>> ACTION NEEDED: set PAYMENT_FAILURE_RATE='$want' on $PAYMENT_FN (or redeploy payment)"
  fail=1
  return 1
}
trap 'cleanup; restore_payment' EXIT
# Convert Ctrl-C / SIGTERM into a normal exit so the single EXIT trap above runs the
# cleanup exactly once (rather than bash resuming the script, or not running EXIT
# reliably for these signals). SIGKILL (kill -9) and a host crash cannot be trapped
# by anything - in that case reset PAYMENT_FAILURE_RATE manually.
trap 'echo; echo "interrupted - cleaning up"; exit 130' INT TERM

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

# Guard: refuse to start if payment is already failing. That means a previous run
# did not restore the knob - proceeding would make the happy-path checks below fail
# confusingly, and step 5 would save '1.0' as the "original" and restore back to it,
# masking the stuck state permanently. Reset it (or redeploy payment) first.
START_RATE=$(aws lambda get-function-configuration --function-name "$PAYMENT_FN" \
  --query 'Environment.Variables.PAYMENT_FAILURE_RATE' --output text \
  --profile "$PROFILE" --region "$REGION")
if [[ "$START_RATE" != "0.0" && "$START_RATE" != "0" ]]; then
  echo "ABORT $PAYMENT_FN PAYMENT_FAILURE_RATE is '$START_RATE', not 0.0 - a previous run may not have cleaned up." >&2
  echo "      Reset PAYMENT_FAILURE_RATE to 0.0 (or redeploy payment) before running this smoke test." >&2
  exit 2
fi

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

# 5. live payment-failure path: force payment to fail every call, then confirm a
# POST /orders surfaces a clean 502 (dependency failure) rather than a 500. We save
# payment's current env first and restore it on exit (EXIT trap), so the knob is
# never left flipped even if the assertion below fails. Merge only PAYMENT_FAILURE_RATE
# so payment's other knobs are preserved.
echo "-- payment failure-path check (flips PAYMENT_FAILURE_RATE=1.0 on $PAYMENT_FN) --"
PAYMENT_ENV_SAVED=$(aws lambda get-function-configuration --function-name "$PAYMENT_FN" \
  --query 'Environment.Variables' --output json --profile "$PROFILE" --region "$REGION")
NEW_ENV=$(printf '%s' "$PAYMENT_ENV_SAVED" | jq -c '. + {"PAYMENT_FAILURE_RATE": "1.0"}')
aws lambda update-function-configuration --function-name "$PAYMENT_FN" \
  --environment "{\"Variables\": $NEW_ENV}" --profile "$PROFILE" --region "$REGION" >/dev/null
aws lambda wait function-updated --function-name "$PAYMENT_FN" --profile "$PROFILE" --region "$REGION"

FAIL_BODY=/tmp/order_smoke_fail_body
code=$(curl -s -o "$FAIL_BODY" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":"smoke-test-customer","items":[{"product_id":"prod-001","price":19.99,"quantity":2}]}' \
  "$API/orders")
# A payment dependency failure must be a 5xx (502), never a 4xx and never a 500.
if [[ "$code" == "502" ]]; then
  echo "PASS  POST /orders (payment forced to fail) -> 502"
else
  echo "FAIL  POST /orders (payment forced to fail) -> $code (expected 502)"; cat "$FAIL_BODY"; echo
  fail=1
fi

restore_payment || true   # also runs via the EXIT trap; the guard makes a clean second call a no-op

exit $fail
