"""Test setup for the Product service.

fault_config creates a boto3 client when imported, which needs a region. On AWS
the region is set for us; locally we set it before the tests import the handler.
"""
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")