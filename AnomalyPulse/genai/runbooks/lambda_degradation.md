# Runbook: Lambda degradation

**Signals.** `lambda_duration_ms_mean` and `lambda_duration_ms_p95` far above baseline on one service, with
its `p50_latency_ms` and `p95_latency_ms` rising by about the same amount, while `request_rate` stays flat.

**What the duration measures.** `lambda_duration_ms` is the CPU time of the handler body, not wall-clock
time. A slow handler raises both duration and latency. A slow dependency raises latency only: then look at
`db_latency_ms`, or for order-service at `dependency_latency_ms`, instead.

**Check**
1. Which service's `lambda_duration_ms_mean` rose, and whether its end-to-end latency rose by about the same
   amount.
2. `cold_start_count`: a rise means new execution environments are starting, which adds start-up time to
   those requests.
3. The function itself (`anomalypulse-product`, `-cart`, `-order` or `-payment`): its CloudWatch Logs for
   slow code paths, and any recent change to its code, layers or configuration. Each runs Python 3.13 on
   arm64 with 1,769 MB of memory (one full vCPU) and a 10 s timeout.
4. `error_rate_5xx`: requests that approach the 10 s timeout fail.

**Act**
1. If a change to the function lines up with the start of the slowdown, roll it back.
2. Otherwise find and fix the slow code path in the handler, using its logs.
3. More memory than 1,769 MB adds a second vCPU, which helps only code that uses more than one; it does not
   speed up single-threaded work.
4. Recheck `lambda_duration_ms_mean` and `p95_latency_ms` after each change.
