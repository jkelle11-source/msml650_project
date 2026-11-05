# Runbook: traffic spike

**Signals.** `request_rate` well above its baseline on one or more services. Whether `p95_latency_ms` and
`error_rate_5xx` moved with it decides how urgent this is.

**Check**
1. Which services' `request_rate` rose, and by how much. product-service serves `GET /products` and
   `GET /products/{id}`; order-service's rate includes `POST /orders`, each of which calls payment-service.
2. Whether `p95_latency_ms` and `error_rate_5xx` stayed near baseline. If they did, the stack is absorbing
   the load: keep watching rather than change anything.
3. `lambda_throttle_count` on every service. The account allows 1,000 concurrent Lambda executions, shared
   by all functions. A throttled invocation never reaches the handler, so it is missing from the service's
   own request records.
4. The Products table (`anomalypulse-products-v2`): it is provisioned at 200 read capacity units per
   second, and `GET /products` scans it, so its reads grow with traffic. Watch product-service's
   `db_consumed_read_capacity` (reported per minute) and `read_throttle_events`.
5. Whether the extra traffic is expected (a launch, a promotion, a client stuck retrying) before acting.

**Act**
1. If latency and errors hold, monitor; no change is needed yet.
2. If `lambda_throttle_count` rises, give the user-facing functions headroom: reserved concurrency for
   them, or a higher account concurrency quota.
3. If product-service reads approach the table's capacity, raise the Products table's provisioned read
   capacity or switch it to on-demand. The Carts and Orders tables are already on-demand.
4. To protect the stack while you find where the traffic comes from, set rate and burst limits on the API
   Gateway stage (`dev`).
