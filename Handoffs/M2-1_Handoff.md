# M2-1 Handoff — Telemetry Contract, Validator Layer & S3 Landing Zone

> **The one-sentence version:** Every request your service handles must now emit **one structured JSON telemetry line** that validates against the frozen schema — Jake has already built the schema, a shared validator you import from a Lambda layer, and the entire pipeline that carries your printed line into S3, so your M2 job is just to **`print()` a valid record per request**. (Rachel also builds one extra thing — the CloudWatch metrics extractor — see §6.) Everything Jake built is **live** in `us-east-2` (stack `anomalypulse-shared`).

---

## 1. Mental model (read this if telemetry is new to you)

**Telemetry is structured data, not log text.** In M1 you may have `print()`ed human-readable log lines. In M2 each request emits **one JSON object** with a fixed set of fields. The reason is the whole point of the project: downstream, an ML model reads these records to detect and classify incidents. A model can't learn from `"order failed, took a while"`; it can learn from `{"latency_ms": 1842, "status_code": 500, ...}`.

**You just `print()` it — the plumbing is already built.** When your handler prints a JSON line to stdout, it lands in CloudWatch Logs.

**The schema is enforced, not a suggestion.** There's a real JSON Schema plus a validator you import. The schema uses `additionalProperties: false`, which means: emit a field that isn't in the schema — a typo, an extra field, or a forbidden one — and the record is **rejected**. This is deliberate; it's what keeps five people's telemetry identical.

**Two tiers of fields, and they never mix.** *Tier-1* (and *correlation*) fields are what a real monitor could observe — latency, status codes, DB capacity. *Tier-2* fields reveal the **cause** of a failure (`error_type`, `db_throttled`, the incident label). **A service must never emit a Tier-2 field.** If a cause-revealing field entered the model's input, the ML would be "solved by cheating" and every accuracy number would be meaningless.

---

## 2. What you do: emit a valid telemetry record

Build a dict with the required fields and print it as a single JSON line, once per request. The validator lives in the layer, so `import` works in your deployed Lambda with no setup:

```python
import json
from datetime import datetime, timezone
from schema_validator import validate   # provided by the anomalypulse-telemetry layer

def _now_ms() -> str:
    # ms precision, UTC, trailing Z — matches the schema regex (exactly 3 decimals)
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def emit(record: dict):
    ok, errors = validate(record)
    if not ok:
        # NEVER let telemetry crash the request. Log the problem and move on.
        print(json.dumps({"telemetry_error": str(errors[0].message)}))
        return
    print(json.dumps(record))   # one line → CloudWatch → shipper → S3
```

Three rules that trip people up (full details in [`Docs/TELEMETRY_CONTRACT.md`](../Docs/TELEMETRY_CONTRACT.md)):

1. **Emit all 14 fields every time. If one doesn't apply, emit `null` — never omit it.** (Product has no dependency, so it emits `"dependency": null`, not a missing key.)
2. **`timestamp` is millisecond precision, UTC, ending in `Z`** — e.g. `2026-09-30T22:12:42.081Z`. The schema regex demands exactly three decimal places, so a bare `.isoformat()` (6-digit microseconds) will **fail**. Use the `_now_ms()` helper above (`isoformat(timespec="milliseconds")` pins it to 3).
3. **`request_id` ties a request together across services.** Source it the same way every service already does — API Gateway's id, falling back to the Lambda context id:

   ```python
   request_id = (event.get("requestContext") or {}).get("requestId") or getattr(context, "aws_request_id", None)
   ```

   And **propagate it downstream**: when Order invokes Payment, it injects `{"requestContext": {"requestId": request_id}}` into the invoke payload, and Payment reads `requestContext.requestId` first — so both records carry the *same* id and can be joined by correlation. (Already implemented in Order/Payment; mirror the pattern if your service ever calls another.)

**Validate in your tests, log-and-continue in production.** A malformed telemetry line must never take down a real request — hence the `emit()` above swallows the error. But your **unit tests** should assert `validate(record)[0] is True` (or use `validate_or_raise`) so you catch schema drift before it ships.

---

## 4. The fields (summary — see the contract doc for the full table)

**Correlation** (identity/timing; emitted by everyone, never a model feature):
`timestamp`, `request_id`, `service`, `endpoint`, `http_method`

**Tier-1 observable** (may become ML features; emit on every event, `null` if N/A):
`status_code`, `latency_ms`, `lambda_duration_ms`, `cold_start`, `db_latency_ms`, `db_consumed_capacity`, `dependency`, `dependency_latency_ms`, `dependency_error`

**Tier-2 ground-truth** (❌ **NEVER emit from a service** — labeling/eval only):
`error_type`, `db_throttled`, `incident_type`, `severity`, `fault_injection_params`

> `db_throttled` (a Tier-2 boolean you must **not** emit) is different from the observable CloudWatch **`ThrottledRequests` count** (a legitimate Tier-1 signal Rachel's extractor collects). Count = observable; boolean flag = the answer. Don't synthesize the flag.

---

## 5. How to see your records land in S3

You need AWS access for this (same one-time setup as [`M1-1_Handoff.md`](M1-1_Handoff.md) §5; skip if you're relying on local tests + Jake's deploy). After your instrumented handler is deployed and you hit your route:

```bash
BUCKET=$(aws ssm get-parameter --name /anomalypulse/telemetry/bucket/name --query Parameter.Value --output text --profile <your-profile>)

# your records should appear under your service's partition:
aws s3 ls "s3://$BUCKET/raw/service=<your-service>/" --recursive --profile <your-profile>

# read one to eyeball the fields:
aws s3 cp "s3://$BUCKET/raw/service=<your-service>/dt=<date>/<the-object>.json" - --profile <your-profile>
```

- Landed under `raw/…`? The pipeline accepted your line. ✅
- Landed under `errors/…` instead? Your line wasn't parseable JSON (or wasn't on a single line). Fix the emit.
- Nothing at all? The subscription filter only forwards lines that **contain a `request_id`** — if your record is missing it, it never ships. Check that first.

---

## 6. Per-person quick start (your M2 ticket)

Everyone: branch per ticket (`m2-<n>-<name>`), edit your `handler.py`, add the telemetry emit, write a validating test, open a PR. Then:

- **Josh — Product (M2-2).** Emit full Tier-1 telemetry for both `GET /products` and `GET /products/{id}`: `latency_ms`, `status_code`, `lambda_duration_ms`, `cold_start`, and DynamoDB `db_latency_ms` / `db_consumed_capacity`. Dependency fields are `null` (Product calls nothing). You're the highest-traffic service, so get the record shape clean — it's the template the others mirror.
- **Linu — Cart (M2-3).** Same Tier-1 record for all three cart routes (`POST /cart`, `GET /cart/{user}`, `DELETE /cart/{user}/{item}`). Dependency fields `null`. Make sure each of the three routes emits — not just the read path.
- **Melisa — Order (M2-4).** The connected one. Populate the **dependency fields** from your Payment call: `dependency` (`"payment-service"`), `dependency_latency_ms` (measured call time), `dependency_error` (true if it failed/timed out). **Propagate your `request_id` into the Payment invoke** so Order and Payment records can be joined — that correlation is what makes dependency-failure and cascade analysis possible later.
- **Rachel — Payment + metrics extractor (M2-5).** Two jobs:
  1. **Instrument Payment.** Emit the Tier-1 record (no table, so `db_*` and `dependency_*` are `null`). ⚠️ **The injected delay is Tier-2 — it must NOT appear in the record.** Your `latency_ms` naturally *includes* any injected slowdown, and that's exactly the observable signal the ML should see **without being told the cause**. The injected value is recorded separately by the simulator (M3), never by your service.
  2. **Build the CloudWatch metrics extractor.** A boto3 module that pulls the non-log signals every service needs: DynamoDB `ConsumedCapacity` and **`ThrottledRequests` counts**, and Lambda `Duration` / `Throttles` / `ColdStarts` (init duration). These come from the CloudWatch **metrics** API, aligned to the same timestamp discipline, and land in S3 alongside the log records. These throttle counts feed the DB-throttling incident you're adjacent to in M3.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `validate()` returns `False` | Missing field, extra/misspelled field, or a Tier-2 field | Emit **all 14** fields (`null` if N/A); check spelling; remove any Tier-2 field. `errors[0].message` names the problem. |
| Validator rejects your `timestamp` | `datetime`'s `%f` gives 6 digits; schema wants exactly 3 | Use the `_now_ms()` helper (§3) — truncate microseconds to milliseconds. |
| Record lands in `errors/`, not `raw/` | Line wasn't parseable JSON, or was split across multiple lines | Emit a single `json.dumps(record)` line; no embedded newlines. |
| Nothing lands in S3 at all | The subscription filter only forwards lines containing `request_id` | Ensure every record has a `request_id`; confirm your handler is deployed. |
| `AccessDeniedException` reading the bucket | Your profile lacks S3 read, or wrong profile | This is a read-only convenience; ask Jake if you need bucket read access. |
| `import schema_validator` fails **locally** | The layer is only on the *deployed* Lambda's path | For local tests, add `AnomalyPulse/layers/telemetry/python/` to `sys.path` and `pip install jsonschema`. |

**Golden rule:** if your record won't validate, print `errors[0].message` — the validator tells you exactly which field and why.

---

## 8. Your M2 checklist

- [ ] Pulled `main`; created your ticket branch `m2-<n>-<name>`
- [ ] Handler emits **one JSON telemetry line per request**, all 14 fields present (`null` where N/A)
- [ ] `timestamp` is ms-precision UTC ending in `Z`; `request_id` set (and propagated downstream if you call another service)
- [ ] **No Tier-2 field** anywhere in the emitted record
- [ ] Telemetry never crashes the request (log-and-continue on invalid)
- [ ] Unit test asserts a sample record **validates clean** against the schema
- [ ] (Melisa) `dependency_*` fields populated from the real Payment call; `request_id` propagated
- [ ] (Rachel) Payment instrumented (injected delay **not** in the record) **+** CloudWatch metrics extractor returning real throttle **counts** and Lambda health metrics
- [ ] PR opened to `main`; after Jake's deploy, confirmed your records appear under `raw/service=<you>/…` in S3

---

*Questions → ping Jake. The field reference of record is [`Docs/TELEMETRY_CONTRACT.md`](../Docs/TELEMETRY_CONTRACT.md); the schema that enforces it lives in the layer.*
