# Runbook: database throttling

**Signals.** `read_throttle_events` or `throttled_requests` above zero, with `db_latency_ms_p95` and
`p95_latency_ms` far above baseline while `p50_latency_ms` barely moves. `error_rate_5xx` often stays near
zero.

**How throttling shows in this stack.** Product, Cart and Order retry DynamoDB calls in the SDK, 7 attempts
in all. The retries happen inside the DynamoDB call, so they are counted in `db_latency_ms`: throttling
shows as tail latency, and as 5xx only when a request runs out of attempts.

**Check**
1. Which table: the throttle counts per service. The Products table (`anomalypulse-products-v2`) is
   provisioned at 200 read and 5 write capacity units per second. The Carts and Orders tables are
   on-demand, with no provisioned capacity to raise.
2. product-service's `db_consumed_read_capacity` (reported per minute) against the table's provisioned
   reads (per second). DynamoDB lends unused capacity saved over the last 5 minutes, so reads can run above
   the provisioned rate for a while before throttling starts.
3. `request_rate`. If it rose, read the traffic spike runbook too. If it is flat, look at what each request
   reads: `GET /products` reads the whole table on every call, page after page, so its cost grows with the
   table, while `GET /products/{id}` is a single item read.
4. `error_rate_5xx`: a rise means requests are running out of their 7 attempts.

**Act**
1. Raise the Products table's provisioned read capacity, or switch it to on-demand.
2. Reduce what `GET /products` reads per call: return one page per call (a limit and a next-page token)
   instead of the whole table, or cache the product list in the function between invocations.
3. Keep the SDK retries as they are during the incident. Fewer attempts would turn tail latency into 5xx
   errors.
4. Recheck `read_throttle_events` and `db_latency_ms_p95` after each change.
