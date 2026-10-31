from datetime import datetime, timedelta, timezone

SERVICES = ("product-service", "cart-service", "order-service", "payment-service")
EXTERNAL = ("product-service", "cart-service", "order-service")
STALE_AFTER = timedelta(minutes = 3)
MIN_OVERLAP = timedelta(seconds = 30)


def service_rows(window: dict | None) -> list[dict]:
    """[{"service", "requests_per_min", "error_rate", "p95_latency_ms"}, ...] in SERVICES order."""
    rows = []
    
    for service in SERVICES:
        prefix = service.split("-")[0]
        
        if window is None:
            rows.append({
                "service": service,
                "requests_per_min": None,
                "error_rate": None,
                "p95_latency_ms": None
            })
            
        else:
            rows.append({
                "service": service,
                "requests_per_min": round(window[f"{prefix}_request_rate"] * 60),
                "error_rate": window[f"{prefix}_error_rate_5xx"],
                "p95_latency_ms": window[f"{prefix}_p95_latency_ms"]
            })
            
    return rows


def system_kpis(window: dict | None) -> dict:
    """{"requests_per_min", "error_rate", "p95_latency_ms"} for the whole system."""
    rows = service_rows(window)
    
    if window is None:
        return {
            "requests_per_min": None,
            "error_rate": None,
            "p95_latency_ms": None
        }

    else:
        total_requests = sum(row["requests_per_min"] for row in rows
                             if row["service"] in EXTERNAL)
        
        errors = sum(row["requests_per_min"] * row["error_rate"] for row in rows
                         if row["service"] in EXTERNAL
                         and row["error_rate"] is not None)
        
        error_rate = errors / total_requests if total_requests else None
        p95_latency_ms = window["system_p95_latency_ms"]
        
        return {
            "requests_per_min": total_requests,
            "error_rate": error_rate,
            "p95_latency_ms": p95_latency_ms
        }

def health_status(window: dict | None, incident: dict | None, now: datetime) -> str:
    """"NO_DATA", "INCIDENT", or "HEALTHY" for the whole system."""
    if window is None:
        return "NO_DATA"
    
    start = datetime.fromisoformat(window["window_start"])
    if now > start + STALE_AFTER:
        return "NO_DATA"
    
    elif incident is not None:
        return "INCIDENT"
    
    else:
        return "HEALTHY"

def service_status(row: dict, health: str, incident: dict | None) -> str:
    """"NO_DATA", "DEGRADED", "IDLE", or "HEALTHY" for one service."""
    if health == "NO_DATA": 
        return "NO_DATA"
    
    if incident is not None and row["service"] in incident["prediction"]["affected_services"]:
        return "DEGRADED"
    
    if row["requests_per_min"] == 0:
        return "IDLE"
    
    else:
        return "HEALTHY"

def fault_rows(manifest: dict, window_start: str | None) -> list[dict]:
    """[{"injector", "target", "active_from", "active_until", "active_now"}, ...] 
    in the manifest's order."""
    faults = []
    
    if window_start is not None:
        start = datetime.fromisoformat(window_start)
        end = start + timedelta(seconds = 60)

    for activation in manifest["activations"]:
        
        if window_start is None:
            active_now = None
        
        else: 
            active_from = datetime.fromisoformat(activation["active_from"])
            active_until = datetime.fromisoformat(activation["active_until"])
            overlap = min(end, active_until) - max(start, active_from)
            active_now = overlap >= MIN_OVERLAP
        
        faults.append({
            "injector": activation["injector"],
            "target": activation["target"],
            "active_from": activation["active_from"],
            "active_until": activation["active_until"],
            "active_now": active_now
        })
        
    return faults

def detection_latency_s(detected_at: str, manifest: dict) -> float | None:
    """Seconds from the earliest fault's active_from to detected_at; 
    None if detected before any fault."""
    if not manifest["activations"]:
        return None
    
    d_at = datetime.fromisoformat(detected_at)
    earliest = min(datetime.fromisoformat(activation["active_from"]) for activation in manifest["activations"])
    
    if d_at < earliest:
        return None
    
    else:
        return (d_at - earliest).total_seconds()

def build_dashboard(window: dict | None, incident: dict | None, manifest: dict | None, now: datetime) -> dict:
    """The contract's object (Docs/DASHBOARD_CONTRACT.md, contract_version 1)."""
    status = health_status(window, incident, now)
    shown = None if status == "NO_DATA" else window
    
    health_report = {
        "window_start": shown["window_start"] if shown is not None else None,
        "status": status,
        **system_kpis(shown),
        "active_incidents": 0 if incident is None else 1,
        "services": [d | {"status": service_status(d, status, incident)} for d in service_rows(shown)]
    }
    
    incident_report = None if incident is None else {
        "incident_id": incident["incident_id"],
        "prediction": incident["prediction"],
        "diagnosis": incident["diagnosis"],
        "ground_truth": None if manifest is None else {
            "experiment_id": manifest["experiment_id"],
            "incident_type": manifest["incident_type"],
            "severity": manifest["severity"],
            "faults": fault_rows(manifest, health_report["window_start"])
        },
        "comparison": None if manifest is None else {
            "detection_latency_s": detection_latency_s(incident["prediction"]["detected_at"], manifest)
        }
    }
    
    return {
        "contract_version": 1,
        "generated_at": iso(now),
        "health": health_report,
        "incident": incident_report
    }

def iso(moment: datetime) -> str:
    """A UTC datetime as 2026-10-30T14:34:05.000Z."""
    # A time with no zone would be written without the Z, and nothing downstream would notice.
    if moment.tzinfo is None:
        raise ValueError(f"{moment} has no time zone; pass an aware datetime, e.g. datetime.now(timezone.utc)")
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    
    