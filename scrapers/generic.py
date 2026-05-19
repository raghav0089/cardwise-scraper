"""
Generic scraper — used for banks without a custom scraper yet.
Falls back to known card data. Extend this for each bank.
"""

from scrapers.base import BaseScraper

# ─────────────────────────────────────────────────────
# Known cards for all major issuers
# Add more as you research them
# This is your baseline dataset — grow it over time
# ─────────────────────────────────────────────────────

KNOWN_CARDS = {

    "axis": [
        {
            "card_id": "axis_ace",
            "card_name": "ACE",
            "bank": "Axis Bank",
            "issuer_type": "bank",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": True,
            "co_brand_partner": "Google Pay",
            "joining_fee": 499.0,
            "annual_fee": 499.0,
            "fee_waiver_condition": "Spend ₹2L annually",
            "fee_waiver_spend": 200000.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "bill_payment", "reward_type": "cashback", "reward_percent": 5.0, "reward_currency": "INR", "notes": "Via Google Pay"},
                {"channel": "all", "merchant_category": "food", "reward_type": "cashback", "reward_percent": 4.0, "reward_currency": "INR", "notes": "Swiggy/Zomato"},
                {"channel": "all", "merchant_category": "travel", "reward_type": "cashback", "reward_percent": 4.0, "reward_currency": "INR", "notes": "Ola"},
                {"channel": "all", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 2.0, "reward_currency": "INR"},
            ],
            "lounge_access": {"domestic_per_quarter": 2, "international_per_year": 0},
            "fuel_surcharge_waiver": True,
            "forex_markup_percent": 3.5,
            "min_income_annual": 300000.0,
            "active": True,
        },
        {
            "card_id": "axis_atlas",
            "card_name": "Atlas",
            "bank": "Axis Bank",
            "issuer_type": "bank",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 5000.0,
            "annual_fee": 5000.0,
            "fee_waiver_condition": "Spend ₹7.5L annually",
            "fee_waiver_spend": 750000.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "travel", "reward_type": "miles", "reward_value": 5, "reward_per_spend": 100, "reward_currency": "EDGE Miles"},
                {"channel": "all", "merchant_category": "default", "reward_type": "miles", "reward_value": 2, "reward_per_spend": 100, "reward_currency": "EDGE Miles"},
            ],
            "lounge_access": {"domestic_per_quarter": 999, "international_per_year": 12, "network": "Priority Pass"},
            "forex_markup_percent": 3.5,
            "min_income_annual": 1500000.0,
            "active": True,
        },
        {
            "card_id": "axis_flipkart",
            "card_name": "Flipkart",
            "bank": "Axis Bank",
            "issuer_type": "bank",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": True,
            "co_brand_partner": "Flipkart",
            "joining_fee": 500.0,
            "annual_fee": 500.0,
            "reward_rules": [
                {"channel": "online", "merchant_category": "shopping", "reward_type": "cashback", "reward_percent": 5.0, "reward_currency": "INR", "notes": "On Flipkart"},
                {"channel": "online", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 4.0, "reward_currency": "INR"},
                {"channel": "offline", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 1.5, "reward_currency": "INR"},
            ],
            "lounge_access": {"domestic_per_quarter": 4, "international_per_year": 0},
            "active": True,
        },
    ],

    "icici": [
        {
            "card_id": "icici_amazon_pay",
            "card_name": "Amazon Pay ICICI Bank",
            "bank": "ICICI Bank",
            "issuer_type": "bank",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": True,
            "co_brand_partner": "Amazon",
            "joining_fee": 0.0,
            "annual_fee": 0.0,
            "reward_rules": [
                {"channel": "online", "merchant_category": "shopping", "reward_type": "cashback", "reward_percent": 5.0, "reward_currency": "Amazon Pay", "notes": "Prime members on Amazon"},
                {"channel": "online", "merchant_category": "shopping", "reward_type": "cashback", "reward_percent": 3.0, "reward_currency": "Amazon Pay", "notes": "Non-Prime on Amazon"},
                {"channel": "all", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 1.0, "reward_currency": "Amazon Pay"},
            ],
            "lounge_access": None,
            "active": True,
        },
        {
            "card_id": "icici_emeralde",
            "card_name": "Emeralde Private Metal",
            "bank": "ICICI Bank",
            "issuer_type": "bank",
            "network": "Mastercard",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 12499.0,
            "annual_fee": 12499.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "travel", "reward_type": "points", "reward_value": 6, "reward_per_spend": 100, "reward_currency": "PAYBACK"},
                {"channel": "all", "merchant_category": "default", "reward_type": "points", "reward_value": 4, "reward_per_spend": 100, "reward_currency": "PAYBACK"},
            ],
            "lounge_access": {"domestic_per_quarter": 999, "international_per_year": 999, "network": "Priority Pass"},
            "concierge": True,
            "golf_benefit": True,
            "min_income_annual": 3600000.0,
            "active": True,
        },
    ],

    "sbi": [
        {
            "card_id": "sbi_cashback",
            "card_name": "Cashback SBI Card",
            "bank": "SBI Card",
            "issuer_type": "bank",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 999.0,
            "annual_fee": 999.0,
            "fee_waiver_condition": "Spend ₹2L annually",
            "fee_waiver_spend": 200000.0,
            "reward_rules": [
                {"channel": "online", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 5.0, "reward_currency": "INR", "cap_per_month": 5000.0},
                {"channel": "offline", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 1.0, "reward_currency": "INR"},
            ],
            "lounge_access": {"domestic_per_quarter": 0, "international_per_year": 0},
            "fuel_surcharge_waiver": True,
            "active": True,
        },
    ],

    "amex": [
        {
            "card_id": "amex_platinum_travel",
            "card_name": "Platinum Travel",
            "bank": "American Express",
            "issuer_type": "bank",
            "network": "Amex",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 3500.0,
            "annual_fee": 5000.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "travel", "reward_type": "points", "reward_value": 5, "reward_per_spend": 50, "reward_currency": "MR Points"},
                {"channel": "all", "merchant_category": "default", "reward_type": "points", "reward_value": 1, "reward_per_spend": 50, "reward_currency": "MR Points"},
            ],
            "lounge_access": {"domestic_per_quarter": 8, "international_per_year": 0},
            "concierge": True,
            "min_income_annual": 600000.0,
            "active": True,
        },
    ],

    "scapia": [
        {
            "card_id": "scapia_federal",
            "card_name": "Scapia",
            "bank": "Federal Bank",
            "issuer_type": "fintech",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 0.0,
            "annual_fee": 0.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "travel", "reward_type": "points", "reward_value": 10, "reward_per_spend": 100, "reward_currency": "Scapia Coins"},
                {"channel": "all", "merchant_category": "default", "reward_type": "points", "reward_value": 2, "reward_per_spend": 100, "reward_currency": "Scapia Coins"},
            ],
            "lounge_access": {"domestic_per_quarter": 999, "international_per_year": 0, "notes": "Unlimited domestic via DreamFolks"},
            "forex_markup_percent": 0.0,
            "min_income_annual": 300000.0,
            "active": True,
        },
    ],

    "onecard": [
        {
            "card_id": "onecard_metal",
            "card_name": "OneCard",
            "bank": "IDFC FIRST / SBM / BOB",
            "issuer_type": "fintech",
            "network": "Visa",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 0.0,
            "annual_fee": 0.0,
            "reward_rules": [
                {"channel": "all", "merchant_category": "top_2_categories", "reward_type": "points", "reward_value": 5, "reward_per_spend": 50, "reward_currency": "1K Points", "notes": "5X on top 2 spend categories"},
                {"channel": "all", "merchant_category": "default", "reward_type": "points", "reward_value": 1, "reward_per_spend": 50, "reward_currency": "1K Points"},
            ],
            "lounge_access": None,
            "forex_markup_percent": 0.0,
            "active": True,
        },
    ],

    "kiwi": [
        {
            "card_id": "kiwi_rupay",
            "card_name": "Kiwi",
            "bank": "SBM Bank",
            "issuer_type": "fintech",
            "network": "RuPay",
            "card_type": "credit",
            "co_branded": False,
            "joining_fee": 0.0,
            "annual_fee": 0.0,
            "reward_rules": [
                {"channel": "upi", "network": "rupay", "merchant_category": "default", "reward_type": "cashback", "reward_percent": 2.0, "reward_currency": "INR", "notes": "On UPI payments via RuPay"},
            ],
            "lounge_access": None,
            "active": True,
        },
    ],

}


class GenericScraper(BaseScraper):
    """
    Used for all banks that don't have a custom scraper yet.
    Returns known card data with proper normalization.
    """

    def __init__(self, issuer: str):
        super().__init__(issuer)

    async def scrape(self) -> list[dict]:
        key = self.issuer.lower().replace(' ', '_')
        known = KNOWN_CARDS.get(key, [])

        if not known:
            print(f"[{self.issuer}] No known cards — scraper needed")
            return []

        normalized = []
        for card in known:
            card['source_url'] = f"https://{self.issuer.lower()}.com"
            normalized.append(self.normalize(card))

        print(f"[{self.issuer}] Loaded {len(normalized)} known cards")
        return normalized
