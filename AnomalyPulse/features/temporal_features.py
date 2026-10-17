"""Rolling and rate-of-change features for ONE run's windows (M4-5).

Input is {window_start: {column: value}} for a single experiment. Each window
uses only itself and EARLIER windows, so no feature sees the future. Call it
once per run, so nothing carries over from the previous experiment.
"""
import statistics

from request_features import SERVICES, _prefix

WINDOW = 3

KEY_COLUMNS = [f"{_prefix(s)}_{suffix}" for s in SERVICES
               for suffix in ("p95_latency_ms", "request_rate", "error_rate_5xx")] + [
    "order_dependency_latency_ms_mean",
    "order_lambda_duration_ms_mean",
]


def temporal_features(rows, columns=KEY_COLUMNS, window=WINDOW):
    """Return {window_start: {column_roll_mean_N, column_roll_std_N, column_change}}."""
    out = {}
    history = {c: [] for c in columns}
    for w in sorted(rows):
        feats = {}
        for c in columns:
            value = rows[w].get(c)
            past = history[c]
            recent = [v for v in past[-(window - 1):] + [value] if v is not None]
            enough = len(recent) >= 2
            feats[f"{c}_roll_mean_{window}"] = statistics.fmean(recent) if enough else None
            feats[f"{c}_roll_std_{window}"] = statistics.pstdev(recent) if enough else None
            prev = past[-1] if past else None
            feats[f"{c}_change"] = value - prev if value is not None and prev is not None else None
            past.append(value)
        out[w] = feats
    return out