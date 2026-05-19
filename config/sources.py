# ─────────────────────────────────────────────────────
# SOURCE REGISTRY
# Add new issuers here — scraper picks them up automatically
# set enabled=False to skip without deleting
# ─────────────────────────────────────────────────────

SOURCES = [

    # ── Major Private Banks ──────────────────────────
    {
        "issuer":       "HDFC",
        "issuer_type":  "bank",
        "scraper":      "hdfc",
        "enabled":      True,
        "urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards",
        ],
    },
    {
        "issuer":       "ICICI",
        "issuer_type":  "bank",
        "scraper":      "icici",
        "enabled":      True,
        "urls": [
            "https://www.icicibank.com/personal-banking/cards/consumer-credit-card",
        ],
    },
    {
        "issuer":       "Axis",
        "issuer_type":  "bank",
        "scraper":      "axis",
        "enabled":      True,
        "urls": [
            "https://www.axisbank.com/retail/cards/credit-card",
        ],
    },
    {
        "issuer":       "SBI",
        "issuer_type":  "bank",
        "scraper":      "sbi",
        "enabled":      True,
        "urls": [
            "https://www.sbicard.com/en/personal/credit-cards.page",
        ],
    },
    {
        "issuer":       "Kotak",
        "issuer_type":  "bank",
        "scraper":      "kotak",
        "enabled":      True,
        "urls": [
            "https://www.kotak.com/en/personal-banking/cards/credit-cards.html",
        ],
    },
    {
        "issuer":       "IndusInd",
        "issuer_type":  "bank",
        "scraper":      "indusind",
        "enabled":      True,
        "urls": [
            "https://www.indusind.com/in/en/personal/cards/credit-card.html",
        ],
    },
    {
        "issuer":       "Yes Bank",
        "issuer_type":  "bank",
        "scraper":      "yesbank",
        "enabled":      True,
        "urls": [
            "https://www.yesbank.in/personal-banking/yes-individual/cards/credit-cards",
        ],
    },
    {
        "issuer":       "IDFC FIRST",
        "issuer_type":  "bank",
        "scraper":      "idfc",
        "enabled":      True,
        "urls": [
            "https://www.idfcfirstbank.com/credit-card",
        ],
    },
    {
        "issuer":       "RBL",
        "issuer_type":  "bank",
        "scraper":      "rbl",
        "enabled":      True,
        "urls": [
            "https://www.rblbank.com/cards/credit-cards",
        ],
    },
    {
        "issuer":       "AU Small Finance",
        "issuer_type":  "bank",
        "scraper":      "au",
        "enabled":      True,
        "urls": [
            "https://www.aubank.in/credit-cards",
        ],
    },

    # ── Public Banks ─────────────────────────────────
    {
        "issuer":       "SBI Cards",
        "issuer_type":  "bank",
        "scraper":      "sbi",
        "enabled":      True,
        "urls": [
            "https://www.sbicard.com/en/personal/credit-cards.page",
        ],
    },
    {
        "issuer":       "Bank of Baroda",
        "issuer_type":  "bank",
        "scraper":      "bob",
        "enabled":      True,
        "urls": [
            "https://www.bankofbaroda.in/personal-banking/digital-products/cards/credit-cards",
        ],
    },

    # ── Foreign Banks ─────────────────────────────────
    {
        "issuer":       "Amex",
        "issuer_type":  "bank",
        "scraper":      "amex",
        "enabled":      True,
        "urls": [
            "https://www.americanexpress.com/in/credit-cards/",
        ],
    },
    {
        "issuer":       "HSBC",
        "issuer_type":  "bank",
        "scraper":      "hsbc",
        "enabled":      True,
        "urls": [
            "https://www.hsbc.co.in/credit-cards/products/",
        ],
    },
    {
        "issuer":       "Standard Chartered",
        "issuer_type":  "bank",
        "scraper":      "sc",
        "enabled":      True,
        "urls": [
            "https://www.sc.com/in/credit-cards/",
        ],
    },

    # ── Fintech / Neo Cards ───────────────────────────
    {
        "issuer":       "OneCard",
        "issuer_type":  "fintech",
        "scraper":      "onecard",
        "enabled":      True,
        "urls": [
            "https://www.getonecard.app/",
        ],
    },
    {
        "issuer":       "Scapia",
        "issuer_type":  "fintech",
        "scraper":      "scapia",
        "enabled":      True,
        "urls": [
            "https://www.scapia.app/",
        ],
    },
    {
        "issuer":       "Kiwi",
        "issuer_type":  "fintech",
        "scraper":      "kiwi",
        "enabled":      True,
        "urls": [
            "https://www.kiwicredit.in/",
        ],
    },
    {
        "issuer":       "Uni Cards",
        "issuer_type":  "fintech",
        "scraper":      "uni",
        "enabled":      True,
        "urls": [
            "https://uni.cards/",
        ],
    },
    {
        "issuer":       "Jupiter",
        "issuer_type":  "fintech",
        "scraper":      "jupiter",
        "enabled":      True,
        "urls": [
            "https://jupiter.money/credit-card/",
        ],
    },
    {
        "issuer":       "Fi Money",
        "issuer_type":  "fintech",
        "scraper":      "fi",
        "enabled":      True,
        "urls": [
            "https://fi.money/features/credit-card",
        ],
    },
    {
        "issuer":       "Slice",
        "issuer_type":  "fintech",
        "scraper":      "slice",
        "enabled":      True,
        "urls": [
            "https://www.sliceit.com/",
        ],
    },

]

# Quick lookup by scraper name
SOURCE_MAP = {s['scraper']: s for s in SOURCES}
