"""
Run this ONCE to create the DynamoDB tables.
python setup_tables.py
"""

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.client('dynamodb', region_name='ap-south-1')


def create_table(name: str, pk: str, sk: str = None):
    key_schema      = [{'AttributeName': pk, 'KeyType': 'HASH'}]
    attr_definitions = [{'AttributeName': pk, 'AttributeType': 'S'}]

    if sk:
        key_schema.append({'AttributeName': sk, 'KeyType': 'RANGE'})
        attr_definitions.append({'AttributeName': sk, 'AttributeType': 'S'})

    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_definitions,
            BillingMode='PAY_PER_REQUEST',   # on-demand — free tier friendly
        )
        print(f"✅ Created table: {name}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"⚠️  Table already exists: {name}")
        else:
            raise


if __name__ == '__main__':
    # Master cards table — one item per card (latest state)
    create_table('cards_master', pk='cardId')

    # Version history — every change logged here
    # PK: cardId, SK: version (e.g. "v0014")
    create_table('cards_versions', pk='cardId', sk='version')

    # Scraper run log — track each daily run
    create_table('scraper_runs', pk='runId')

    # Change events — devaluations, new cards, discontinued
    create_table('card_change_events', pk='cardId', sk='changedAt')

    print("\n✅ All tables ready.")
