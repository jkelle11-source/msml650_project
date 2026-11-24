"""Tests for cost/measured.py (no AWS). Two kinds:
  - the arithmetic, on small made-up inputs whose answers are worked out by hand here, not by the code under test;
  - the committed M9-1 inputs, against facts that hold independently of the model (the bill's quantities and
    prices, one Diagnosis call per scored minute, one Bedrock call per explanation call, 60 healthy runs at 20 rps).

    python3 -m pytest cost -q        # from AnomalyPulse/
"""
from pathlib import Path

import pytest

from cost import measured as m

INPUTS = Path(__file__).parent / "inputs" / "m9"
REAL = m.Inputs(INPUTS)
RES = m.report(REAL)
FN = "anomalypulse-"


def approx(x, rel=1e-9):
    return pytest.approx(x, rel=rel)


def fake(**attrs):
    """An Inputs with made-up attributes, keeping its ce_qty / ce_cost helpers."""
    inp = m.Inputs.__new__(m.Inputs)
    inp.__dict__.update({"memory": {}, "batches": {}, "days": {}, "runs": [], "ops": {}, "ce": [], "reports": {},
                         "incidents": {}, **attrs})
    return inp


def ce_row(day, usage_type, qty, service="x", cost=0.0):
    return {"day": day, "service": service, "usage_type": usage_type, "qty": qty, "cost": cost, "unit": ""}


def fns(**kw):
    return {FN + k: {"gb_s": v[0], "invocations": v[1], "log_bytes": v[2]} for k, v in kw.items()}


# ---- the arithmetic, by hand ----

def test_a_lambda_costs_its_gb_seconds_plus_its_requests():
    # 1,000 GB-s at $0.0000133334 = $0.0133334; a million requests at $0.20 a million = $0.20
    assert m.fn_cost(1000, 1_000_000) == approx(0.0133334 + 0.20)


def test_a_bill_row_is_priced_at_list_price_by_usage_type_and_service():
    assert m.list_cost(ce_row("d", "USE2-ApiGatewayRequest", 1000, "Amazon API Gateway")) == approx(0.0035)
    # S3 and SQS share the usage type USE2-Requests-Tier1 at different prices
    assert m.list_cost(ce_row("d", "USE2-Requests-Tier1", 1000, m.S3)) == approx(0.005)
    assert m.list_cost(ce_row("d", "USE2-Requests-Tier1", 1000, m.SQS)) == approx(0.0004)
    # Bedrock bills per million tokens: 2 (million) input tokens at $3 a million
    assert m.list_cost(ce_row("d", m.BEDROCK_IN, 2.0)) == approx(6.0)
    # no list price here: keep what was billed
    assert m.list_cost(ce_row("d", "USE2-TimedStorage-ByteHrs", 5.0, m.S3, cost=0.12)) == 0.12


def test_the_services_logs_are_anomalypulse_s_and_the_runner_s_tooling():
    assert m.log_group("product") == "anomalypulse"
    assert m.log_group("pipeline-aggregator") == "anomalypulse"
    assert m.log_group("stub-501") == "tooling"


def test_bedrock_per_call_is_the_batch_days_billed_tokens_over_the_explanation_calls():
    days = {d: {"functions": fns(explain=(0, 100, 0))} for d in m.BATCH_DAYS}
    ce = [ce_row(d, m.BEDROCK_IN, 0.3) for d in m.BATCH_DAYS] + [ce_row(d, m.BEDROCK_OUT, 0.2) for d in m.BATCH_DAYS]
    b = m.bedrock_per_call(fake(days=days, ce=ce))
    # 0.9 million input tokens over 300 calls = 3,000 a call; 0.6 million output = 2,000 a call
    assert (b["calls"], b["input_tokens"], b["output_tokens"]) == (300, approx(3000), approx(2000))
    assert b["cost"] == approx(3000 * 3e-6 + 2000 * 15e-6)          # $0.009 + $0.03 = $0.039


def test_bill_lines_take_the_batch_days_above_the_idle_days_per_request():
    days = {d: {"api": 1_000_000} for d in m.BATCH_DAYS}
    ce = ([ce_row(d, "USE2-Requests-Tier1", 100_000, m.S3) for d in m.BATCH_DAYS]
          + [ce_row(d, "USE2-Requests-Tier1", 10_000, m.S3) for d in m.IDLE_DAYS]
          + [ce_row(d, "USE2-BoxUsage:c7g.large", 24, "Amazon Elastic Compute Cloud - Compute") for d in m.BATCH_DAYS])
    batches = {"b": {"start": "2026-11-13T23:00:00+00:00", "end": "2026-11-14T01:00:00+00:00", "minutes": 120}}
    rates, runner = m.bill_rates(fake(days=days, ce=ce, batches=batches))
    s3 = rates["S3 PUT-class requests"]
    # 300,000 on the batch days less 3 x 10,000 idle = 270,000, over 3,000,000 requests
    assert s3["per_request"] == approx(0.09)
    assert s3["per_month"] == approx(10_000 * 730 / 24)              # the idle days' level, a month
    assert rates["SQS requests"]["per_request"] == 0                 # no SQS rows: nothing
    # the runner: 72 hours over the batch minutes inside Nov 14-16, which is only the hour after midnight
    assert runner["runner instance"]["per_batch_minute"] == approx(72 / 60)


def test_batch_minutes_count_only_the_part_inside_the_days():
    batches = {"a": {"start": "2026-11-13T23:00:00+00:00", "end": "2026-11-14T01:00:00+00:00"},
               "b": {"start": "2026-11-16T23:30:00+00:00", "end": "2026-11-17T02:00:00+00:00"},
               "c": {"start": "2026-11-15T10:00:00+00:00", "end": "2026-11-15T10:17:00+00:00"},
               "d": {"start": "2026-11-18T10:00:00+00:00", "end": "2026-11-18T12:00:00+00:00"}}     # outside: none
    assert m.batch_minutes_in(fake(batches=batches), m.BATCH_DAYS) == approx(60 + 30 + 17)


def small_batch():
    """One 120-minute batch: 120 diagnosis calls (2 cold) billed 6,000 ms, 3,600 of it start-up; the explanation
    Lambda used 1,000 GB-s, and the incident store 1,300, 1,000 of which was waiting on it."""
    memory = {FN + "diagnosis": 1024, FN + "incidents": 512, FN + "explain": 512, FN + "pipeline-window-builder": 512}
    usage = {"api": 0, "functions": fns(**{"pipeline-window-builder": (240, 120, 0), "diagnosis": (3, 120, 0),
                                           "incidents": (1300, 120, 0), "explain": (1000, 10, 0)}),
             "tables": {FN + "incidents": {"read_units": 0, "write_units": 120}}}
    batches = {"b": {"minutes": 120, "usage": usage}}
    reports = {"batches": {"b": {"calls": 120, "cold_starts": 2, "billed_ms": 6000, "duration_ms": 1200,
                                 "init_ms": 3600, "max_mb": 250}},
               "idle": {"calls": 0, "start": "s", "end": "e"}}
    return fake(memory=memory, batches=batches, reports=reports)


def test_an_inference_costs_its_billed_milliseconds_at_1_gb_plus_the_request():
    n = m.per_inference(small_batch(), 1.0)
    # 6 GB-s at $0.0000133334 plus 120 requests at $0.0000002, over 120 calls
    assert n["cost"] == approx((6 * 1.33334e-5 + 120 * 2e-7) / 120)
    # without the start-up time: (6,000 - 3,600) / 120 = 20 ms a call
    assert n["warm_cost"] == approx(0.020 * 1.33334e-5 + 2e-7)
    assert (n["billed_ms_per_call"], n["handler_ms_per_call"], n["cold_start_share_of_billed"]) == (50, 10, approx(0.6))


def test_the_scored_minute_path_leaves_the_store_s_wait_to_the_incidents():
    n = m.per_inference(small_batch(), 1.0)
    # window builder 240 GB-s, diagnosis 6 (its REPORT lines, not CloudWatch's 3), the store 1,300 less the 1,000
    # it waited: 546 GB-s; 360 requests; 120 table writes; over 120 minutes
    assert n["scored_minute_path"] == approx((546 * 1.33334e-5 + 360 * 2e-7 + 120 * 0.625e-6) / 120)


def test_projection_scales_each_line_by_its_basis_and_takes_the_free_allowance_once():
    lines = [{"line": "api", "group": "app", "price": 1e-6, "free": None, "per": {"request": 1.0}},
             {"line": "fn", "group": "anomalypulse", "price": 1.33334e-5, "free": "lambda_gbs", "per": {"minute": 10.0}},
             {"line": "inc", "group": "anomalypulse", "price": 0.05, "free": None, "per": {"incident": 1.0}},
             {"line": "fixed", "group": "app", "price": 2.0, "free": None, "per": {"month": 3.0}}]
    p = m.project(lines, 2, incidents=30)
    # 2 rps x 2,628,000 s = 5,256,000 requests -> $5.256; 10 GB-s x 43,800 minutes = 438,000 GB-s -> $5.8400292;
    # 30 incidents -> $1.50; the fixed line $6.00
    assert p["requests"] == 5_256_000
    assert p["app"] == approx(5.256 + 6.0)
    assert p["anomalypulse"] == approx(438_000 * 1.33334e-5 + 1.5)
    # only 400,000 of the 438,000 GB-s are free
    assert p["always_free"] == approx(-400_000 * 1.33334e-5)
    assert p["total_after_free"] == approx(5.256 + 6.0 + 38_000 * 1.33334e-5 + 1.5)


def test_dynamodb_s_free_capacity_is_the_price_list_s_18_600_unit_hours():
    lines = [{"line": "rcu", "group": "app", "price": 0.00013, "free": "rcu_h", "per": {"month": 200 * 730}},
             {"line": "alarms", "group": "anomalypulse", "price": 0.10, "free": "cw_alarms", "per": {"month": 7.0}}]
    p = m.project(lines, 1)
    # 146,000 read unit-hours, 18,600 of them free at $0.00013; all 7 alarms inside the free 10 at $0.10
    assert p["total"] == approx(146_000 * 0.00013 + 0.70)
    assert p["always_free"] == approx(-(18_600 * 0.00013 + 0.70))


def test_gbs_factor_is_the_batch_days_bill_over_cloudwatch():
    days = {**{d: {"functions": fns(a=(1000, 0, 0))} for d in m.BATCH_DAYS},
            **{d: {"functions": fns(a=(100, 0, 0))} for d in m.IDLE_DAYS}}
    ce = ([ce_row(d, "USE2-Lambda-GB-Second-ARM", 1006) for d in m.BATCH_DAYS]
          + [ce_row(d, "USE2-Lambda-GB-Second-ARM", 120) for d in m.IDLE_DAYS])
    # 3,018 billed over 3,000 measured; the idle days' 1.2 is left out
    assert m.gbs_factor(fake(days=days, ce=ce)) == approx(1.006)


def test_an_incident_costs_its_bedrock_calls_the_explanation_lambda_and_the_store_s_wait():
    memory = {FN + "incidents": 512, FN + "explain": 512}
    batches = {"b": {"usage": {"functions": fns(explain=(100, 10, 2 ** 30))}}}
    incidents = {"b": {"incidents": 5, "bedrock_calls": 10}}
    i = m.per_incident(fake(memory=memory, batches=batches, incidents=incidents), 1.0, {"cost": 0.03})
    # Bedrock: 10 calls at $0.03 = $0.30; the explanation Lambda: 100 GB-s, 10 requests and 1 GB of logs ($0.50);
    # the store waits as long, at the same memory: another 100 GB-s. Over 5 incidents.
    assert i["calls_per_incident"] == 2
    assert i["bedrock"] == approx(0.06)
    assert i["store_waiting"] == approx(100 * 1.33334e-5 / 5)
    assert i["cost"] == approx((0.30 + 100 * 1.33334e-5 + 10 * 2e-7 + 0.50 + 100 * 1.33334e-5) / 5)


def test_idle_prices_every_row_at_list_and_keeps_what_was_billed():
    def day(d):
        return [ce_row(d, "USE2-ReadCapacityUnit-Hrs", 4800, m.DYNAMODB, cost=0.624),
                ce_row(d, "USE2-WriteCapacityUnit-Hrs", 120, m.DYNAMODB, cost=0.0),         # free on the bill
                ce_row(d, "USE2-CW:Requests", 38_880, m.CLOUDWATCH, cost=0.0),               # free on the bill
                ce_row(d, "USE2-Requests-Tier1", 20_000, m.S3, cost=0.10),
                ce_row(d, "USE2-EBS:VolumeUsage.gp3", 0.5333, "EC2 - Other", cost=0.04267),  # no list price here
                ce_row(d, "USE2-CW:GMD-Metrics", 177, m.CLOUDWATCH, cost=0.00177)]           # this analysis's own
    memory = {FN + "metrics-extractor": 1769, FN + "diagnosis": 1024}
    days = {d: {"functions": fns(**{"metrics-extractor": (0, 1440, 0), "diagnosis": (0, 0, 0)})} for d in m.IDLE_DAYS}
    inp = fake(ce=[r for d in m.IDLE_DAYS for r in day(d)], memory=memory, days=days, reports={"idle": {"calls": 0}})
    i = m.idle(inp)
    # the store: 4,800 read and 120 write unit-hours at $0.00013 and $0.00065; AnomalyPulse: 38,880 CloudWatch
    # requests at $0.01 a thousand and 20,000 S3 PUTs at $0.005 a thousand; the runner's disk as billed
    assert i["by_group_day"] == {"app": approx(0.624 + 0.078), "anomalypulse": approx(0.3888 + 0.10),
                                 "tooling": approx(0.04267)}
    assert i["day"] == approx(0.702 + 0.4888 + 0.04267)
    assert i["billed_day"] == approx(0.624 + 0.10 + 0.04267)
    assert i["month"] == approx(i["day"] * 730 / 24)
    assert i["functions_running_per_day"] == {"metrics-extractor": 1440}


def test_the_bill_cross_check_takes_the_batch_days_above_idle_scaled_to_every_batch_minute():
    ce = ([ce_row(d, "USE2-ApiGatewayRequest", 1_000_000, "Amazon API Gateway", cost=3.0) for d in m.BATCH_DAYS]
          + [ce_row(d, "USE2-TimedStorage-ByteHrs", 1.0, m.S3, cost=0.1) for d in m.BATCH_DAYS + m.IDLE_DAYS])
    batches = {"b": {"start": "2026-11-13T23:00:00+00:00", "end": "2026-11-14T01:00:00+00:00", "minutes": 120}}
    c = m.bill_cross_check(fake(ce=ce, batches=batches), 21.0)
    # list: 3 x $3.50 + 3 x $0.10 of storage, less 3 idle days' $0.10; 60 of the batch's 120 minutes are inside
    assert c["batch_minutes_inside"] == 60
    assert c["test_set_from_bill"] == approx(10.5 * 120 / 60)
    assert c["model_over_bill"] == approx(1.0)
    assert c["free_allowances_used"] == approx(10.8 - 9.3)           # the bill charged $3.00, not $3.50, a day


def test_cost_lines_group_by_kind():
    assert [m.line_kind(x) for x in ("Lambda product", "logs diagnosis", "DynamoDB carts", "S3 PUT-class requests",
                                     "API Gateway requests")] == ["Lambda", "logs", "DynamoDB", "S3 PUT-class requests",
                                                                  "API Gateway requests"]


def test_s3_puts_on_the_healthy_basis_scale_by_the_pipeline_s_writes_per_request():
    pipe = lambda ship, agg: fns(**{"telemetry-shipper": (0, ship, 0), "pipeline-aggregator": (0, agg, 0)})
    runs = [{"usage": {"api": 1000, "functions": pipe(35, 20)}}]
    days = {d: {"api": 1000, "functions": pipe(48, 14)} for d in m.BATCH_DAYS}
    b = m.request_basis(fake(runs=runs, days=days), {"S3 PUT-class requests": {"per_request": 0.0682}})
    # 55 writes per 1,000 requests healthy, 62 on the batch days: 0.0682 x 55 / 62
    assert b["pipeline_writes_per_request"] == {"healthy_runs": approx(0.055), "batch_days": approx(0.062)}
    assert b["shipper_calls_per_request"] == {"healthy_runs": approx(0.035), "batch_days": approx(0.048)}
    assert b["healthy_over_batch"] == approx(55 / 62)
    assert b["s3_put_per_request_healthy"] == approx(0.0682 * 55 / 62)
    lines = [{"line": "S3 PUT-class requests", "per": {"request": 0.0682, "month": 5.0}}, {"line": "x", "per": {"request": 1.0}}]
    alt = m.healthy_s3_lines(lines, b)
    assert alt[0]["per"] == {"request": approx(0.0605), "month": 5.0} and alt[1] == lines[1]


def ops_batch(p50, p95, p99, mx, errors, throttles, conc, rpm_max, sent, deleted, visible, age_max, age_median):
    return {"api": {"latency_p50": 15.0, "latency_p95": 900.0, "latency_p99": 3000.0, "latency_maximum": 9000.0,
                    "requests_per_minute_mean": 0, "requests_per_minute_max": rpm_max, "4xxerror": 1.0, "5xxerror": 2.0},
            "functions": {FN + "product": {"duration_p50": p50, "duration_p95": p95, "duration_p99": p99,
                                           "duration_maximum": mx, "errors": errors, "throttles": throttles,
                                           "concurrency_max": conc},
                          FN + "dashboard": dict.fromkeys(("duration_p50", "duration_p95", "duration_p99", "duration_maximum",
                                                          "errors", "throttles", "concurrency_max"))},
            "queue": {"NumberOfMessagesSent": sent, "NumberOfMessagesDeleted": deleted, "visible_minute_max": visible,
                      "age_s_minute_max": age_max, "age_s_minute_median": age_median},
            "failure_queues": {"dlq": 0.0}}


def healthy_run(api, p50, p99, product_p95):
    return {"minutes": 9, "usage": {"api": api},
            "ops": {"api": {"latency_p50": p50, "latency_p95": 2 * p50, "latency_p99": p99, "latency_maximum": 3 * p99},
                    "functions": {FN + "product": {"duration_p95": product_p95}, FN + "dashboard": {"duration_p95": None}}}}


def test_operations_by_hand():
    memory = {FN + "product": 1769, FN + "dashboard": 256}
    ops = {"a": ops_batch(5, 50, 90, 100, None, 0, 3, 600, 10, 9, 1, 12, 4),
           "b": ops_batch(7, 70, 80, 120, 2, None, 5, 1200, 5, 6, 0, 20, 6)}
    batches = {"a": {"usage": {"api": 6000}, "minutes": 10}, "b": {"usage": {"api": 12000}, "minutes": 20}}
    runs = [healthy_run(10_800, 15, 30, 7), healthy_run(9_720, 16, 40, 8), healthy_run(11_340, 18, 35, 9)]
    o = m.operations(fake(memory=memory, ops=ops, batches=batches, runs=runs))
    # 18,000 requests in 30 minutes = 10 a second; the busiest minute 1,200 = 20 a second; healthy runs at 20, 18
    # and 21 a second
    assert o["throughput"] == {"requests": 18000, "minutes": 30, "mean_rps": approx(10.0), "busiest_minute_rps": approx(20.0),
                               "healthy_rps_median": approx(20.0)}
    assert o["api_latency_healthy_ms"]["latency_p50"] == {"median": 16, "max": 18}
    assert o["api_latency_healthy_ms"]["latency_p99"] == {"median": 35, "max": 40}
    # the median batch p50, the worst batch p95 and p99, the highest maximum and concurrency; no data counts as 0
    # errors; the dashboard, never called, is left out
    assert o["functions"] == {"product": {"group": "app", "p50_median_of_batches": 6, "p95_worst_batch": 70,
                                          "p99_worst_batch": 90, "max": 120, "p95_healthy_median": 8, "errors": 2,
                                          "throttles": 0, "concurrency_max": 5}}
    assert o["queue"] == {"messages_sent": 15, "messages_deleted": 15, "most_visible": 1, "oldest_message_s_max": 20,
                          "oldest_message_s_minute_median": 5}
    assert o["failure_queues_most_visible"] == {"dlq": [0.0, 0.0]}


def test_sagemaker_options_by_hand():
    s = m.serverless_comparison({"handler_ms_per_call": 10.0, "cost": 1e-6, "warm_cost": 4e-7})
    data = (4662 + 1000) / 2 ** 30 * 0.016
    month = s["month"]
    assert month["SageMaker Serverless Inference, 1 GB"]["idle"] == 0
    assert month["SageMaker Serverless Inference, 1 GB"]["one a minute"] == approx(43_800 * (0.010 * 0.00002 + data))
    assert month["Serverless Inference + 1 provisioned concurrency"]["idle"] == approx(0.0000117 * 2_628_000)
    assert month["real-time endpoint, ml.t2.medium"]["idle"] == approx(0.056 * 730)
    assert month["real-time endpoint, ml.c6g.large"]["idle"] == approx(0.0816 * 730)
    # the Lambda: measured cost at one a minute, a warm call's at one a second
    assert month["Lambda (measured)"]["one a minute"] == approx(1e-6 * 43_800)
    assert month["Lambda (measured)"]["one a second"] == approx(4e-7 * 2_628_000)


# ---- the committed M9-1 inputs ----

def test_measured_usage_matches_the_bill_s_quantities_on_every_day():
    for d, row in RES["bill_check"]["days"].items():
        assert row["api_requests"][0] == pytest.approx(row["api_requests"][1], rel=1e-4, abs=1), d
        assert row["lambda_requests"][0] == pytest.approx(row["lambda_requests"][1], rel=1e-5), d
        assert row["log_gib"][0] == pytest.approx(row["log_gib"][1], rel=2e-3), d
        assert row["dynamodb_write_units"][0] == pytest.approx(row["dynamodb_write_units"][1], rel=1e-3), d
        # the bill rounds each call up to the next millisecond and bills cold starts' start-up time, which
        # CloudWatch's Duration leaves out: a little above CloudWatch, never below
        cw, bill = row["lambda_gb_s"]
        assert 1.0 <= bill / cw < 1.012, d
    assert 1.0 < RES["gbs_factor"] < 1.01


def test_the_bill_s_unit_prices_are_the_list_prices_used_here():
    for name, (bill, here) in RES["bill_check"]["prices"].items():
        assert bill == pytest.approx(here, rel=5e-3), name


def test_the_model_s_test_set_total_matches_the_bill_priced_at_list():
    c = RES["bill_cross_check"]
    assert c["model_over_bill"] == pytest.approx(1.0, abs=0.02)


def test_the_pipeline_s_own_writes_explain_most_s3_puts():
    assert 0.9 < RES["bill_check"]["s3_puts_explained"] <= 1.0


def test_one_diagnosis_call_per_scored_minute_and_one_window_build_per_minute():
    for bid, b in REAL.batches.items():
        f = b["usage"]["functions"]
        assert REAL.reports["batches"][bid]["calls"] == f[FN + "diagnosis"]["invocations"], bid
        assert f[FN + "pipeline-window-builder"]["invocations"] == b["minutes"], bid
        # nothing is scored in the first couple of minutes or in the tail (6 short, 7 in the stress batch)
        assert 0 < b["minutes"] - f[FN + "diagnosis"]["invocations"] <= 10, bid


def test_each_bedrock_call_is_one_explanation_call():
    for bid, i in REAL.incidents.items():
        assert i["bedrock_calls"] == REAL.batches[bid]["usage"]["functions"][FN + "explain"]["invocations"], bid


def test_nothing_is_scored_when_idle():
    assert RES["per_inference"]["idle_calls"] == 0
    assert set(RES["idle"]["functions_running_per_day"]) == {"pipeline-window-builder", "metrics-extractor"}
    assert RES["idle"]["billed_day"] == pytest.approx(0.84, abs=0.01)       # the bill's idle floor
    # at list price, what the bill left free: the metrics extractor's 38,880 CloudWatch requests a day ($0.01 a
    # thousand), the Products table's 5 write units (120 unit-hours at $0.00065) and 7 alarms ($0.10 a month);
    # storage adds under a cent
    assert RES["idle"]["day"] - RES["idle"]["billed_day"] == pytest.approx(0.3888 + 0.078 + 0.7 / 30, abs=0.01)


def test_the_products_table_s_fixed_capacity_is_priced_at_its_own_rates():
    p = {r["rps"]: r for r in RES["projections"]}
    # 200 read and 5 write units for 730 hours, at $0.00013 and $0.00065 a unit-hour, whatever the rate
    for rps in (1, 100):
        assert p[rps]["lines"]["DynamoDB products-v2 provisioned reads"] == pytest.approx(18.98, abs=1e-4)
        assert p[rps]["lines"]["DynamoDB products-v2 provisioned writes"] == pytest.approx(2.3725, abs=1e-4)


def test_an_incident_with_runbooks_scales_only_its_bedrock_share():
    i, rb = RES["per_incident"], RES["bedrock_per_call_runbooks_on"]
    assert i["cost_runbooks_on"] == approx(i["bedrock"] * rb["over_runbooks_off"] + i["explain_lambda"] + i["store_waiting"])


def test_the_healthy_runs_are_the_60_normal_test_runs_at_20_rps():
    runs = REAL.runs
    assert len(runs) == 60 and len({r["experiment_id"] for r in runs}) == 60
    assert all(r["minutes"] == 9 for r in runs)
    rps = [r["usage"]["api"] / (r["minutes"] * 60) for r in runs]
    assert all(x == pytest.approx(20, rel=0.05) for x in rps)            # 19.6 to 20.55 in nine minutes
    assert sorted(rps)[30] == pytest.approx(20, rel=0.01)


def test_the_test_set_is_371_runs_and_317_incidents():
    e = RES["experiments"]["test_set"]
    assert (e["attempts"], e["accepted"]) == (371, 370)
    assert RES["per_incident"]["incidents"] == 317
    assert e["total"] == pytest.approx(sum(b["total"] for b in RES["experiments"]["batches"].values()))


def test_projections_are_a_straight_line_in_the_request_rate():
    p = {r["rps"]: r for r in RES["projections"]}
    slope_low = (p[20]["total"] - p[1]["total"]) / 19
    slope_high = (p[100]["total"] - p[20]["total"]) / 80
    assert slope_low == approx(slope_high, rel=1e-9)
    assert all(p[r]["app"] > 0 and 0 < p[r]["anomalypulse_share"] < 1 for r in p)
    # API Gateway alone is $3.50 a million requests: at 100 rps, 262.8 million -> $919.80
    assert p[100]["lines"]["API Gateway requests"] == pytest.approx(919.80, abs=0.01)


def test_the_runbooks_on_call_cost_is_m9_5_s_billed_day():
    rb = RES["bedrock_per_call_runbooks_on"]
    # M9-5 reported $1.44 for its 38 calls from their recorded tokens; the bill says the same
    assert rb["billed"] == pytest.approx(1.44, abs=0.005)
    assert rb["cost"] == pytest.approx(rb["billed"] / 38, rel=1e-3)
    assert rb["input_tokens"] > RES["bedrock_per_call"]["input_tokens"]      # the runbook text is input


def test_the_store_s_wait_on_bedrock_is_the_explanation_lambda_s_time():
    lines = {x["line"]: x for x in RES["unit_usage"]}
    wait = lines["Lambda incidents GB-s, waiting on the explanation"]["per"]["incident"]
    own = lines["Lambda explain GB-s"]["per"]["incident"]
    assert wait == approx(own)                     # same memory, same seconds


def test_the_store_s_time_is_split_between_minutes_and_incidents_without_counting_any_twice():
    lines = {x["line"]: x for x in RES["unit_usage"]}
    minutes = sum(b["minutes"] for b in REAL.batches.values())
    incidents = sum(i["incidents"] for i in REAL.incidents.values())
    split = (lines["Lambda incidents GB-s"]["per"]["minute"] * minutes
             + lines["Lambda incidents GB-s, waiting on the explanation"]["per"]["incident"] * incidents)
    measured = sum(b["usage"]["functions"][FN + "incidents"]["gb_s"] for b in REAL.batches.values())
    assert split == approx(measured * RES["gbs_factor"])
