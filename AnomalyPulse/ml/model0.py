"""Model 0: the threshold baseline (M5-3). No training, no SageMaker, no package.

A window is flagged as an anomaly if ANY service trips either rule:
  latency: its p95 latency is more than RATIO x a baseline, or
  errors:  its 5xx error rate is above ERROR_LIMIT.
Two baselines for the latency rule. Neither includes the current window: a mean that did
would turn "2 x the mean" into "4 x the mean of the other two".
  previous window   flag(row): the previous lookback window's p95, read from the row
                    itself as p95 - change.
  earlier mean      flag_vs_earlier(row, earlier_rows): the mean p95 of the last EARLIER
                    lookback windows, which the caller passes. The stronger floor; M5-3's
                    margin is reported against it.
A run's lookback windows are its labeled windows plus its startup minute, as the feature
matrix builds them, so an excluded window (such as DB-throttling onset) is never a baseline.
An empty value (None) skips that check. Scoring treats "anomaly" as any label but NORMAL.
"""
SERVICES = ("product", "cart", "order", "payment")
RATIO = 2.0
ERROR_LIMIT = 0.05
EARLIER = 3
NORMAL = "NORMAL"


def _num(row, name):
    value = row.get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _trips(row, svc, base):
    """True if the service's p95 is above RATIO x base, or its 5xx rate above ERROR_LIMIT."""
    p95 = _num(row, f"{svc}_p95_latency_ms")
    if p95 is not None and base is not None and p95 > RATIO * base:
        return True
    errors = _num(row, f"{svc}_error_rate_5xx")
    return errors is not None and errors > ERROR_LIMIT


def flag(row):
    """True if any service trips a rule, against the previous lookback window's p95."""
    for svc in SERVICES:
        p95 = _num(row, f"{svc}_p95_latency_ms")
        change = _num(row, f"{svc}_p95_latency_ms_change")
        if _trips(row, svc, None if p95 is None or change is None else p95 - change):
            return True
    return False


def flag_vs_earlier(row, earlier_rows, n=EARLIER):
    """True if any service trips a rule, against the mean of its p95 over the last n rows of
    `earlier_rows`: the run's earlier lookback windows, in time order. Empty values are left
    out of the mean; with none, only the error rule can fire."""
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    earlier = earlier_rows[-n:]
    for svc in SERVICES:
        values = [v for v in (_num(r, f"{svc}_p95_latency_ms") for r in earlier) if v is not None]
        if _trips(row, svc, sum(values) / len(values) if values else None):
            return True
    return False


def predict(rows):
    """flag() for each row: the previous-window baseline."""
    return [flag(row) for row in rows]


def score(labels, flagged):
    """Anomaly = any label but NORMAL. Returns precision, recall, f1, fpr, accuracy and
    the share of each anomaly class that was flagged (the cascade margin lives there)."""
    if len(labels) != len(flagged):
        raise ValueError("labels and flags differ in length")
    truth = [label != NORMAL for label in labels]
    tp = sum(t and f for t, f in zip(truth, flagged))
    fp = sum(f and not t for t, f in zip(truth, flagged))
    fn = sum(t and not f for t, f in zip(truth, flagged))
    tn = len(labels) - tp - fp - fn
    ratio = lambda a, b: a / b if b else 0.0
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    classes = sorted({l for l in labels if l != NORMAL})
    return {
        "precision": precision, "recall": recall,
        "f1": ratio(2 * precision * recall, precision + recall),
        "fpr": ratio(fp, fp + tn), "accuracy": ratio(tp + tn, len(labels)),
        "recall_by_class": {c: ratio(sum(f for l, f in zip(labels, flagged) if l == c),
                                     sum(1 for l in labels if l == c)) for c in classes},
    }
