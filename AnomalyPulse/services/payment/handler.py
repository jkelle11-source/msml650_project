import json

def handler(event, context):
    # Stub - always approves. 
    # Rachel: Fill with real fault-injection knobs (latency, failure, timeout).
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": {"service": "payment", "outcome": "success"},
                            "error": None})
    }