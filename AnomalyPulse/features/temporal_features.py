"""Rolling and rate-of-change features for ONE run's windows (M4-5).

Input is {window_start: {column: value}} for a single experiment. Each window
uses only itself and EARLIER windows, so no feature sees the future. Call it
once per run, so nothing carries over from the previous experiment.

Only the run's labeled windows are looked back over (`lookback`: M4-4's
labels.jsonl rows with included: true). An excluded window gets features from
itself and the earlier labeled windows, but never feeds a later window. So the
DB-throttling surge, which M4-4 excludes as onset, never reaches the first
labeled throttling window: the injector spends DynamoDB's burst capacity that
way, and real throttling has nothing like it. Nor do startup's cold starts reach
the baseline. Blanking the features that drew on an excluded window instead
would leave blanks in training only where DB throttling starts: a fingerprint
of its own (decided in M4-4's review, 2026-10-19).
"""
import statistics

from features.request_features import SERVICES, _prefix

WINDOW = 3

KEY_COLUMNS = [f"{_prefix(s)}_{suffix}" for s in SERVICES
               for suffix in ("p95_latency_ms", "request_rate", "error_rate_5xx")] + [
    "order_dependency_latency_ms_mean",
    "order_lambda_duration_ms_mean",
]


def temporal_features(rows, columns=KEY_COLUMNS, window=WINDOW, lookback=None):
    """Return {window_start: {column_roll_mean_N, column_roll_std_N, column_change}}.

    lookback: the windows a later window may draw on (the run's labeled windows).
    None looks back over every window, for a run with no labels yet.
    """
    out = {}
    history = {c: [] for c in columns}
    for w in sorted(rows):
        feeds_later = lookback is None or w in lookback
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
            if feeds_later:
                past.append(value)
        out[w] = feats
    return out