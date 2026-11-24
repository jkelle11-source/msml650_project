# Blind scoring: recommended actions (M9-5)

## Incident 1: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.2077 share; payment-service error_rate_5xx 0 → 0.4574 share; order-service p95_latency_ms 25.0588 → 4055.63 ms; payment-service p95_latency_ms 4.5056 → 4053.89 ms; order-service dependency_error_count 0 → 43 count/min; order-service dependency_latency_ms_p95 22.1478 → 4580.65 ms; order-service dependency_latency_ms_mean 16.2449 → 1913.26 ms; payment-service lambda_duration_ms_mean 0.5855 → 1.8976 ms; order-service db_latency_ms_p95 5.2849 → 16.461 ms; order-service lambda_duration_ms_p95 4.7629 → 9.817 ms.

**A**

1. Inspect payment-service Lambda logs and X-Ray traces from the incident window starting 2026-11-13T23:44:00Z to identify what the handler is doing during the elevated execution time and where errors originate.
2. Check payment-service Lambda concurrency limits and throttle metrics in CloudWatch to determine whether invocations are being throttled or hitting concurrency ceilings.
3. Review payment-service Lambda configuration for any downstream calls it makes that could be timing out and inflating duration and error rates.
4. Consider lowering the timeout on order-service's calls to payment-service so that hung dependency calls fail faster and reduce order-service p95 latency and error accumulation.
5. If payment-service errors persist, evaluate routing order-service POST /orders requests to a fallback or circuit-breaker path to prevent further order-service 5xx growth.

**B**

1. Check payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment for errors or panics to identify what is failing inside the handler.
2. Check payment-service lambda_throttle_count in CloudWatch to determine whether Lambda throttling is contributing to the dependency error count.
3. If payment-service is throttled, assign it reserved concurrency to protect its capacity from other functions.
4. After identifying and addressing the fault in payment-service, recheck order-service dependency_error_count and error_rate_5xx to confirm recovery before making further changes.
5. Consider a shorter read timeout on order-service's call to payment-service to fail fast and free waiting connections while payment-service remains degraded.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 2: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 14.98 → 38.5167 rps; cart-service request_rate 2.0467 → 8.1333 rps; order-service request_rate 2.97 → 12.1833 rps; payment-service request_rate 0.9433 → 6.25 rps; product-service db_consumed_read_capacity 449.3 → 1155.5 RCU/min; order-service db_consumed_read_capacity 60.8 → 178.5 RCU/min; cart-service db_consumed_write_capacity 122.8 → 488 WCU/min; order-service db_consumed_write_capacity 56.6 → 375 WCU/min; order-service p50_latency_ms 3.555 → 14.12 ms; product-service error_rate_5xx 0 → 0 share.

**A**

1. Check DynamoDB provisioned or on-demand capacity limits for all three tables (product, cart, order) and verify that no throttling is imminent given current consumption rates, scaling capacity if needed.
2. Monitor order-service p50 and p95 latency closely as the spike continues, since p50 has already risen 4x and further degradation may push latency to customer-impacting levels.
3. Review Lambda concurrency limits for all four services to ensure they can sustain current request rates without throttling.
4. Verify payment-service health and downstream capacity, as its request rate has risen 6.6x and it has no DynamoDB table to absorb backpressure.
5. If the spike is unexpected, investigate API Gateway access logs and upstream sources to determine whether this is legitimate traffic or an anomalous event requiring rate limiting.

**B**

1. Monitor lambda_throttle_count on all four services to detect whether the shared Lambda concurrency limit is being approached under the elevated load.
2. Watch product-service db_consumed_read_capacity and read_throttle_events closely, as reads are already at 1155.5 RCU/min and further traffic growth could approach provisioned capacity; be prepared to raise provisioned read capacity or switch the Products table to on-demand.
3. Check whether the traffic surge is expected (a promotion, a launch, or a client retrying) before making any infrastructure changes, since latency and errors are currently within acceptable bounds.
4. If latency or error rates begin to climb, set rate and burst limits on the API Gateway stage to protect the stack while the source of traffic is investigated.
5. If lambda_throttle_count rises, request a higher account concurrency quota or use reserved concurrency to protect critical functions such as order-service and payment-service.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 3: LAMBDA_DEGRADATION

Affected: product-service, payment-service. Metrics (baseline → current): product-service p95_latency_ms 6.5595 → 903.502 ms; payment-service request_rate 0.9433 → 2.15 rps; product-service lambda_duration_ms_mean 2.486 → 759.65 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.3012 ms; product-service lambda_duration_ms_p95 3.1188 → 761.593 ms; product-service p50_latency_ms 4.0869 → 893.32 ms; product-service p99_latency_ms 9.587 → 906.043 ms; product-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 4.4294 ms.

**A**

1. Inspect recent product-service Lambda logs and X-Ray traces from the 2026-11-14T01:00:00Z window to identify what is consuming handler execution time.
2. Check whether a dependency called inside the product-service Lambda handler (such as its DynamoDB table) is responding slowly, as handler duration accounts for nearly all end-to-end latency.
3. Review any recent code or configuration changes deployed to the product-service Lambda function that could have introduced a slow code path or blocking call.
4. If the slowdown persists, consider increasing the product-service Lambda memory allocation, which also scales CPU, to determine whether resource contention is a factor.
5. Set a CloudWatch alarm on product-service lambda_duration_ms_p95 to alert earlier if handler duration exceeds a defined threshold in future windows.

**B**

1. Inspect CloudWatch Logs for /aws/lambda/anomalypulse-product for slow code paths or unexpected computation active during this window.
2. Check the CloudWatch Errors metric for anomalypulse-product to identify any invocations cut off by the timeout, which would not appear in error_rate_5xx or the latency percentiles.
3. Review any recent changes to the anomalypulse-product function code, layers, or configuration that could account for the ~306x increase in handler execution time, and roll back only if logs confirm the slow path is in code that change touched.
4. After identifying and addressing the slow code path, recheck lambda_duration_ms_mean and p95_latency_ms for product-service to confirm recovery.
5. Investigate the payment-service request rate increase in parallel to determine whether it reflects a separate workload shift.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 4: CASCADING_FAILURE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.2327 share; payment-service error_rate_5xx 0 → 0.4702 share; order-service p95_latency_ms 25.0588 → 4476.09 ms; payment-service p95_latency_ms 4.5056 → 4384.69 ms; product-service request_rate 14.98 → 129.983 rps; cart-service request_rate 2.0467 → 29.7667 rps; order-service request_rate 2.97 → 37.8833 rps; payment-service request_rate 0.9433 → 18.75 rps; order-service dependency_error_count 0 → 529 count/min; order-service dependency_latency_ms_p95 22.1478 → 4881.05 ms.

**A**

1. Investigate payment-service Lambda logs and concurrency metrics immediately to identify why 47% of requests are returning 5xx at 4.38 s p95, as this is the highest user-facing error rate and directly affects order-service.
2. Apply rate and burst limits on the API Gateway stage to cap incoming traffic while both faults are being worked, protecting Lambda concurrency shared across all functions.
3. Check Lambda throttle counts on all four services, since the account's concurrent execution pool is shared and the surge across product-service, cart-service, order-service and payment-service may be exhausting it.
4. Once payment-service is stabilised, recheck order-service error rate and dependency error count to confirm improvement before making further changes.
5. Record the timeline of each signal as observed without linking one fault to the other, so post-incident analysis can distinguish the traffic surge from the payment-service fault.

**B**

1. Inspect payment-service Lambda logs and error details to identify the source of the 47% 5xx error rate and near-4.4 s p95 latency.
2. Check payment-service Lambda concurrency limits and throttle counts in CloudWatch, given the roughly 20x increase in incoming request rate.
3. Review order-service's handling of payment dependency failures, including timeout settings, retry configuration, and any circuit-breaker logic, to limit error propagation.
4. Investigate the traffic surge source via API Gateway access logs and metrics for all four services to determine whether the volume increase is organic, a retry storm, or unexpected traffic.
5. Consider temporarily raising reserved concurrency or applying rate limiting at API Gateway for payment-service to stabilise error rates while the root issue is investigated.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 0 |

**Better:** A

## Incident 5: DATABASE_THROTTLING

Affected: payment-service. Metrics (baseline → current): payment-service request_rate 0.9433 → 2.1833 rps; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 3.58 ms.

**A**

1. Check read_throttle_events, write_throttle_events, and throttled_requests in CloudWatch for any DynamoDB table associated with services that call payment-service, particularly order-service, to confirm where throttling is occurring.
2. Review db_latency_ms_p95 and db_consumed_read_capacity for the order-service table to identify whether capacity limits are being approached.
3. If throttling is confirmed on a provisioned table, raise its read or write capacity units or switch it to on-demand mode.
4. Monitor payment-service p95_latency_ms and error_rate_5xx closely over the next few minutes, as throttling-related tail latency or 5xx errors may emerge if the elevated request rate continues.
5. Recheck read_throttle_events and db_latency_ms_p95 after any capacity change to confirm resolution.

**B**

1. Inspect DynamoDB throttle metrics for the order-service table, as order-service calls payment-service on every POST /orders and may be surfacing throttle pressure into this request path.
2. Check Lambda throttle counts and DynamoDB consumed capacity for order-service to confirm whether the increased payment-service request rate corresponds to a surge in order processing.
3. Review payment-service Lambda duration and cold-start metrics in CloudWatch for the same one-minute window to determine whether the higher request rate is straining concurrency limits.
4. If order-service DynamoDB throttling is confirmed, consider increasing provisioned capacity or enabling DynamoDB auto-scaling on that table.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 6: DATABASE_THROTTLING

Affected: payment-service. Metrics (baseline → current): payment-service request_rate 0.9433 → 2.0833 rps; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 4.2586 ms.

**A**

1. Check CloudWatch DynamoDB throttle metrics (ThrottledRequests, WriteThrottleEvents, ReadThrottleEvents) for any tables in the request path to confirm throttling activity.
2. Inspect Lambda logs and X-Ray traces for payment-service to identify any SDK retry loops or delayed DynamoDB calls not yet reflected in the one-minute latency window.
3. Review DynamoDB provisioned or on-demand capacity settings for tables that may be in the upstream call chain affecting payment-service.
4. Monitor request_rate, p95_latency_ms, and error_rate_5xx for payment-service over the next several minutes to determine whether the elevated request rate persists or latency begins to climb.

**B**

1. Check throttle counts (throttled_requests, read_throttle_events, write_throttle_events) for any DynamoDB table accessed upstream in the call chain, as these metrics were not present in the current window but are the primary signal for DATABASE_THROTTLING.
2. Review payment-service Lambda logs and any downstream dependency calls for signs of SDK retry activity or elevated internal latency not captured in the p95 surface metric.
3. Monitor payment-service request_rate closely; if it continues to rise, review whether the upstream order-service call volume is increasing in step, since order-service calls payment-service on every POST /orders.
4. After any changes, recheck payment-service p95_latency_ms and error_rate_5xx to confirm no degradation is developing.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 0 | 1 |

**Better:** B

## Incident 7: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.4095 share; payment-service error_rate_5xx 0 → 1 share; order-service p95_latency_ms 25.0588 → 4436.7 ms; payment-service p95_latency_ms 4.5056 → 4419.41 ms; order-service dependency_error_count 0 → 95 count/min; order-service dependency_latency_ms_p95 22.1478 → 4434.93 ms; order-service dependency_latency_ms_mean 16.2449 → 4431.04 ms; payment-service lambda_duration_ms_mean 0.5855 → 1.7205 ms; payment-service p50_latency_ms 0.0384 → 4417.97 ms; order-service p99_latency_ms 30.0105 → 4439.26 ms.

**A**

1. Inspect payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment for errors or exceptions, focusing on what is consuming time outside the handler body given the gap between lambda_duration_ms_mean and end-to-end latency.
2. Check payment-service lambda_throttle_count in CloudWatch to determine whether Lambda throttling is contributing to failures; if so, configure reserved concurrency for payment-service.
3. Do not add retries to order-service's call to payment-service while payment-service is failing, as each retry would add more invocations and extend wait times for clients.
4. After identifying and fixing the payment-service issue, recheck payment-service error_rate_5xx and order-service dependency_error_count to confirm recovery.
5. Verify that GET requests to order-service remain unaffected, since only POST /orders invokes payment-service, to scope the customer impact accurately.

**B**

1. Inspect payment-service Lambda logs and API Gateway access logs for the window starting 2026-11-14T05:07:00Z to identify the source of 5xx responses and the latency gap between handler duration and end-to-end response time.
2. Check API Gateway configuration and integration timeout settings for payment-service to determine whether a timeout or integration error is responsible for the near-4.4 s response times.
3. Implement or activate a circuit breaker in order-service for the payment-service dependency so that sustained payment failures fail fast rather than queueing calls and amplifying latency to callers.
4. Review Lambda concurrency limits and throttle metrics for payment-service to rule out invocation-level throttling inflating end-to-end latency without affecting handler CPU time.
5. Once payment-service is restored, verify order-service 5xx rate and dependency error count return to baseline before closing the incident.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 8: CASCADING_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.5042 share; payment-service error_rate_5xx 0 → 1 share; order-service p95_latency_ms 25.0588 → 4257.5 ms; payment-service p95_latency_ms 4.5056 → 4245.6 ms; payment-service request_rate 0.9433 → 2 rps; order-service dependency_error_count 0 → 120 count/min; order-service dependency_latency_ms_p95 22.1478 → 4715.41 ms; order-service dependency_latency_ms_mean 16.2449 → 4276.3 ms; payment-service lambda_duration_ms_mean 0.5855 → 2.5909 ms; order-service db_latency_ms_p95 5.2849 → 17.737 ms.

**A**

1. Inspect payment-service Lambda logs and error details immediately, as it is returning 5xx on every request with p95 latency over 4.2 seconds.
2. Check order-service DynamoDB table metrics for throttling or capacity exhaustion to address the ~3.4x rise in db_latency_ms_p95.
3. Verify or implement a circuit breaker in order-service for its calls to payment-service to prevent timeout accumulation while payment-service is fully failing.
4. Review payment-service Lambda concurrency limits and timeout settings in case invocations are being queued or hard-stopped.
5. Examine API Gateway access logs for payment-service to determine whether errors originate at the Lambda layer or earlier in the request path.

**B**

1. Inspect payment-service Lambda logs and invocation errors immediately to identify what is producing its 100% 5xx error rate and near-4.25 s p95 latency.
2. Check payment-service Lambda concurrency and throttle counts at the account level, as lambda_duration_ms_mean has risen and every request is currently failing.
3. After stabilizing payment-service, recheck order-service error rate and dependency error count to determine whether its 50.4% 5xx rate and 120 dependency errors per minute persist independently.
4. Investigate order-service DynamoDB latency, which rose from 5.3 ms to 17.7 ms, as a potentially independent fault that may remain after payment-service is restored.
5. Consider applying rate and burst limits on the API Gateway stage while both faults are being worked to protect the rest of the stack.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 0 |

**Better:** B

## Incident 9: CASCADING_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.5745 share; payment-service error_rate_5xx 0 → 0.9937 share; order-service p95_latency_ms 25.0588 → 2742.37 ms; payment-service p95_latency_ms 4.5056 → 1968.18 ms; payment-service request_rate 0.9433 → 2.65 rps; order-service dependency_error_count 0 → 158 count/min; order-service dependency_latency_ms_p95 22.1478 → 2429.85 ms; order-service dependency_latency_ms_mean 16.2449 → 1983.54 ms; order-service lambda_duration_ms_mean 2.9034 → 635.678 ms; payment-service lambda_duration_ms_mean 0.5855 → 1.9276 ms.

**A**

1. Examine payment-service Lambda logs and error details immediately to identify what is producing the 99.37% 5xx rate, as this is the highest-impact fault and is affecting order-service downstream.
2. Check payment-service Lambda concurrency and throttle counts to determine whether invocations are being throttled given the request rate has risen to 2.65 rps.
3. Apply or tighten API Gateway rate and burst limits on the stage receiving payment-service traffic to protect the function while the handler fault is being resolved.
4. After payment-service is stabilised, recheck order-service error rate and dependency error count to confirm whether order-service recovers on its own.
5. Review order-service Lambda concurrency and logs independently to confirm no additional fault is active once the payment-service issue is resolved.

**B**

1. Inspect payment-service Lambda logs and error details immediately to identify the nature of the internal failure producing the 99.4% 5xx rate.
2. Check payment-service Lambda concurrency limits and reserved concurrency configuration for signs of exhaustion or throttling.
3. Review order-service timeout and retry settings for calls to payment-service to prevent further request queuing and latency amplification.
4. Consider temporarily disabling or circuit-breaking order-service calls to payment-service to stop order-service from accumulating failed dependency calls while payment-service is restored.
5. Once payment-service is stabilized, verify that order-service error rates and dependency latency return to baseline before declaring the incident resolved.

| | Correct | Safe |
|---|---|---|
| A | 1 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 10: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.4762 share; payment-service error_rate_5xx 0 → 1 share; order-service p95_latency_ms 25.0588 → 4321.22 ms; payment-service p95_latency_ms 4.5056 → 4303.56 ms; payment-service request_rate 0.9433 → 1.8333 rps; order-service dependency_error_count 0 → 110 count/min; order-service dependency_latency_ms_p95 22.1478 → 4318.97 ms; order-service dependency_latency_ms_mean 16.2449 → 4315.12 ms; payment-service lambda_duration_ms_mean 0.5855 → 1.669 ms; order-service p50_latency_ms 3.555 → 7.54 ms.

**A**

1. Inspect payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment to identify what error or exception is producing the 100% 5xx rate.
2. Check payment-service lambda_throttle_count in CloudWatch; throttled invocations fail immediately and add to order-service dependency_error_count without increasing lambda handler duration.
3. Verify whether GET requests to order-service are healthy, since only POST /orders invokes payment-service and unaffected paths would confirm the failure is isolated to that dependency.
4. Once the payment-service fault is identified and fixed, do not add retries to order-service's call while payment-service is still struggling, as each retry adds another invocation and more latency.
5. After each remediation step, recheck payment-service error_rate_5xx and order-service dependency_error_count to confirm recovery.

**B**

1. Inspect payment-service Lambda CloudWatch logs for the current window to identify what is generating the 5xx responses at near-timeout latency.
2. Check API Gateway access logs for payment-service to determine whether errors are originating at the Lambda layer or before it.
3. Review any external integrations or third-party calls within the payment-service Lambda handler, as the handler itself executes in under 2 ms while end-to-end latency exceeds 4.3 s.
4. Add or tighten a timeout and circuit-breaker on the order-service call to payment-service to limit blast radius while the payment-service issue is resolved.
5. Once the root failure in payment-service is resolved, verify that order-service error and latency metrics return to their baselines before closing the incident.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 11: LAMBDA_DEGRADATION

Affected: product-service, cart-service, payment-service. Metrics (baseline → current): cart-service p95_latency_ms 8.8653 → 1614.99 ms; payment-service request_rate 0.9433 → 1.9167 rps; product-service throttled_requests 0 → 118 count/min; product-service read_throttle_events 0 → 155 count/min; product-service lambda_duration_ms_mean 2.486 → 2.2606 ms; cart-service lambda_duration_ms_mean 2.2794 → 143.339 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.2322 ms; product-service db_latency_ms_mean 4.044 → 47.2286 ms; cart-service lambda_duration_ms_p95 4.4234 → 1370.92 ms; product-service p99_latency_ms 9.587 → 1587.93 ms.

**A**

1. Inspect cart-service Lambda CloudWatch Logs and X-Ray traces covering the period from 17:04 UTC to identify what is consuming execution time and driving handler duration 63x above baseline.
2. Review cart-service Lambda memory and timeout configuration; a memory increase may resolve execution slowdowns if the function is CPU-bound.
3. Increase product-service DynamoDB read capacity or enable auto-scaling to clear the active read throttling of 155 events per minute, which is also elevating SDK-level db latency nearly 12x.
4. Monitor payment-service closely given its request rate has doubled; if cart-service or order-service retries are amplifying call volume, resolving the Lambda degradation should reduce downstream pressure.
5. Set up CloudWatch alarms on cart-service lambda_duration_ms_p95 and product-service read_throttle_events to catch recurrence of these conditions before they reach this severity.

**B**

1. Inspect CloudWatch Logs at /aws/lambda/anomalypulse-cart for slow code paths in the cart-service handler to identify what is consuming CPU time.
2. Check the CloudWatch Errors metric for anomalypulse-cart to identify any invocations hitting the timeout cutoff, which would not appear in error_rate_5xx or latency percentiles.
3. Review any recent changes to the cart-service Lambda function code, layers, or configuration, and roll back only if logs confirm the slow path is in code or configuration that change touched.
4. Investigate the product-service DynamoDB table for read capacity provisioning or on-demand scaling limits given read_throttle_events at 155/min and throttled_requests at 118/min.
5. Recheck cart-service lambda_duration_ms_mean and p95_latency_ms, and product-service db_latency_ms_mean and read_throttle_events, after each remediation step.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 12: TRAFFIC_SPIKE

Affected: payment-service. Metrics (baseline → current): payment-service request_rate 0.9433 → 1.7833 rps; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 4.3828 ms.

**A**

1. Monitor payment-service request_rate and p95_latency_ms closely over the next several minutes; if latency and errors remain near baseline, no immediate change is required.
2. Check lambda_throttle_count on payment-service and order-service to confirm the shared Lambda concurrency limit is not being approached.
3. Investigate whether the traffic increase is expected (such as a promotion or a client retrying) before taking any corrective action.
4. If the rate continues to climb and throttling appears, consider setting rate and burst limits on the API Gateway stage to protect the stack.

**B**

1. Monitor payment-service request_rate over the next several minutes to determine whether volume continues to climb or stabilizes.
2. Check order-service metrics for a corresponding rise in POST /orders traffic, since order-service calls payment-service on every order.
3. Watch payment-service error_rate_5xx and p95_latency_ms closely; if request rate keeps rising, errors or latency increases may follow.
4. Review Lambda concurrency limits for payment-service to confirm headroom exists for sustained higher throughput.
5. If the spike persists, consider provisioning additional Lambda concurrency for payment-service to prevent throttling.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 1 | 0 |

**Better:** A

## Incident 13: LAMBDA_DEGRADATION

Affected: product-service, payment-service. Metrics (baseline → current): product-service p95_latency_ms 6.5595 → 718.75 ms; payment-service request_rate 0.9433 → 2.1 rps; product-service lambda_duration_ms_mean 2.486 → 529.74 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.2554 ms; product-service lambda_duration_ms_p95 3.1188 → 604.641 ms; product-service p50_latency_ms 4.0869 → 708.266 ms; product-service p99_latency_ms 9.587 → 748.833 ms; product-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 3.6258 ms.

**A**

1. Inspect product-service Lambda logs and AWS X-Ray traces for the current window to identify what is consuming execution time within the handler body.
2. Check product-service Lambda configuration for memory allocation and timeout settings, and review CloudWatch metrics for concurrency utilization and any throttle events.
3. Review any recent deployments, layer updates, or environment variable changes applied to product-service Lambda.
4. Examine product-service DynamoDB table metrics for elevated read or write latency that could be contributing to slow handler execution.
5. Investigate the increase in payment-service request rate to confirm it aligns with expected upstream traffic and is not a side effect of product-service retries.

**B**

1. Check CloudWatch Logs at /aws/lambda/anomalypulse-product for slow code paths, exceptions, or any log lines showing unexpectedly long operations within the current incident window.
2. Review the CloudWatch Duration and Errors metrics for anomalypulse-product to determine whether any invocations are approaching or hitting the 10 s timeout, as those would not appear in error_rate_5xx or latency percentiles.
3. If a recent code, layer, or configuration change was deployed to anomalypulse-product, compare logs before and after to determine whether the slow path is in code that change touched before deciding to roll back.
4. After any remediation, recheck lambda_duration_ms_mean and p95_latency_ms for product-service to confirm the handler has returned to baseline.
5. Investigate why payment-service request_rate has risen to 2.1 rps from a baseline of 0.9433 rps to determine whether upstream call patterns have shifted.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 14: DATABASE_THROTTLING

Affected: cart-service. Metrics (baseline → current): cart-service throttled_requests 0 → 0 count/min; cart-service read_throttle_events 0 → 0 count/min; cart-service write_throttle_events 0 → 0 count/min; cart-service db_latency_ms_p95 6.5829 → 5.6299 ms; cart-service db_consumed_read_capacity 0 → 0 RCU/min; cart-service error_rate_5xx 0 → 0 share; cart-service p95_latency_ms 8.8653 → 8.3336 ms; cart-service request_rate 2.0467 → 3.5667 rps.

**A**

1. Review the cart-service DynamoDB table's provisioned or on-demand capacity settings in the AWS console to confirm no throttling is being absorbed by SDK retries within this window.
2. Extend the monitoring window beyond one minute and watch throttled_requests, read_throttle_events, and write_throttle_events for any non-zero values as the elevated request rate continues.
3. Check DynamoDB CloudWatch metrics at finer granularity (per-second view) for the cart-service table to detect short bursts of throttling that may average to zero over one minute.
4. If throttling is confirmed, increase provisioned capacity or switch to on-demand mode for the cart-service DynamoDB table to handle the higher request rate.
5. Set a CloudWatch alarm on throttled_requests for the cart-service table to alert immediately if throttle events appear.

**B**

1. Monitor read_throttle_events and throttled_requests on the cart-service table closely over the next several minutes given the 74% rise in request_rate, as throttling may emerge if the elevated rate persists.
2. Check db_consumed_read_capacity in the next window to confirm whether the increased request rate is driving higher DynamoDB read consumption.
3. Recheck db_latency_ms_p95 and p95_latency_ms for any tail-latency growth that would indicate SDK retries beginning to accumulate inside DynamoDB calls.
4. If throttle counts rise, review the cart-service DynamoDB table capacity mode and consider whether the table's current configuration can sustain the observed request rate.
5. Watch error_rate_5xx for any increase, which would indicate requests exhausting all retry attempts against DynamoDB.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 15: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 14.98 → 26.85 rps; cart-service request_rate 2.0467 → 6.3 rps; order-service request_rate 2.97 → 8.0667 rps; payment-service request_rate 0.9433 → 3.9833 rps; order-service db_consumed_read_capacity 60.8 → 122.5 RCU/min; cart-service db_consumed_write_capacity 122.8 → 378 WCU/min; order-service db_consumed_write_capacity 56.6 → 239 WCU/min; order-service p50_latency_ms 3.555 → 7.245 ms; product-service error_rate_5xx 0 → 0 share; cart-service error_rate_5xx 0 → 0 share.

**A**

1. Check DynamoDB CloudWatch throttle metrics for cart-service and order-service tables immediately, as write capacity on both has risen sharply and may approach provisioned limits if the spike continues.
2. Verify payment-service error rates and latency, since order-service calls it on every POST and its request rate has risen 4.2x.
3. Review Lambda throttle counts and cold start counts for all four services to confirm concurrency limits are not being approached.
4. If the spike is sustained, increase DynamoDB provisioned write capacity or confirm auto-scaling is active and responsive for the cart and order tables.
5. Set alarms on 5xx error rates and p95 latency across all services so any degradation is caught before it becomes user-visible.

**B**

1. Monitor all four services for rising p95 latency or 5xx errors; if they remain near baseline no immediate change is needed.
2. Watch product-service db_consumed_read_capacity and read_throttle_events closely, as read load on the Products table grows with request rate and could approach provisioned limits.
3. Check lambda_throttle_count on all services to confirm the shared Lambda concurrency limit is not being approached.
4. Determine whether the traffic increase is expected (promotion, launch, or a client retrying) before making capacity changes.
5. If latency or errors begin to rise, apply rate and burst limits on the API Gateway stage to protect the stack while the traffic source is identified.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 16: CASCADING_FAILURE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service error_rate_5xx 0 → 0.0156 share; product-service p95_latency_ms 6.5595 → 3196.23 ms; product-service request_rate 14.98 → 27.8667 rps; cart-service request_rate 2.0467 → 6.15 rps; order-service request_rate 2.97 → 8.8333 rps; payment-service request_rate 0.9433 → 4.55 rps; product-service throttled_requests 0 → 5223 count/min; product-service read_throttle_events 0 → 7031 count/min; product-service lambda_duration_ms_mean 2.486 → 7.4922 ms; product-service db_latency_ms_p95 5.6117 → 3195.96 ms.

**A**

1. Increase read capacity on the product-service DynamoDB table immediately to reduce the 7031 read throttle events/min and restore p95 latency and error rate; this is the fault with the most direct user impact.
2. Set rate and burst limits on the API Gateway stage to cap the traffic surge and prevent further pressure on all services while the throttling fault is being addressed.
3. Check Lambda concurrency across all four services to confirm the shared account concurrency limit is not being exhausted given the broad request rate increase.
4. Review product-service Lambda logs for timeout and retry patterns during the throttling window to understand the error volume.
5. After increasing DynamoDB capacity, recheck all signals including request_rate, read_throttle_events, db_latency_ms_p95, and error_rate_5xx to confirm each fault has cleared before making further changes.

**B**

1. Immediately increase provisioned read capacity or enable on-demand mode for the product-service DynamoDB table to relieve the 7031 read throttle events per minute.
2. Investigate the source of the traffic surge: product-service request rate nearly doubled and downstream services (cart, order, payment) are all running at roughly 3 to 5x their baseline rates.
3. Review and raise Lambda concurrency limits for product-service if throttling persists after DynamoDB capacity is restored, since lambda_duration_ms_mean has also tripled from 2.5 ms to 7.5 ms.
4. Consider adding a DynamoDB Accelerator (DAX) cache in front of the product-service table to absorb read spikes and reduce direct DynamoDB read pressure.
5. Monitor cart-service, order-service, and payment-service error rates and latencies closely, as all are operating well above their normal request rates.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 0 |

**Better:** A

## Incident 17: DATABASE_THROTTLING

Affected: product-service, payment-service. Metrics (baseline → current): product-service error_rate_5xx 0 → 0.0281 share; product-service p95_latency_ms 6.5595 → 3195.68 ms; payment-service request_rate 0.9433 → 2.0333 rps; product-service throttled_requests 0 → 2181 count/min; product-service read_throttle_events 0 → 3333 count/min; product-service write_throttle_events 0 → 0 count/min; product-service db_latency_ms_p95 5.6117 → 3195.11 ms; product-service db_consumed_read_capacity 449.3 → 59267.5 RCU/min; product-service db_latency_ms_mean 4.044 → 509.33 ms; product-service lambda_duration_ms_p95 3.1188 → 13.1984 ms.

**A**

1. Immediately increase provisioned read capacity units or switch to on-demand capacity mode on the product-service DynamoDB table to relieve the read throttling.
2. Inspect DynamoDB auto-scaling configuration on the product-service table and confirm that scaling policies are enabled and correctly targeted for the current read demand.
3. Review CloudWatch logs and DynamoDB metrics for the product-service table to identify whether a scan, hot partition, or high-frequency read pattern is driving the 132x increase in consumed read capacity.
4. Add application-level or API Gateway caching for frequently read product data to reduce direct DynamoDB read pressure.
5. Monitor product-service Lambda error logs during the throttling window to triage which request paths are generating 5xx responses and whether retries are amplifying read capacity consumption.

**B**

1. Raise the Products table provisioned read capacity or switch it to on-demand mode to relieve the read throttling immediately.
2. After changing capacity, recheck read_throttle_events and db_latency_ms_p95 to confirm the throttle counts and tail latency are returning toward baseline.
3. Reduce the read cost of GET /products by returning one page per call with a limit and next-page token instead of scanning the whole table, or cache the product list inside the Lambda function between invocations.
4. Keep SDK retry settings unchanged during the incident; reducing retry attempts would convert the current tail latency into a higher 5xx error rate.
5. Investigate the payment-service request rate increase in parallel, as it has risen to 2.03 rps from a baseline of 0.94 rps and may warrant separate attention.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 18: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.4818 share; payment-service error_rate_5xx 0 → 1 share; order-service p95_latency_ms 25.0588 → 1944.25 ms; payment-service p95_latency_ms 4.5056 → 1927.98 ms; payment-service request_rate 0.9433 → 1.7667 rps; order-service dependency_error_count 0 → 106 count/min; order-service dependency_latency_ms_p95 22.1478 → 1942.59 ms; order-service dependency_latency_ms_mean 16.2449 → 1938.18 ms; payment-service lambda_duration_ms_mean 0.5855 → 1.3126 ms; order-service p50_latency_ms 3.555 → 7.58 ms.

**A**

1. Check payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment for errors, panics, or timeout messages in the current window to identify what is failing inside the handler.
2. Inspect payment-service lambda_throttle_count in CloudWatch to determine whether Lambda throttling is contributing to the 100% error rate and dependency error count.
3. Review payment-service cold_start_count: cold starts appear in order-service's dependency call time but not in payment-service's own lambda_duration_ms, which could explain the latency gap.
4. After identifying and fixing the payment-service fault, recheck dependency_error_count and order-service error_rate_5xx to confirm recovery before making further changes.
5. Do not add retries to order-service's call to payment-service while payment-service is at 100% error rate, as each retry would add additional invocations and extend latency for waiting clients.

**B**

1. Inspect payment-service Lambda logs and CloudWatch error metrics to identify what is producing the 5xx responses and elevated end-to-end latency.
2. Check API Gateway access logs and integration timeout settings for payment-service to determine whether requests are timing out or being rejected before reaching Lambda.
3. Review Lambda concurrency settings and throttle counts for payment-service to rule out concurrency exhaustion as a contributor.
4. Consider temporarily short-circuiting order-service calls to payment-service to stop cascading 5xx errors to callers while payment-service is investigated.
5. Once payment-service is stabilised, verify that order-service dependency error counts and 5xx rates return to their baselines before closing the incident.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 19: LAMBDA_DEGRADATION

Affected: product-service, payment-service. Metrics (baseline → current): product-service p95_latency_ms 6.5595 → 665.122 ms; payment-service request_rate 0.9433 → 1.8833 rps; product-service lambda_duration_ms_mean 2.486 → 557.776 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.258 ms; product-service lambda_duration_ms_p95 3.1188 → 559.611 ms; product-service p50_latency_ms 4.0869 → 655.063 ms; product-service p99_latency_ms 9.587 → 697.752 ms; product-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 3.7834 ms.

**A**

1. Inspect recent Lambda logs and X-Ray traces for product-service to identify what is consuming handler CPU time during the degraded window starting 2026-11-15T07:58:00Z.
2. Check whether a recent code deployment or configuration change was applied to the product-service Lambda function.
3. Review product-service DynamoDB table metrics for elevated read or write latency that may be adding to handler execution time.
4. Consider increasing the memory allocation for the product-service Lambda, as higher memory also increases CPU share and may reduce execution duration.
5. If the slowdown persists, force a Lambda function update or redeploy to recycle the execution environment.

**B**

1. Inspect CloudWatch Logs at /aws/lambda/anomalypulse-product for the current window to identify which code path in the handler is consuming the extra CPU time.
2. Check the CloudWatch Duration and Errors metrics for anomalypulse-product to determine whether any invocations are approaching or hitting the 10 s timeout, as those would not appear in error_rate_5xx or the latency percentiles.
3. If logs point to a specific recent code or configuration change on anomalypulse-product, roll back that change; otherwise fix the slow code path directly rather than rolling back on timing alone.
4. After any change, recheck lambda_duration_ms_mean and p95_latency_ms for product-service to confirm the handler has returned to baseline levels.
5. Monitor payment-service request_rate to determine whether it stabilises once product-service latency recovers.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 20: LAMBDA_DEGRADATION

Affected: order-service, payment-service. Metrics (baseline → current): order-service p95_latency_ms 25.0588 → 1612.87 ms; payment-service request_rate 0.9433 → 2.1 rps; order-service lambda_duration_ms_mean 2.9034 → 1345.28 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.2494 ms; order-service db_consumed_write_capacity 56.6 → 126 WCU/min; order-service lambda_duration_ms_p95 4.7629 → 1352.1 ms; order-service p50_latency_ms 3.555 → 1590.59 ms; order-service p99_latency_ms 30.0105 → 1638.58 ms; order-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share.

**A**

1. Inspect CloudWatch Logs for the anomalypulse-order Lambda function (log group /aws/lambda/anomalypulse-order) to identify which code path in the handler is running slowly.
2. Check the CloudWatch Errors metric for anomalypulse-order to determine whether any invocations are hitting the 10 s timeout and being cut off before completing, as these would not appear in error_rate_5xx or the latency percentiles.
3. Review any recent changes to the anomalypulse-order function code, layers, or configuration; if logs confirm the slow path is in code touched by a recent change, roll it back.
4. Recheck lambda_duration_ms_mean and p95_latency_ms for order-service after each remediation step to confirm improvement.
5. If no recent code change is found, profile the handler for newly expensive computation or unexpected loops that could account for the ~463x increase in mean duration.

**B**

1. Inspect order-service Lambda logs and AWS X-Ray traces for the 08:28 UTC window to identify what the handler body is executing during the extended duration, such as a slow synchronous loop, a blocking SDK call, or an unexpected retry cycle.
2. Check the order-service Lambda function's memory utilization and duration against its configured timeout; if executions are approaching the timeout, raise the timeout and memory allocation as a temporary mitigation.
3. Review order-service DynamoDB write operations given that consumed write capacity doubled; check for large batch writes or SDK retry storms contributing to handler latency.
4. Set a Lambda concurrency alarm and review CloudWatch Lambda Insights for order-service to detect memory exhaustion or CPU throttling within the execution environment.
5. If the degradation persists, consider deploying a rollback of the order-service Lambda function version while the root cause in handler execution is investigated.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 0 |

**Better:** A

## Incident 21: CASCADING_FAILURE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.65 share; payment-service error_rate_5xx 0 → 1 share; order-service p95_latency_ms 25.0588 → 1785.83 ms; payment-service p95_latency_ms 4.5056 → 1756.29 ms; product-service request_rate 14.98 → 117.183 rps; cart-service request_rate 2.0467 → 25.95 rps; order-service request_rate 2.97 → 49.4333 rps; payment-service request_rate 0.9433 → 32.1333 rps; order-service dependency_error_count 0 → 1928 count/min; order-service dependency_latency_ms_p95 22.1478 → 2159.63 ms.

**A**

1. Investigate payment-service Lambda logs and concurrency immediately, as it is returning 5xx errors on every request and is the most user-visible fault; reduce or restore its ability to handle the current 32.13 rps load.
2. Apply rate and burst limits on the API Gateway stage to cap incoming traffic toward the pre-surge baseline while the payment-service fault is being resolved.
3. Check Lambda throttle counts on all four services, since the account-wide Lambda concurrency pool is shared and the traffic surge across all services may be exhausting it.
4. After payment-service errors are resolved, recheck order-service 5xx rate and p95 latency to confirm recovery, then address any remaining order-service issues separately.
5. Record the timestamps at which each signal changes as fixes are applied, so the effect of each individual action can be distinguished.

**B**

1. Inspect payment-service Lambda logs and error details immediately to identify the failure mode behind its 100% 5xx error rate.
2. Implement or activate a circuit breaker in order-service to stop forwarding requests to payment-service while it is fully degraded, reducing order-service latency and error accumulation.
3. Review and raise Lambda concurrency limits for payment-service and order-service to handle the sharply elevated request rates.
4. Check DynamoDB consumed capacity and throttle metrics for the product-service, cart-service, and order-service tables given the large traffic increase across all services.
5. Evaluate API Gateway throttling or rate limiting to protect downstream services from the continued traffic surge.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 0 |

**Better:** A

## Incident 22: LAMBDA_DEGRADATION

Affected: product-service, payment-service. Metrics (baseline → current): product-service p95_latency_ms 6.5595 → 1486.42 ms; payment-service request_rate 0.9433 → 2 rps; product-service lambda_duration_ms_mean 2.486 → 1110.59 ms; payment-service lambda_duration_ms_mean 0.5855 → 0.3077 ms; product-service lambda_duration_ms_p95 3.1188 → 1256.9 ms; product-service p50_latency_ms 4.0869 → 1475.06 ms; product-service p99_latency_ms 9.587 → 1521.88 ms; product-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 3.5959 ms.

**A**

1. Inspect recent product-service Lambda logs and X-Ray traces from the 2026-11-15T13:59:00Z window to identify where handler time is being spent.
2. Check whether a recent deployment or configuration change affected the product-service Lambda function code or its environment variables.
3. Review product-service DynamoDB call latency and throttle metrics to determine if the database is contributing to the extended handler duration.
4. If the issue persists, consider forcing a Lambda function update or version rollback to replace the current execution environment and clear any runtime-level degradation.
5. Set a CloudWatch alarm on product-service lambda_duration_ms_mean or p95_latency_ms to detect future Lambda degradation automatically.

**B**

1. Inspect CloudWatch Logs for /aws/lambda/anomalypulse-product for slow code paths, errors, or unusual execution patterns in the current window starting 2026-11-15T13:59:00Z.
2. Check the CloudWatch Errors metric for anomalypulse-product to determine whether any invocations are hitting the 10-second timeout, since those would not appear in error_rate_5xx or the latency percentiles.
3. Review any recent changes to the anomalypulse-product function code, layers, or configuration that could account for a handler slowdown of this magnitude.
4. After identifying the slow code path in the logs, fix or roll back only if logs confirm the change touched that path, then recheck lambda_duration_ms_mean and p95_latency_ms to confirm recovery.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 23: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 14.98 → 96.2333 rps; cart-service request_rate 2.0467 → 23.2 rps; order-service request_rate 2.97 → 30.45 rps; payment-service request_rate 0.9433 → 15.35 rps; product-service db_consumed_read_capacity 449.3 → 2887 RCU/min; order-service db_consumed_read_capacity 60.8 → 453 RCU/min; cart-service db_consumed_write_capacity 122.8 → 1392 WCU/min; order-service db_consumed_write_capacity 56.6 → 921 WCU/min; order-service p50_latency_ms 3.555 → 12.35 ms; product-service error_rate_5xx 0 → 0 share.

**A**

1. Check DynamoDB auto-scaling policies and provisioned capacity limits for the product-service, cart-service, and order-service tables, as consumed capacity has risen up to 16x and throttling may begin if the spike continues.
2. Review Lambda concurrency limits and reserved concurrency settings for all four services to ensure invocations are not being throttled at the Lambda layer.
3. Inspect API Gateway throttling and usage-plan limits to confirm they can sustain the elevated request rates across all services.
4. Monitor payment-service error rates and latency closely, as its request rate has risen 16.3x and it has no DynamoDB table to absorb backpressure.
5. Set up CloudWatch alarms on DynamoDB write_throttle_events and read_throttle_events for the cart-service and product-service tables to get early warning if throttling begins.

**B**

1. Determine whether the traffic spike is expected (a promotion, a launch, or a client stuck retrying) before making any configuration changes.
2. Monitor lambda_throttle_count on all four services to detect whether the shared Lambda concurrency limit is being reached; throttled invocations would not appear in the recorded request rates.
3. Watch product-service read_throttle_events and db_consumed_read_capacity closely; if reads continue rising toward table capacity limits, raise the Products table provisioned read capacity or switch it to on-demand.
4. If latency and error rates hold near baseline as they currently do, continue monitoring without immediate changes.
5. To protect the stack while investigating the traffic source, consider setting rate and burst limits on the API Gateway stage.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 24: DATABASE_THROTTLING

Affected: payment-service. Metrics (baseline → current): payment-service request_rate 0.9433 → 1.8667 rps; payment-service error_rate_5xx 0 → 0 share; payment-service p95_latency_ms 4.5056 → 4.629 ms.

**A**

1. Inspect Lambda concurrency and throttle counts for payment-service in CloudWatch to determine whether the higher request volume is approaching concurrency limits.
2. Review order-service dependency metrics, including dependency_error_count and dependency_latency_ms_p95, as order-service calls payment-service on every POST /orders and would reflect any emerging impact.
3. Check CloudWatch metrics for any DynamoDB tables in upstream services that feed traffic to payment-service for read or write throttle events that may correlate with the request-rate increase.
4. Set a CloudWatch alarm on payment-service request rate so a further increase triggers an alert before latency or error rates degrade.

**B**

1. Check read_throttle_events and throttled_requests on the DynamoDB tables accessed along the call path that reaches payment-service, particularly on the Orders table, to confirm which table is throttling.
2. Review db_latency_ms_p95 on order-service to determine whether tail latency is rising there, since order-service calls payment-service on every POST /orders.
3. Monitor payment-service p95_latency_ms and error_rate_5xx over the next several minutes; if the request rate remains elevated, 5xx errors may appear as retries are exhausted.
4. After identifying the throttled table, raise its provisioned capacity or switch it to on-demand mode to relieve the throttle.
5. Recheck read_throttle_events and db_latency_ms_p95 on the affected table after each capacity change to confirm improvement.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 25: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 14.98 → 43.1 rps; cart-service request_rate 2.0467 → 10.2 rps; order-service request_rate 2.97 → 13.9833 rps; payment-service request_rate 0.9433 → 6.9 rps; product-service db_consumed_read_capacity 449.3 → 1293.5 RCU/min; order-service db_consumed_read_capacity 60.8 → 212.5 RCU/min; cart-service db_consumed_write_capacity 122.8 → 612 WCU/min; order-service db_consumed_write_capacity 56.6 → 414 WCU/min; order-service p50_latency_ms 3.555 → 7.67 ms; product-service error_rate_5xx 0 → 0 share.

**A**

1. Check DynamoDB table provisioned capacity or on-demand scaling status for product-service, cart-service, and order-service to ensure they can absorb the increased read and write load without throttling.
2. Review API Gateway and Lambda concurrency limits across all four services to confirm they are not approaching throttle thresholds under the elevated request rates.
3. Monitor for the emergence of DynamoDB throttle events or Lambda throttle counts on any of the four services as load continues.
4. If the spike is sustained, consider pre-warming or increasing reserved concurrency on the most heavily loaded Lambdas, particularly payment-service which saw a 7.3x rate increase.
5. Investigate the source of the traffic surge via API Gateway access logs to determine whether it is organic, a retry storm, or unexpected client behaviour.

**B**

1. Determine whether the traffic increase is expected, such as a promotion or a misbehaving client retrying, before making any infrastructure changes.
2. Monitor lambda_throttle_count on all four services, as the shared Lambda concurrency limit could be approached at these elevated request rates.
3. Watch product-service read_throttle_events closely: db_consumed_read_capacity has risen to 1293.5 RCU/min and may approach the Products table provisioned capacity; if throttling appears, raise the provisioned read capacity or switch the table to on-demand.
4. If latency and error rates hold steady, continue monitoring without making changes.
5. If the spike continues or errors begin to rise, set rate and burst limits on the API Gateway stage to protect the stack while the traffic source is investigated.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 26: TRAFFIC_SPIKE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): product-service request_rate 14.98 → 57.2333 rps; cart-service request_rate 2.0467 → 13.9167 rps; order-service request_rate 2.97 → 17.6833 rps; payment-service request_rate 0.9433 → 8.5167 rps; product-service db_consumed_read_capacity 449.3 → 1717.5 RCU/min; order-service db_consumed_read_capacity 60.8 → 275 RCU/min; cart-service db_consumed_write_capacity 122.8 → 835 WCU/min; order-service db_consumed_write_capacity 56.6 → 511 WCU/min; product-service error_rate_5xx 0 → 0 share; cart-service error_rate_5xx 0 → 0 share.

**A**

1. Check DynamoDB table provisioned throughput or on-demand capacity limits for product-service, cart-service, and order-service, as consumed capacity has risen up to 9x and throttling may begin if the spike continues.
2. Review Lambda concurrency limits for all four services to ensure they can sustain the current request rates without throttling.
3. Monitor payment-service closely, as its request rate rose 9x and it has no DynamoDB table to absorb backpressure; sustained load could increase dependency error counts seen by order-service.
4. Set up or review API Gateway throttling limits to protect downstream services if the traffic spike continues to grow.
5. Investigate the source of the traffic surge to determine whether it is organic, a bot or scraping event, or a misconfigured client.

**B**

1. Monitor all four services closely for any rise in error_rate_5xx or p95_latency_ms, as the stack is currently absorbing the load but capacity headroom is being consumed.
2. Check lambda_throttle_count on all services; a shared account concurrency limit could be approached under sustained load at these elevated rates.
3. Watch product-service db_consumed_read_capacity and read_throttle_events, as read capacity has already risen to 1717.5 RCU/min under this spike.
4. Determine whether the traffic surge is expected (a promotion, a client retry loop, or an external event) before making configuration changes.
5. If errors begin to rise, apply rate and burst limits on the API Gateway stage to protect the stack while the traffic source is investigated.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 27: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.3673 share; payment-service error_rate_5xx 0 → 0.7317 share; order-service p95_latency_ms 25.0588 → 3648.78 ms; payment-service p95_latency_ms 4.5056 → 3633.66 ms; payment-service request_rate 0.9433 → 2.05 rps; order-service dependency_error_count 0 → 90 count/min; order-service dependency_latency_ms_p95 22.1478 → 3648.12 ms; order-service dependency_latency_ms_mean 16.2449 → 2660.22 ms; payment-service lambda_duration_ms_mean 0.5855 → 2.2295 ms; order-service db_latency_ms_p95 5.2849 → 19.904 ms.

**A**

1. Inspect payment-service Lambda logs and recent invocation errors in CloudWatch Logs to identify the internal failure mode (exceptions, timeouts, or misconfigurations).
2. Check payment-service Lambda configuration for concurrency limits, reserved concurrency, and timeout settings that could explain the near-total failure rate.
3. Verify any external dependencies or integrations invoked inside the payment-service Lambda handler, as payment-service has no DynamoDB table but may call other endpoints.
4. Implement or verify a circuit breaker or timeout in order-service for its calls to payment-service to limit error propagation while payment-service is degraded.
5. If the payment-service Lambda was recently redeployed or its environment variables changed, roll back to the last known good version to restore service.

**B**

1. Inspect payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment for errors, exceptions, or timeout signals in the handler to identify what is failing inside Payment.
2. Check payment-service lambda_throttle_count in CloudWatch to determine whether Lambda throttling is contributing to the dependency_error_count in order-service.
3. Compare dependency_latency_ms_p95 on order-service against payment-service p95_latency_ms; they are nearly identical at roughly 3648 ms, so investigate whether calls are approaching or hitting the read timeout on order-service's call to payment-service.
4. Once the root failure in payment-service is identified and fixed, verify that order-service dependency_error_count and error_rate_5xx return to baseline before closing the incident.
5. Do not add retries to order-service's call to payment-service while payment-service is struggling, as each retry would add additional invocations and further increase latency exposure.

| | Correct | Safe |
|---|---|---|
| A | 0 | 0 |
| B | 1 | 1 |

**Better:** B

## Incident 28: DEPENDENCY_FAILURE

Affected: order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.1694 share; payment-service error_rate_5xx 0 → 0.3727 share; order-service p95_latency_ms 25.0588 → 2265.36 ms; payment-service p95_latency_ms 4.5056 → 2249.58 ms; payment-service request_rate 0.9433 → 1.8333 rps; order-service dependency_error_count 0 → 41 count/min; order-service dependency_latency_ms_p95 22.1478 → 2264.43 ms; order-service dependency_latency_ms_mean 16.2449 → 851.318 ms; order-service p99_latency_ms 30.0105 → 2268.97 ms; payment-service p99_latency_ms 6.949 → 2249.76 ms.

**A**

1. Inspect payment-service Lambda logs and recent invocation errors immediately to identify the internal failure mode producing 37.3% 5xx responses and ~2.25 s p95 latency.
2. Check payment-service Lambda configuration for timeout settings, memory limits, and any upstream HTTP or third-party calls the handler makes that could be stalling.
3. Review API Gateway access logs for payment-service to determine whether errors are originating inside the Lambda handler or at the gateway layer.
4. Enforce a hard timeout on order-service's outbound calls to payment-service and consider returning a graceful error to callers rather than waiting the full 2+ seconds, to limit blast radius while payment-service is degraded.
5. Once payment-service is stabilised, verify order-service error and latency metrics return to their baselines and that no orders were silently dropped during the incident window.

**B**

1. Inspect payment-service CloudWatch Logs at /aws/lambda/anomalypulse-payment and check lambda_duration_ms_mean and lambda_duration_ms_p95 to identify what is failing or slow inside the payment handler.
2. Check payment-service lambda_throttle_count in CloudWatch to determine whether throttled invocations are contributing to the dependency_error_count on order-service.
3. Do not add retries to order-service's call to payment-service while payment-service is struggling, as each retry adds an additional invocation and extends wait time for clients.
4. If payment-service is found to be throttled, assign it reserved concurrency to protect its share of Lambda concurrency from other functions.
5. After each change, recheck dependency_error_count and order-service error_rate_5xx to confirm improvement.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 1 | 1 |

**Better:** B

## Incident 29: CASCADING_FAILURE

Affected: product-service, cart-service, order-service, payment-service. Metrics (baseline → current): order-service error_rate_5xx 0 → 0.3919 share; payment-service error_rate_5xx 0 → 0.7617 share; order-service p95_latency_ms 25.0588 → 4456.51 ms; payment-service p95_latency_ms 4.5056 → 3988.62 ms; product-service request_rate 14.98 → 62.2 rps; cart-service request_rate 2.0467 → 14.7167 rps; order-service request_rate 2.97 → 20.1167 rps; payment-service request_rate 0.9433 → 10.35 rps; order-service dependency_error_count 0 → 473 count/min; order-service dependency_latency_ms_p95 22.1478 → 4491.31 ms.

**A**

1. Investigate payment-service Lambda logs and concurrency immediately, as it has the highest 5xx rate at 76.2% and the longest p95 latency at 3988.6 ms, and address that fault first to stop the largest share of user-visible errors.
2. Apply rate and burst limits on the API Gateway stage to cap incoming traffic while both faults are being worked, given all services are running at 4x to 11x their baseline request rates.
3. Check Lambda throttle counts on all four services, since the elevated request rates across all functions may be competing for the shared Lambda concurrency limit.
4. After stabilizing payment-service, recheck order-service error rate and dependency error count to confirm whether order-service errors clear or a separate fault remains.
5. Record the timestamp of each change so that the effect of each fix can be distinguished from the other concurrent fault.

**B**

1. Inspect payment-service Lambda logs and CloudWatch metrics immediately to identify the source of the 76.2% 5xx error rate and extreme latency.
2. Check payment-service Lambda concurrency limits and throttle counts to determine whether invocations are being throttled or timing out under the elevated request rate.
3. Implement or verify a circuit breaker on order-service's calls to payment-service to stop accumulating 473 dependency errors per minute and shed load quickly.
4. Review Lambda concurrency reservations and auto-scaling settings for all four services to handle the observed traffic increase across the board.
5. Once payment-service is stabilised, confirm order-service error rate and dependency error count return to baseline before declaring the incident resolved.

| | Correct | Safe |
|---|---|---|
| A | 0 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 30: DATABASE_THROTTLING

Affected: product-service, payment-service. Metrics (baseline → current): payment-service request_rate 0.9433 → 2.0833 rps; product-service throttled_requests 0 → 297 count/min; product-service read_throttle_events 0 → 533 count/min; product-service write_throttle_events 0 → 0 count/min; product-service db_latency_ms_p95 5.6117 → 5.0939 ms; product-service db_consumed_read_capacity 449.3 → 67974 RCU/min; product-service p99_latency_ms 9.587 → 41.8643 ms; product-service error_rate_5xx 0 → 0 share; payment-service error_rate_5xx 0 → 0 share; product-service p95_latency_ms 6.5595 → 6.81 ms.

**A**

1. Raise the Products table provisioned read capacity or switch it to on-demand mode to relieve the current throttling immediately.
2. Check db_consumed_read_capacity against the Products table provisioned read rate to quantify how far over capacity reads are running, and inspect whether GET /products calls are scanning the full table on every request.
3. Reduce per-call read cost on GET /products by adding pagination (a limit and next-page token) or caching the product list inside the Lambda function between invocations.
4. Keep the SDK retry configuration unchanged during the incident; reducing retry attempts would convert the current tail latency into 5xx errors.
5. After each change, recheck read_throttle_events and db_latency_ms_p95 to confirm throttling is subsiding.

**B**

1. Increase provisioned read capacity units or switch the product-service DynamoDB table to on-demand capacity mode to absorb the current read demand of 67,974 RCU/min.
2. Enable DynamoDB Auto Scaling on the product-service table so read capacity adjusts automatically to demand spikes.
3. Inspect the product-service table for hot partition keys or unintended full-table scan operations that may account for the 151x jump in consumed read capacity.
4. Add a caching layer such as DynamoDB Accelerator (DAX) in front of the product-service table to reduce repeated read load on the table.
5. Monitor payment-service request rate to determine whether its roughly 2.2x traffic increase is sustained and whether it will add further stress to downstream resources.

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 1 | 1 |

**Better:** A
