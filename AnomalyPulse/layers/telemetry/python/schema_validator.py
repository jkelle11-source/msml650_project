from jsonschema import Draft202012Validator

_SCHEMA_PATH: String = "infra/shared/telemetry_schema.json"

_SCHEMA: String = json.loads(_SCHEMA_PATH)

validator: Callable = Draft202012Validator(schema)

def validate(record: object) -> bool:
    return validator.is_valid(record)

def validate_or_raise(record: object, strict: bool = False):
    valid = validate(record)
    if strict and not valid:
        return validator.iter_errors(record)
    return valid