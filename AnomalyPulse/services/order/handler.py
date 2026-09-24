import json

def handler(event, context):
    # Stub - proves the API -> Lambda wiring. 
    # Melisa: Fill with real DynamoDB reads + writes + Payment invoke.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": {"service": "order", "route": event.get("resource")},
                            "error": None})
    }