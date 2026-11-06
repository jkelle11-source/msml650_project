# Runbook: cascading failure (concurrent faults)

**Signals.** Two or more kinds of fault at once: for example, `request_rate` well above baseline together
with `dependency_error_count`, or throttle counts together with a rise in `lambda_duration_ms_mean`.

**Treat the faults as concurrent.** The telemetry shows that they overlap; it shows no link between them.
Their order of onset is an observed sequence, useful for the timeline only. Work each fault with its own
runbook, and stabilise the user-facing one first.

**Check**
1. List every signal that moved, by kind:
   - traffic: `request_rate`;
   - database: `read_throttle_events`, `throttled_requests`, `db_latency_ms_p95`;
   - handler: `lambda_duration_ms_mean`;
   - payment dependency: `dependency_error_count`, `dependency_latency_ms_p95`.
2. Rank the affected services by user impact: `error_rate_5xx` first, then `p95_latency_ms`.
3. `lambda_throttle_count` on every service, not only the busiest: the account's 1,000 concurrent Lambda
   executions are shared by all functions.

**Act**
1. Stop the errors users see first, using that fault's runbook: for a failing payment dependency, Payment's
   own logs and concurrency; for DynamoDB throttling, the Products table's read capacity.
2. Then work the remaining faults one at a time, rechecking every signal above after each change, so the
   effect of each change can be seen.
3. If traffic rose, rate and burst limits on the API Gateway stage (`dev`) protect the rest of the stack
   while the other faults are worked.
4. Keep the timeline as observed: record when each signal moved, without assigning one fault to another.
