"""
HDFC Bank credit card scraper.
Scrapes https://www.hdfcbank.com/personal/pay/cards/credit-cards
"""

from scrapers.base import BaseScraper


class HDFCScraper(BaseScraper):

    def __init__(self):
        super().__init__('HDFC')
        self.url = 'https://www.hdfcbank.com/personal/pay/cards/credit-cards'

    async def scrape(self) -> list[dict]:
        page, browser, pw = await self.get_page(
            self.url,
            wait_for='.card-listing, .credit-card-list, [class*="card"]'
        )

        cards = []

        try:
            # HDFC lists cards as individual tiles/blocks
            # Try multiple selector patterns since their page structure changes
            card_elements = await page.query_selector_all(
                '.card-item, .credit-card-item, [class*="card-box"], [class*="cardItem"]'
            )

            if not card_elements:
                # Fallback — get all card links from the page
                card_elements = await page.query_selector_all('a[href*="credit-card"]')

            for el in card_elements:
                try:
                    card = await self._parse_card_element(el, page)
                    if card:
                        cards.append(self.normalize(card))
                except Exception as e:
                    print(f"[HDFC] Error parsing card element: {e}")
                    continue

            # If scraping fails, fall back to known HDFC cards
            if not cards:
                print("[HDFC] Dynamic scraping failed — using known card list")
                cards = self._known_cards()

        except Exception as e:
            print(f"[HDFC] Scrape error: {e} — using known cards")
            cards = self._known_cards()
        finally:
            await self.close(browser, pw)

        print(f"[HDFC] Found {len(cards)} cards")
        return cards

    async def _parse_card_element(self, el, page) -> dict | None:
        name = await el.get_attribute('aria-label') or await el.inner_text()
        name = name.strip()[:100] if name else None

        if not name or len(name) < 3:
            return None

        link = await el.get_attribute('href') or ''
        if not link.startswith('http'):
            link = f"https://www.hdfcbank.com{link}" if link else None

        return {
            "card_id":         self.make_card_id('hdfc', name),
            "card_name":       name,
            "bank":            "HDFC Bank",
            "issuer_type":     "bank",
            "network":         "Visa",          # most HDFC cards are Visa
            "card_type":       "credit",
            "co_branded":      False,
            "annual_fee":      None,            # need detail page for this
            "joining_fee":     None,
            "reward_rules":    [],
            "lounge_access":   None,
            "active":          True,
            "official_page":   link,
            "apply_link":      link,
            "source_url":      self.url,
        }

    def _known_cards(self) -> list[dict]:
        """
        Hardcoded fallback for top HDFC cards with full data.
        Used when scraping fails or as baseline.
        Update these when HDFC changes terms.
        """
        known = [
            {
                "card_id":          "hdfc_regalia_gold",
                "card_name":        "Regalia Gold",
                "bank":             "HDFC Bank",
                "issuer_type":      "bank",
                "network":          "Visa",
                "card_type":        "credit",
                "co_branded":       False,
                "joining_fee":      2500.0,
                "annual_fee":       2500.0,
                "fee_waiver_condition": "Spend ₹4L annually",
                "fee_waiver_spend": 400000.0,
                "reward_rules": [
                    {
                        "channel":          "all",
                        "merchant_category": "travel",
                        "reward_type":      "points",
                        "reward_value":     4,
                        "reward_per_spend": 150,
                        "reward_currency":  "RP",
                    },
                    {
                        "channel":          "all",
                        "merchant_category": "dining",
                        "reward_type":      "points",
                        "reward_value":     4,
                        "reward_per_spend": 150,
                        "reward_currency":  "RP",
                    },
                    {
                        "channel":          "all",
                        "merchant_category": "default",
                        "reward_type":      "points",
                        "reward_value":     2,
                        "reward_per_spend": 150,
                        "reward_currency":  "RP",
                    },
                ],
                "reward_program":   "SmartBuy",
                "point_value_inr":  0.50,
                "lounge_access": {
                    "domestic_per_quarter":   8,
                    "international_per_year": 6,
                    "network": "Priority Pass / DreamFolks",
                },
                "fuel_surcharge_waiver":   True,
                "fuel_waiver_percent":     1.0,
                "fuel_waiver_cap_monthly": 500.0,
                "forex_markup_percent":    2.0,
                "insurance_cover":         True,
                "insurance_cover_amount":  5000000.0,
                "concierge":               True,
                "milestone_benefits": [
                    {"spend_threshold": 500000, "benefit": "5000 bonus RP", "benefit_type": "points"},
                    {"spend_threshold": 800000, "benefit": "Free night ITC/Marriott", "benefit_type": "voucher"},
                ],
                "welcome_benefits": ["2500 reward points on first spend"],
                "min_income_annual": 1200000.0,
                "apply_link":   "https://www.hdfcbank.com/personal/pay/cards/credit-cards/regalia-gold-credit-card",
                "official_page": "https://www.hdfcbank.com/personal/pay/cards/credit-cards/regalia-gold-credit-card",
                "active":        True,
                "source_url":    self.url,
            },
            {
                "card_id":          "hdfc_millennia",
                "card_name":        "Millennia",
                "bank":             "HDFC Bank",
                "issuer_type":      "bank",
                "network":          "Visa",
                "card_type":        "credit",
                "co_branded":       False,
                "joining_fee":      1000.0,
                "annual_fee":       1000.0,
                "fee_waiver_condition": "Spend ₹1L annually",
                "fee_waiver_spend": 100000.0,
                "reward_rules": [
                    {
                        "channel":          "online",
                        "merchant_category": "default",
                        "reward_type":      "cashback",
                        "reward_percent":   5.0,
                        "reward_currency":  "cashback",
                        "cap_per_month":    1000.0,
                    },
                    {
                        "channel":          "offline",
                        "merchant_category": "default",
                        "reward_type":      "cashback",
                        "reward_percent":   1.0,
                        "reward_currency":  "cashback",
                    },
                ],
                "lounge_access": {
                    "domestic_per_quarter":  2,
                    "international_per_year": 0,
                },
                "fuel_surcharge_waiver": True,
                "forex_markup_percent":  3.5,
                "min_income_annual":     300000.0,
                "apply_link":   "https://www.hdfcbank.com/personal/pay/cards/credit-cards/millennia-credit-card",
                "official_page": "https://www.hdfcbank.com/personal/pay/cards/credit-cards/millennia-credit-card",
                "active":        True,
                "source_url":    self.url,
            },
            {
                "card_id":         "hdfc_swiggy",
                "card_name":       "Swiggy HDFC Bank",
                "bank":            "HDFC Bank",
                "issuer_type":     "bank",
                "network":         "Visa",
                "card_type":       "credit",
                "co_branded":      True,
                "co_brand_partner": "Swiggy",
                "joining_fee":     500.0,
                "annual_fee":      500.0,
                "fee_waiver_condition": "Spend ₹2L annually",
                "fee_waiver_spend": 200000.0,
                "reward_rules": [
                    {
                        "channel":           "online",
                        "merchant_category": "food",
                        "reward_type":       "cashback",
                        "reward_percent":    10.0,
                        "reward_currency":   "swiggy_money",
                        "notes":             "On Swiggy app only",
                    },
                    {
                        "channel":           "online",
                        "merchant_category": "default",
                        "reward_type":       "cashback",
                        "reward_percent":    5.0,
                        "reward_currency":   "swiggy_money",
                    },
                    {
                        "channel":           "offline",
                        "merchant_category": "default",
                        "reward_type":       "cashback",
                        "reward_percent":    1.0,
                        "reward_currency":   "swiggy_money",
                    },
                ],
                "lounge_access": None,
                "min_income_annual": 250000.0,
                "active": True,
                "source_url": self.url,
            },
            {
                "card_id":         "hdfc_tata_neu_plus",
                "card_name":       "Tata Neu Plus HDFC Bank",
                "bank":            "HDFC Bank",
                "issuer_type":     "bank",
                "network":         "Visa / RuPay",
                "card_type":       "credit",
                "co_branded":      True,
                "co_brand_partner": "Tata Neu",
                "joining_fee":     499.0,
                "annual_fee":      499.0,
                "fee_waiver_condition": "Spend ₹1.5L annually",
                "fee_waiver_spend": 150000.0,
                "reward_rules": [
                    {
                        "channel":           "online",
                        "merchant_category": "tata_brands",
                        "reward_type":       "points",
                        "reward_percent":    2.0,
                        "reward_currency":   "NeuCoins",
                    },
                    {
                        "channel":           "all",
                        "merchant_category": "default",
                        "reward_type":       "points",
                        "reward_percent":    1.0,
                        "reward_currency":   "NeuCoins",
                    },
                ],
                "lounge_access": {
                    "domestic_per_quarter":  2,
                    "international_per_year": 0,
                },
                "active": True,
                "source_url": self.url,
            },
        ]

        return [self.normalize(c) for c in known]
