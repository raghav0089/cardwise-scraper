"""DynamoDB + S3 persistence."""
from __future__ import annotations
import os, json, gzip, logging, datetime as dt
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger(__name__)
REGION = os.getenv("AWS_REGION", "ap-south-1")
dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3       = boto3.client("s3", region_name=REGION)

CARDS    = dynamodb.Table(os.getenv("DDB_CARDS_TABLE",   "cards_master"))
SOURCES  = dynamodb.Table(os.getenv("DDB_SOURCES_TABLE", "cards_sources"))
CHANGES  = dynamodb.Table(os.getenv("DDB_CHANGES_TABLE", "cards_changes"))
BUCKET   = os.getenv("S3_BUCKET")


def _to_ddb(obj):
    if isinstance(obj, float): return Decimal(str(obj))
    if isinstance(obj, list):  return [_to_ddb(x) for x in obj]
    if isinstance(obj, dict):  return {k: _to_ddb(v) for k, v in obj.items() if v is not None}
    return obj


def archive_raw(url: str, html: str) -> str | None:
    if not BUCKET or not html: return None
    today = dt.date.today().isoformat()
    key = f"raw/{today}/{abs(hash(url))}.html.gz"
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=gzip.compress(html.encode("utf-8", errors="ignore")),
                  ContentType="text/html", ContentEncoding="gzip",
                  Metadata={"source_url": url[:1024]})
    return f"s3://{BUCKET}/{key}"


def get_existing(card_id: str) -> dict | None:
    r = CARDS.get_item(Key={"card_id": card_id})
    return r.get("Item")


def upsert_card(card: dict) -> None:
    CARDS.put_item(Item=_to_ddb(card))


def record_change(change: dict) -> None:
    CHANGES.put_item(Item=_to_ddb(change))


def mark_source(url: str, *, sha: str, etag: str | None) -> None:
    SOURCES.put_item(Item=_to_ddb({
        "url": url,
        "last_seen_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "raw_text_sha256": sha,
        "etag": etag,
    }))


def source_unchanged(url: str, sha: str) -> bool:
    r = SOURCES.get_item(Key={"url": url}).get("Item")
    return bool(r and r.get("raw_text_sha256") == sha)
