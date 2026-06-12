"""DynamoDB + S3 persistence.

All functions are no-ops when AWS_REGION or DDB_CARDS_TABLE are not set,
so the scraper can run and produce out/cards.json without AWS credentials.
"""
from __future__ import annotations
import os, gzip, logging, datetime as dt
from decimal import Decimal

log = logging.getLogger(__name__)

REGION        = os.getenv("AWS_REGION",        "ap-south-1").strip()
BUCKET        = os.getenv("S3_BUCKET",         "").strip()
_CARDS_NAME   = os.getenv("DDB_CARDS_TABLE",   "cards_master").strip()
_SOURCES_NAME = os.getenv("DDB_SOURCES_TABLE", "cards_sources").strip()

_AWS_ENABLED  = bool(REGION and _CARDS_NAME)

# Lazy singletons — created on first use, never at import time
_dynamodb  = None
_s3_client = None
_CARDS   = None
_SOURCES = None


def _ddb():
    global _dynamodb, _CARDS, _SOURCES
    if _dynamodb is None:
        import boto3
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _CARDS   = _dynamodb.Table(_CARDS_NAME)
        _SOURCES = _dynamodb.Table(_SOURCES_NAME)
    return _CARDS, _SOURCES


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3", region_name=REGION)
    return _s3_client


def _to_ddb(obj):
    if isinstance(obj, float): return Decimal(str(obj))
    if isinstance(obj, list):  return [_to_ddb(x) for x in obj]
    if isinstance(obj, dict):  return {k: _to_ddb(v) for k, v in obj.items() if v is not None}
    return obj


def archive_raw(url: str, html: str) -> str | None:
    if not _AWS_ENABLED or not BUCKET or not html:
        return None
    try:
        today = dt.date.today().isoformat()
        key   = f"raw/{today}/{abs(hash(url))}.html.gz"
        _s3().put_object(Bucket=BUCKET, Key=key,
                         Body=gzip.compress(html.encode("utf-8", errors="ignore")),
                         ContentType="text/html", ContentEncoding="gzip",
                         Metadata={"source_url": url[:1024]})
        return f"s3://{BUCKET}/{key}"
    except Exception as e:
        log.warning("S3 archive failed for %s: %s", url, e)
        return None


def get_existing(card_id: str) -> dict | None:
    if not _AWS_ENABLED:
        return None
    try:
        cards, _ = _ddb()
        r = cards.get_item(Key={"card_id": card_id})
        return r.get("Item")
    except Exception as e:
        log.warning("DDB get_existing failed %s: %s", card_id, e)
        return None


def upsert_card(card: dict) -> None:
    if not _AWS_ENABLED:
        return
    try:
        cards, _ = _ddb()
        cards.put_item(Item=_to_ddb(card))
    except Exception as e:
        log.warning("DDB upsert_card failed %s: %s", card.get("card_id"), e)


def mark_source(url: str, *, sha: str, etag: str | None) -> None:
    if not _AWS_ENABLED:
        return
    try:
        _, sources = _ddb()
        sources.put_item(Item=_to_ddb({
            "url": url,
            "last_seen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "raw_text_sha256": sha,
            "etag": etag,
        }))
    except Exception as e:
        log.warning("DDB mark_source failed %s: %s", url, e)


def source_unchanged(url: str, sha: str) -> bool:
    if not _AWS_ENABLED:
        return False   # no persistence → always re-scrape
    try:
        _, sources = _ddb()
        r = sources.get_item(Key={"url": url}).get("Item")
        return bool(r and r.get("raw_text_sha256") == sha)
    except Exception as e:
        log.warning("DDB source_unchanged failed %s: %s", url, e)
        return False   # on error, re-scrape rather than silently skip
