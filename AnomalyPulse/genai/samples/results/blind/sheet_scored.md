# Blind scoring: recommended actions

For each incident below there are two lists of recommended actions, **A** and **B**. Score each list 0 or 1 on each of four criteria. Don't try to guess where the lists came from; judge only what they say.

| Criterion | 1 if the list... |
|---|---|
| **Correct** | contains no false fact about this stack (see the facts below): e.g. a table on a service that has none, raising capacity on an on-demand table, a setting that doesn't exist |
| **Specific** | names concrete components, settings or metrics, not just "check the logs" |
| **Ordered** | puts the most useful step first |
| **Safe** | has nothing destructive or irreversible as a first step |

## Stack facts

- Four services (product, cart, order, payment), each a Lambda (`anomalypulse-<service>`): Python 3.13, arm64, 1,769 MB (one vCPU), 10 s timeout. The account allows 1,000 concurrent Lambda executions, shared by all functions.
- Products table: provisioned at 200 RCU / 5 WCU per second; `GET /products` scans it. Carts and Orders tables: on-demand (no capacity to raise). **Payment has no table.**
- Product, Cart and Order retry DynamoDB calls in the SDK (7 attempts in all); retries are inside `db_latency_ms`.
- Order calls Payment synchronously on every `POST /orders` (2 s connect timeout, 6 s read timeout, no retries); failures count in `dependency_error_count`.
- `lambda_duration_ms` is handler CPU time, not wall-clock time. API Gateway (REST, stage `dev`) fronts all four services.

## Incident 1: LAMBDA_DEGRADATION

Affected: order-service. Metrics (baseline → current): order-service lambda_duration_ms_mean 2 → 352 ms; order-service p50_latency_ms 17 → 360 ms; order-service p95_latency_ms 24 → 375 ms; order-service error_rate_5xx 0.0 → 0.0 share; order-service request_rate 2.9 → 2.9 rps.

**A**

1. Check CloudWatch Logs for the anomalypulse-order Lambda function for slow code paths or unexpected blocking operations in the handler body, focusing on the window starting at 2026-10-30T16:03:00Z.
2. Review any recent changes to the anomalypulse-order function's code, layers, or configuration that align with the start of the slowdown, and roll back if a change is identified.
3. Check order-service cold_start_count to determine whether new execution environments are contributing to the elevated handler duration.
4. After any change, recheck lambda_duration_ms_mean and p95_latency_ms to confirm the handler duration has returned toward its 2 ms baseline.
5. Monitor error_rate_5xx closely: if handler duration continues to climb toward the 10 s function timeout, request failures will follow.

**B**

1. Inspect order-service Lambda CloudWatch Logs for the current window starting 2026-10-30T16:03:00Z to identify any slow operations, timeouts, or unexpected code paths inside the handler.
2. Check DynamoDB metrics for the order-service table (db_latency_ms_mean, db_latency_ms_p95, throttled_requests) to determine whether database calls are contributing to the handler slowdown.
3. Review dependency_latency_ms_mean and dependency_error_count for order-service to assess whether calls to payment-service are slower than baseline.
4. Check cold_start_count for order-service to determine whether Lambda cold starts are contributing to the elevated execution time.
5. If handler slowdown persists, consider increasing Lambda memory allocation or investigating any recently deployed code changes to order-service.

| | Correct | Specific | Ordered | Safe |
|---|---|---|---|---|
| A | 1 | 1 | 1 | 0 |
| B | 1 | 1 | 1 | 1 |

## Incident 2: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): payment-service p95_latency_ms 110 → 2900 ms; order-service dependency_latency_ms_p95 112 → 2950 ms; order-service dependency_error_count 0 → 57 count/min; order-service error_rate_5xx 0.0 → 0.167 share; order-service p95_latency_ms 130 → 2950 ms.

**A**

1. Check payment-service CloudWatch Logs (anomalypulse-payment) and lambda_duration_ms_mean to identify what is making the handler slow or failing it internally.
2. Check payment-service lambda_throttle_count and cold_start_count in CloudWatch; if throttling is present, assign reserved concurrency to payment-service.
3. Note that dependency_latency_ms_p95 at 2950 ms is approaching order-service's 6 s read timeout; calls that reach the timeout will also fail, so resolving payment-service slowness is urgent before the timeout is hit at scale.
4. Do not add retries to order-service's payment call while payment-service is struggling, as each retry adds another invocation and extends wait time.
5. After each remediation step, recheck order-service dependency_error_count and error_rate_5xx to confirm recovery.

**B**

1. Inspect payment-service Lambda logs and CloudWatch metrics starting at 14:32:00 to identify the source of the latency spike.
2. Check payment-service Lambda duration metrics and any downstream integrations it calls for signs of slow execution or external timeouts.
3. Consider adding or lowering a timeout on order-service calls to payment-service to limit blast radius while the root issue is investigated.
4. If payment-service cannot recover quickly, evaluate whether order-service can queue or defer payment calls to stop 5xx errors from reaching end users.
5. Review payment-service cold start counts and throttle counts for anomalies that may explain the latency increase.

| | Correct | Specific | Ordered | Safe |
|---|---|---|---|---|
| A | 1 | 1 | 1 | 1 |
| B | 1 | 1 | 1 | 1 |

## Incident 3: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service. Metrics (baseline → current): product-service request_rate 15.0 → 45.1 rps; product-service p95_latency_ms 24 → 31 ms; product-service error_rate_5xx 0.0 → 0.0 share; cart-service request_rate 2.0 → 6.0 rps; order-service request_rate 2.9 → 8.8 rps; order-service p95_latency_ms 58 → 61 ms.

**A**

1. Monitor lambda_throttle_count on all three services; the shared account concurrency limit could become a constraint if request rates continue to rise.
2. Watch product-service db_consumed_read_capacity and read_throttle_events, as GET /products scans the Products table and reads grow with traffic.
3. If latency and error rates remain near baseline, continue monitoring without making changes, as the stack is currently absorbing the load.
4. If throttling appears or latency climbs, set rate and burst limits on the API Gateway stage to protect the stack while the source of the traffic is identified.
5. If product-service reads approach the provisioned capacity of the Products table, raise its provisioned read capacity or switch it to on-demand mode.

**B**

1. Monitor Lambda concurrency and cold start counts on product-service, cart-service, and order-service to detect saturation as traffic remains elevated.
2. Watch DynamoDB consumed capacity on product-service, cart-service, and order-service tables for signs of approaching provisioned limits.
3. Review API Gateway throttle settings to ensure the spike does not exceed configured limits and begin producing 429 errors.
4. Continue monitoring 5xx error rates and p95 latency across all three services for early signs of degradation if traffic climbs further.

| | Correct | Specific | Ordered | Safe |
|---|---|---|---|---|
| A | 1 | 1 | 1 | 1 |
| B | 0 | 1 | 1 | 1 |

## Incident 4: DATABASE_THROTTLING

Affected: product-service. Metrics (baseline → current): product-service p95_latency_ms 6 → 1586 ms; product-service p50_latency_ms 5 → 6 ms; product-service db_latency_ms_p95 4 → 1580 ms; product-service read_throttle_events 0 → 1400 count/min; product-service db_consumed_read_capacity 480 → 11900 RCU/min; product-service error_rate_5xx 0.0 → 0.004 share; product-service request_rate 15.0 → 15.1 rps.

**A**

1. Immediately increase provisioned read capacity units on the product-service DynamoDB table, or switch the table to on-demand capacity mode to absorb the current 11,900 RCU/min load.
2. Enable DynamoDB auto-scaling on the product-service table so read capacity adjusts automatically to demand spikes.
3. Investigate recent changes to read access patterns on the product-service table, such as table scans or large batch reads, that could account for the 24.8x jump in consumed read capacity without a corresponding traffic increase.
4. Add a caching layer such as DynamoDB Accelerator in front of the product-service table to reduce direct DynamoDB read pressure for frequently accessed items.
5. Set CloudWatch alarms on read_throttle_events and db_consumed_read_capacity for the product-service table to detect future throttling before it affects p95 latency.

**B**

1. Raise the Products table provisioned read capacity or switch it to on-demand mode immediately to relieve the active throttling.
2. Check db_consumed_read_capacity against the table's provisioned limit to confirm headroom; the current 11900 RCU/min is roughly 198 RCU/s, which can be compared directly to the provisioned read capacity units per second.
3. Investigate whether GET /products full-table scans account for the elevated read consumption; paginating the scan or caching the product list in the Lambda function between invocations would reduce per-request RCU cost.
4. After raising capacity, recheck read_throttle_events and db_latency_ms_p95 to confirm throttling has subsided.
5. Do not reduce SDK retry attempts while throttling is active, as doing so would convert the current tail latency into additional 5xx errors.

| | Correct | Specific | Ordered | Safe |
|---|---|---|---|---|
| A | 1 | 1 | 1 | 1 |
| B | 1 | 1 | 1 | 1 |

## Incident 5: CASCADING_FAILURE

Affected: product-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 15.0 → 44.8 rps; order-service request_rate 2.9 → 8.7 rps; payment-service p95_latency_ms 110 → 1020 ms; order-service dependency_error_count 0 → 150 count/min; order-service error_rate_5xx 0.0 → 0.25 share; order-service p95_latency_ms 130 → 1030 ms.

**A**

1. Investigate payment-service Lambda logs and concurrency configuration immediately to address its p95 latency spike from 110 ms to 1020 ms, as this is directly reflected in order-service 5xx errors and dependency error counts.
2. Apply rate and burst limits on the API Gateway stage to protect the stack while the traffic surge, which has pushed product-service to 44.8 rps and order-service to 8.7 rps, is ongoing.
3. Check lambda_throttle_count on all services to determine whether the traffic surge is exhausting shared Lambda concurrency across the account.
4. After stabilizing payment-service, recheck order-service error_rate_5xx and dependency_error_count to confirm errors are clearing before making further changes.
5. Record the onset times from the observed sequence when filing the incident so each fault's timeline remains distinct and no signal is attributed to another.

**B**

1. Inspect payment-service Lambda logs and metrics immediately to identify what is holding p95 latency at 1020 ms and address that degradation.
2. Check whether payment-service Lambda is being throttled or experiencing cold starts under the elevated order-service call volume.
3. Review API Gateway and Lambda concurrency limits for order-service and payment-service; consider raising reserved concurrency or enabling provisioned concurrency to handle the current load.
4. Implement or tighten a timeout and circuit-breaker on order-service calls to payment-service to stop the 150 dependency errors per minute from propagating as 5xx responses to clients.
5. Investigate the upstream traffic surge: verify whether the 3x increase in product-service and order-service request rates is expected or indicates a runaway client, bot traffic, or misconfigured retry loop.

| | Correct | Specific | Ordered | Safe |
|---|---|---|---|---|
| A | 1 | 1 | 1 | 1 |
| B | 1 | 1 | 1 | 1 |
