import pytest

from simulator import stack_config

TABLE = "anomalypulse-products-v2"


class FakeLambda:
    def __init__(self, memory=1769):
        self.memory = memory

    def get_function_configuration(self, FunctionName):
        return {"FunctionName": FunctionName, "MemorySize": self.memory, "Timeout": 10,
                "Runtime": "python3.13", "Architectures": ["arm64"], "CodeSha256": f"sha-{FunctionName}",
                "Layers": [{"Arn": "arn:layer:anomalypulse-telemetry:7", "CodeSize": 1}],
                "RuntimeVersionConfig": {"RuntimeVersionArn": "arn:runtime:python3.13::v1"}}


class FakeDynamoDB:
    def __init__(self, rcu=200, scan_pages=(0.5,), billing=None):
        self.rcu, self.scan_pages, self.billing = rcu, list(scan_pages), billing
        self.scans = []

    def describe_table(self, TableName):
        table = {"TableName": TableName, "ItemCount": 25,
                 "ProvisionedThroughput": {"ReadCapacityUnits": self.rcu, "WriteCapacityUnits": 5}}
        if self.billing:
            table["BillingModeSummary"] = {"BillingMode": self.billing}
        return {"Table": table}

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        page = {"Items": [], "ConsumedCapacity": {"TableName": TABLE,
                                                  "CapacityUnits": self.scan_pages[len(self.scans) - 1]}}
        if len(self.scans) < len(self.scan_pages):
            page["LastEvaluatedKey"] = {"id": {"S": "prod-010"}}
        return page


class FakeSSM:
    def get_parameter(self, Name):
        assert Name == "/anomalypulse/tables/products/name"
        return {"Parameter": {"Value": TABLE}}


class FakeSession:
    def __init__(self, **clients):
        self.clients = {"lambda": FakeLambda(), "dynamodb": FakeDynamoDB(), "ssm": FakeSSM(), **clients}

    def client(self, name, **kwargs):
        return self.clients[name]


def test_the_frozen_stack_has_no_problems():
    config = stack_config.read(FakeSession())
    assert stack_config.problems(config) == []
    assert set(config["functions"]) == set(stack_config.FUNCTIONS)
    product = config["functions"]["product-service"]
    assert product["code_sha256"] == "sha-anomalypulse-product"
    assert product["layers"] == ["arn:layer:anomalypulse-telemetry:7"]
    assert product["runtime_version_arn"] == "arn:runtime:python3.13::v1"
    assert config["products_table"] == {"name": TABLE, "billing_mode": "PROVISIONED",
                                         "read_capacity_units": 200, "write_capacity_units": 5,
                                         "scan_rcu": 0.5}


def test_the_scan_is_read_the_way_product_reads_it():
    dynamodb = FakeDynamoDB()
    stack_config.read(FakeSession(dynamodb=dynamodb))
    assert dynamodb.scans == [{"TableName": TABLE, "ReturnConsumedCapacity": "TOTAL"}]


def test_a_paged_scan_costs_every_page():
    config = stack_config.read(FakeSession(dynamodb=FakeDynamoDB(scan_pages=(0.5, 0.5))))
    assert config["products_table"]["scan_rcu"] == 1.0


@pytest.mark.parametrize("clients, problem", [
    ({"lambda": FakeLambda(memory=128)}, "has 128 MB, not 1769"),
    ({"dynamodb": FakeDynamoDB(rcu=500)}, "at 500 RCU"),
    ({"dynamodb": FakeDynamoDB(rcu=0, billing="PAY_PER_REQUEST")}, "PAY_PER_REQUEST"),
    ({"dynamodb": FakeDynamoDB(scan_pages=(4.0,))}, "costs 4 RCU, not 0.5"),
])
def test_problems(clients, problem):
    found = stack_config.problems(stack_config.read(FakeSession(**clients)))
    assert any(problem in p for p in found), found


def test_the_extractors_memory_is_recorded_but_not_checked():
    config = stack_config.read(FakeSession())
    config["functions"]["metrics-extractor"]["memory_mb"] = 128
    assert stack_config.problems(config) == []


def test_changes_lists_every_difference_by_path():
    old = stack_config.read(FakeSession())
    new = stack_config.read(FakeSession())
    new["functions"]["cart-service"]["layers"] = ["arn:layer:anomalypulse-telemetry:8"]
    new["products_table"]["write_capacity_units"] = 10
    assert stack_config.changes(old, new) == [
        "functions.cart-service.layers: ['arn:layer:anomalypulse-telemetry:7'] -> "
        "['arn:layer:anomalypulse-telemetry:8']",
        "products_table.write_capacity_units: 5 -> 10",
    ]
    assert stack_config.changes(old, old) == []
