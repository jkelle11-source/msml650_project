"""Seed the Products table with dummy products.

Idempotent: every product has a fixed id, so re-running overwrites the same
items instead of adding duplicates.

Usage (needs AWS credentials with write access to the Products table):
    pip install boto3
    python seed.py --profile <your-profile>
    python seed.py --profile <your-profile> --table anomalypulse-products   # skip the SSM lookup
"""
import argparse
from decimal import Decimal

import boto3

SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"

# (name, category, price, stock)
_CATALOG = [
    ("Wireless Mouse", "electronics", "24.99", 150),
    ("Mechanical Keyboard", "electronics", "89.00", 75),
    ("USB-C Hub", "electronics", "39.50", 120),
    ("27-inch Monitor", "electronics", "229.99", 30),
    ("Noise-Cancelling Headphones", "electronics", "179.00", 45),
    ("Webcam 1080p", "electronics", "49.99", 90),
    ("Portable SSD 1TB", "electronics", "109.95", 60),
    ("Stainless Water Bottle", "home", "18.00", 200),
    ("Ceramic Coffee Mug", "home", "12.50", 250),
    ("French Press", "home", "32.00", 80),
    ("Desk Lamp", "home", "44.99", 70),
    ("Throw Blanket", "home", "29.99", 110),
    ("Scented Candle", "home", "15.00", 180),
    ("Running Shoes", "apparel", "119.99", 55),
    ("Cotton T-Shirt", "apparel", "19.99", 300),
    ("Hooded Sweatshirt", "apparel", "49.00", 140),
    ("Baseball Cap", "apparel", "22.00", 160),
    ("Wool Socks (3-pack)", "apparel", "16.99", 220),
    ("Yoga Mat", "sports", "34.99", 95),
    ("Adjustable Dumbbells", "sports", "249.00", 20),
    ("Resistance Bands Set", "sports", "27.50", 130),
    ("Paperback Notebook", "office", "8.99", 400),
    ("Gel Pens (12-pack)", "office", "11.49", 350),
    ("Backpack", "office", "59.99", 85),
    ("Desk Organizer", "office", "21.00", 100),
]

PRODUCTS = [
    {
        "id": f"prod-{i:03d}",
        "name": name,
        "category": category,
        "price": Decimal(price),  # DynamoDB rejects floats
        "stock": stock,
    }
    for i, (name, category, price, stock) in enumerate(_CATALOG, start=1)
]


def main():
    parser = argparse.ArgumentParser(description="Seed the AnomalyPulse Products table.")
    parser.add_argument("--profile", help="AWS CLI profile (default: standard credential chain)")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--table", help=f"table name (default: read from SSM {SSM_TABLE_PARAM})")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table_name = args.table or session.client("ssm").get_parameter(
        Name=SSM_TABLE_PARAM
    )["Parameter"]["Value"]

    table = session.resource("dynamodb").Table(table_name)
    with table.batch_writer() as batch:
        for product in PRODUCTS:
            batch.put_item(Item=product)

    print(f"Seeded {len(PRODUCTS)} products into {table_name}")


if __name__ == "__main__":
    main()
