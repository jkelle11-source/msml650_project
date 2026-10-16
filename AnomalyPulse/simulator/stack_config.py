"""The stack's frozen configuration (M4-1): checked before every batch, recorded with it.

M4 keeps the stack as it was at PR #70 for the whole dataset
(Milestones/M04_Dataset.md, "Generation settings"). A batch refuses to start
unless:

    every service function has 1,769 MB
    the Products table is provisioned at 200 RCU
    one Scan of Products costs 0.5 RCU (a partition change would raise it)

read() also returns what a deploy would change: each function's code hash,
layers, runtime version and timeout, and the table's capacity. The batch
runner records it with each batch and pins it at a dataset's first batch, so
a deploy in the middle of generation is caught (registry.py, batch.py).
"""
from simulator.injectors.db_throttling import SSM_TABLE_PARAM

# The four services, plus the metrics extractor: its CloudWatch snapshots are
# Tier-1 features too, so a change to it changes the dataset. Only the
# services' memory is checked; every function's settings are recorded.
FUNCTIONS = {
    "product-service": "anomalypulse-product",
    "cart-service": "anomalypulse-cart",
    "order-service": "anomalypulse-order",
    "payment-service": "anomalypulse-payment",
    "metrics-extractor": "anomalypulse-metrics-extractor",
}
SERVICE_FUNCTIONS = ("product-service", "cart-service", "order-service", "payment-service")

MEMORY_MB = 1769
PRODUCTS_RCU = 200
SCAN_RCU = 0.5


def _function(lambda_client, name: str) -> dict:
    c = lambda_client.get_function_configuration(FunctionName=name)
    return {
        "function_name": name,
        "memory_mb": c["MemorySize"],
        "timeout_s": c["Timeout"],
        "runtime": c.get("Runtime"),
        "architectures": c.get("Architectures", []),
        "code_sha256": c["CodeSha256"],
        "layers": sorted(layer["Arn"] for layer in c.get("Layers", [])),
        "runtime_version_arn": (c.get("RuntimeVersionConfig") or {}).get("RuntimeVersionArn"),
    }


def _scan_rcu(dynamodb, table: str) -> float:
    """What one full Scan costs, read the way Product reads it (eventually consistent)."""
    total, kwargs = 0.0, {"TableName": table, "ReturnConsumedCapacity": "TOTAL"}
    while True:
        page = dynamodb.scan(**kwargs)
        total += page["ConsumedCapacity"]["CapacityUnits"]
        if "LastEvaluatedKey" not in page:
            return total
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def read(session) -> dict:
    """The stack's configuration now. Plain JSON, and the same every time unless someone deploys.

    It costs one Scan of Products (0.5 RCU), so call it while no experiment is
    running.
    """
    lambda_client = session.client("lambda")
    functions = {key: _function(lambda_client, name) for key, name in FUNCTIONS.items()}

    table = session.client("ssm").get_parameter(Name=SSM_TABLE_PARAM)["Parameter"]["Value"]
    dynamodb = session.client("dynamodb")
    description = dynamodb.describe_table(TableName=table)["Table"]
    throughput = description.get("ProvisionedThroughput", {})
    return {
        "functions": functions,
        "products_table": {
            "name": table,
            # A table created provisioned has no BillingModeSummary.
            "billing_mode": description.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
            "read_capacity_units": throughput.get("ReadCapacityUnits"),
            "write_capacity_units": throughput.get("WriteCapacityUnits"),
            "scan_rcu": _scan_rcu(dynamodb, table),
        },
    }


def problems(config: dict) -> list[str]:
    """Why `config` is not M4's frozen configuration. Empty when it is."""
    found = []
    for key in SERVICE_FUNCTIONS:
        function = config["functions"][key]
        if function["memory_mb"] != MEMORY_MB:
            found.append(f"{function['function_name']} has {function['memory_mb']} MB, not {MEMORY_MB}")
    table = config["products_table"]
    if table["billing_mode"] != "PROVISIONED" or table["read_capacity_units"] != PRODUCTS_RCU:
        found.append(f"{table['name']} is {table['billing_mode']} at {table['read_capacity_units']} RCU, "
                     f"not PROVISIONED at {PRODUCTS_RCU}")
    if table["scan_rcu"] != SCAN_RCU:
        found.append(f"a Scan of {table['name']} costs {table['scan_rcu']:g} RCU, not {SCAN_RCU:g}: "
                     f"its partitions have changed, and so has every GET /products' read cost")
    return found


def changes(pinned: dict, current: dict) -> list[str]:
    """Every setting that differs between two configurations, as 'path: old -> new'."""
    found = []

    def walk(old, new, path):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                walk(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        elif old != new:
            found.append(f"{path}: {old!r} -> {new!r}")

    walk(pinned, current, "")
    return found
