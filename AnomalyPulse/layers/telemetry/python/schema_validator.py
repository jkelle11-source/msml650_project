import json
from pathlib import Path
from jsonschema import Draft202012Validator


path = Path(__file__).parent/"telemetry_schema.json"

schema = json.loads(path.read_text())

Draft202012Validator.check_schema(schema)

validator = Draft202012Validator(schema)

def validate(record):
    errors = [error for error in validator.iter_errors(record)]
    if len(errors) == 0:
        return (True, errors)
    else:
        return (False, errors)
    
def validate_or_raise(record):
    valid, errors = validate(record)
    if not valid:
        raise errors[0]