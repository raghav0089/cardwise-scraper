"""
Main scraper runner.

Run once:
    python main.py

Run for specific issuer only:
    python main.py --issuer hdfc

Enable daily cron later via:
    - GitHub Actions (cheapest, free)
    - AWS EventBridge → ECS Fargate task (scalable)
    - Simple cron on an EC2 (easy)
"""

import asyncio
import argparse
from datetime import datetime, timezone

from config.sources import SOURCES
from scrapers.hdfc import HDFCScraper
from scrapers.generic import GenericScraper
from utils.db import (
    save_card,
    save_s3_snapshot,
    log_run_start,
    log_run_complete,
    log_run_failed,
)


# ─────────────────────────────────────────────────────
# SCRAPER REGISTRY
# Maps issuer name → scraper class
# Add custom scrapers here as you build them
# ─────────────────────────────────────────────────────
SCRAPER_MAP = {
    'hdfc':     HDFCScraper,
    # Add more as you build them:
    # 'icici':  ICICIScraper,
    # 'axis':   AxisScraper,
    # 'sbi':    SBIScraper,
    # 'amex':   AmexScraper,
}


def get_scraper(source: dict):
    """Return the right scraper for a source, fallback to generic."""
    scraper_key = source['scraper']

    if scraper_key in SCRAPER_MAP:
        return SCRAPER_MAP[scraper_key]()

    # No custom scraper yet — use generic with known data
    return GenericScraper(scraper_key)


# ─────────────────────────────────────────────────────
# MAIN RUN
# ─────────────────────────────────────────────────────

async def run(issuer_filter: str = None):
    run_id   = log_run_start()
    run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    stats = {
        'total_issuers': 0,
        'total_cards':   0,
        'new':           0,
        'updated':       0,
        'unchanged':     0,
        'errors':        0,
    }

    print(f"\n{'='*60}")
    print(f"CardWise Scraper — {run_date}")
    print(f"Run ID: {run_id}")
    print(f"{'='*60}\n")

    sources = [s for s in SOURCES if s['enabled']]

    # Filter to specific issuer if passed
    if issuer_filter:
        sources = [s for s in sources if s['scraper'].lower() == issuer_filter.lower()]
        if not sources:
            print(f"No source found for issuer: {issuer_filter}")
            return

    for source in sources:
        issuer = source['issuer']
        print(f"\n[{issuer}] Starting...")

        try:
            scraper = get_scraper(source)
            cards   = await scraper.scrape()

            if not cards:
                print(f"[{issuer}] No cards returned")
                continue

            # Save raw snapshot to S3
            save_s3_snapshot(issuer, cards, run_date)

            # Save each card to DynamoDB
            issuer_stats = {'new': 0, 'updated': 0, 'unchanged': 0}
            for card in cards:
                try:
                    result = save_card(card)
                    issuer_stats[result] += 1
                    stats[result] += 1
                    stats['total_cards'] += 1
                except Exception as e:
                    print(f"  ❌ Error saving {card.get('card_name', '?')}: {e}")
                    stats['errors'] += 1

            stats['total_issuers'] += 1
            print(
                f"[{issuer}] Done — "
                f"{issuer_stats['new']} new, "
                f"{issuer_stats['updated']} updated, "
                f"{issuer_stats['unchanged']} unchanged"
            )

        except Exception as e:
            print(f"[{issuer}] ❌ Scraper failed: {e}")
            stats['errors'] += 1
            continue

    # Log run completion
    log_run_complete(run_id, stats)

    print(f"\n{'='*60}")
    print(f"Run complete.")
    print(f"Issuers: {stats['total_issuers']} | Cards: {stats['total_cards']}")
    print(f"New: {stats['new']} | Updated: {stats['updated']} | Unchanged: {stats['unchanged']}")
    print(f"Errors: {stats['errors']}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CardWise card data scraper')
    parser.add_argument('--issuer', type=str, help='Run for one issuer only (e.g. hdfc)')
    args = parser.parse_args()

    asyncio.run(run(issuer_filter=args.issuer))
