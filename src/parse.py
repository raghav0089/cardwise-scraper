"""Rule-based card data extractor for Jina-rendered markdown.

Handles both:
  - Listing pages  (e.g. /credit-cards)   → multiple cards
  - Product pages  (e.g. /regalia)        → single card with full detail
"""
from __future__ import annotations
import re, logging
from typing import Optional

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

def _num(s: str) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except Exception:
        return None

def _ctx(md: str, m: re.Match, before: int = 60, after: int = 200) -> str:
    return md[max(0, m.start() - before): m.end() + after].replace("\n", " ").strip()


# ─────────────────────────────────────────────────────────────
# Multi-card page detection & splitting
# ─────────────────────────────────────────────────────────────

# Headings that name a card product (## Card Name)
_CARD_HDR = re.compile(
    r"^#{2,3}\s+(.{5,100}(?:credit|debit|prepaid|forex|card|miles|rupay)[^\n]{0,60})$",
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
        name = m.group(1).strip()
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

def _name(md: str) -> Optional[str]:
    for pat in _NAME_PATS:
        m = pat.search(md)
        if m:
            val = m.group(1).strip().rstrip("|").strip()
            # Reject generic titles
            if 5 < len(val) < 120 and not re.search(r"log\s*in|sign\s*in|menu|nav", val, re.I):
                return val
    return None


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
    "secured":       re.compile(r"\bsecured\b|\bagainst\s+fd\b", re.I),
    "entry":         re.compile(r"\bentry.?level\b|\bbasic\b", re.I),
}


# ─────────────────────────────────────────────────────────────
# Fees
# ─────────────────────────────────────────────────────────────

_INR = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)"
_FEE_PATS = [
    ("joining_fee_inr",       re.compile(r"join(?:ing)?\s*fee[^₹\d\n]{0,40}"  + _INR, re.I)),
    ("annual_fee_inr",        re.compile(r"annual\s*(?:fee|membership)[^₹\d\n]{0,40}" + _INR, re.I)),
    ("renewal_fee_inr",       re.compile(r"renewal\s*fee[^₹\d\n]{0,40}"       + _INR, re.I)),
    ("fee_waiver_spend_inr",  re.compile(r"(?:fee\s+waiver|waived?\s+on\s+(?:annual\s+)?spend)[^₹\d\n]{0,50}" + _INR, re.I)),
    ("addon_card_fee_inr",    re.compile(r"(?:add.?on|supplementary)\s*card\s*fee[^₹\d\n]{0,40}" + _INR, re.I)),
    ("cash_advance_fee_pct",  re.compile(r"cash\s*advance\s*fee[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("finance_charge_pct_mo", re.compile(r"(?:finance\s*charge|monthly\s*interest|interest\s*rate)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
    ("fx_markup_pct",         re.compile(r"(?:forex|foreign\s*currency|cross.?currency)[^%\d\n]{0,40}([\d.]+)\s*%", re.I)),
]
_FUEL_RE = re.compile(r"fuel\s+surcharge\s+waiver[^.\n]{0,120}", re.I)
_GST_RE  = re.compile(r"\+\s*gst|exclusive\s+of\s+gst|plus\s+applicable\s+taxes", re.I)

def _fees(md: str) -> dict:
    out: dict = {}
    for key, pat in _FEE_PATS:
        if m := pat.search(md):
            out[key] = _num(m.group(1))
    if m := _FUEL_RE.search(md):
        out["fuel_surcharge_waiver"] = m.group(0).strip()
    if _GST_RE.search(md):
        out["gst_extra"] = True
    return out


# ─────────────────────────────────────────────────────────────
# Rewards  —  normalise everything to base_rate_pct (% of spend)
# ─────────────────────────────────────────────────────────────

_RP_PER_SPEND = re.compile(
    r"(\d+)\s*(?:reward\s+)?points?\s+(?:per|for\s+every|on\s+every)\s+[₹rs.]*\s*([\d,]+)", re.I)
_CASHBACK_ALL = re.compile(r"([\d.]+)\s*%\s*cashback\s+on\s+all", re.I)
_POINT_VAL    = re.compile(r"1\s*(?:reward\s+|edge\s+|indus)?(?:point|mile|rp|rm)\s*[=:]\s*[₹rs.]*\s*([\d.]+)", re.I)
_CURR_RE      = re.compile(
    r"\b(reward\s+points?|cashback|edge\s+miles?|indus\s*miles?|neucoins|"
    r"membership\s+rewards?|kotak\s+points?|cred\s+coins|1fc\s+points?|scapia\s+coins?)\b", re.I)
_ACC_RE       = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:x\b|X\b|%)\s*(?:reward\s+points?|rp|edge\s+miles?|cashback)?\s+"
    r"on\s+([\w\s&/,\-]+?)(?=[,.()\n]|$)", re.I)
_EXCL_RE      = re.compile(r"(?:no\s+rewards?|not\s+earned?|excluded?)\s+on\s+([\w\s,&/]+?)(?=[.\n]|$)", re.I)
_REDEEM_RE    = re.compile(r"redeem[^\n]*?(?:for|at|against)\s+([\w\s,&/\-]+?)(?=[.\n])", re.I)
_EXPIRY_RE    = re.compile(r"(?:points?\s+(?:expire|valid)\s+for\s+)?(\d+)\s*months?\s+(?:from|of)", re.I)

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

    # Base rate
    if m := _RP_PER_SPEND.search(md):
        rp, spend = float(m.group(1)), _num(m.group(2))
        if spend and spend > 0:
            out["base_rate_pct"] = round((pv or 0.25) * rp / spend * 100, 4)
    elif m := _CASHBACK_ALL.search(md):
        out["base_rate_pct"] = float(m.group(1))
        out["currency"] = "cashback"

    # Currency name
    if m := _CURR_RE.search(md):
        out.setdefault("currency", m.group(1).title())
    elif issuer_id and issuer_id in _ISSUER_CURRENCY:
        out.setdefault("currency", _ISSUER_CURRENCY[issuer_id])

    # Accelerated categories
    acc, seen = [], set()
    for m in _ACC_RE.finditer(md):
        rate_str, cat = m.group(1), m.group(2).strip().rstrip(".,")
        if not cat or cat.lower() in seen or len(cat) > 60:
            continue
        seen.add(cat.lower())
        # Convert "NX" multiplier to actual % using base rate
        rate = float(rate_str)
        if "x" in m.group(0).lower() and out.get("base_rate_pct"):
            rate = round(rate * out["base_rate_pct"], 4)
        acc.append({"category": cat, "rate_pct": rate, "cap_inr": None, "notes": None})
    if acc:
        out["accelerated"] = acc

    # Exclusions
    excl = [m.group(1).strip() for m in _EXCL_RE.finditer(md)]
    if excl:
        out["exclusions"] = excl

    # Redemption modes
    modes = [m.group(1).strip() for m in _REDEEM_RE.finditer(md)]
    if modes:
        out["redemption_modes"] = modes[:8]

    if m := _EXPIRY_RE.search(md):
        out["expiry_months"] = int(m.group(1))

    return out


# ─────────────────────────────────────────────────────────────
# Welcome benefit
# ─────────────────────────────────────────────────────────────

_WELCOME_RE = re.compile(
    r"(?:welcome|joining)\s+(?:benefit|gift|bonus|offer)[s:]*\s*([^.\n]{20,300})", re.I)

def _welcome(md: str) -> Optional[str]:
    m = _WELCOME_RE.search(md)
    return m.group(1).strip() if m else None


# ─────────────────────────────────────────────────────────────
# Milestones
# ─────────────────────────────────────────────────────────────

_MILE_RE = re.compile(
    r"(?:spend|spending|spends?)\s+(?:of\s+)?[₹rs.]*\s*([\d,]+)[^.\n]{0,80}"
    r"(?:get|earn|receive|enjoy|unlock|bonus)\s+([^.\n]{10,150})", re.I)

def _milestones(md: str) -> list:
    out, seen = [], set()
    for m in _MILE_RE.finditer(md):
        spend = _num(m.group(1))
        if not spend or spend in seen:
            continue
        seen.add(spend)
        # Detect period
        ctx = md[m.start():m.end() + 60].lower()
        period = ("monthly" if "month" in ctx else
                  "quarterly" if "quarter" in ctx else
                  "annual" if "annual" in ctx or "year" in ctx else None)
        out.append({"spend_inr": spend, "reward": m.group(2).strip(),
                    "value_inr": None, "period": period})
    return out


# ─────────────────────────────────────────────────────────────
# Lounge
# ─────────────────────────────────────────────────────────────

_DOM_LG  = re.compile(r"(\d+)\s+(?:complimentary\s+)?domestic\s+(?:airport\s+)?lounge", re.I)
_INTL_LG = re.compile(r"(\d+)\s+(?:complimentary\s+)?international\s+(?:airport\s+)?lounge", re.I)
_LG_PROG = re.compile(r"\b(Priority\s+Pass|DreamFolks|LoungeKey|Lounge\s+Key)\b", re.I)
_LG_SPND = re.compile(r"lounge[^.\n]{0,80}spend[^₹\d]{0,20}" + _INR, re.I)
_QTR     = re.compile(r"per\s+quarter|quarterly", re.I)
_GUEST   = re.compile(r"guest|complimentary\s+companion", re.I)

def _lounge(md: str) -> dict:
    out: dict = {}
    if m := _DOM_LG.search(md):
        v = int(m.group(1))
        if _QTR.search(_ctx(md, m, after=60)):
            v *= 4
        out["domestic_visits_year"] = v
    if m := _INTL_LG.search(md):
        v = int(m.group(1))
        if _QTR.search(_ctx(md, m, after=60)):
            v *= 4
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

_INS_PATS = [
    ("air_accident_inr",        re.compile(r"air\s+accident[^₹\d\n]{0,50}"       + _INR, re.I)),
    ("lost_card_inr",           re.compile(r"lost\s+card[^₹\d\n]{0,50}"          + _INR, re.I)),
    ("purchase_protection_inr", re.compile(r"purchase\s+protection[^₹\d\n]{0,50}" + _INR, re.I)),
    ("travel_inr",              re.compile(r"travel\s+insurance[^₹\d\n]{0,50}"   + _INR, re.I)),
]

def _insurance(md: str) -> dict:
    out: dict = {}
    for key, pat in _INS_PATS:
        if m := pat.search(md):
            out[key] = _num(m.group(1))
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
                offers.append({"partner": partner, "benefit": snippet})
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
            out.append({"kind": kind, "value": _ctx(md, m, before=20, after=180).strip()})
    return out


# ─────────────────────────────────────────────────────────────
# Eligibility
# ─────────────────────────────────────────────────────────────

_ELG_PATS = [
    ("min_age",             re.compile(r"min(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)),
    ("max_age",             re.compile(r"max(?:imum)?\s+age[^:\d]{0,10}(\d+)", re.I)),
    ("min_income_inr_year", re.compile(r"(?:min(?:imum)?\s+)?(?:annual\s+)?income[^₹\d\n]{0,30}" + _INR, re.I)),
]
_SAL_RE  = re.compile(r"\bsalaried\b", re.I)
_SELF_RE = re.compile(r"self.?employed|business\s+owner", re.I)

def _eligibility(md: str) -> dict:
    out: dict = {}
    for key, pat in _ELG_PATS:
        if m := pat.search(md):
            v = _num(m.group(1))
            if v is not None:
                out[key] = int(v) if key.endswith("age") else v
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
# Core single-card parser
# ─────────────────────────────────────────────────────────────

def _parse_one(md: str, base: dict) -> Optional[dict]:
    """Parse one card's markdown section. `base` has issuer_id, issuer_name, source_url."""
    if not re.search(r"credit\s+card|debit\s+card|prepaid|forex\s+card", md, re.I):
        return None

    card: dict = {
        "issuer_id":   base.get("issuer_id"),
        "issuer_name": base.get("issuer_name"),
        "source_url":  base.get("source_url"),
        "category":    "credit",
    }

    if n := _name(md):
        card["card_name"] = n

    for cat, pat in _CAT_RE.items():
        if pat.search(md):
            card["category"] = cat
            break

    if m := _NET_RE.search(md):
        raw = m.group(1).lower()
        card["network"] = _NET_NORM.get(raw, m.group(1).title())

    for seg, pat in _SEG_RE.items():
        if pat.search(md):
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
    md   = page.get("markdown", "")
    base = {
        "issuer_id":   page.get("issuer_id"),
        "issuer_name": page.get("issuer_name"),
        "source_url":  page.get("source_url"),
    }

    sections = _split_sections(md)
    if sections:
        # Listing page — parse each card section separately
        cards = []
        for card_name, section_md in sections:
            card = _parse_one(section_md, base)
            if card:
                card.setdefault("card_name", card_name)
                cards.append(card)
        log.debug("listing page %s → %d cards", base["source_url"], len(cards))
        return cards
    else:
        # Product page — single card
        card = _parse_one(md, base)
        return [card] if card else []
