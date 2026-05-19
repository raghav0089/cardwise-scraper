import boto3
import json
import uuid
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
s3       = boto3.client('s3', region_name='ap-south-1')

cards_table   = dynamodb.Table('cards_master')
versions_table = dynamodb.Table('cards_versions')
events_table  = dynamodb.Table('card_change_events')
runs_table    = dynamodb.Table('scraper_runs')

S3_BUCKET = 'plenti-card-snapshots'   # create this bucket in S3


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ─────────────────────────────────────────────────────
# SAVE OR UPDATE A CARD
# ─────────────────────────────────────────────────────

def save_card(card: dict) -> str:
    """
    Save card to DynamoDB with change detection.
    Returns: 'new' | 'updated' | 'unchanged'
    """
    card_id  = card['card_id']
    new_hash = card['source_hash']

    # Check if card already exists
    existing = cards_table.get_item(Key={'cardId': card_id}).get('Item')

    if not existing:
        # ── New card ──────────────────────────────────
        card['version']  = 1
        card['cardId']   = card_id   # DynamoDB PK

        cards_table.put_item(Item=card)

        # Save first version
        _save_version(card, version=1, change_type='new_card')

        # Log change event
        _log_event(card_id, 'new_card', f"New card added: {card['card_name']}", card)

        print(f"  ✅ NEW: {card['card_name']}")
        return 'new'

    elif existing.get('source_hash') != new_hash:
        # ── Card changed ──────────────────────────────
        new_version = int(existing.get('version', 1)) + 1
        card['version'] = new_version
        card['cardId']  = card_id

        cards_table.put_item(Item=card)

        # Save new version snapshot
        _save_version(card, version=new_version, change_type='updated')

        # Detect what changed
        changes = _detect_changes(existing, card)
        _log_event(card_id, 'updated', changes, card)

        print(f"  🔄 UPDATED: {card['card_name']} (v{new_version}) — {changes}")
        return 'updated'

    else:
        # ── No change — just update verified timestamp ─
        cards_table.update_item(
            Key={'cardId': card_id},
            UpdateExpression='SET last_verified_at = :t',
            ExpressionAttributeValues={':t': now_iso()}
        )
        return 'unchanged'


def mark_card_inactive(card_id: str):
    """Mark a card as discontinued — never delete."""
    cards_table.update_item(
        Key={'cardId': card_id},
        UpdateExpression='SET active = :a, discontinued_at = :t',
        ExpressionAttributeValues={':a': False, ':t': now_iso()}
    )
    _log_event(card_id, 'discontinued', 'Card no longer found on issuer page', {})
    print(f"  ⚠️  DISCONTINUED: {card_id}")


# ─────────────────────────────────────────────────────
# VERSION HISTORY
# ─────────────────────────────────────────────────────

def _save_version(card: dict, version: int, change_type: str):
    versions_table.put_item(Item={
        'cardId':      card['card_id'],
        'version':     f"v{str(version).zfill(4)}",   # v0001, v0002 etc — sorts correctly
        'change_type': change_type,
        'snapshot':    card,
        'created_at':  now_iso(),
    })


# ─────────────────────────────────────────────────────
# CHANGE DETECTION
# ─────────────────────────────────────────────────────

def _detect_changes(old: dict, new: dict) -> str:
    """Produce a human-readable summary of what changed."""
    changes = []

    # Fee changes
    if old.get('annual_fee') != new.get('annual_fee'):
        changes.append(f"annual fee: ₹{old.get('annual_fee')} → ₹{new.get('annual_fee')}")

    if old.get('joining_fee') != new.get('joining_fee'):
        changes.append(f"joining fee: ₹{old.get('joining_fee')} → ₹{new.get('joining_fee')}")

    # Lounge changes
    old_lounge = (old.get('lounge_access') or {}).get('domestic_per_quarter')
    new_lounge = (new.get('lounge_access') or {}).get('domestic_per_quarter')
    if old_lounge != new_lounge:
        changes.append(f"domestic lounge: {old_lounge} → {new_lounge} per quarter")

    # Reward changes (detect if reward_rules list changed)
    if old.get('reward_rules') != new.get('reward_rules'):
        changes.append("reward rules changed")

    # Forex markup
    if old.get('forex_markup_percent') != new.get('forex_markup_percent'):
        changes.append(f"forex markup: {old.get('forex_markup_percent')}% → {new.get('forex_markup_percent')}%")

    return '; '.join(changes) if changes else 'minor update'


# ─────────────────────────────────────────────────────
# CHANGE EVENTS
# ─────────────────────────────────────────────────────

def _log_event(card_id: str, event_type: str, description: str, card: dict):
    events_table.put_item(Item={
        'cardId':      card_id,
        'changedAt':   now_iso(),
        'event_type':  event_type,   # new_card, updated, discontinued
        'description': description,
        'card_name':   card.get('card_name', ''),
        'bank':        card.get('bank', ''),
    })


# ─────────────────────────────────────────────────────
# S3 SNAPSHOT — raw backup of each scraper run
# ─────────────────────────────────────────────────────

def save_s3_snapshot(issuer: str, cards: list[dict], run_date: str):
    """
    Save raw scrape results to S3 for debugging and re-processing.
    s3://plenti-card-snapshots/hdfc/2026-05-17.json
    """
    try:
        key  = f"{issuer.lower()}/{run_date}.json"
        body = json.dumps(cards, indent=2, default=str)

        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=body,
            ContentType='application/json',
        )
        print(f"  📦 S3 snapshot: s3://{S3_BUCKET}/{key}")
    except Exception as e:
        print(f"  ⚠️  S3 snapshot failed (non-critical): {e}")


# ─────────────────────────────────────────────────────
# SCRAPER RUN LOG
# ─────────────────────────────────────────────────────

def log_run_start() -> str:
    run_id = str(uuid.uuid4())
    runs_table.put_item(Item={
        'runId':      run_id,
        'started_at': now_iso(),
        'status':     'running',
    })
    return run_id


def log_run_complete(run_id: str, stats: dict):
    runs_table.update_item(
        Key={'runId': run_id},
        UpdateExpression='SET completed_at = :c, #s = :s, stats = :st',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':c':  now_iso(),
            ':s':  'completed',
            ':st': stats,
        }
    )


def log_run_failed(run_id: str, error: str):
    runs_table.update_item(
        Key={'runId': run_id},
        UpdateExpression='SET completed_at = :c, #s = :s, error = :e',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':c': now_iso(),
            ':s': 'failed',
            ':e': error,
        }
    )
