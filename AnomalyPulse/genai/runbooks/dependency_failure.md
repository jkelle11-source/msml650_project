# Runbook: dependency failure (order-service → payment-service)

**Signals.** On order-service: `dependency_error_count` above zero, `dependency_latency_ms_p95` and
`error_rate_5xx` up. On payment-service: `p95_latency_ms` up, or its own `error_rate_5xx`.

**How the call works.** Order invokes the payment Lambda synchronously on every `POST /orders`, with a 2 s
connect timeout, a 6 s read timeout and no retries. A failed or timed-out call is answered to the client as
a 5xx and counted in `dependency_error_count`; the time it took is in `dependency_latency_ms`.
payment-service has no table.

**Check**
1. payment-service's `p95_latency_ms` against order-service's `dependency_latency_ms_p95`. If they match,
   the time is spent in Payment's handler. If Order's call is much slower, look at payment-service's
   `cold_start_count`: Payment's start-up runs before its handler, so it is in Order's call time but not in
   Payment's own latency.
2. payment-service's `lambda_throttle_count`. A throttled call fails at once, with no retry, so it adds to
   `dependency_error_count` without adding call time.
3. Whether `dependency_latency_ms_p95` sits near the 6 s read timeout: calls that reach it fail.
4. payment-service's `error_rate_5xx`, `lambda_duration_ms_mean` and CloudWatch Logs
   (`/aws/lambda/anomalypulse-payment`).
5. Whether `GET` requests to order-service are healthy: only `POST /orders` calls Payment.

**Act**
1. Find and fix what slows or fails Payment, from its logs and its duration.
2. Don't add retries to Order's call while Payment is struggling: each retry adds another invocation and
   more waiting.
3. If payment-service is throttled, give it reserved concurrency, so the other functions can't use its
   share; that also caps Payment at the reserved number.
4. To fail fast instead of holding orders open, a shorter read timeout on Order's call is an option.
5. Recheck `dependency_error_count` and order-service's `error_rate_5xx` after each change.
