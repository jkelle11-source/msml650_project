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
# path: it schedules a one-minute payment-failure fault in the fault-config table
# (the M3 control plane, as the simulator does), confirms POST /orders surfaces a
# clean 502 (not a 500) from the dependency failure, and deletes the fault
# afterwards. POST invokes payment for real, but payment is stateless (no residue
# to clean); the failure path persists no order.
#
# Never run it during an experiment: its requests and its fault would land in that
# experiment's telemetry. It refuses to start while the experiment lock is held.
#
# Requires: jq; an AWS profile with dynamodb:DeleteItem on the Orders table and
# logs:FilterLogEvents on the order and payment log groups (/aws/lambda/anomalypulse-order,
# /aws/lambda/anomalypulse-payment); and the simulator profile (default `sim`, set up
# as in Handoffs/M3-1_Handoff.md) to read the lock and write the fault. The fault is
# deleted on exit (EXIT trap), even on assertion failure or Ctrl-C, and its window
# closes on its own a minute after it opens.
# Run after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile> [simulator-profile]
set -euo pipefail

command -v jq >/dev/null || { echo "jq is required (brew install jq)" >&2; exit 2; }

PROFILE="${1:?usage: $0 <aws-profile> [simulator-profile]}"
SIM_PROFILE="${2:-sim}"
REGION=us-east-2
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
API="${API%/}"
TABLE=$(aws ssm get-parameter --name /anomalypulse/tables/orders/name \
  --query Parameter.Value --output text --profile "$PROFILE" --region "$REGION")
FAULT_TABLE=$(aws ssm get-parameter --name /anomalypulse/tables/fault-config/name \
  --query Parameter.Value --output text --profile "$SIM_PROFILE" --region "$REGION")
LOCK_TABLE=$(aws ssm get-parameter --name /anomalypulse/tables/experiments/name \
  --query Parameter.Value --output text --profile "$SIM_PROFILE" --region "$REGION")

BODY=/tmp/order_smoke_body
ORDER_ID=""
ORDER_LOG=/aws/lambda/anomalypulse-order
PAYMENT_LOG=/aws/lambda/anomalypulse-payment
FAULT_ID=""   # the payment-failure fault this run scheduled; set only while it exists

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

remove_fault() {
  # Delete the payment-failure fault this run scheduled. A no-op if none was written
  # (or it is already gone). Even if this fails, the fault's window closes on its
  # own a minute after it opened, and DynamoDB's TTL removes the item later.
  [[ -n "$FAULT_ID" ]] || return 0
  if aws dynamodb delete-item --table-name "$FAULT_TABLE" \
       --key "{\"service\": {\"S\": \"payment-service\"}, \"fault_id\": {\"S\": \"$FAULT_ID\"}}" \
       --profile "$SIM_PROFILE" --region "$REGION" >/dev/null 2>&1; then
    echo "CLEAN fault $FAULT_ID deleted from $FAULT_TABLE"
    FAULT_ID=""
  else
    echo "WARN  could not delete fault $FAULT_ID from $FAULT_TABLE; its window closes on its own"
  fi
}
trap 'cleanup; remove_fault' EXIT
# Convert Ctrl-C / SIGTERM into a normal exit so the single EXIT trap above runs the
# cleanup exactly once (rather than bash resuming the script, or not running EXIT
# reliably for these signals). SIGKILL (kill -9) and a host crash cannot be trapped
# by anything - in that case the fault still expires with its window.
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

# Guard: refuse to start while an experiment holds the lock. Telemetry is tied to
# an experiment by time alone, so this script's requests and its payment fault
# would land in that experiment's labels. A lock past its expires_at is a dead run's.
LOCK_EXPIRES=$(aws dynamodb get-item --table-name "$LOCK_TABLE" --key '{"pk": {"S": "LOCK"}}' \
  --query 'Item.expires_at.N' --output text --profile "$SIM_PROFILE" --region "$REGION")
if [[ "$LOCK_EXPIRES" != "None" && "$LOCK_EXPIRES" -ge $(date +%s) ]]; then
  echo "ABORT an experiment holds the lock (until epoch $LOCK_EXPIRES). Run this after it ends." >&2
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

# 5. live payment-failure path: schedule a payment-failure fault, as the simulator's
# payment-failure injector does, then confirm a POST /orders surfaces a clean 502
# (dependency failure) rather than a 500. Payment caches fault items for 5 s, so the
# window opens 12 s after the write, when every warm payment environment has it. The
# window lasts a minute and the EXIT trap deletes the item, so payment is never left
# failing. timeout_ms is how long payment waits before it fails.
NOW_S=$(date +%s)
FAULT_ID="smoke-test-$NOW_S#0"
FROM_MS=$(( (NOW_S + 12) * 1000 ))
UNTIL_MS=$(( FROM_MS + 60000 ))
echo "-- payment failure-path check (fault $FAULT_ID in $FAULT_TABLE) --"
aws dynamodb put-item --table-name "$FAULT_TABLE" --profile "$SIM_PROFILE" --region "$REGION" --item "$(jq -cn \
  --arg id "$FAULT_ID" --arg from "$FROM_MS" --arg until "$UNTIL_MS" --arg exp "$(( UNTIL_MS / 1000 + 3600 ))" \
  '{service: {S: "payment-service"}, fault_id: {S: $id}, injector: {S: "payment-failure"},
    params: {M: {timeout_ms: {N: "200"}}}, active_from_ms: {N: $from},
    active_until_ms: {N: $until}, expires_at: {N: $exp}}')"
WAIT_S=$(( FROM_MS / 1000 - $(date +%s) + 1 ))
(( WAIT_S > 0 )) && sleep "$WAIT_S"

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

remove_fault   # also runs via the EXIT trap; the guard makes a second call a no-op

exit $fail
