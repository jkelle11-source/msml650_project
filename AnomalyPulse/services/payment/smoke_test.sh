#!/usr/bin/env bash
# Post-deploy check of the Payment endpoint against the live API.
# Stateless: no DB writes, nothing to clean up. Covers the default happy path
# only -- the fault-injection knobs (PAYMENT_LATENCY_MS / PAYMENT_FAILURE_RATE /
# PAYMENT_TIMEOUT) are env vars driven by the M3 incident simulator, not here.
# Run after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

PROFILE="${1:?usage: $0 <aws-profile>}"
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region us-east-2)
API="${API%/}"

BODY=/tmp/payment_smoke_body

fail=0
code=$(curl -s -o "$BODY" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"smoke-test-order","amount":19.99}' "$API/payments")

if [[ "$code" == "200" ]] && grep -q '"outcome": "success"' "$BODY"; then
  echo "PASS  POST /payments -> $code (outcome success)"
else
  echo "FAIL  POST /payments -> $code (expected 200 + outcome success)"; cat "$BODY"; echo
  fail=1
fi

exit $fail
