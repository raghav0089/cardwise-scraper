"""Rule-based card data extractor for Jina-rendered markdown.

Handles both:
  - Listing pages  (e.g. /credit-cards)   → multiple cards
  - Product pages  (e.g. /regalia)        → single card with full detail
"""
from __future__ import annotations
import re, logging
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Known reward point / mile values (INR per unit)
# Used as fallback when the page doesn't state the value.
# ─────────────────────────────────────────────────────────────
_KNOWN_POINT_VALUE: dict[str, float] = {
    "hdfc":             0.25,   # 1 RP = ₹0.25 (standard); Infinia/Diners can be ₹1
    "icici":            0.25,
    "sbi_card":         0.25,
    "kotak":            0.25,
    "axis":             0.20,   # 1 eDGE Mile = ₹0.20
    "indusind":         1.00,   # 1 IndusMiles = ₹1
    "rbl":              0.25,
    "yes":              0.25,
    "idfc_first":       0.25,
    "federal":          0.25,
    "au_sfb":           0.25,
    "standard_chartered": 0.25,
    "hsbc":             0.25,
    "amex":             0.50,   # 1 MR ≈ ₹0.50 (varies heavily by redemption)
    "onecard":          0.10,   # 1FC points, value varies
    "scapia":           0.50,   # Scapia coins ≈ ₹0.50
}

# ─────────────────────────────────────────────────────────────
# Reward currency names used by each issuer
# ─────────────────────────────────────────────────────────────
_ISSUER_CURRENCY: dict[str, str] = {
    "hdfc":    "Reward Points",
    "icici":   "Reward Points",
    "sbi_card":"Reward Points",
    "kotak":   "Kotak Points",
    "axis":    "eDGE Miles",
    "indusind":"IndusMiles",
    "rbl":     "Reward Points",
    "amex":    "Membership Rewards",
    "onecard": "1FC Points",
    "scapia":  "Scapia Coins",
    "idfc_first": "Reward Points",
}


# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────

_LAKH_RE  = re.compile(r"\blakh|\blac\b", re.I)
_CRORE_RE = re.compile(r"\bcrore|\bcr\b", re.I)

def _num(s: str, suffix: str = "") -> Optional[float]:
    """Parse a number, handling Indian lakh/crore suffixes in `suffix`."""
    try:
        n = float(re.sub(r"[^\d.]", "", s))
    except Exception:
        return None
    if _LAKH_RE.search(suffix):
        n *= 100_000
    elif _CRORE_RE.search(suffix):
        n *= 10_000_000
    return n

def _ctx(md: str, m: re.Match, before: int = 60, after: int = 200) -> str:
    return md[max(0, m.start() - before): m.end() + after].replace("\n", " ").strip()

# Jina adds "Title: ...\nURL Source: ...\nMarkdown Content:\n" at the start of every page
_JINA_HEADER_RE = re.compile(
    r"^(?:Title|URL Source|Markdown Content)\s*:[^\n]*\n?", re.I | re.MULTILINE)

def _clean_text(text: str, limit: int = 250) -> str:
    """Strip Jina headers, images, URLs, markdown noise from a display snippet."""
    text = _JINA_HEADER_RE.sub("", text)
    text = re.sub(r'!\[[^\]\n]*\]?(?:\([^)]*\))?', ' ', text)  # ![alt](url) image, closed or not
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)   # [label](url) → label
    # Orphan markdown fragments left by snippet-window truncation
    text = re.sub(r'\[[^\]]*$', ' ', text)                 # trailing unclosed [label
    text = re.sub(r'\]\([^)]*\)?', ' ', text)              # orphan ](url) or ](url
    text = re.sub(r'\S*\.(?:pdf|html|aspx|php|jpg|png|webp)\)?', ' ', text, flags=re.I)  # orphan file refs
    text = re.sub(r'\(\s*https?\S*\)?', ' ', text)         # orphan (url / (url)
    text = re.sub(r'^\s*\S{0,12}\)\s+', '', text)          # leading "pdf) " orphan from prev link
    text = re.sub(r'\(opens?\s+in\s+a\s+new\s+tab\)?', ' ', text, flags=re.I)
    text = re.sub(r'\b(?:click\s+here|view\s+(?:more|less|details)|know\s+more|'
                  r'read\s+more|learn\s+more)\b', ' ', text, flags=re.I)
    text = re.sub(r'https?://\S+', '', text)                # bare URLs
    text = re.sub(r'[*_`#|]', ' ', text)                   # markdown punctuation
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[\s:>\-–—]+', '', text)               # leading list/heading residue
    if len(text) > limit:                                  # cut at a word boundary
        cut = text[:limit]
        sp  = cut.rfind(" ")
        text = (cut[:sp] if sp > limit * 0.6 else cut).rstrip()
    return text.strip(" :-–—[(")

_GENERIC_PREFIX_RE = re.compile(
    r"^(?:apply\s+for|get|best|find|compare|about|explore|discover|learn)\s+", re.I)
# Lowercase article "the" followed by a non-specific adjective — signals a marketing slogan
# e.g., "the Luxury Credit Card", "the Best Airline Credit Card"
_MARKETING_ARTICLE_RE = re.compile(
    r"^the\s+(?:best|luxury|premium|ultimate|perfect|right|ideal|only|most|top|first|new)\s",
    re.I,
)

def _clean_card_name(raw: str) -> str:
    """Turn a noisy Jina/SEO title into a clean card product name."""
    # [Card Name](url) → Card Name
    val = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', raw).strip()
    # Strip sub-page heading prefixes that leak in ("Fees and Charges of X Card",
    # "Top Benefits of X Card", "Eligibility for X Card", "Features of X Card").
    val = re.sub(
        r'^(?:top\s+|key\s+|all\s+|more\s+|exclusive\s+)?'
        r'(?:fees?\s+(?:and|&)\s+charges?|eligibility|features?|benefits?|'
        r'rewards?|redemption|terms?\s+(?:and|&)\s+conditions?|about)\s+(?:of|for|on)\s+',
        '', val, flags=re.I).strip()
    # Strip "| Bank Name" suffix  (Jina Title: format)
    val = re.sub(r'\s*\|.*$', '', val).strip()
    # Strip " - <Bank/Finance/Financial Name>" suffix (common in page titles)
    val = re.sub(r'\s+[-–]\s+(?:[A-Za-z\s]+\s+)?(?:Bank|Finance|Financial|Fin\b)\s*$', '', val, flags=re.I).strip()
    # Strip trailing " Online" or "Online -" suffixes
    val = re.sub(r'\s+[-–]\s+(?:Apply\s+)?Online\s*$', '', val, flags=re.I).strip()
    val = re.sub(r'\s+Online\s*$', '', val, flags=re.I).strip()
    # "Short Label: Full Card Name" — take the more specific (usually longer) part
    if ':' in val:
        before, after = val.split(':', 1)
        before = before.strip()
        after  = _GENERIC_PREFIX_RE.sub('', after.strip())
        b_card = bool(re.search(r'credit|debit|prepaid|forex|card', before, re.I))
        a_card = bool(re.search(r'credit|debit|prepaid|forex|card', after,  re.I))
        if a_card and (not b_card or len(after) > len(before)):
            val = after
        elif b_card:
            val = before
    # "Something - Apply for Actual Card Name [Online]" → extract the real card name
    m = re.search(r'[-–]\s*(?:apply\s+for|get\s+(?:a\s+)?|best\s+)\s*(.+)', val, re.I)
    if m:
        candidate = _GENERIC_PREFIX_RE.sub('', m.group(1).strip())
        candidate = re.sub(r'\s+(?:online|now|today|here)$', '', candidate, flags=re.I).strip()
        if re.search(r'credit|debit|prepaid|forex|card', candidate, re.I) and len(candidate) > 5:
            val = candidate
    # Strip " - tagline" when part before dash is already the card name (min 10 chars)
    if '-' in val or '–' in val:
        m = re.match(r'^(.{10,60}(?:credit|debit|prepaid|forex|card))\s*[-–].+$', val, re.I)
        if m and len(m.group(1)) < len(val) - 5:
            val = m.group(1).strip()
    # Strip trailing description suffixes that are NOT part of the product name
    val = re.sub(
        r'\s+(?:–|-|&|and\s+)?\s*(?:enjoy|get|earn|save)\s+[^|]+$', '', val, flags=re.I).strip()
    val = re.sub(
        r'\s+(?:rewards?\s+(?:and|&)\s+benefits?|benefits?\s+(?:and|&)\s+(?:rewards?|features?)|'
        r'features?\s+(?:and|&)\s+benefits?|offers?\s+(?:and|&)\s+benefits?)\s*$',
        '', val, flags=re.I).strip()
    # Strip "With [adjective] Benefits/Rewards/Offers" marketing suffix
    val = re.sub(
        r'\s+with\s+(?:unlimited|exclusive|unmatched|extraordinary|great|premium|amazing)\s+'
        r'(?:benefits?|offers?|rewards?|privileges?)\s*$',
        '', val, flags=re.I).strip()
    # Truncate SEO tails that follow the card-type keyword:
    # "Avios Visa Infinite Credit Card Online - Check Benefits & Rewards" → "Avios ... Credit Card"
    m = re.match(
        r'^(.*?\b(?:credit|debit|prepaid|forex)\s+card)\b\s+'
        r'(?:online|apply|check|benefits?|rewards?|features?|eligibilit\w*|fees?|charges?|'
        r'offers?|details?|now|today|—|-|\|)',
        val, re.I)
    if m and len(m.group(1)) >= 8:
        val = m.group(1).strip()
    # Strip "in the [Industry/Market/Country]" SEO tail
    val = re.sub(r'\s+in\s+(?:the\s+)?(?:industry|market|country|world|india)\s*$', '', val, flags=re.I).strip()
    # Reject slug-like names (hyphens, no spaces) that Jina sometimes produces from link text
    if re.match(r'^[a-z0-9]+(-[a-z0-9]+)+$', val):
        return ""
    # If the remaining name starts with a marketing article ("the Luxury/Best/..."), signal
    # rejection by returning empty string — the caller falls back to the next heading or URL slug.
    if _MARKETING_ARTICLE_RE.match(val):
        return ""
    return val.strip()


# ─────────────────────────────────────────────────────────────
# Multi-card page detection & splitting
# ─────────────────────────────────────────────────────────────

# Headings that name a card product.
# Handles both plain headings (## Regalia Credit Card)
# and Jina markdown link headings (## [Regalia Credit Card](url))
_CARD_HDR = re.compile(
    r"^#{2,3}\s+"                                  # ## or ###
    r"(?:\[)?"                                     # optional opening [
    r"([^\]\n]{5,100}"                             # card name (no ] or newline)
    r"(?:credit|debit|prepaid|forex|card|miles|rupay)"  # must contain a card keyword
    r"[^\]\n]{0,80})"                              # rest of card name
    r"(?:\]\([^)]*\))?",                           # optional ](url) — not captured
    re.I | re.MULTILINE,
)

def _split_sections(md: str) -> list[tuple[str, str]]:
    """Split a listing page into (card_name, markdown_section) pairs.

    Returns [] if the page looks like a single product page.
    """
    matches = list(_CARD_HDR.finditer(md))
    # Need at least 2 card-name headings to be a listing page
    if len(matches) < 2:
        return []
    sections = []
    for i, m in enumerate(matches):
        name  = m.group(1).strip()
        # Strip any residual markdown link syntax inside the name
        name  = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', name).strip()
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections.append((name, md[start:end]))
    return sections


# ─────────────────────────────────────────────────────────────
# Card name
# ─────────────────────────────────────────────────────────────

_NAME_PATS = [
    re.compile(r"^Title:\s*(.+)", re.I | re.MULTILINE),
    re.compile(r"^#\s+(.{5,100})", re.MULTILINE),
    re.compile(r"^##\s+(.{5,80})", re.MULTILINE),
]
# Headings that are navigation/marketing, NOT individual card product names
_GENERIC_NAME_RE = re.compile(
    r"^(?:get|find|apply|compare|about|faq|faqs|blog|blogs|know|choose|check|"
    r"explore|discover|learn|why|what|how|tips|type|top\s+\d|all\s+credit|"
    r"more|see|view|contact|news|media|press|career|privacy|terms|site|"
    r"eligib|calculat|offers?\s+on|offers\s*$|reward\s+program|reward\s+point|"
    r"interest\s+rate|annual\s+fee|joining\s+fee|customer\s+care|"
    r"frequently\s+asked|important\s+information|"
    # Generic section headings, not card product names
    r"card\s+benefits?\b|card\s+features?\b|benefits?\s+(?:and|&)\s+features?\b|"
    r"features?\s+(?:and|&)\s+benefits?\b|"
    # Marketing slogans used as page headings by HDFC and others ("the Luxury Credit Card",
    # "the Best Airline Credit Card", "Enjoy Cashback with Spends on...")
    r"enjoy\s+|save\s+(?:more|big|on)|earn\s+(?:more|reward|cashback)|"
    r"maximize?|maximise\s+|"
    r"the\s+(?:best|luxury|premium|ultimate|perfect|right|ideal|only|most)\s|"
    r"(?:india[''']?s|your)\s+(?:best|top|#1)|unlock\s+|experience\s+the)\b",
    re.I,
)

_BAD_NAME_INLINE = re.compile(
    r"log\s*in|sign\s*in|menu\b|nav\b|calculat|savings\s+calculator|"
    r"find\s+what|links\s+below|skip\s+to|use\s+the",
    re.I,
)

def _name(md: str) -> Optional[str]:
    for pat in _NAME_PATS:
        for m in pat.finditer(md):   # try ALL matches, not just the first
            val = _clean_card_name(m.group(1).strip())
            if not val:
                continue
            if _GENERIC_NAME_RE.search(val):
                continue
            if _BAD_NAME_INLINE.search(val):
                continue
            if 5 < len(val) < 120:
                return val
    return None


# ─────────────────────────────────────────────────────────────
# Name from URL slug (fallback)
# ─────────────────────────────────────────────────────────────

_SLUG_ABBREVS = frozenset({"hdfc", "sbi", "icici", "upi", "emi", "idfc", "rbl", "rrb",
                            "nri", "bsl", "irctc", "bpcl", "iocl", "hpcl", "au", "sc"})
_SLUG_PROPER  = {"rupay": "RuPay", "amex": "Amex", "payback": "PAYBACK",
                 "mmb": "MMB", "nykaa": "Nykaa", "swiggy": "Swiggy"}

def _name_from_url(url: str) -> Optional[str]:
    """Derive a clean card name from a product-page URL slug.

    /credit-cards/millennia-credit-card                          → 'Millennia Credit Card'
    /credit-cards/tata-card-apply-for-tata-neu-plus-credit-card → 'Tata Neu Plus Credit Card'
    """
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if not slug or not re.search(r"card|miles|rupay|cashback", slug, re.I):
        return None
    # Strip SEO junk: "some-brand-apply-for-actual-card-name" → keep everything after "apply-for-"
    slug = re.sub(r"^.*?apply-for-", "", slug, flags=re.I)
    # Strip trailing modifiers that aren't part of the product name
    slug = re.sub(r"\.(page|html|aspx|php)$", "", slug, flags=re.I)
    slug = re.sub(r"-(online|now|today|here|quickly|india|new)$", "", slug, flags=re.I)
    if not re.search(r"card|miles|rupay|cashback", slug, re.I):
        return None
    parts = slug.split("-")
    result = []
    for p in parts:
        pl = p.lower()
        if pl in _SLUG_ABBREVS:
            result.append(p.upper())
        elif pl in _SLUG_PROPER:
            result.append(_SLUG_PROPER[pl])
        else:
            result.append(p.capitalize())
    name = " ".join(result)
    return name if 5 < len(name) < 100 else None


# ─────────────────────────────────────────────────────────────
# Category / network / segment
# ─────────────────────────────────────────────────────────────

_CAT_RE = {
    "credit":  re.compile(r"credit\s+card", re.I),
    "debit":   re.compile(r"debit\s+card", re.I),
    "prepaid": re.compile(r"prepaid\s+card", re.I),
    "forex":   re.compile(r"forex\s+card|multi.?currency\s+card", re.I),
}
_NET_RE   = re.compile(r"\b(Visa|Mastercard|RuPay|Amex|American\s+Express|Diners)\b", re.I)
_NET_NORM = {
    "american express": "Amex", "amex": "Amex",
    "visa": "Visa", "mastercard": "Mastercard",
    "rupay": "RuPay", "diners": "Diners",
}
_SEG_RE = {
    "super-premium": re.compile(r"super.?premium|ultra.?premium", re.I),
    "premium":       re.compile(r"\bpremium\b", re.I),
    "student":       re.compile(r"\bstudent\b", re.I),
    # Require "secured card/credit" or "against FD" — avoid false hits on "secure payment"
    "secured":       re.compile(r"secured\s+(?:credit\s+)?card|against\s+(?:fd|fixed\s+deposit)", re.I),
    "entry":         re.compile(r"\bentry.?level\b|\bbasic\b", re.I),
}


# ─────────────────────────────────────────────────────────────
# Fees
# ─────────────────────────────────────────────────────────────

_INR = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)(?!\s*[xX%])"
# Extended pattern that also captures lakh/crore suffix (group 2) for spend thresholds
_INR_LAKH = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore|cr\b)?"
# Fee amounts require ₹/rs/inr prefix — prevents reward multiplier numbers (4X, 2 Air Miles)
# being captured as fees when the actual fee is "Nil" followed by reward descriptions.
_INR_FEE = r"(?:₹|rs\.?\s*|inr\s*)([\d,]+(?:\.\d+)?)(?!\s*[xX%])"
# Bridge between a fee label and its amount. Skips a parenthetical that may itself
# contain digits ("(2nd Year Onwards)", "(1st year)") as a unit, but otherwise won't
# cross loose digits — so it binds the amount belonging to *this* label. Lazy.
_FEEBRIDGE = r"(?:\([^)\n]*\)|[^₹\d\n]){0,45}?"
_FEE_PATS = [
    # Negative lookaheads prevent "fee reversal/waiver" phrasing from matching the actual fee field
    # "Joining/Renewal Membership Fee" — allow "/" between Joining and Renewal
    ("joining_fee_inr",       re.compile(r"join(?:ing)?[\s/]*(?:or\s+|renewal\s+)?(?:membership\s+)?fee(?!\s+(?:reversal|waiver|waived?))" + _FEEBRIDGE + _INR_FEE, re.I)),
    ("annual_fee_inr",        re.compile(r"annual\s*(?:fee|membership)(?!\s+(?:reversal|waiver|waived?))" + _FEEBRIDGE + _INR_FEE, re.I)),
    ("renewal_fee_inr",       re.compile(r"renewal\s*(?:membership\s+)?fee(?!\s+(?:reversal|waiver|waived?))" + _FEEBRIDGE + _INR_FEE, re.I)),
    # fee_waiver uses _INR_LAKH (2 groups) to handle "1 Lakh" amounts; "fee reversal" = fee waiver
    ("fee_waiver_spend_inr",  re.compile(
        r"(?:fee\s+waiver|fee\s+waived?|waived?\s+(?:on|by)\s+(?:annual\s+|spending\s+)?spend|"
        r"annual\s+spend[s]?\s+of|waiv\w+\s+fee|fee\s+reversal\s+on\s+(?:annual\s+)?spend)[^₹\d\n]{0,80}" + _INR_LAKH, re.I)),
    ("addon_card_fee_inr",    re.compile(r"(?:add.?on|supplementary)\s*card\s*fee[^₹\d\n]{0,40}" + _INR, re.I)),
    ("cash_advance_fee_pct",  re.compile(r"cash\s*advance\s*fee[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("finance_charge_pct_mo", re.compile(r"(?:finance\s*charge|monthly\s*interest|interest\s*rate)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("fx_markup_pct",         re.compile(r"(?:forex|foreign\s*currency|cross.?currency)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
]
# "Nil"/free fee statements → capture as ₹0 (e.g. Fleet: "Joining/Renewal Fees: Nil").
_NIL_JOIN_RE = re.compile(
    r"join(?:ing)?[\s/]*(?:or\s+|renewal\s+)?(?:membership\s+)?fees?\s*[:\-–]?\s*(?:is\s+)?"
    r"(?:nil|free|zero|waived|₹?\s*0\b|rs\.?\s*0\b)|"
    r"(?:no|nil|zero)\s+joining\s+fee|does\s+not\s+charge\s+joining", re.I)
_NIL_RENEW_RE = re.compile(
    r"(?:annual|renewal|membership)\s*fees?\s*[:\-–]?\s*(?:is\s+)?"
    r"(?:nil|free|zero|waived|₹?\s*0\b|rs\.?\s*0\b)|"
    r"(?:no|nil|zero)\s+(?:annual|renewal)\s+fee|does\s+not\s+charge\s+(?:joining\s+and\s+)?renewal", re.I)
_LIFETIME_FREE_RE = re.compile(r"lifetime\s+free|life\s*time\s+free|free\s+for\s+life", re.I)
# Forex markup stated either order: "3.5% forex markup" OR "Markup of 3.5% on foreign currency"
_FX_MARKUP_RE = re.compile(
    r"(?:forex|foreign\s*currency|cross.?currency|markup|mark-up|foreign\s+exchange)"
    r"[^%\d\n]{0,30}([\d.]+)\s*%|"
    r"([\d.]+)\s*%[^.\n]{0,25}(?:forex|foreign\s+currency|markup|mark-up|cross.?currency)", re.I)
_FUEL_RE = re.compile(r"fuel\s+surcharge\s+waiver[^.\n]{0,120}", re.I)
_GST_RE  = re.compile(r"\+\s*gst|exclusive\s+of\s+gst|plus\s+applicable\s+taxes", re.I)
# "Spend ₹2L or more in a year ... get your renewal fee waived" — amount precedes the waiver keyword
_FEE_WAIVER_REV = re.compile(
    r"(?:spend|spends?)\s+" + _INR_LAKH + r"[^.\n]{0,80}(?:fee\s+waiv|renewal\s+fee\s+waiv|annual\s+fee\s+waiv)",
    re.I,
)

# Fields that use _INR_LAKH (2 groups: amount, suffix)
_TWO_GROUP_FIELDS = frozenset({"fee_waiver_spend_inr"})

def _fees(md: str) -> dict:
    out: dict = {}
    for key, pat in _FEE_PATS:
        if m := pat.search(md):
            if key in _TWO_GROUP_FIELDS:
                try:
                    suffix = m.group(2) or ""
                except IndexError:
                    suffix = ""
                val = _num(m.group(1), suffix)
                # fee_waiver_spend_inr should always be a meaningful spend amount (≥ ₹1000).
                # Guard against list-item numbers like "1" matching the optional-₹ pattern.
                if key == "fee_waiver_spend_inr" and val is not None and val < 1000:
                    continue
                out[key] = val
            else:
                out[key] = _num(m.group(1))
    # Reversed fee-waiver pattern ("Spend ₹2L or more ... fee waived") — fill if not found above
    if "fee_waiver_spend_inr" not in out:
        if m := _FEE_WAIVER_REV.search(md):
            val = _num(m.group(1), m.group(2) or "")
            if val is not None and val >= 1000:
                out["fee_waiver_spend_inr"] = val
    if "fx_markup_pct" not in out:
        if m := _FX_MARKUP_RE.search(md):
            v = _num(m.group(1) or m.group(2))
            if v is not None and 0 < v <= 10:        # plausible markup range
                out["fx_markup_pct"] = v
    if m := _FUEL_RE.search(md):
        out["fuel_surcharge_waiver"] = _clean_text(m.group(0))
    if _GST_RE.search(md):
        out["gst_extra"] = True
    # "Nil" / free fees: many cards state "Joining/Renewal Fee: Nil" or are "Lifetime
    # Free". Capture these as ₹0 instead of leaving the field blank.
    if "joining_fee_inr" not in out:
        if _NIL_JOIN_RE.search(md):
            out["joining_fee_inr"] = 0
    if "annual_fee_inr" not in out and "renewal_fee_inr" not in out:
        if _NIL_RENEW_RE.search(md):
            out["renewal_fee_inr"] = 0
    if _LIFETIME_FREE_RE.search(md):
        out.setdefault("joining_fee_inr", 0)
        out.setdefault("renewal_fee_inr", 0)
    return out


# ─────────────────────────────────────────────────────────────
# Rewards  —  normalise everything to base_rate_pct (% of spend)
# ─────────────────────────────────────────────────────────────

# Reward currency unit names — broad, NOT just "points": 6E Rewards, CashPoints,
# NeuCoins, EDGE Miles, Reward Points, Cashback, Coins, Miles, etc.
_RUNIT = (r"(?:reward\s*points?|cash\s*points?|cashpoints?|neu\s*coins?|neucoins?|"
          r"edge\s*miles?|indus\s*miles?|membership\s+rewards?|kotak\s+points?|"
          r"reward\s*miles?|rewards?|points?|coins?|miles?|cash\s*back|cashback)")
# "N <unit> per ₹X [Spends] on <category>" — generic earn-rate line.
# Groups: 1=rate, 2=unit, 3=spend, 4=trailing category text (optional)
_EARN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s+(?:[\w]+\s+){0,2}?(" + _RUNIT + r")"
    r"\s+(?:per|for\s+every|on\s+every|/)\s*₹?\s*(?:rs\.?\s*)?([\d,]+)"
    r"([^.\n]{0,80})?", re.I)
# Legacy points-per-spend (kept for back-compat callers)
_RP_PER_SPEND = re.compile(
    r"(\d+)\s+(?:\w+\s+){0,3}points?\s+(?:per|for\s+every|on\s+every)\s+[₹rs.]*\s*([\d,]+)", re.I)
# Does the trailing text mean the BASE rate (all spends) rather than a category?
_BASE_CAT_RE = re.compile(r"\b(all\s+(?:other\s+)?(?:spends?|retail|purchases?)|other\s+spends?|everywhere|every\s+spend)\b", re.I)
# N% cashback on all spends (base rate)
_CASHBACK_ALL = re.compile(r"([\d.]+)\s*%\s*cash\s*back\s+on\s+all", re.I)
# N% cashback / reward on [specific category]  — also catches "5% on Swiggy"
_CASHBACK_CAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:cash\s*back|cashback|reward\s+points?|rewards?)?"
    r"\s+on\s+((?!all\b)[\w\s&/,+.\-]{3,60}?)(?=\s*[,.()\n*]|$)", re.I)
# NX multiplier: "5X reward points on dining"
_MULTIPLIER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX]\s+(?:reward\s+)?points?\s+on\s+([\w\s&/,\-]{3,60}?)(?=[,.()\n]|$)", re.I)
# Cap on category rewards: "up to ₹X per month/year"
_CAP_RE        = re.compile(r"(?:up\s+to|max(?:imum)?)\s+[₹rs.]*\s*([\d,]+)\s*(?:per\s+(?:month|quarter|year))?", re.I)
_POINT_VAL     = re.compile(
    r"1\s+(?:[\w]+\s+){0,3}(?:point|mile|rp)\s*[=:]\s*[₹rs.]*\s*([\d.]+)", re.I)
_CURR_RE       = re.compile(
    r"\b(reward\s+points?|cashback|edge\s+miles?|indus\s*miles?|neucoins|"
    r"membership\s+rewards?|kotak\s+points?|cred\s+coins|1fc\s+points?|scapia\s+coins?)\b", re.I)
_EXCL_RE       = re.compile(
    r"(?:no\s+rewards?|not\s+earn(?:ed)?|excluded?)\s+on\s+([\w\s,&/]+?)(?=[.\n]|$)", re.I)
_REDEEM_RE     = re.compile(r"redeem[^\n]*?(?:for|at|against)\s+([\w\s,&/\-]+?)(?=[.\n])", re.I)
# A genuine redemption destination mentions one of these.
_REDEEM_SIG    = re.compile(
    r"statement|cash\s*back|cashback|flight|hotel|travel|voucher|product|catalogue|"
    r"amazon|smartbuy|gift|merchandise|airmiles|air\s+miles|points?\s+transfer|"
    r"charity|fuel|recharge|bill", re.I)
_EXPIRY_RE     = re.compile(r"(?:points?\s+(?:expire|valid)\s+for\s+)?(\d+)\s*months?\s+(?:from|of)", re.I)

def _rewards(md: str, issuer_id: Optional[str] = None) -> dict:
    out: dict = {}

    # Point value — page-stated first, then issuer default
    pv: Optional[float] = None
    if m := _POINT_VAL.search(md):
        pv = _num(m.group(1))
        out["point_value_inr"] = pv
    elif issuer_id and issuer_id in _KNOWN_POINT_VALUE:
        pv = _KNOWN_POINT_VALUE[issuer_id]
        out["point_value_inr"] = pv

    # ── Generic earn-rate engine: "N <unit> per ₹X [on <category>]" ──────────────
    # Handles 6E Rewards / CashPoints / NeuCoins / Miles / Cashback, not just "points".
    earn_acc, earn_seen, earn_unit = [], set(), None
    for m in _EARN_RE.finditer(md):
        rate, unit, spend = float(m.group(1)), m.group(2), _num(m.group(3))
        trailing = (m.group(4) or "").strip(" :*-–").strip()
        if not spend or spend <= 0:
            continue
        earn_unit = earn_unit or unit
        # % value of this line: cashback units are already %; point/reward units use pv.
        if re.search(r"cash\s*back", unit, re.I):
            pct = round(rate / spend * 100, 4) if spend >= 100 else rate
        else:
            pct = round((pv or 0.25) * rate / spend * 100, 4)
        # Strip lead-in ("spent on", "Spends on", "on") and any cap/parenthetical tail.
        cat = re.sub(r"^(?:spent\s+on|spends?\s+on|spent|on|for)\s+", "", trailing, flags=re.I)
        cat = re.split(r"\bcapped\b|\bup\s+to\b|\bsubject\s+to\b|\(", cat, flags=re.I)[0].strip(" .,*&-/")
        # Is what remains a real category, or just generic spend words → base rate?
        meaningful = re.sub(
            r"\b(all|other|your|the|retail|spends?|spent|purchases?|transactions?|"
            r"everywhere|every|category|categories|merchant)\b", "", cat, flags=re.I).strip(" .,*&-/")
        if not trailing or _BASE_CAT_RE.search(trailing) or len(meaningful) < 3:
            out.setdefault("base_rate_pct", pct)
        elif cat.lower() not in earn_seen and len(cat) < 70:
            earn_seen.add(cat.lower())
            cap_m = _CAP_RE.search(trailing)
            earn_acc.append({"category": cat, "rate_pct": pct,
                             "cap_inr": _num(cap_m.group(1)) if cap_m else None,
                             "notes": f"{m.group(1)} {unit.strip()} per ₹{m.group(3)}"})

    # Currency name — prefer the unit actually used in earn lines, then _CURR_RE, then issuer default
    if earn_unit and not re.search(r"cash\s*back", earn_unit, re.I):
        out["currency"] = re.sub(r"\s+", " ", earn_unit).strip().title()
    elif m := _CURR_RE.search(md):
        out["currency"] = re.sub(r"\s+", " ", m.group(1)).strip().title()
    elif issuer_id and issuer_id in _ISSUER_CURRENCY:
        out["currency"] = _ISSUER_CURRENCY[issuer_id]

    # Base rate — flat cashback on all spends (fallback if earn engine didn't set it)
    if not out.get("base_rate_pct"):
        if m := _CASHBACK_ALL.search(md):
            out["base_rate_pct"] = float(m.group(1))
            out.setdefault("currency", "Cashback")

    # Accelerated / per-category rates (seed with earn-engine results)
    acc, seen = list(earn_acc), set(earn_seen)

    # Cashback / reward % per category
    for m in _CASHBACK_CAT.finditer(md):
        rate_str = m.group(1)
        cat      = m.group(2).strip().rstrip(".,").strip()
        if not cat or len(cat) < 3 or len(cat) > 70 or cat.lower() in seen:
            continue
        # Skip redemption cap lines ("Redeem up to 70% on travel bookings") —
        # these state max % redeemable with points, not an earn rate
        ctx_before = md[max(0, m.start() - 80) : m.start()]
        if re.search(r"\bredeem\b", ctx_before, re.I):
            continue
        seen.add(cat.lower())
        # look for cap in the next 120 chars
        cap_m = _CAP_RE.search(md, m.end(), m.end() + 120)
        cap   = _num(cap_m.group(1)) if cap_m else None
        acc.append({"category": cat, "rate_pct": float(rate_str),
                    "cap_inr": cap, "notes": None})

    # Multiplier: NX points on category
    for m in _MULTIPLIER_RE.finditer(md):
        mult = float(m.group(1))
        cat  = m.group(2).strip().rstrip(".,").strip()
        if not cat or cat.lower() in seen:
            continue
        seen.add(cat.lower())
        base = out.get("base_rate_pct") or 0
        acc.append({"category": cat, "rate_pct": round(mult * base, 4) if base else None,
                    "cap_inr": None, "notes": f"{mult}X points"})

    if acc:
        # Deduplicate: drop an entry if same rate_pct and any key word in its category
        # already appears in a kept entry (handles "5% on Amazon" vs "5% on leading brands - Amazon").
        deduped: list[dict] = []
        kept_tokens: list[tuple[float | None, set[str]]] = []  # (rate_pct, word_set)
        stop = {"on", "at", "the", "a", "an", "and", "or", "of", "in", "for", "to",
                "with", "across", "select", "all", "up"}
        for entry in acc:
            rate = entry.get("rate_pct")
            words = {w.lower() for w in re.split(r"\W+", entry["category"]) if len(w) > 2} - stop
            duplicate = any(
                rate == kept_rate and bool(words & kept_words)
                for kept_rate, kept_words in kept_tokens
            )
            if not duplicate:
                deduped.append(entry)
                kept_tokens.append((rate, words))
        # Drop misleading entries: no rate, or a category that's only generic spend
        # words ("spent", "all retail spends"). The raw text lives in highlights[].
        clean = []
        for e in deduped:
            if e.get("rate_pct") is None:
                continue
            meaningful = re.sub(
                r"\b(all|other|your|the|retail|spends?|spent|purchases?|transactions?|"
                r"everywhere|every|category|categories|merchant|on|at|for|/-)\b",
                "", e["category"], flags=re.I).strip(" .,*&-/")
            if len(meaningful) < 3:
                continue
            clean.append(e)
        if clean:
            out["accelerated"] = clean

    # Exclusions & redemption
    excl = [m.group(1).strip() for m in _EXCL_RE.finditer(md)]
    if excl:
        out["exclusions"] = excl
    # A real redemption mode names a destination (statement, cashback, flights,
    # vouchers, Amazon Pay, catalogue, etc.) — filter out prose like "the push of a button".
    modes = []
    for m in _REDEEM_RE.finditer(md):
        mode = _clean_text(m.group(1)).strip().rstrip(".,")
        if mode and _REDEEM_SIG.search(mode) and 3 <= len(mode) <= 60:
            modes.append(mode)
    if modes:
        # de-dup preserving order
        seen_m, uniq_m = set(), []
        for x in modes:
            if x.lower() not in seen_m:
                seen_m.add(x.lower()); uniq_m.append(x)
        out["redemption_modes"] = uniq_m[:8]
    if m := _EXPIRY_RE.search(md):
        out["expiry_months"] = int(m.group(1))

    return out


# ─────────────────────────────────────────────────────────────
# Welcome benefit
# ─────────────────────────────────────────────────────────────

_WELCOME_RE = re.compile(
    r"(?:"
    r"(?:welcome|joining)\s+(?:benefit|gift|bonus|offer|reward|voucher)s?"
    r"|on\s+(?:card\s+)?(?:joining|activation|first\s+(?:transaction|spend|swipe))"
    r"|as\s+a\s+welcome"
    r")[s:\-—]*\s*([^\n]{15,400})", re.I)

# A welcome_benefit string is only meaningful if it quantifies something —
# money, points, a voucher, a membership, etc. Used to drop vague LLM/markdown
# fragments that merely contain the words "welcome benefit".
_WB_VALUE_RE = re.compile(
    r"₹|\brs\.?\s*\d|\binr\b|\d\s*%|\bpoints?\b|\bmiles?\b|\bcashback\b|\bvoucher|"
    r"\bmembership\b|\bsubscription\b|\bgift\s+card\b|\bbonus\b|\bwaiv|\bfree\b|"
    r"\breward|\bcoins?\b|\bnights?\b|\bstay\b|\bvalued?\s+at\b|\bworth\b",
    re.I,
)
# A welcome_benefit string is junk if it is navigation / markdown / CTA noise
# rather than an actual benefit description.
_WB_JUNK_RE = re.compile(
    r"https?://|\]\(|!\[|\bclick\s+here\b|\bapply\s+now\b|\bknow\s+more\b|"
    r"\bread\s+more\b|\blearn\s+more\b|\bview\s+(?:all|more|details)\b|"
    r"\bterms\s+(?:and|&)\s+conditions\b|\bt\s*&\s*c\b|\bskip\s+to\b|"
    r"\bmain\s+menu\b|\bnavigation\b",
    re.I,
)

def _welcome(md: str) -> Optional[str]:
    m = _WELCOME_RE.search(md)
    if not m:
        return None
    val = _clean_text(m.group(1))
    if not val or _WB_JUNK_RE.search(val):
        return None
    if not _WB_VALUE_RE.search(val) and len(val) < 30:
        return None
    return val


# ─────────────────────────────────────────────────────────────
# Milestones
# ─────────────────────────────────────────────────────────────

# Milestone: "Spend ₹X get Y", "On annual spends of ₹X, get Y", "Achieve ₹X spends → Y"
_MILE_RE = re.compile(
    r"(?:on\s+)?(?:annual\s+|yearly\s+|quarterly\s+|achieving\s+|reaching\s+)?"
    r"(?:spend|spending|spends?)\s+(?:of\s+)?[₹rs.]*\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore|cr\b)?[^.\n]{0,90}?"
    r"(?:get|earn|receive|enjoy|unlock|bonus|complimentary|worth|voucher|free)\s+([^.\n]{8,150})", re.I)

def _milestones(md: str) -> list:
    out, seen = [], set()
    for m in _MILE_RE.finditer(md):
        spend = _num(m.group(1), m.group(2) or "")
        if not spend or spend in seen:
            continue
        seen.add(spend)
        # Detect period
        ctx = md[m.start():m.end() + 60].lower()
        period = ("monthly" if "month" in ctx else
                  "quarterly" if "quarter" in ctx else
                  "annual" if "annual" in ctx or "year" in ctx else None)
        out.append({"spend_inr": spend, "reward": _clean_text(m.group(3)),
                    "value_inr": None, "period": period})
    return out


# ─────────────────────────────────────────────────────────────
# Lounge
# ─────────────────────────────────────────────────────────────

# "N domestic airport lounge" or "N lounge visits per quarter/year"
# "Unlimited lounge access" — super-premium cards like Diners Club Black
_LG_UNLIMITED = re.compile(r"\bunlimited\s+(?:airport\s+)?lounge\s+(?:visit|access)", re.I)

_DOM_LG  = re.compile(
    r"(\d+)\s+(?:complimentary\s+)?(?:domestic\s+)?(?:airport\s+)?lounge"
    r"(?:\s+(?:visit|access|trip)s?)?(?:[^.\n]{0,40}domestic)?", re.I)
_INTL_LG = re.compile(
    r"(\d+)\s+(?:complimentary\s+)?(?:"
    r"international\s+(?:airport\s+)?lounge(?:\s+(?:visit|access|trip)s?)?"
    r"|(?:airport\s+)?lounge(?:\s+(?:visit|access|trip)s?)?[^.\n]{0,60}outside\s+india"
    r")", re.I)
_LG_PROG = re.compile(r"\b(Priority\s+Pass|DreamFolks|LoungeKey|Lounge\s+Key)\b", re.I)
# Spend required to unlock lounge: "spend ₹X per quarter" or "minimum spend ₹X"
_LG_SPND = re.compile(
    r"(?:lounge[^.\n]{0,100}|unlock\s+lounge[^.\n]{0,60})"
    r"(?:spend|spends?)[^₹\d\n]{0,20}" + _INR, re.I)
_QTR_RE  = re.compile(r"per\s+(?:calendar\s+)?quarter|quarterly|every\s+(?:calendar\s+)?quarter|each\s+(?:calendar\s+)?quarter", re.I)
_HALF_RE = re.compile(r"per\s+half.?year|bi.?annual", re.I)
_GUEST   = re.compile(r"\bguest\b|complimentary\s+companion", re.I)

def _lounge(md: str) -> dict:
    out: dict = {}

    # Unlimited lounge (Diners Club Black, Infinia, etc.) — no per-visit count
    if _LG_UNLIMITED.search(md):
        out["unlimited"] = True

    if m := _DOM_LG.search(md):
        v = int(m.group(1))
        ctx = _ctx(md, m, before=10, after=80)
        if _QTR_RE.search(ctx):
            v *= 4
            out["domestic_visits_note"] = f"{m.group(1)} per quarter"
        elif _HALF_RE.search(ctx):
            v *= 2
            out["domestic_visits_note"] = f"{m.group(1)} per half-year"
        out["domestic_visits_year"] = v

    if m := _INTL_LG.search(md):
        v = int(m.group(1))
        ctx = _ctx(md, m, before=10, after=80)
        if _QTR_RE.search(ctx):
            v *= 4
            out["international_visits_note"] = f"{m.group(1)} per quarter"
        out["international_visits_year"] = v

    if m := _LG_PROG.search(md):
        out["program"] = m.group(1).replace("  ", " ")
    if m := _LG_SPND.search(md):
        out["spend_unlock_inr"] = _num(m.group(1))
    if _GUEST.search(md):
        out["guest_allowed"] = True
    return out


# ─────────────────────────────────────────────────────────────
# Insurance
# ─────────────────────────────────────────────────────────────

# Insurance amounts often read as "Air Accident cover of ₹1 crore", "Credit Shield
# of Rs. 1,00,000", "Lost Card Liability ₹X". Use _INR_LAKH to capture lakh/crore.
_INS_PATS = [
    ("air_accident_inr",        re.compile(r"(?:air|personal)\s+accident(?:al)?\s*(?:death)?\s*(?:cover|insurance|liability)?[^₹\d\n]{0,40}" + _INR_LAKH, re.I)),
    ("lost_card_inr",           re.compile(r"(?:lost\s+card|card\s+liability|credit\s+shield|fraud(?:ulent)?\s+(?:protection|liability))[^₹\d\n]{0,40}" + _INR_LAKH, re.I)),
    ("purchase_protection_inr", re.compile(r"purchase\s+protection[^₹\d\n]{0,40}" + _INR_LAKH, re.I)),
    ("travel_inr",              re.compile(r"(?:travel|overseas|baggage|flight\s+delay|trip)\s+(?:insurance|cover|delay)[^₹\d\n]{0,40}" + _INR_LAKH, re.I)),
]
_INS_TWO_GROUP = frozenset({"air_accident_inr", "lost_card_inr", "purchase_protection_inr", "travel_inr"})

def _insurance(md: str) -> dict:
    out: dict = {}
    for key, pat in _INS_PATS:
        if m := pat.search(md):
            suffix = m.group(2) if (key in _INS_TWO_GROUP and m.lastindex and m.lastindex >= 2) else ""
            val = _num(m.group(1), suffix or "")
            # Insurance covers are large — ignore tiny matches (likely a stray number).
            if val is not None and val >= 10000:
                out[key] = val
    return out


# ─────────────────────────────────────────────────────────────
# Partner offers
# ─────────────────────────────────────────────────────────────

_PARTNERS = [
    "Swiggy", "Zomato", "Amazon", "Flipkart", "BookMyShow", "Myntra",
    "Ola", "Uber", "IRCTC", "MakeMyTrip", "Cleartrip", "Nykaa",
    "BigBasket", "Blinkit", "PhonePe", "Paytm", "Tata Neu", "Cred",
    "Cult.fit", "Lenskart", "Dominos", "Pizza Hut", "Starbucks",
    "Ajio", "Netmeds", "1mg", "Apollo", "Zepto", "PVR", "INOX",
]
_OFFER_SIG = re.compile(r"%|₹|cashback|discount|\boff\b|voucher|\bfree\b", re.I)

def _partner_offers(md: str) -> list:
    offers = []
    for partner in _PARTNERS:
        for m in re.finditer(re.escape(partner), md, re.I):
            snippet = _ctx(md, m, before=80, after=250)
            if _OFFER_SIG.search(snippet):
                benefit = _clean_text(snippet)
                # Only keep if cleaning left a substantive, signal-bearing sentence
                if benefit and len(benefit) >= 12 and _OFFER_SIG.search(benefit):
                    offers.append({"partner": partner, "benefit": benefit})
                break
    return offers


# ─────────────────────────────────────────────────────────────
# Perks (catch-all)
# ─────────────────────────────────────────────────────────────

_PERK_PATS = [
    ("golf",              re.compile(r"\bgolf\b(?!\s+simulator)", re.I)),
    ("golf_simulator",    re.compile(r"golf\s+simulator", re.I)),
    ("movie",             re.compile(r"(?:movie\s+ticket|free\s+movie|pvr|inox)", re.I)),
    ("dining",            re.compile(r"(?:\bdining\b|restaurant\s+(?:offer|discount|benefit))", re.I)),
    ("concierge",         re.compile(r"\bconcierge\b", re.I)),
    ("spa",               re.compile(r"\bspa\b", re.I)),
    ("hotel",             re.compile(r"(?:complimentary\s+(?:hotel|night|stay)|hotel\s+upgrade)", re.I)),
    ("subscription",      re.compile(r"amazon\s+prime|netflix|hotstar|zee5|zomato\s+gold|swiggy\s+one|sonyliv|jiocinema", re.I)),
    ("railway_lounge",    re.compile(r"railway\s+lounge", re.I)),
    ("roadside_assist",   re.compile(r"roadside\s+assist", re.I)),
    ("annual_voucher",    re.compile(r"annual\s+(?:travel|shopping|lifestyle)\s+voucher", re.I)),
    ("forex_perk",        re.compile(r"zero\s+(?:cross.?currency|forex)\s+markup|zero\s+foreign", re.I)),
    ("contactless",       re.compile(r"contactless|tap.?to.?pay|nfc\s+enable", re.I)),
    ("virtual_card",      re.compile(r"virtual\s+card", re.I)),
    ("emi_conversion",    re.compile(r"(?:no.?cost\s+emi|instant\s+emi|emi\s+conversion)", re.I)),
]

def _perks(md: str) -> list:
    out = []
    for kind, pat in _PERK_PATS:
        if m := pat.search(md):
            raw = _ctx(md, m, before=0, after=220)
            out.append({"kind": kind, "value": _clean_text(raw)})
    return out


# ─────────────────────────────────────────────────────────────
# Eligibility
# ─────────────────────────────────────────────────────────────

_ELG_PATS = [
    ("min_age", re.compile(r"min(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)),
    ("max_age", re.compile(r"max(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)),
]
_SAL_RE  = re.compile(r"\bsalaried\b", re.I)
_SELF_RE = re.compile(r"self.?employed|business\s+owner", re.I)
# Income line, capturing whether it's monthly or annual and any lakh/crore suffix.
# Matches "Net Monthly Income > ₹20,000", "Annual Income ₹6 Lakh", "ITR > ₹6 Lakh per annum".
_INCOME_RE = re.compile(
    r"(monthly|annual|per\s+annum|p\.?a\.?|itr|year)?\s*"
    r"(?:net\s+|gross\s+)?income[^₹\d\n]{0,20}" + _INR_LAKH,
    re.I,
)
_MONTHLY_CTX = re.compile(r"month", re.I)

def _income_year(md: str) -> Optional[float]:
    """Return minimum annual income in INR, converting monthly figures ×12."""
    best: Optional[float] = None
    for m in _INCOME_RE.finditer(md):
        amount = _num(m.group(2), m.group(3) or "")
        if amount is None or amount <= 0:
            continue
        # "income" with a bare number and no ₹ can be noise (e.g. credit score) — require
        # either a unit suffix (lakh/crore) or a value that reads like real income.
        window = md[max(0, m.start() - 25): m.end()]
        if _MONTHLY_CTX.search(window) and "annum" not in window.lower():
            amount *= 12
        if amount < 50_000:        # below a plausible annual income floor → skip
            continue
        if best is None or amount < best:
            best = amount
    return best

def _eligibility(md: str) -> dict:
    out: dict = {}
    for key, pat in _ELG_PATS:
        if m := pat.search(md):
            v = _num(m.group(1))
            if v is not None:
                out[key] = int(v)
    if inc := _income_year(md):
        out["min_income_inr_year"] = inc
    if _SAL_RE.search(md):  out["salaried"]      = True
    if _SELF_RE.search(md): out["self_employed"]  = True
    return out


# ─────────────────────────────────────────────────────────────
# Apply URL
# ─────────────────────────────────────────────────────────────

_APPLY_RE = re.compile(r"\[(?:apply|apply\s+now)[^\]]*\]\((https?://[^\s)]+)\)", re.I)

def _apply_url(md: str) -> Optional[str]:
    m = _APPLY_RE.search(md)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────
# Raw highlights — capture substantive benefit/reward/fee bullets VERBATIM so no
# card detail is ever lost to imperfect normalization. (Schema/normalize later.)
# ─────────────────────────────────────────────────────────────

_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+(.+)$", re.M)
_HL_SIG = re.compile(
    r"₹|\brs\.?\s*\d|\d\s*%|reward|point|mile|coin|cashback|cash\s*back|lounge|"
    r"complimentary|voucher|free|discount|waiv|insurance|emi|fuel|golf|markup|"
    r"membership|milestone|welcome|per\s*₹|spends?\s+on|cap(?:ped)?\b|annual\s+fee|"
    r"joining\s+fee", re.I)

def _highlights(md: str, limit: int = 50) -> list:
    """Substantive benefit/reward/fee bullet lines, cleaned but kept verbatim."""
    out, seen = [], set()
    for m in _BULLET_RE.finditer(md):
        line = _clean_text(m.group(1), limit=220)
        key = line.lower()
        if line and _HL_SIG.search(line) and key not in seen and 8 <= len(line) <= 220:
            seen.add(key); out.append(line)
        if len(out) >= limit:
            break
    return out


# ─────────────────────────────────────────────────────────────
# Core single-card parser
# ─────────────────────────────────────────────────────────────

def _parse_one(md: str, base: dict) -> Optional[dict]:
    """Parse one card's markdown section. `base` has issuer_id, issuer_name, source_url."""
    if not re.search(r"credit\s+card|debit\s+card|prepaid|forex\s+card", md, re.I):
        return None

    src_url = base.get("source_url") or ""
    # Infer category from URL path first (most reliable); page content can refine it
    if "/debit-cards/" in src_url or "/debit-card" in src_url:
        default_cat = "debit"
    elif "/prepaid" in src_url:
        default_cat = "prepaid"
    elif "/forex" in src_url or "/multi-currency" in src_url:
        default_cat = "forex"
    elif "/business" in src_url or "/corporate" in src_url:
        default_cat = "business"
    else:
        default_cat = "credit"

    card: dict = {
        "issuer_id":   base.get("issuer_id"),
        "issuer_name": base.get("issuer_name"),
        "source_url":  src_url,
        "category":    default_cat,
    }

    n_page = _name(md)
    n_url  = _name_from_url(src_url) if src_url else None
    # Prefer page name when it explicitly identifies card type (credit/debit).
    # If the URL says credit-card but the page name only says "card" (e.g. "Entertainment Card"),
    # the URL slug is usually more accurate (e.g. "Platinum Times Credit Card").
    url_has_type = bool(re.search(r"credit.card|debit.card", src_url, re.I))
    page_has_type = bool(re.search(r"credit\s+card|debit\s+card|prepaid|forex\s+card", n_page or "", re.I))
    if n_page and (page_has_type or not url_has_type) and re.search(r'\bcard\b', n_page, re.I):
        card["card_name"] = n_page
    elif n_url:
        card["card_name"] = n_url
    elif n_page:
        card["card_name"] = n_page

    # Let page content override category (e.g., a credit card on a debit page URL)
    for cat, pat in _CAT_RE.items():
        if pat.search(md):
            card["category"] = cat
            break

    if m := _NET_RE.search(md):
        raw = m.group(1).lower()
        card["network"] = _NET_NORM.get(raw, m.group(1).title())
    # Fallback: infer network from the card name ("... RuPay ...", "Diners Club ...").
    if not card.get("network") and (nm := card.get("card_name")):
        if m := _NET_RE.search(nm):
            card["network"] = _NET_NORM.get(m.group(1).lower(), m.group(1).title())
        elif re.search(r"\bdiners\b", nm, re.I):
            card["network"] = "Diners"

    # Search first 60% of the page for segment keywords, with markdown link text
    # stripped — navigation menus list "[Premium & Super Premium Credit Card](url)"
    # which would otherwise falsely trigger the super-premium classifier.
    seg_md = re.sub(r'\[[^\]]{0,120}\]\([^)]*\)', '', md[: int(len(md) * 0.6)])
    for seg, pat in _SEG_RE.items():
        if pat.search(seg_md):
            card["segment"] = seg
            break

    if re.search(r"\bmetal\s+card\b", md, re.I):
        card["card_material"] = "metal"

    iid = base.get("issuer_id")
    if f := _fees(md):       card["fees"]           = f
    if r := _rewards(md, iid): card["rewards"]      = r
    if w := _welcome(md):    card["welcome_benefit"] = w
    if ms := _milestones(md): card["milestones"]    = ms
    if lg := _lounge(md):    card["lounge_access"]  = lg
    if ins := _insurance(md): card["insurance"]     = ins
    if po := _partner_offers(md): card["partner_offers"] = po
    if pk := _perks(md):     card["perks"]          = pk
    if el := _eligibility(md): card["eligibility"]  = el
    if au := _apply_url(md): card["apply_url"]      = au
    # Raw verbatim capture — nothing lost to normalization (schema comes later)
    if hl := _highlights(md): card["highlights"]    = hl

    if not card.get("card_name"):
        return None

    return card


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def parse_cards(page: dict) -> list[dict]:
    """Parse a Jina markdown page into a list of card dicts.

    Listing pages (multiple cards) return multiple dicts.
    Product pages return a single dict (or empty list if not a card page).
    """
    md         = page.get("markdown", "")
    is_listing = page.get("is_listing", False)
    base = {
        "issuer_id":   page.get("issuer_id"),
        "issuer_name": page.get("issuer_name"),
        "source_url":  page.get("source_url"),
    }

    # Only attempt section-splitting on listing pages — detail pages contain many
    # ##-level headings with "card" keywords that would be misidentified as sections.
    if is_listing:
        sections = _split_sections(md)
        if sections:
            cards = []
            for card_name, section_md in sections:
                if _GENERIC_NAME_RE.search(card_name):
                    log.debug("skip generic section: %s", card_name)
                    continue
                card = _parse_one(section_md, base)
                if card:
                    card.setdefault("card_name", card_name)
                    cards.append(card)
            log.info("listing page %s → %d card(s) from %d section(s)",
                     base["source_url"], len(cards), len(sections))
            return cards
        # Listing page that didn't split — skip; detail pages will have the data.
        log.debug("listing page with no splittable sections, skipping: %s", base["source_url"])
        return []

    # Detail page — parse as a single card
    card = _parse_one(md, base)
    return [card] if card else []
