#!/usr/bin/env bash
# Post-deploy check of the Order endpoints against the live API.
#
# Self-cleaning: POST /orders creates a row whose order_id is server-generated,
# and there is no DELETE /orders route, so this script removes the row it creates
# straight from DynamoDB. The delete is registered as an EXIT trap, so cleanup
# runs on every exit path (assertion failure, `set -e` abort, Ctrl-C), and
# DynamoDB's delete-item is idempotent so it is harmless when nothing was created.
#
# Covers the happy path + a 404, a live CloudWatch cross-service correlation check
# (the created order's telemetry line and the payment line it triggered must share
# one request_id -- M2-4 DoD, PROJECT_PLAN Section 3), plus a live payment-failure
# path: it flips payment's PAYMENT_FAILURE_RATE knob (M3 incident simulator) to 1.0,
# confirms POST /orders surfaces a clean 502 (not a 500) from the dependency failure,
# and restores the knob afterwards. POST invokes payment for real, but payment is
# stateless (no residue to clean); the failure path persists no order.
#
# Requires: jq, and an AWS profile with dynamodb:DeleteItem on the Orders table,
# lambda:GetFunctionConfiguration and lambda:UpdateFunctionConfiguration on the
# payment function, and logs:FilterLogEvents on the order and payment log groups
# (/aws/lambda/anomalypulse-order, /aws/lambda/anomalypulse-payment). The payment
# knob is always restored on exit (EXIT trap), even on assertion failure or Ctrl-C.
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
ORDER_LOG=/aws/lambda/anomalypulse-order
PAYMENT_LOG=/aws/lambda/anomalypulse-payment
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

# Pull telemetry records emitted since CORR_START_MS from a log group. START/END/REPORT
# lines are not JSON, so `try fromjson catch empty` drops them; each surviving line is
# one telemetry record. Filtering happens in jq (the window is only a few seconds of
# traffic), so there is no CloudWatch filter-pattern quoting to get wrong. `|| true`
# keeps a transient logs error (throttle, not-yet-created group) from aborting under
# set -e. ($rid is a jq --arg, used by the payment filter; empty for the order one.)
_logs_since() {  # _logs_since <log-group> <jq-select-filter> [request-id] -> newest match's request_id
  aws logs filter-log-events --log-group-name "$1" --start-time "$CORR_START_MS" \
      --output json --profile "$PROFILE" --region "$REGION" 2>/dev/null \
    | jq -r --arg rid "${3:-}" \
        "[.events[].message|(try fromjson catch empty)|select($2)|.request_id]|last//empty" \
    || true
}

correlate() {
  # The created order's telemetry line and the payment line that order's dependency
  # call produced must carry the SAME request_id, so the two services' records join
  # (M2-4 DoD). CloudWatch ingestion lags the request by a few seconds, so poll both
  # groups until each appears or a 60s deadline passes. We read order's POST/201 line
  # to learn the id (it is not in the HTTP response body), then look for a
  # payment-service line stamped with that id.
  echo "-- CloudWatch correlation check (order & payment share request_id) --"
  local deadline=$(( $(date +%s) + 60 )) req="" pay=""
  while [[ $(date +%s) -lt $deadline ]]; do
    [[ -z "$req" ]] && req=$(_logs_since "$ORDER_LOG" \
      '.service=="order-service" and .http_method=="POST" and .endpoint=="/orders" and .status_code==201')
    if [[ -n "$req" ]]; then
      pay=$(_logs_since "$PAYMENT_LOG" '.service=="payment-service" and .request_id==$rid' "$req")
      [[ -n "$pay" ]] && break
    fi
    sleep 4
  done
  if [[ -n "$req" && "$pay" == "$req" ]]; then
    echo "PASS  correlation: order & payment share request_id=$req"
  else
    echo "FAIL  correlation: order request_id='${req:-<none>}', payment match='${pay:-<none>}'"
    echo "      (telemetry can lag; confirm both Lambdas deployed and logs:FilterLogEvents is granted)"
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

# Window start for the correlation check below. Whole-second epoch *1000 (BSD `date`
# has no %N for ms), minus 5s of slack so clock skew can't push it past our request.
CORR_START_MS=$(( ($(date +%s) - 5) * 1000 ))

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

# 2b. the create above drove order -> payment; confirm both telemetry records
# landed in CloudWatch carrying the same request_id (cross-service correlation).
correlate

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
