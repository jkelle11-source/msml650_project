import json

def handler(event, context):
    # Stub - proves the API -> Lambda wiring. 
    # Linu: Fill with real DynamoDB reads/writes.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": {"service": "cart", "route": event.get("resource")},
                            "error": None})
    }