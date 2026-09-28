#!/usr/bin/env bash
# Post-deploy check of the Cart endpoints against the live API.
# Self-cleaning: it writes to a throwaway user_id and deletes what it creates,
# so it leaves no residue in the Carts table.
# Run after Jake redeploys the merged PR.
#   ./smoke_test.sh <your-profile>
set -euo pipefail

PROFILE="${1:?usage: $0 <aws-profile>}"
API=$(aws ssm get-parameter --name /anomalypulse/api/base-url \
  --query Parameter.Value --output text --profile "$PROFILE" --region us-east-2)
API="${API%/}"

USER="smoke-test-user"
ITEM="smoke-test-item"
BODY=/tmp/cart_smoke_body

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

exit $fail
