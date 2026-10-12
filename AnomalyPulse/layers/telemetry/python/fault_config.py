import json
import os
import time
import boto3
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from botocore.config import Config

_TTL_SECONDS = float(os.environ.get("FAULT_CONFIG_TTL_SECONDS", "5"))
_dynamodb = boto3.resource("dynamodb", config = Config(connect_timeout = 1,
                                                       read_timeout = 1,
                                                       retries = {"total_max_attempts": 1}))
_cache = {}

def _plain(value):
    """
    Recursively turn DynamoDB's Decimal numbers into int or float.
    
    Walk dicts and lists; a Decimal with no fractional part becomes int and 
    any other Decimal becomes float; everything else passes through.
    """
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        else: 
            return float(value)
    if isinstance(value, dict):
        return {k:_plain(v) for k,v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
        

def _fetch(service: str) -> list[dict]:
    """
    The only function that talks to DynamoDB. Allowed to raise.
    
    1. Read FAULT_CONFIG_TABLE from the environment here, not at import.
       Unset -> return [].
    2. Query the `service` partition.
    3. Return the items through _plain(). Do NOT filter by time here.
    """
    FAULT_CONFIG_TABLE = os.environ.get("FAULT_CONFIG_TABLE", [])
    
    if not FAULT_CONFIG_TABLE: 
        return []
    
    response = _dynamodb.Table(FAULT_CONFIG_TABLE).query(
        KeyConditionExpression = Key("service").eq(service)
    )
    
    return [_plain(item) for item in response['Items']]

def _items(service: str) -> list[dict]:
    """
    Cached items for `service`, refreshed when older than _TTL_SECONDS.
    
    1. Look up (fetched_at, items) in _cache; a missing entry counts as stale.
    2. If stale: try _fetch(). On any exception keep the old items and print
    one JSON line, e.g., {"fault_config_error": <exception class name>}.
    3. Whether the fetch worked or not, store (now, items) back in _cache.
    4. Return items.
    """
    fetched_at, items = _cache.get(service, (None, []))
    if (fetched_at is None) or (time.monotonic() - fetched_at > _TTL_SECONDS):
        try:
            items = _fetch(service)
        except Exception as e: 
            print(json.dumps({"fault_config_error": type(e).__name__}))
        _cache[service] = (time.monotonic(), items)
    return items
        
    

def active_faults(service:str, now_ms: int | None = None) -> list[dict]:
    """
    Fault activations in effect for `service` at `now_ms` (default: now).
    
    1. Default now_ms to the current epoch milliseconds.
    2. Return the items from _items() whose window covers now_ms.
    3. Wrap the whole body so anything unexpected returns [].
    """
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
            
        items = _items(service)
        
        now_items = [item for item in items 
                     if item["active_from_ms"] 
                     <= now_ms 
                     < item["active_until_ms"]]
        
        return now_items
    except Exception:
        return []