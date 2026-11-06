# Runbook: Lambda degradation

**Signals.** `lambda_duration_ms_mean` and `lambda_duration_ms_p95` far above baseline on one service, with
its `p50_latency_ms` and `p95_latency_ms` rising by about the same amount, while `request_rate` stays flat.

**What the duration measures.** `lambda_duration_ms` is the CPU time of the handler body, not wall-clock
time, so handler code that computes more raises both duration and latency. Time spent waiting raises latency
only: on DynamoDB it is in `db_latency_ms`, and on Payment (order-service) in `dependency_latency_ms`. The
function's CloudWatch `Duration` metric is wall-clock time, a different number.

**Check**
1. Which service's `lambda_duration_ms_mean` rose, and whether its end-to-end latency rose by about the same
   amount.
2. The function itself (`anomalypulse-product`, `-cart`, `-order` or `-payment`): its CloudWatch Logs
   (`/aws/lambda/<function>`) for slow code paths, and any recent change to its code, layers or
   configuration. Each runs Python 3.13 on arm64 with 1,769 MB of memory (one full vCPU) and a 10 s timeout.
3. Requests that reach the 10 s timeout. They are cut off before they write a record, so they are missing
   from `error_rate_5xx` and the latency percentiles; the function's CloudWatch `Errors` metric counts them.
4. `cold_start_count`. Start-up runs before the handler, so its time is in neither `lambda_duration_ms` nor
   `latency_ms`: more cold starts do not account for a slower handler.

**Act**
1. Find the slow code path in the handler's logs.
2. Don't roll back on timing alone. Roll back a recent change only if the logs place the slow path in code
   or configuration that change touched; otherwise fix the code path.
3. Memory above 1,769 MB adds CPU beyond one vCPU, which helps only code that runs on more than one; it does
   not speed up single-threaded Python.
4. Recheck `lambda_duration_ms_mean` and `p95_latency_ms` after each change.
