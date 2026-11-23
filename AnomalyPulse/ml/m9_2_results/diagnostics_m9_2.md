# M9-2 diagnostics (after scoring)

These explain the scored results. They are not results themselves: nothing was fitted or tuned, and a swap test is offline evidence about which columns a result depends on, not a controlled live experiment.

Code: commit `ee9083bfdd52d98afe8b013df42c44551dfa0231`; seed 20261123; generated 2026-11-23T06:56:00+00:00.

## The Isolation Forest on healthy windows

Cut-off: -0.3695 (scores below it are flagged).

| Phase | Healthy windows | Flagged | Median score | Highest (most normal) score |
|---|---|---|---|---|
| val | 336 | 4 | -0.337 | -0.320 |
| test | 770 | 770 | -0.422 | -0.394 |
| stress | 20 | 0 | -0.342 | -0.327 |

Same day (2026-11-16): the shifted mix's test runs had 154 of 154 healthy windows flagged, the default mix's stress runs 0 of 20.

Healthy-window means:

| Column | Validation | Test |
|---|---|---|
| product_request_rate | 15.00 | 13.00 |
| cart_request_rate | 2.00 | 3.01 |
| order_request_rate | 2.97 | 3.99 |
| payment_request_rate | 0.99 | 2.00 |
| order_db_consumed_write_capacity | 59.63 | 119.79 |
| order_p50_latency_ms | 3.41 | 10.52 |

Swap test: with the 22 request-rate and database-capacity columns of the 770 healthy test windows replaced by validation values, the flagged share goes from 100.0% to 6.1%.

## DEPENDENCY_FAILURE called CASCADING_FAILURE

`POST /orders` is 33.3% of Order's traffic in the default mix and 50.0% of Order's traffic in the shifted mix.

| Runs | Windows | Dependency recall | Share with Order p50 above 100 ms |
|---|---|---|---|
| validation | 105 | 0.990 | 0.000 |
| test | 265 | 0.540 | 0.472 |
| stress | 25 | 1.000 | 0.000 |

Test windows with a slow Order p50: 122 of 125 called a cascade. With a fast one: 0 of 140.

Swap test: replacing only `order_p50_latency_ms` with validation dependency-failure values takes dependency recall from 0.540 to 1.000. Control: the same swap on the 118 real cascade windows leaves their recall at 1.000 (was 1.000).
