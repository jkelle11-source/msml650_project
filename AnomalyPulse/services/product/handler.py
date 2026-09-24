import json

def handler(event, context):
    # Stub - proves the API -> Lambda wiring. 
    # Josh: Fill with real DynamoDB reads.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": {"service": "product", "route": event.get("resource")}
                            "error": None})
    }