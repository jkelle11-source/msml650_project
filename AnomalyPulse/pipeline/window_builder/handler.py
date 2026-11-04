from features.build_matrix import build_matrix
from features.request_features import SERVICES, percentile
from features import windows

def _numbers(recs: list[dict]) -> dict:
    """{"requests_per_min", "error_rate", "p95_latency_ms"} of these records; the last two None if there are none."""
    requests_per_min = len(recs)
    
    error_rate = sum(1 for r in recs if 500 <= r["status_code"] <= 599 
                     ) / requests_per_min if requests_per_min else None
    
    p95_latency_ms = percentile([r["latency_ms"] for r in recs], 95)
    
    return {"requests_per_min": requests_per_min,
            "error_rate": error_rate,
            "p95_latency_ms": p95_latency_ms}

def dedupe(records: list[dict]) -> list[dict]:
    """The records without repeats: the first of each (service, request_id), in their order."""
    seen = set()
    no_dupes = []
    
    for r in records:
        
        if (r["service"], r["request_id"]) not in seen:
            seen.add((r["service"], r["request_id"]))
            no_dupes.append(r)
            
    return no_dupes

def health(records: list[dict], window_start: str) -> dict:
    """The dashboard's numbers for one minute, from its records."""
    no_dupes = dedupe(records)
    
    filtered = [r for r in no_dupes 
                if windows.request_window(r["timestamp"]) == window_start]
    
    system_dict = _numbers([r for r in filtered if r["service"] != "payment-service"])
    
    service_dict = [{"service": s, **_numbers([r for r in filtered if r["service"] == s])}
                    for s in SERVICES]
    
    return {"window_start": window_start, **system_dict, "services": service_dict}

def build_window(records: list[dict], snapshots: list[dict], window_start: str) -> dict | None:
    """{"window_start": W, <the 106 columns>} from the records and snapshots of W-2..W; None if W has neither."""
    no_dupes = dedupe(records)
    matrix = build_matrix(no_dupes, snapshots)
    return {"window_start": window_start, **matrix[window_start]} if window_start in matrix else None
    
def is_complete(window: dict) -> bool:
    """True if no column is empty: the only window the Diagnosis Lambda scores."""
    return all(v is not None for k, v in window.items() if k != "window_start")
    