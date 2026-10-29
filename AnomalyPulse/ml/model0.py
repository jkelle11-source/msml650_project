"""Model 0: the threshold baseline (M5-3). No training, no SageMaker, no package.

A window is flagged as an anomaly if ANY service trips either rule:
  latency: its p95 latency is more than RATIO x a baseline, or
  errors:  its 5xx error rate is above ERROR_LIMIT.
Two baselines for the latency rule, both built only from Tier-1 feature columns:
  "rolling_mean"    the ticket's literal rule: the service's rolling mean of p95.
                    That mean includes the current window, so it is a weak floor.
  "previous_window" the previous window's p95, recovered as p95 - change.
An empty value (None) skips that check. Scoring treats "anomaly" as any label but NORMAL.
"""
SERVICES = ("product", "cart", "order", "payment")
RATIO = 2.0
ERROR_LIMIT = 0.05
BASELINES = ("rolling_mean", "previous_window")
NORMAL = "NORMAL"


def _num(row, name):
    value = row.get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def flag(row, baseline="previous_window"):
    """True if any service trips the latency or error rule in this feature row."""
    if baseline not in BASELINES:
        raise ValueError(f"baseline must be one of {BASELINES}, got {baseline!r}")
    for svc in SERVICES:
        p95 = _num(row, f"{svc}_p95_latency_ms")
        if baseline == "rolling_mean":
            base = _num(row, f"{svc}_p95_latency_ms_roll_mean_3")
        else:
            change = _num(row, f"{svc}_p95_latency_ms_change")
            base = None if p95 is None or change is None else p95 - change
        if p95 is not None and base is not None and p95 > RATIO * base:
            return True
        errors = _num(row, f"{svc}_error_rate_5xx")
        if errors is not None and errors > ERROR_LIMIT:
            return True
    return False


def predict(rows, baseline="previous_window"):
    return [flag(row, baseline) for row in rows]


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