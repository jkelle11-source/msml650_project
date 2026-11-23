from collections import defaultdict
from cost.collect import registry_attempts, metric_series, parse
from datetime import datetime, timedelta

TAIL = timedelta(minutes = 5)

def floor_minute(t):
    return t.replace(second = 0, microsecond = 0)

def ceil_minute(t):
    floor = floor_minute(t)
    return floor if floor == t else floor + timedelta(minutes = 1)

def batch_windows(attempts):
    """{batch_id: {"start", "end", "attempts", "accepted"}} for v1's test and stress attempts."""
    grouped = defaultdict(list)
    
    for attempt in attempts:
        if not (attempt["dataset"] == "v1" and attempt["phase"] in ["test", "stress"]):
            continue
        grouped[attempt["batch_id"]].append(attempt)
    
    windows = {}
    
    for bid, atts in grouped.items():
        
        windows[bid] = {
            "start": floor_minute(min(parse(a["started_at"]) for a in atts)),
            "end": ceil_minute(max(parse(a["ended_at"]) for a in atts)) + TAIL,
            "attempts": len(atts),
            "accepted": sum(a["verdict"] == "accepted" for a in atts)
        }

    return windows

def batch_usage(cw, memory, start, end):
    """Usage in [start, end] from 1-minute sums:
    {"api": requests, "functions": {name: {"invocations", "gb_s", "log_bytes"}}}"""
    
