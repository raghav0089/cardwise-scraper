import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import async_playwright, Page


class BaseScraper(ABC):
    """
    Every bank scraper extends this.
    Handles: browser setup, hash generation, normalization helpers.
    Only implement scrape() in each child scraper.
    """

    def __init__(self, issuer: str):
        self.issuer = issuer

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """
        Return list of normalized card dicts.
        Each dict must match the Card schema.
        """
        ...

    # ── Browser helpers ───────────────────────────────

    async def get_page(self, url: str, wait_for: str = None) -> tuple:
        """Launch Playwright, navigate to URL, return (page, browser, playwright)"""
        pw       = await async_playwright().start()
        browser  = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        context  = await browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1280, 'height': 800},
        )
        page = await context.new_page()

        await page.goto(url, wait_until='domcontentloaded', timeout=30000)

        if wait_for:
            await page.wait_for_selector(wait_for, timeout=15000)

        return page, browser, pw

    async def close(self, browser, pw):
        await browser.close()
        await pw.stop()

    # ── Normalization helpers ─────────────────────────

    def make_card_id(self, bank: str, card_name: str) -> str:
        """hdfc + Regalia Gold → hdfc_regalia_gold"""
        combined = f"{bank}_{card_name}"
        return re.sub(r'[^a-z0-9]+', '_', combined.lower()).strip('_')

    def extract_amount(self, text: str) -> Optional[float]:
        """Extract first number from strings like '₹2,500 + GST'"""
        if not text:
            return None
        cleaned = text.replace(',', '').replace('₹', '').replace('Rs.', '')
        match = re.search(r'(\d+(?:\.\d+)?)', cleaned)
        return float(match.group(1)) if match else None

    def extract_lounge_count(self, text: str) -> Optional[int]:
        """'4 complimentary lounge visits per quarter' → 4"""
        if not text:
            return None
        if any(w in text.lower() for w in ['unlimited', 'complimentary unlimited']):
            return 999
        match = re.search(r'(\d+)', text)
        return int(match.group(1)) if match else None

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def hash_card(self, card: dict) -> str:
        """SHA256 of the card data — detect changes on next run"""
        # Remove meta fields before hashing (they always change)
        skip = {'source_hash', 'last_verified_at', 'version'}
        filtered = {k: v for k, v in card.items() if k not in skip}
        return hashlib.sha256(
            json.dumps(filtered, sort_keys=True, default=str).encode()
        ).hexdigest()

    def normalize(self, card: dict) -> dict:
        """Add hash and timestamp to a card dict before saving."""
        card['last_verified_at'] = self.now_iso()
        card['source_hash']      = self.hash_card(card)
        return card
