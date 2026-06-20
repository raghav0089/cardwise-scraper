#!/usr/bin/env python3
"""
URL health checker for issuers.yaml
Checks every list_url and sitemap_url for HTTP status, redirects, and error page content.
"""

import concurrent.futures
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15
WORKERS = 10

ERROR_PHRASES = [
    "404",
    "not found",
    "page does not exist",
    "page not found",
    "access denied",
    "coming soon",
    "under construction",
    "forbidden",
    "503 service",
    "502 bad gateway",
    "temporarily unavailable",
    "no longer available",
    "this page has moved",
    "we couldn't find",
    "cannot be found",
    "doesn't exist",
    "error occurred",
    "something went wrong",
    "internal server error",
]


@dataclass
class UrlRecord:
    issuer_id: str
    issuer_name: str
    url_type: str  # "list_url" or "sitemap_url"
    original_url: str
    status_code: Optional[int] = None
    final_url: Optional[str] = None
    error: Optional[str] = None
    redirected: bool = False
    domain_changed: bool = False
    error_page_content: bool = False
    matched_phrase: Optional[str] = None
    elapsed: float = 0.0

    @property
    def original_domain(self):
        return urlparse(self.original_url).netloc

    @property
    def final_domain(self):
        if self.final_url:
            return urlparse(self.final_url).netloc
        return None

    @property
    def category(self):
        if self.error:
            return "BROKEN"
        if self.status_code and self.status_code >= 400:
            return "BROKEN"
        if self.error_page_content:
            return "BROKEN"
        if self.redirected:
            return "REDIRECTED"
        return "OK"


def _urls_differ_significantly(original: str, final: str) -> bool:
    """Return True if the redirect target is meaningfully different from the original."""
    if original == final:
        return False
    orig_parsed = urlparse(original)
    final_parsed = urlparse(final)
    # Domain changed
    if orig_parsed.netloc != final_parsed.netloc:
        return True
    # Path changed (ignore trailing slash and fragment differences)
    orig_path = orig_parsed.path.rstrip("/")
    final_path = final_parsed.path.rstrip("/")
    if orig_path != final_path:
        return True
    return False


def check_url(record: UrlRecord) -> UrlRecord:
    try:
        t0 = time.monotonic()
        resp = requests.get(
            record.original_url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        record.elapsed = time.monotonic() - t0
        record.status_code = resp.status_code
        record.final_url = resp.url

        if _urls_differ_significantly(record.original_url, resp.url):
            record.redirected = True
            orig_domain = urlparse(record.original_url).netloc
            final_domain = urlparse(resp.url).netloc
            if orig_domain != final_domain:
                record.domain_changed = True

        # Check response body for error phrases (first 50KB only)
        body_sample = resp.text[:50000].lower()
        for phrase in ERROR_PHRASES:
            if phrase in body_sample:
                # Only flag if the status is also suspicious or phrase is strong
                # For strong indicators, always flag
                strong = [
                    "page does not exist", "page not found", "cannot be found",
                    "doesn't exist", "access denied", "we couldn't find",
                    "this page has moved", "no longer available",
                    "temporarily unavailable", "internal server error",
                    "something went wrong",
                ]
                if phrase in strong or record.status_code >= 400:
                    record.error_page_content = True
                    record.matched_phrase = phrase
                    break
                # For ambiguous phrases like "404", only flag on non-200
                if record.status_code != 200:
                    record.error_page_content = True
                    record.matched_phrase = phrase
                    break

    except requests.exceptions.Timeout:
        record.error = "TIMEOUT"
    except requests.exceptions.SSLError as e:
        record.error = f"SSL_ERROR: {e}"
    except requests.exceptions.ConnectionError as e:
        record.error = f"CONNECTION_ERROR: {e}"
    except requests.exceptions.TooManyRedirects:
        record.error = "TOO_MANY_REDIRECTS"
    except Exception as e:
        record.error = f"ERROR: {e}"

    return record


# ── Build URL list from issuers.yaml ──────────────────────────────────────────

ISSUERS = [
    # ---------- Private banks ----------
    {
        "id": "hdfc", "name": "HDFC Bank",
        "sitemap_url": "https://www.hdfc.bank.in/sitemap.xml",
        "list_urls": [
            "https://www.hdfc.bank.in/credit-cards",
            "https://www.hdfc.bank.in/debit-cards",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards",
            "https://www.hdfcbank.com/personal/pay/cards/debit-cards",
            "https://www.hdfcbank.com/personal/pay/cards/business-credit-card",
        ],
    },
    {
        "id": "icici", "name": "ICICI Bank",
        "list_urls": [
            "https://www.icici.bank.in/personal-banking/cards/credit-card",
        ],
    },
    {
        "id": "axis", "name": "Axis Bank",
        "sitemap_url": "https://www.axis.bank.in/sitemap-english.xml",
        "list_urls": [],
    },
    {
        "id": "sbi_card", "name": "SBI Card",
        "sitemap_url": "https://www.sbicard.com/sitemap.xml",
        "list_urls": [],
    },
    {
        "id": "kotak", "name": "Kotak Mahindra Bank",
        "sitemap_url": "https://www.kotak.bank.in/sitemap-personalbanking.xml",
        "list_urls": [],
    },
    {
        "id": "idfc_first", "name": "IDFC FIRST Bank",
        "sitemap_url": "https://www.idfcfirstbank.com/sitemap.xml",
        "list_urls": [
            "https://www.idfcfirstbank.com/credit-card",
        ],
    },
    {
        "id": "yes_bank", "name": "YES Bank",
        "list_urls": [
            "https://www.yes.bank.in/personal-banking/yes-individual/cards",
            "https://www.yes.bank.in/personal-banking/yes-individual/cards/credit-cards",
            "https://www.yes.bank.in/personal-banking/yes-individual/cards/debit-card",
            "https://www.yes.bank.in/personal-banking/yes-individual/cards/prepaid-cards",
            "https://www.yes.bank.in/personal-banking/yes-individual/cards/mctc-card",
        ],
    },
    {
        "id": "indusind", "name": "IndusInd Bank",
        "sitemap_url": "https://www.indusind.bank.in/sitemap.xml",
        "list_urls": [],
    },
    {
        "id": "rbl", "name": "RBL Bank",
        "list_urls": [
            "https://www.rbl.bank.in/personal-banking/cards/credit-cards",
            "https://www.rbl.bank.in/personal-banking/cards/credit-cards/category",
            "https://www.rbl.bank.in/personal-banking/cards/debit-cards",
        ],
    },
    {
        "id": "standard_chartered", "name": "Standard Chartered India",
        "list_urls": [
            "https://www.sc.bank.in/credit-cards/",
            "https://www.sc.bank.in/debit-cards/",
        ],
    },
    {
        "id": "hsbc", "name": "HSBC India",
        "list_urls": [
            "https://www.hsbc.co.in/credit-cards/",
        ],
    },
    {
        "id": "idbi", "name": "IDBI Bank",
        "list_urls": [
            "https://www.idbibank.in/credit-card.aspx",
        ],
    },
    {
        "id": "federal", "name": "Federal Bank",
        "list_urls": [
            "https://www.federalbank.co.in/credit-cards",
        ],
    },
    {
        "id": "au_sfb", "name": "AU Small Finance Bank",
        "list_urls": [
            "https://www.au.bank.in/personal-banking/credit-cards",
        ],
    },
    {
        "id": "bandhan", "name": "Bandhan Bank",
        "list_urls": [
            "https://www.bandhanbank.com/personal/cards/credit-card",
            "https://www.bandhanbank.com/personal/cards/debit-card",
        ],
    },
    {
        "id": "south_indian", "name": "South Indian Bank",
        "list_urls": [
            "https://www.southindianbank.com/personal-banking/cards/credit-cards",
            "https://www.southindianbank.com/personal-banking/cards/debit-cards",
        ],
    },
    {
        "id": "karur_vysya", "name": "Karur Vysya Bank",
        "sitemap_url": "https://www.kvb.bank.in/sitemap.xml",
        "list_urls": [
            "https://www.kvb.bank.in/personal/cards/kvb-credit-cards/",
        ],
    },
    # ---------- PSU banks ----------
    {
        "id": "bob", "name": "Bank of Baroda (BOBCARD)",
        "list_urls": [
            "https://www.bobcard.co.in/credit-card-types",
            "https://bankofbaroda.bank.in/digital-products/cards/credit-cards",
        ],
    },
    {
        "id": "bajaj", "name": "Bajaj Finserv",
        "list_urls": [
            "https://www.bajajfinserv.in/credit-card",
            "https://www.bajajfinserv.in/credit-card-apply-online",
        ],
    },
    {
        "id": "pnb", "name": "PNB / PNB Cards",
        "list_urls": [
            "https://pnb.bank.in/card-index.html",
            "https://creditcard.pnb.bank.in/",
            "https://pnb.bank.in/PNB-World-travel-card.html",
            "https://pnb.bank.in/card-index.html#ruplaycard_tab",
            "https://pnb.bank.in/Platinum-debit-card.html",
        ],
    },
    {
        "id": "canara", "name": "Canara Bank",
        "list_urls": [
            "https://www.canarabank.bank.in/pages/exclusive-credit-card-offers",
            "https://www.canarabank.bank.in/pages/exclusive-debit-card-offers",
        ],
    },
    {
        "id": "union", "name": "Union Bank of India",
        "list_urls": [
            "https://www.unionbankofindia.co.in/english/Credit-Cards.aspx",
        ],
    },
    {
        "id": "indian_bank", "name": "Indian Bank",
        "list_urls": [
            "https://www.indianbank.in/departments/credit-card/",
        ],
    },
    # ---------- Networks ----------
    {
        "id": "amex", "name": "American Express India",
        "list_urls": [
            "https://www.americanexpress.com/in/credit-cards/",
        ],
    },
    {
        "id": "diners", "name": "Diners Club India",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/diners-club",
        ],
    },
    # ---------- Neobanks ----------
    {
        "id": "onecard", "name": "OneCard (FPL Technologies)",
        "list_urls": ["https://www.getonecard.app/"],
    },
    {
        "id": "cred", "name": "CRED",
        "list_urls": [
            "https://cred.club/cred-cards",
            "https://cred.club/credit-cards",
        ],
    },
    {
        "id": "jupiter", "name": "Jupiter",
        "list_urls": [
            "https://jupiter.money/edge-csb-bank-rupay-credit-card/",
            "https://jupiter.money/",
        ],
    },
    {
        "id": "fi", "name": "Fi Money",
        "list_urls": [
            "https://fi.money/credit-cards",
            "https://fi.money/features/debit-card",
        ],
    },
    {
        "id": "niyo", "name": "Niyo",
        "list_urls": ["https://www.goniyo.com/"],
    },
    {
        "id": "slice", "name": "slice (North East SFB)",
        "list_urls": ["https://www.sliceit.com/"],
    },
    {
        "id": "uni", "name": "Uni Cards",
        "list_urls": ["https://www.uni.cards/"],
    },
    {
        "id": "kiwi", "name": "Kiwi (RuPay CC on UPI)",
        "list_urls": ["https://gokiwi.in/"],
    },
    {
        "id": "super_money", "name": "Super.money (Flipkart)",
        "list_urls": [
            "https://super.money/",
            "https://super.money/credit-card",
        ],
    },
    {
        "id": "paytm", "name": "Paytm",
        "list_urls": [
            "https://paytm.com/credit-card",
            "https://business.paytm.com/credit-card",
        ],
    },
    {
        "id": "zaggle", "name": "Zaggle",
        "list_urls": ["https://www.zaggle.in/"],
    },
    {
        "id": "open", "name": "Open Money",
        "list_urls": ["https://open.money/"],
    },
    {
        "id": "razorpayx", "name": "RazorpayX",
        "list_urls": ["https://razorpay.com/x/corporate-cards/"],
    },
    {
        "id": "karbon", "name": "Karbon Card",
        "list_urls": ["https://karboncard.com/"],
    },
    {
        "id": "enkash", "name": "EnKash",
        "list_urls": ["https://www.enkash.com/"],
    },
    {
        "id": "elixir", "name": "Elixir",
        "list_urls": ["https://www.elixir.cards/"],
    },
    # ---------- Co-brands ----------
    {
        "id": "tata_neu", "name": "Tata Neu (HDFC co-brand)",
        "list_urls": [
            "https://www.tataneu.com/creditcard/",
            "https://www.tataneu.com/tata-neu-card",
            "https://www.hdfc.bank.in/credit-cards/tata-neu-infinity-hdfc-bank-credit-card",
            "https://www.hdfc.bank.in/credit-cards/tata-neu-plus-hdfc-bank-credit-card",
        ],
    },
    {
        "id": "flipkart_axis", "name": "Flipkart Axis",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/flipkart-axisbank-credit-card",
            "https://www.axis.bank.in/cards/credit-card/flipkart-axis-bank-super-elite-credit-card",
        ],
    },
    {
        "id": "amazon_pay_icici", "name": "Amazon Pay ICICI",
        "list_urls": [
            "https://www.amazon.in/amazonpay/icicibank/creditcard",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/amazon-pay-credit-card",
        ],
    },
    {
        "id": "myntra_kotak", "name": "Myntra Kotak",
        "list_urls": [
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/pvr-inox-kotak-credit-card.html",
        ],
    },
    {
        "id": "swiggy_hdfc", "name": "Swiggy HDFC",
        "list_urls": [
            "https://www.hdfc.bank.in/credit-cards/swiggy-hdfc-bank-credit-card",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/swiggy-hdfc-bank-credit-card",
        ],
    },
    {
        "id": "airtel_axis", "name": "Airtel Axis Bank",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/airtel-axis-bank-credit-card",
        ],
    },
    {
        "id": "unity_sfb", "name": "Unity Small Finance Bank (Roar Bank)",
        "list_urls": [
            "https://roar.bank.in/",
            "https://roar.bank.in/en/about",
        ],
    },
    {
        "id": "indianoil_axis", "name": "IndianOil Axis Bank",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/indianoil-axis-bank-credit-card",
            "https://www.axis.bank.in/cards/credit-card/indianoil-premium-credit-card",
        ],
    },
    {
        "id": "swiggy_axis", "name": "Swiggy Axis Bank",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/axis-bank-ace-credit-card",
        ],
    },
    {
        "id": "bpcl_sbi", "name": "BPCL SBI Card",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/bpcl-sbi-card.html",
            "https://www.sbicard.com/en/personal/credit-cards/bpcl-sbi-card-octane.html",
        ],
    },
    {
        "id": "irctc_sbi", "name": "IRCTC SBI Card",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/irctc-sbi-platinum-card.html",
            "https://www.sbicard.com/en/personal/credit-cards/irctc-premier-card.html",
        ],
    },
    {
        "id": "ola_sbi", "name": "OLA SBI Card",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/ola-money-sbi-card.html",
        ],
    },
    # ---------- Forex ----------
    {
        "id": "bookmyforex", "name": "BookMyForex",
        "list_urls": ["https://www.bookmyforex.com/forex-card/"],
    },
    {
        "id": "scapia", "name": "Scapia (Federal Bank)",
        "list_urls": ["https://www.scapia.cards/"],
    },
    {
        "id": "thomas_cook_forex", "name": "Thomas Cook Forex Card",
        "list_urls": ["https://www.thomascook.in/foreign-exchange"],
    },
    # ---------- PSU banks (additional) ----------
    {
        "id": "bank_of_india", "name": "Bank of India",
        "list_urls": [
            "https://bankofindia.bank.in/credit-card",
            "https://bankofindia.bank.in/debit-card",
        ],
    },
    {
        "id": "bank_of_maharashtra", "name": "Bank of Maharashtra",
        "list_urls": [
            "https://bankofmaharashtra.bank.in/bom-credit-card",
            "https://bankofmaharashtra.bank.in/debit-cards",
        ],
    },
    {
        "id": "central_bank", "name": "Central Bank of India",
        "list_urls": [
            "https://centralbank.bank.in/en/credit-cards",
            "https://centralbank.bank.in/en/debit-cards",
        ],
    },
    {
        "id": "iob", "name": "Indian Overseas Bank",
        "list_urls": [
            "https://www.iob.bank.in/en/credit-card-offers",
            "https://www.iob.bank.in/en/debit-cards",
        ],
    },
    {
        "id": "uco_bank", "name": "UCO Bank",
        "list_urls": [
            "https://www.uco.bank.in/credit-card1",
            "https://www.uco.bank.in/debit-card",
        ],
    },
    {
        "id": "punjab_sind", "name": "Punjab & Sind Bank",
        "list_urls": [
            "https://punjabandsind.bank.in/content/credit-cards",
            "https://punjabandsind.bank.in/content/debit-cards",
        ],
    },
    # ---------- Private banks (additional) ----------
    {
        "id": "karnataka_bank", "name": "Karnataka Bank",
        "list_urls": [
            "https://www.karnatakabank.bank.in/personal/credit-cards",
            "https://www.karnatakabank.bank.in/personal/cards",
        ],
    },
    {
        "id": "city_union_bank", "name": "City Union Bank",
        "list_urls": [
            "https://www.cityunionbank.com/cub-credit-cards-for-individuals",
            "https://www.cityunionbank.com/creditcard",
        ],
    },
    {
        "id": "dcb_bank", "name": "DCB Bank",
        "list_urls": ["https://www.dcb.bank.in/personal/cards"],
    },
    {
        "id": "dhanlaxmi_bank", "name": "Dhanlaxmi Bank",
        "list_urls": ["https://www.dhan.bank.in/credit-card/"],
    },
    {
        "id": "saraswat_bank", "name": "Saraswat Bank",
        "list_urls": ["https://www.saraswat.bank.in/navigation.aspx?id=Credit-Cards"],
    },
    {
        "id": "jk_bank", "name": "J&K Bank",
        "list_urls": ["https://jkb.bank.in/transactions/services/credit-cards"],
    },
    {
        "id": "csb_bank", "name": "CSB Bank",
        "list_urls": [
            "https://www.csb.bank.in/credit-card",
            "https://www.csb.bank.in/csb-bank-edge-credit-card",
        ],
    },
    {
        "id": "dbs_india", "name": "DBS Bank India",
        "list_urls": [
            "https://www.dbs.bank.in/in/credit-cards/default.html",
            "https://www.dbs.bank.in/in/credit-cards/supercard.html",
        ],
    },
    # ---------- Small Finance Banks (additional) ----------
    {
        "id": "equitas_sfb", "name": "Equitas Small Finance Bank",
        "list_urls": ["https://equitas.bank.in/personal-banking/pay/credit-cards/"],
    },
    {
        "id": "suryoday_sfb", "name": "Suryoday Small Finance Bank",
        "list_urls": [
            "https://suryoday.bank.in/personal/cards/credit-cards/secured-credit-card/",
            "https://suryoday.bank.in/personal/cards/credit-cards/secured-ssfb-rupay-select/",
        ],
    },
    {
        "id": "esaf_sfb", "name": "ESAF Small Finance Bank",
        "list_urls": ["https://www.esaf.bank.in/credit-card/"],
    },
    {
        "id": "jana_sfb", "name": "Jana Small Finance Bank",
        "list_urls": ["https://www.jana.bank.in/"],
    },
    {
        "id": "utkarsh_sfb", "name": "Utkarsh Small Finance Bank",
        "list_urls": ["https://www.utkarsh.bank.in/personal/cards/credit-card/wish-credit-card"],
    },
    # ---------- HDFC co-brand / variant detail pages ----------
    {
        "id": "hdfc_pixel", "name": "HDFC PIXEL Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/pixel-play-credit-card",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/pixel-go-credit-card",
        ],
    },
    {
        "id": "indianoil_hdfc", "name": "IndianOil HDFC Bank Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/indianoil-hdfc-bank-credit-card",
        ],
    },
    {
        "id": "irctc_hdfc", "name": "IRCTC HDFC Bank RuPay Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/irctc-hdfc-bank-rupay-credit-card",
        ],
    },
    {
        "id": "club_vistara_hdfc", "name": "Club Vistara HDFC Bank Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/club-vistara-hdfc-bank-credit-card",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/club-vistara-hdfc-bank-signature-credit-card",
        ],
    },
    {
        "id": "marriott_bonvoy_hdfc", "name": "Marriott Bonvoy HDFC Bank Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/marriott-bonvoy-hdfc-bank-credit-card",
        ],
    },
    {
        "id": "eazydiner_hdfc", "name": "EazyDiner HDFC Bank Credit Card",
        "list_urls": [
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/eazydiner-hdfc-bank-credit-card",
        ],
    },
    {
        "id": "phonepe_hdfc", "name": "PhonePe HDFC Bank Credit Card",
        "list_urls": [
            "https://www.hdfc.bank.in/credit-cards/phonepe-hdfc-bank-ultimo-credit-card",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/phonepe-hdfc-bank-ultimo-credit-card",
            "https://www.hdfcbank.com/personal/pay/cards/credit-cards/phonepe-hdfc-bank-uno-credit-card",
        ],
    },
    # ---------- Additional SBI Card co-brands ----------
    {
        "id": "apollo_sbi", "name": "Apollo SBI Card SELECT",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/apollo-sbi-card-select.html",
            "https://www.sbicard.com/en/personal/credit-cards/apollo-sbi-card.html",
        ],
    },
    {
        "id": "club_vistara_sbi", "name": "Club Vistara SBI Card",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/club-vistara-sbi-card.html",
            "https://www.sbicard.com/en/personal/credit-cards/club-vistara-sbi-card-prime.html",
        ],
    },
    {
        "id": "air_india_sbi", "name": "Air India SBI Card",
        "list_urls": [
            "https://www.sbicard.com/en/personal/credit-cards/air-india-sbi-platinum-card.html",
            "https://www.sbicard.com/en/personal/credit-cards/air-india-sbi-signature-card.html",
        ],
    },
    # ---------- Additional ICICI co-brands ----------
    {
        "id": "icici_hpcl", "name": "HPCL ICICI Bank Credit Card",
        "list_urls": [
            "https://www.icici.bank.in/personal-banking/cards/credit-card/hpcl-super-saver",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/hpcl-coral-credit-card",
        ],
    },
    {
        "id": "icici_makemytrip", "name": "MakeMyTrip ICICI Bank Credit Card",
        "list_urls": [
            "https://www.icici.bank.in/personal-banking/cards/credit-card/makemytrip/makemytrip-icici-credit-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/makemytrip/signature-credit-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/makemytrip/platinum-credit-card",
        ],
    },
    {
        "id": "times_black_icici", "name": "Times Black ICICI Bank Credit Card",
        "list_urls": [
            "https://www.icici.bank.in/personal-banking/cards/credit-card/times-black-icici-credit-card",
        ],
    },
    {
        "id": "icici_imobile_payback", "name": "ICICI Bank Coral & Premium Cards",
        "list_urls": [
            "https://www.icici.bank.in/personal-banking/cards/credit-card/coral-credit-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/rubyx-credit-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/sapphiro-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/emeralde-credit-card",
            "https://www.icici.bank.in/personal-banking/cards/credit-card/emeralde-private-metal-credit-card",
        ],
    },
    # ---------- Additional Axis co-brands ----------
    {
        "id": "club_vistara_axis", "name": "Club Vistara Axis Bank Credit Card",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/axis-bank-vistara-credit-card",
            "https://www.axis.bank.in/cards/credit-card/axis-bank-vistara-signature-credit-card",
            "https://www.axis.bank.in/cards/credit-card/axis-bank-vistara-infinite-credit-card",
        ],
    },
    {
        "id": "samsung_axis", "name": "Samsung Axis Bank Credit Card",
        "list_urls": [
            "https://www.axis.bank.in/cards/credit-card/samsung-axis-bank-infinite-credit-card",
            "https://www.axis.bank.in/cards/credit-card/samsung-axis-bank-signature-credit-card",
        ],
    },
    # ---------- Additional IndusInd co-brands ----------
    {
        "id": "club_vistara_indusind", "name": "Club Vistara IndusInd Bank Credit Card",
        "list_urls": [
            "https://www.indusind.bank.in/in/en/personal/cards/credit-card/club-vistara-indusind-bank-credit-card.html",
        ],
    },
    {
        "id": "eazydiner_indusind", "name": "EazyDiner IndusInd Bank Credit Card",
        "list_urls": [
            "https://www.indusind.bank.in/in/en/personal/cards/credit-card/eazydiner-indusind-bank-credit-card.html",
        ],
    },
    # ---------- Additional Kotak co-brands ----------
    {
        "id": "indigo_kotak", "name": "IndiGo Kotak Bank Credit Card",
        "list_urls": [
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/indigo-credit-card.html",
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/indigo-kotak-credit-card.html",
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/indigo-kotak-xl-credit-card.html",
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/indigo-xl-credit-card.html",
        ],
    },
    {
        "id": "pvr_kotak", "name": "PVR INOX Kotak Credit Card",
        "list_urls": [
            "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/pvr-inox-kotak-credit-card.html",
        ],
    },
    # ---------- BOBCARD premium variants ----------
    {
        "id": "bobcard_premium", "name": "BOBCARD Premium Cards (Eterna / Tiitan / Swag)",
        "list_urls": [
            "https://www.bobcard.co.in/credit-card-types/eterna",
            "https://www.bobcard.co.in/credit-card-types/tiitan",
            "https://www.bobcard.co.in/credit-card-types/swag",
            "https://www.bobcard.co.in/credit-card-types/prime",
        ],
    },
    # ---------- Other fintechs ----------
    {
        "id": "stashfin", "name": "StashFin",
        "list_urls": ["https://www.stashfin.com/"],
    },
    {
        "id": "paisabazaar_stepup", "name": "Paisabazaar StepUp Card (SBM Bank)",
        "list_urls": ["https://www.paisabazaar.com/stepupcard/"],
    },
]


def build_records():
    records = []
    seen = set()
    for issuer in ISSUERS:
        iid = issuer["id"]
        iname = issuer["name"]
        for url in issuer.get("list_urls", []):
            # Strip fragment for HTTP request (fragments are client-side)
            clean_url = url.split("#")[0]
            key = (iid, clean_url, "list_url")
            if key in seen:
                continue
            seen.add(key)
            records.append(UrlRecord(
                issuer_id=iid,
                issuer_name=iname,
                url_type="list_url",
                original_url=clean_url,
            ))
        if "sitemap_url" in issuer:
            url = issuer["sitemap_url"]
            key = (iid, url, "sitemap_url")
            if key not in seen:
                seen.add(key)
                records.append(UrlRecord(
                    issuer_id=iid,
                    issuer_name=iname,
                    url_type="sitemap_url",
                    original_url=url,
                ))
    return records


def print_report(results):
    broken = [r for r in results if r.category == "BROKEN"]
    redirected = [r for r in results if r.category == "REDIRECTED"]
    ok = [r for r in results if r.category == "OK"]

    def fmt(r):
        parts = [f"  [{r.issuer_id}] {r.issuer_name} ({r.url_type})"]
        parts.append(f"    URL:    {r.original_url}")
        if r.error:
            parts.append(f"    ERROR:  {r.error}")
        else:
            parts.append(f"    STATUS: {r.status_code}  ({r.elapsed:.1f}s)")
            if r.final_url and r.final_url != r.original_url:
                domain_tag = " [DOMAIN CHANGED]" if r.domain_changed else ""
                parts.append(f"    FINAL:  {r.final_url}{domain_tag}")
            if r.error_page_content:
                parts.append(f"    NOTE:   Error content detected ('{r.matched_phrase}')")
        return "\n".join(parts)

    sep = "=" * 80

    print(sep)
    print(f"URL HEALTH CHECK REPORT  —  {len(results)} URLs checked")
    print(sep)
    print(f"  BROKEN:     {len(broken)}")
    print(f"  REDIRECTED: {len(redirected)}")
    print(f"  OK:         {len(ok)}")
    print()

    print(sep)
    print(f"BROKEN ({len(broken)})")
    print(sep)
    if broken:
        for r in broken:
            print(fmt(r))
            print()
    else:
        print("  (none)")
        print()

    print(sep)
    print(f"REDIRECTED ({len(redirected)})")
    print(sep)
    if redirected:
        for r in redirected:
            print(fmt(r))
            print()
    else:
        print("  (none)")
        print()

    print(sep)
    print(f"OK ({len(ok)})")
    print(sep)
    if ok:
        for r in ok:
            print(fmt(r))
            print()
    else:
        print("  (none)")
        print()


def main():
    records = build_records()
    print(f"Checking {len(records)} URLs with {WORKERS} workers...", file=sys.stderr)
    print(file=sys.stderr)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(check_url, r): r for r in records}
        done_count = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            done_count += 1
            status = result.error or str(result.status_code)
            cat = result.category
            print(f"  [{done_count:>3}/{len(records)}] {cat:>10}  {status:<15}  {result.original_url}", file=sys.stderr)

    print(file=sys.stderr)
    print_report(results)


if __name__ == "__main__":
    main()
