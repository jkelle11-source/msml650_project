#!/usr/bin/env bash
# Post-deploy check of the Product endpoints against the live API.
# Run after seed.py and after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

PROFILE="${1:?usage: $0 <aws-profile>}"
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region us-east-2)
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

check /products 200
check /products/prod-001 200
check /products/does-not-exist 404

curl -s "$API/products/prod-001"; echo
exit $fail
